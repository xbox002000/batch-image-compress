## 0.1.1
- Store / README copy: lead with WebP/JPEG/PNG optimization pipeline; AVIF marked runtime optional / currently unavailable on this runtime (not a selling point).
- Clarify differentiation from batch-image-downloader (no crawl / no img extract). Failed free by default.

## 0.1.0
- Initial private release: batch image URL compress/convert → KV + metadata (bytesBefore/After, savedPct).
- Inputs: imageUrls and/or sourceDatasetId / sourceKvStoreId (batch-image-downloader compatible).
- Formats: WebP / JPEG / PNG; AVIF when runtime supports it.
- Optional EXIF/GPS strip; optional strip-only cheaper event.
- PPE: image-converted (primary $0.002), image-strip-only ($0.001); failed/non-image free by default.
- Default memory 512 MB. No page crawl.
