"""Download + Pillow compress/convert with EXIF strip and AVIF capability probe."""
from __future__ import annotations

import hashlib
import io
import time
from typing import Any

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

# Probe once
_AVIF_OK: bool | None = None

_EXT_FROM_CT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/avif": "avif",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
}

_FMT_TO_CT_EXT = {
    "jpeg": ("image/jpeg", "jpg"),
    "jpg": ("image/jpeg", "jpg"),
    "png": ("image/png", "png"),
    "webp": ("image/webp", "webp"),
    "avif": ("image/avif", "avif"),
    "gif": ("image/gif", "gif"),
    "bmp": ("image/bmp", "bmp"),
    "tiff": ("image/tiff", "tiff"),
}

_KEEP_FAMILY = {
    "JPEG": "jpeg",
    "JPG": "jpeg",
    "PNG": "png",
    "WEBP": "webp",
    "AVIF": "avif",
    "GIF": "png",  # re-encode animated GIF as static PNG when keep
    "BMP": "png",
    "TIFF": "png",
}


def avif_supported() -> bool:
    global _AVIF_OK
    if _AVIF_OK is not None:
        return _AVIF_OK
    try:
        import PIL.features as features  # noqa: WPS433

        if features.check("avif"):
            _AVIF_OK = True
            return True
    except Exception:  # noqa: BLE001
        pass
    # try encode round-trip
    try:
        buf = io.BytesIO()
        Image.new("RGB", (2, 2), (0, 0, 0)).save(buf, format="AVIF", quality=50)
        _AVIF_OK = buf.tell() > 0
    except Exception:  # noqa: BLE001
        _AVIF_OK = False
    return _AVIF_OK


def sniff_type(data: bytes, header_ct: str | None) -> tuple[str | None, str]:
    ct = (header_ct or "").split(";", 1)[0].strip().lower() or None
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "gif"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis", b"mif1"):
            return "image/avif", "avif"
    if ct and ct in _EXT_FROM_CT:
        return ct, _EXT_FROM_CT[ct]
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").upper()
            mapping = {
                "JPEG": ("image/jpeg", "jpg"),
                "PNG": ("image/png", "png"),
                "GIF": ("image/gif", "gif"),
                "WEBP": ("image/webp", "webp"),
                "AVIF": ("image/avif", "avif"),
                "BMP": ("image/bmp", "bmp"),
                "TIFF": ("image/tiff", "tiff"),
            }
            if fmt in mapping:
                return mapping[fmt]
    except Exception:  # noqa: BLE001
        pass
    return ct, "bin"


def _prepare_image(im: Image.Image, max_width: int, max_height: int) -> Image.Image:
    im = ImageOps.exif_transpose(im) or im
    w, h = im.size
    mw = max_width if max_width and max_width > 0 else None
    mh = max_height if max_height and max_height > 0 else None
    if mw or mh:
        tw = mw or w
        th = mh or h
        if w > tw or h > th:
            im = im.copy()
            im.thumbnail((tw, th), Image.Resampling.LANCZOS)
    return im


def _save(
    im: Image.Image,
    fmt: str,
    quality: int,
    *,
    strip_exif: bool,
) -> tuple[bytes, str, str]:
    fmt = fmt.lower().strip()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt == "avif" and not avif_supported():
        raise RuntimeError("AVIF not supported in this runtime")

    out = io.BytesIO()
    save_kw: dict[str, Any] = {}
    work = im

    if fmt == "jpeg":
        if work.mode in ("RGBA", "LA", "P"):
            work = work.convert("RGB")
        elif work.mode != "RGB":
            work = work.convert("RGB")
        save_kw.update(quality=quality, optimize=True)
        if not strip_exif:
            # keep exif if present
            exif = im.getexif()
            if exif:
                save_kw["exif"] = exif.tobytes()
        work.save(out, format="JPEG", **save_kw)
        return out.getvalue(), "image/jpeg", "jpg"

    if fmt == "png":
        if work.mode == "P":
            work = work.convert("RGBA")
        save_kw.update(optimize=True)
        work.save(out, format="PNG", **save_kw)
        return out.getvalue(), "image/png", "png"

    if fmt == "webp":
        if work.mode == "P":
            work = work.convert("RGBA")
        save_kw.update(quality=quality, method=4)
        work.save(out, format="WEBP", **save_kw)
        return out.getvalue(), "image/webp", "webp"

    if fmt == "avif":
        if work.mode in ("P",):
            work = work.convert("RGBA")
        save_kw.update(quality=quality)
        work.save(out, format="AVIF", **save_kw)
        return out.getvalue(), "image/avif", "avif"

    raise ValueError(f"Unsupported target format: {fmt}")


def strip_only_bytes(data: bytes) -> tuple[bytes, str, str, int, int]:
    """Re-save in source format without EXIF. Returns (bytes, ct, ext, w, h)."""
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        fmt = (im.format or "PNG").upper()
        target = _KEEP_FAMILY.get(fmt, "png")
        w, h = im.size
        # force strip
        out, ct, ext = _save(im, target, quality=90, strip_exif=True)
        return out, ct, ext, int(w), int(h)


def convert_bytes(
    data: bytes,
    *,
    target_format: str,
    quality: int = 80,
    max_width: int = 0,
    max_height: int = 0,
    strip_exif: bool = True,
) -> tuple[bytes, str, str, int, int, str]:
    """
    Returns (out_bytes, contentType, ext, width, height, format_used).
    """
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        src_fmt = (im.format or "").upper()
        target = target_format.lower().strip()
        if target == "keep":
            target = _KEEP_FAMILY.get(src_fmt, "webp")
        if target == "avif" and not avif_supported():
            raise RuntimeError("AVIF encoding not available in this environment")
        work = _prepare_image(im, max_width, max_height)
        out, ct, ext = _save(work, target, quality, strip_exif=strip_exif)
        w, h = work.size
        return out, ct, ext, int(w), int(h), target


async def fetch_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    max_retries: int,
) -> tuple[bytes | None, str | None, list[str]]:
    """Download raw bytes. Returns (data, header_ct, errors)."""
    errors: list[str] = []
    last_err = "download failed"
    for attempt in range(max_retries + 1):
        try:
            async with client.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
                if resp.status_code >= 400:
                    last_err = f"HTTP {resp.status_code}"
                    if resp.status_code < 500 and resp.status_code != 429:
                        return None, None, [last_err]
                    errors.append(last_err)
                    continue
                header_ct = resp.headers.get("content-type")
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        return None, header_ct, [f"Oversized: >{max_bytes} bytes"]
                    chunks.append(chunk)
                data = b"".join(chunks)
                if not data:
                    return None, header_ct, ["Empty body"]
                return data, header_ct, []
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {str(e)[:160]}"
            errors.append(last_err)
        if attempt < max_retries:
            await _sleep(0.4 * (attempt + 1))
    return None, None, errors or [last_err]


async def _sleep(secs: float) -> None:
    import asyncio

    await asyncio.sleep(secs)


def process_image(
    data: bytes,
    *,
    header_ct: str | None,
    target_format: str,
    quality: int,
    max_width: int,
    max_height: int,
    strip_exif: bool,
    strip_only: bool,
) -> dict[str, Any]:
    """Pure CPU path. Returns item dict with optional _data / _ext for KV write."""
    t0 = time.perf_counter()
    bytes_before = len(data)
    src_ct, _src_ext = sniff_type(data, header_ct)

    # Reject non-images early
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.load()
            src_format = (probe.format or "").upper()
    except (UnidentifiedImageError, OSError, ValueError) as e:
        return {
            "status": "error",
            "contentType": src_ct,
            "bytesBefore": bytes_before,
            "bytesAfter": None,
            "savedPct": None,
            "width": None,
            "height": None,
            "format": None,
            "sha256": None,
            "kvKey": None,
            "strippedExif": False,
            "errors": [f"Non-image or unreadable: {type(e).__name__}: {str(e)[:120]}"],
            "durationMs": int((time.perf_counter() - t0) * 1000),
            "_data": None,
            "_ext": "bin",
            "_charge_event": None,
        }

    try:
        if strip_only:
            out, ct, ext, w, h = strip_only_bytes(data)
            fmt_used = ext if ext != "jpg" else "jpeg"
            charge = "image-strip-only"
            stripped = True
        else:
            out, ct, ext, w, h, fmt_used = convert_bytes(
                data,
                target_format=target_format,
                quality=quality,
                max_width=max_width,
                max_height=max_height,
                strip_exif=strip_exif,
            )
            charge = "image-converted"
            stripped = bool(strip_exif)
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "contentType": src_ct,
            "bytesBefore": bytes_before,
            "bytesAfter": None,
            "savedPct": None,
            "width": None,
            "height": None,
            "format": None,
            "sha256": None,
            "kvKey": None,
            "strippedExif": False,
            "sourceFormat": src_format,
            "errors": [f"Convert failed: {type(e).__name__}: {str(e)[:200]}"],
            "durationMs": int((time.perf_counter() - t0) * 1000),
            "_data": None,
            "_ext": "bin",
            "_charge_event": None,
        }

    bytes_after = len(out)
    saved = None
    if bytes_before > 0:
        saved = round((1.0 - (bytes_after / bytes_before)) * 100.0, 2)
    digest = hashlib.sha256(out).hexdigest()
    return {
        "status": "success",
        "contentType": ct,
        "bytesBefore": bytes_before,
        "bytesAfter": bytes_after,
        "savedPct": saved,
        "width": w,
        "height": h,
        "format": fmt_used,
        "sourceFormat": src_format.lower() if src_format else None,
        "sha256": digest,
        "kvKey": None,
        "strippedExif": stripped,
        "errors": [],
        "durationMs": int((time.perf_counter() - t0) * 1000),
        "_data": out,
        "_ext": ext,
        "_charge_event": charge,
    }
