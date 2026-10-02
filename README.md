# Batch Image Compress & Convert — WebP/JPEG/PNG

**Batch WebP / JPEG / PNG optimization pipeline for image URL lists (or `batch-image-downloader` dataset/KV fields) with before/after sizes.**

Give this Actor direct image URLs — or point it at an upstream Apify dataset / KV store from **batch-image-downloader**. It downloads (or reads KV bytes), re-encodes with Pillow to **WebP, JPEG, or PNG**, optionally strips EXIF/GPS, stores the result in KV, and writes a dataset row with `bytesBefore` / `bytesAfter` / `savedPct`, dims, format, and sha256. Failed, non-image, and oversized inputs are free by default.

**Positioning:** batch optimization pipeline for page-speed / CMS / Core Web Vitals — **not** site crawling or image scraping. Differentiates from **batch-image-downloader** (no page crawl, no `<img>` extraction).

**AVIF:** runtime optional / currently unavailable on this runtime — not a selling point. Prefer WebP/JPEG/PNG.

## What you get

- Batch **compress / convert** of image URL lists → KV (`opt-<sha256>.<ext>`)
- Compatible with **batch-image-downloader** output (`sourceUrl`, `kvKey`, optional `sourceDatasetId` / `sourceKvStoreId`)
- Formats: **WebP / JPEG / PNG** (primary). AVIF is runtime optional / currently unavailable on this runtime
- Metadata: content type, bytes before/after, savedPct, width/height, sha256, format
- Optional **EXIF / GPS strip**; optional cheaper **strip-only** mode (no pixel re-encode)
- Default **512 MB** memory (target 256–512); failed / non-image / oversized free unless you opt in
- **No page crawl, no `<img>` extraction** — pass image URLs or an upstream media dataset

## Measured results

Cloud benches on build **0.1.1**, **512 MB** (Asia/Taipei 2026-10-02):

| Run | Input | Stored | Peak RSS | Platform cost |
|---|---|---|---|---|
| smoke | 3 URLs (2 ok / 1×404 free) → WebP | 2 | 89 MB | ~$0.00039 |
| bench | 12 URLs (10 ok / 2 err free) → WebP | 10 | 92 MB | ~$0.00087 (~$0.000087 / image) |
| strip-only | 2 URLs metadata strip | 2 | ~90 MB | ~$0.00038 |

Bench avg savedPct ≈ **32%** (WebP q80). See **Measured results** above for measured platform cost.

## Use cases

- Optimize a product-image URL list for Core Web Vitals before CMS upload
- Chain after **batch-image-downloader**: download → compress into WebP/JPEG/PNG with EXIF stripped
- Privacy pass: strip GPS/EXIF from owned asset packs without changing pixels (`stripOnly`)

## How to use

```json
{
  "imageUrls": [
    { "url": "https://www.w3.org/Icons/w3c_home.png" },
    { "url": "https://httpbin.org/image/jpeg" }
  ],
  "targetFormat": "webp",
  "quality": 80,
  "stripExif": true,
  "maxImages": 50,
  "maxConcurrency": 4
}
```

Chain from batch-image-downloader:

```json
{
  "sourceDatasetId": "<dataset-id-from-downloader-run>",
  "sourceKvStoreId": "<kv-store-id-from-downloader-run>",
  "imageUrlField": "sourceUrl",
  "kvKeyField": "kvKey",
  "targetFormat": "webp",
  "quality": 80,
  "stripExif": true
}
```

Example success row:

```json
{
  "sourceUrl": "https://httpbin.org/image/jpeg",
  "status": "success",
  "format": "webp",
  "contentType": "image/webp",
  "bytesBefore": 35588,
  "bytesAfter": 12400,
  "savedPct": 65.16,
  "width": 239,
  "height": 178,
  "sha256": "…",
  "kvKey": "opt-<sha256>.webp",
  "strippedExif": true,
  "durationMs": 45
}
```

## Pricing (pay-per-event)

| Event | Price | When |
|---|---|---|
| Actor start | **$0.001** / GB (1 GB min) | Once per run |
| Image converted (primary) | **$0.002** | Each successful compress/convert KV store |
| Image strip-only | **$0.001** | When `stripOnly` succeeds (metadata only) |
| Failed / non-image / oversized / duplicate | **$0** | Default (`chargeFailedImages=false`) |

Example: 1,000 successful converts ≈ **$2.00** + start (before platform CU/storage). Failed items are never charged by default.

## Integrations

```python
from apify_client import ApifyClient
client = ApifyClient("<YOUR_TOKEN>")
run = client.actor("YOUR_USERNAME/batch-image-compress").call(run_input={
    "imageUrls": [
        {"url": "https://www.w3.org/Icons/w3c_home.png"},
        {"url": "https://httpbin.org/image/jpeg"},
    ],
    "targetFormat": "webp",
    "quality": 80,
    "stripExif": True,
    "maxImages": 50,
})
for item in client.dataset(run["defaultDatasetId"]).iterate_items():
    print(item.get("sourceUrl"), item.get("status"), item.get("savedPct"), item.get("kvKey"))
```

## Related Actors

- [Batch Image Downloader](https://apify.com/YOUR_USERNAME/batch-image-downloader) — download image URLs or extract page imgs into KV (this Actor does not crawl)
- [Sitemap URL Extractor](https://apify.com/YOUR_USERNAME/sitemap-url-discovery) — discover image/PDF URLs from sitemaps
- [Bulk URL Status Checker](https://apify.com/YOUR_USERNAME/url-status-checker) — filter live URLs before optimize

## Known limits

- Does **not** crawl HTML pages or extract `<img>` / `og:image` — use **batch-image-downloader** for that
- AVIF is runtime optional / currently unavailable on this runtime — pick WebP/JPEG/PNG
- Animated GIF / multi-frame: first-frame / static re-encode behavior via Pillow
- Copyright / robots: you are responsible for lawful use of source assets you optimize
- Not a “scrape any website’s images” tool — designed for owned or permitted URL lists and optimization pipelines

## License

AGPL-3.0 (see `LICENSE`). Runtime stack: `httpx` (BSD), `Pillow` (MIT-CMU), Apify SDK.
