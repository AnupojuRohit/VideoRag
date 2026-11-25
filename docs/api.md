# VideoRAG API

The local API is served by FastAPI at `http://localhost:8000`. Health endpoints
are `/health` and `/ready`. Application routes are prefixed with `/api/v1`.

All application errors use this envelope:

```json
{
  "error": {
    "code": "invalid_media",
    "message": "The uploaded file is not a readable MP4 video."
  }
}
```

Validation errors return HTTP 422 with `invalid_request`; the response does not
echo rejected values or local file paths.

## Upload preflight

`POST /api/v1/videos/preflight`

Request:

```json
{ "sha256_hash": "64 lowercase or uppercase hexadecimal characters" }
```

An unseen hash returns `can_upload: true` and `duplicate_action: "upload"`.
Existing content returns the canonical asset and job IDs, status, and one of:

| Action | Meaning |
| --- | --- |
| `reuse_ready` | Reuse an already verified asset |
| `reuse_uploaded` | Reuse the canonical asset while ingestion dispatch is pending |
| `attach_existing_job` | Reuse an active job |
| `reuse_paused_quota` | Reuse a quota-paused asset; no provider call is started |
| `retry_available` | A failed/cancelled asset can be retried by a later retry API |

SHA-256 identifies exact bytes only. It does not detect re-encoded or similar
videos.

## Upload video bytes

`POST /api/v1/videos/upload?filename=<URL-encoded-name>`

The request body is the raw file (not multipart). Required headers:

- `Content-Type: video/mp4` (or `application/octet-stream`)
- `X-Content-SHA256: <64-character SHA-256>`

The API streams the body to a temporary file while enforcing the configured
byte limit and computing an independent server hash. It rejects a hash mismatch,
then inspects the actual streams with FFprobe. The initial supported input is
MP4 with H.264 video and optional AAC audio, at most 500 MB (500,000,000 bytes)
and 60 minutes by default; the byte and duration limits are configurable.

New uploads create one canonical UUID asset, an `UPLOADED` job, and its stage
records in a single database transaction. A successful response is not a claim
that processing has started: Celery dispatch is implemented in Phase 3. Repeated
uploads return the canonical IDs and a status-specific `duplicate_action`.

Example response:

```json
{
  "asset_id": "UUID",
  "job_id": "UUID",
  "sha256_hash": "hex digest",
  "original_filename": "lecture.mp4",
  "file_size_bytes": 123456,
  "duration_seconds": 12.5,
  "status": "UPLOADED",
  "duplicate": false,
  "duplicate_action": "uploaded"
}
```

Known error codes include `invalid_sha256`, `hash_mismatch`, `file_too_large`,
`video_too_long`, `unsupported_extension`, `unsupported_media_type`,
`unsupported_container`, `unsupported_video_codec`,
`unsupported_audio_codec`, `invalid_media`, and `storage_unavailable`.

## Implemented later

Asset/library listing, job progress/retry/cancel, video detail, and
question/answer routes are not implemented yet.