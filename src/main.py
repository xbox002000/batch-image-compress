"""Apify Actor: batch image compress / convert (URL list or downloader dataset/KV → optimized KV)."""
from __future__ import annotations

import asyncio
import datetime as dt
import re
import resource
import time
from collections import Counter
from urllib.parse import urlparse

import httpx
from apify import Actor

from charging import Charger
from compress import avif_supported, fetch_bytes, process_image
from inputs import collect_urls, url_from_item

EVENT = "image-converted"
EVENT_STRIP = "image-strip-only"
UA_DEFAULT = "Mozilla/5.0 (compatible; BatchImageCompress/0.1; +https://apify.com)"
PUSH_BATCH = 50


async def main() -> None:
    async with Actor:
        t0 = time.time()
        inp = await Actor.get_input() or {}
        charger = Charger(EVENT)

        proxy_url = None
        if (inp.get("proxyConfiguration") or {}).get("useApifyProxy") or (
            inp.get("proxyConfiguration") or {}
        ).get("proxyUrls"):
            pc = await Actor.create_proxy_configuration(actor_proxy_input=inp["proxyConfiguration"])
            proxy_url = await pc.new_url() if pc else None

        conc = max(1, min(int(inp.get("maxConcurrency", 4)), 20))
        timeout = float(inp.get("requestTimeoutSecs", 30))
        retries = int(inp.get("maxRetries", 2))
        min_delay = int(inp.get("minDelayMsPerHost", 50) or 0)
        max_bytes = int(inp.get("maxBytesPerImage", 15_728_640))
        target_format = (inp.get("targetFormat") or "webp").strip().lower()
        if target_format not in ("webp", "jpeg", "png", "avif", "keep"):
            target_format = "webp"
        quality = int(inp.get("quality", 80))
        max_width = int(inp.get("maxWidth", 0) or 0)
        max_height = int(inp.get("maxHeight", 0) or 0)
        strip_exif = bool(inp.get("stripExif", True))
        strip_only = bool(inp.get("stripOnly", False))
        skip_dupes = bool(inp.get("skipDuplicatesByHash", True))
        kv_prefix = inp.get("kvKeyPrefix") or "opt-"
        kv_prefix = re.sub(r"[^a-zA-Z0-9!\-_\.'()]", "-", kv_prefix)
        charge_failed = bool(inp.get("chargeFailedImages", False))
        url_field = (inp.get("imageUrlField") or "sourceUrl").strip() or "sourceUrl"
        kv_key_field = (inp.get("kvKeyField") or "kvKey").strip() or "kvKey"
        source_dataset_id = (inp.get("sourceDatasetId") or "").strip() or None
        source_kv_id = (inp.get("sourceKvStoreId") or "").strip() or None

        avif_ok = avif_supported()
        if target_format == "avif" and not avif_ok and not strip_only:
            Actor.log.warning(
                "AVIF encoding not available in this runtime — rows targeting AVIF will error (free). "
                "Choose webp/jpeg/png or keep."
            )

        headers = {
            "User-Agent": inp.get("userAgent") or UA_DEFAULT,
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        }
        limits = httpx.Limits(max_connections=conc * 2, max_keepalive_connections=conc)

        # Job list: (sourceUrl|None, kvKey|None, label)
        jobs: list[dict] = []
        seen_urls: set[str] = set()

        async with httpx.AsyncClient(
            headers=headers,
            proxy=proxy_url,
            limits=limits,
            http2=False,
            follow_redirects=True,
        ) as client:
            image_urls, warns = await collect_urls(inp.get("imageUrls"), client=client)
            for w in warns:
                Actor.log.warning(w)
            for u in image_urls:
                if u not in seen_urls:
                    seen_urls.add(u)
                    jobs.append({"sourceUrl": u, "kvKey": None, "fromDataset": False})

            if source_dataset_id:
                try:
                    ds = await Actor.open_dataset(id=source_dataset_id)
                    n_from_ds = 0
                    async for item in ds.iterate_items():
                        if not isinstance(item, dict):
                            continue
                        u = url_from_item(item, preferred_field=url_field)
                        k = item.get(kv_key_field) if kv_key_field else None
                        k = str(k).strip() if k else None
                        if not u and not k:
                            continue
                        # Prefer unique by URL when present
                        key_id = u or f"kv:{k}"
                        if key_id in seen_urls:
                            continue
                        seen_urls.add(key_id)
                        jobs.append({"sourceUrl": u, "kvKey": k, "fromDataset": True})
                        n_from_ds += 1
                    Actor.log.info(f"Loaded {n_from_ds} image job(s) from dataset {source_dataset_id}")
                except Exception as e:  # noqa: BLE001
                    Actor.log.warning(f"Could not read sourceDatasetId={source_dataset_id}: {e}")

            if not jobs:
                raise ValueError(
                    "Provide at least one image URL in `imageUrls`, or a `sourceDatasetId` with "
                    "sourceUrl/url fields (compatible with batch-image-downloader). "
                    "This Actor does not crawl pages or extract <img> tags."
                )

            max_images = int(inp.get("maxImages", 500) or 0) or None
            budget_event = EVENT_STRIP if strip_only else EVENT
            budget = charger.budget(budget_event)
            if budget is not None and (max_images is None or budget < max_images):
                Actor.log.info(f"Spending limit allows about {budget} image(s); capping maxImages.")
                max_images = budget
            if max_images is not None:
                jobs = jobs[:max_images]

            source_store = None
            if source_kv_id:
                try:
                    source_store = await Actor.open_key_value_store(id=source_kv_id)
                except Exception as e:  # noqa: BLE001
                    Actor.log.warning(f"Could not open sourceKvStoreId={source_kv_id}: {e}")
                    source_store = None

            sem = asyncio.Semaphore(conc)
            host_locks: dict[str, asyncio.Lock] = {}
            host_last: dict[str, float] = {}
            status_counts: Counter = Counter()
            looked_up = 0
            charged_n = 0
            strip_charged_n = 0
            free_n = 0
            stored_n = 0
            lock = asyncio.Lock()
            pending_charge: list[dict] = []
            pending_strip: list[dict] = []
            pending_free: list[dict] = []
            seen_hashes: set[str] = set()
            store = await Actor.open_key_value_store()

            async def flush(force: bool = False) -> None:
                nonlocal charged_n, strip_charged_n, free_n, pending_charge, pending_strip, pending_free
                if pending_charge and (force or len(pending_charge) >= PUSH_BATCH):
                    batch = pending_charge
                    pending_charge = []
                    n = await charger.push_and_charge(batch, EVENT)
                    charged_n += n
                    if n < len(batch):
                        charger.limit_reached = True
                if pending_strip and (force or len(pending_strip) >= PUSH_BATCH):
                    batch = pending_strip
                    pending_strip = []
                    n = await charger.push_and_charge(batch, EVENT_STRIP)
                    strip_charged_n += n
                    if n < len(batch):
                        charger.limit_reached = True
                if pending_free and (force or len(pending_free) >= PUSH_BATCH):
                    batch = pending_free
                    pending_free = []
                    await charger.push_free(batch)
                    free_n += len(batch)

            async def host_gate(url: str) -> None:
                if min_delay <= 0 or not url:
                    return
                host = urlparse(url).hostname or ""
                if host not in host_locks:
                    host_locks[host] = asyncio.Lock()
                async with host_locks[host]:
                    last = host_last.get(host, 0.0)
                    wait = (last + min_delay / 1000.0) - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    host_last[host] = time.monotonic()

            async def load_source(job: dict) -> tuple[bytes | None, str | None, list[str], str | None]:
                """Return (data, header_ct, errors, sourceUrl)."""
                url = job.get("sourceUrl")
                kv_key = job.get("kvKey")
                errs: list[str] = []
                if kv_key and source_store is not None:
                    try:
                        val = await source_store.get_value(kv_key)
                        if isinstance(val, (bytes, bytearray)) and val:
                            return bytes(val), None, [], url
                        if val is None:
                            errs.append(f"KV key missing: {kv_key}")
                        else:
                            errs.append(f"KV value not bytes for key {kv_key}")
                    except Exception as e:  # noqa: BLE001
                        errs.append(f"KV read failed: {type(e).__name__}: {str(e)[:120]}")
                if url:
                    await host_gate(url)
                    data, ct, ferrs = await fetch_bytes(
                        client, url, timeout=timeout, max_bytes=max_bytes, max_retries=retries
                    )
                    return data, ct, ferrs, url
                return None, None, errs or ["No sourceUrl and no readable kvKey"], url

            async def one(job: dict) -> None:
                nonlocal looked_up, stored_n
                if charger.limit_reached:
                    return
                async with sem:
                    if charger.limit_reached:
                        return
                    data, header_ct, load_errs, url = await load_source(job)
                    if data is None:
                        item = {
                            "sourceUrl": url,
                            "status": "error",
                            "contentType": None,
                            "bytesBefore": None,
                            "bytesAfter": None,
                            "savedPct": None,
                            "width": None,
                            "height": None,
                            "format": None,
                            "sha256": None,
                            "kvKey": None,
                            "strippedExif": False,
                            "errors": load_errs,
                            "durationMs": 0,
                            "_data": None,
                            "_ext": "bin",
                            "_charge_event": None,
                        }
                    else:
                        item = process_image(
                            data,
                            header_ct=header_ct,
                            target_format=target_format,
                            quality=quality,
                            max_width=max_width,
                            max_height=max_height,
                            strip_exif=strip_exif,
                            strip_only=strip_only,
                        )
                        item["sourceUrl"] = url

                    data_out = item.pop("_data", None)
                    ext = item.pop("_ext", "bin")
                    charge_event = item.pop("_charge_event", None)
                    billable = False

                    if item["status"] == "success" and data_out is not None:
                        digest = item["sha256"]
                        if skip_dupes and digest in seen_hashes:
                            item["status"] = "duplicate"
                            item["errors"] = (item.get("errors") or []) + [
                                "Duplicate output sha256 — not re-stored"
                            ]
                            item["kvKey"] = None
                            data_out = None
                            charge_event = None
                        else:
                            key = f"{kv_prefix}{digest}.{ext}"
                            try:
                                await store.set_value(
                                    key,
                                    data_out,
                                    content_type=item["contentType"] or "application/octet-stream",
                                )
                            except Exception as e:  # noqa: BLE001
                                item["status"] = "error"
                                item["errors"] = (item.get("errors") or []) + [
                                    f"KV store failed: {type(e).__name__}: {str(e)[:200]}"
                                ]
                                item["kvKey"] = None
                                data_out = None
                                charge_event = None
                            else:
                                item["kvKey"] = key
                                stored_n += 1
                                if digest:
                                    seen_hashes.add(digest)
                                billable = True

                    if charge_failed and item["status"] == "error":
                        billable = True
                        charge_event = charge_event or (EVENT_STRIP if strip_only else EVENT)

                    row = {k: v for k, v in item.items() if not k.startswith("_")}

                    async with lock:
                        looked_up += 1
                        status_counts[item["status"]] += 1
                        if billable and charge_event == EVENT_STRIP:
                            pending_strip.append(row)
                        elif billable:
                            pending_charge.append(row)
                        else:
                            pending_free.append(row)
                        await flush()
                        if looked_up % 10 == 0 or looked_up == len(jobs):
                            await Actor.set_status_message(
                                f"Processed {looked_up}/{len(jobs)} — "
                                f"ok {status_counts['success']}, "
                                f"dup {status_counts['duplicate']}, "
                                f"err {status_counts['error']}"
                            )

            Actor.log.info(
                f"{len(jobs)} image(s); concurrency={conc}; target={target_format}; "
                f"stripOnly={strip_only}; stripExif={strip_exif}; avifSupported={avif_ok}; "
                f"chargeFailed={charge_failed}; mem target 256–512MB"
            )
            await Actor.set_status_message(f"Compressing {len(jobs)} image(s)…")
            await asyncio.gather(*(one(j) for j in jobs))
            async with lock:
                await flush(force=True)

        peak_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
        summary = {
            "totalAttempted": looked_up,
            "stored": stored_n,
            "chargedConverted": charged_n,
            "chargedStripOnly": strip_charged_n,
            "free": free_n,
            "success": status_counts["success"],
            "duplicate": status_counts["duplicate"],
            "error": status_counts["error"],
            "byStatus": dict(status_counts),
            "targetFormat": target_format,
            "stripOnly": strip_only,
            "stripExif": strip_exif,
            "quality": quality,
            "maxWidth": max_width,
            "maxHeight": max_height,
            "avifSupported": avif_ok,
            "skipDuplicatesByHash": skip_dupes,
            "chargeFailedImages": charge_failed,
            "inputImageUrls": len(inp.get("imageUrls") or []),
            "sourceDatasetId": source_dataset_id,
            "sourceKvStoreId": source_kv_id,
            "maxImagesReached": max_images is not None and looked_up >= max_images,
            "kvKeyPrefix": kv_prefix,
            "chargedEvents": dict(charger.counts),
            "durationSecs": round(time.time() - t0, 1),
            "peakMemoryMb": peak_mb,
            "finishedAt": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
        await store.set_value("SUMMARY", summary)
        await store.set_value("OUTPUT", summary)
        msg = (
            f"Done: attempted {looked_up}, stored {stored_n}, "
            f"charged convert {charged_n}, strip {strip_charged_n}, free {free_n}; "
            f"success {status_counts['success']}, duplicate {status_counts['duplicate']}, "
            f"error {status_counts['error']}. "
            f"Charged events: {charger.counts or 'nothing'}."
        )
        Actor.log.info(msg + f" Peak memory {peak_mb} MB, {summary['durationSecs']} s.")
        await Actor.set_status_message(msg, is_terminal=True)


if __name__ == "__main__":
    asyncio.run(main())
