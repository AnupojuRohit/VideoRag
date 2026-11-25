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
videos. The response includes `external_processing_consent` so the UI can ask
for consent before resuming an existing unprocessed asset.

## External processing consent

`POST /api/v1/videos/{asset_id}/consent`

Request:

```json
{ "accepted": true }
```

This records a versioned, per-asset acknowledgement that audio/video-derived
content may be sent to the configured external AI provider. The frontend
discloses that Gemini's unpaid tier may use submitted content to improve Google
products and may be human-reviewed. Do not upload sensitive, confidential, or
personal content. Separately, provider calls remain disabled until an API key
and explicit unpaid-project configuration are provided in the local environment.

## Upload video bytes

`POST /api/v1/videos/upload?filename=<URL-encoded-name>`

The request body is the raw file (not multipart). Required headers:

- `Content-Type: video/mp4` (or `application/octet-stream`)
- `X-Content-SHA256: <64-character SHA-256>`
- `X-External-Processing-Consent: true`

The API streams the body to a temporary file while enforcing the configured
byte limit and computing an independent server hash. It rejects a hash mismatch,
then inspects the actual streams with FFprobe. The initial supported input is
MP4 with H.264 video and optional AAC audio, at most 500 MB (500,000,000 bytes)
and 60 minutes by default; the byte and duration limits are configurable.

New uploads create one canonical UUID asset, an `UPLOADED` job, and its stage
records in a single database transaction. After commit, the API dispatches the
job to Celery and returns its current status. PostgreSQL remains the durable
source of truth; periodic reconciliation republishes jobs if Redis loses a
message or a worker lease expires. Repeated uploads return canonical IDs and a
status-specific `duplicate_action`.

Example response:

```json
{
  "asset_id": "UUID",
  "job_id": "UUID",
  "sha256_hash": "hex digest",
  "original_filename": "lecture.mp4",
  "file_size_bytes": 123456,
  "duration_seconds": 12.5,
  "status": "QUEUED",
  "duplicate": false,
  "duplicate_action": "uploaded"
}
```

Known error codes include `invalid_sha256`, `hash_mismatch`, `file_too_large`,
`video_too_long`, `unsupported_extension`, `unsupported_media_type`,
`unsupported_container`, `unsupported_video_codec`,
`unsupported_audio_codec`, `invalid_media`, and `storage_unavailable`.

## Job progress

`GET /api/v1/jobs/{job_id}`

Returns lifecycle state, the current stage, completed/total stage counts,
checkpoint-safe retry availability, quota cooldown, and a per-stage status
list. Progress is derived from completed/skipped stages; it is not time-based.
The response contains error codes and user-facing messages, not local paths,
provider credentials, stack traces, or transcript text.

Lifecycle values are `UPLOADED`, `QUEUED`, `PROCESSING`, `INDEXING`,
`VERIFYING`, `READY`, `PAUSED_QUOTA`, `FAILED`, and `CANCELLED`. Stage values
include `PENDING`, `RUNNING`, `COMPLETED`, `SKIPPED`, `PAUSED_QUOTA`, `FAILED`,
and `CANCELLED`.

## Job controls

- `POST /api/v1/jobs/{job_id}/cancel` marks active/pending stages cancelled.
  Running media/provider calls stop at their next checkpoint.
- `POST /api/v1/jobs/{job_id}/retry` retries a failed retryable job or resumes a
  cancelled job from persisted checkpoints.
- `POST /api/v1/jobs/{job_id}/resume` resumes a quota-paused job once its
  `resume_after` time has passed. When the provider did not send `Retry-After`,
  the owner can explicitly retry; no quota value is guessed.

Invalid lifecycle transitions return a structured HTTP 409 error.

## Phase 4 audio artifacts

The worker inspects audio timeline metadata with FFprobe, extracts mono 16 kHz
FLAC using FFmpeg, segments below the verified 30-minute word-timestamp limit,
and sends segments to Gemini 3.5 Transcribe only when both consent gates are
satisfied. Each segment retains its original-video offset. Transcripts and
timestamped chunks are written to local work artifacts and checkpoints; they
are not yet inserted into the shared pgvector/FTS index. Consequently, the job
remains `PROCESSING` and is not marked `READY` until the later visual and shared
indexing phases are implemented.

The provider uses the official Interactions API and Files API with
`store: false`, requests verbatim word timestamps, and deletes temporary
provider files after the interaction. Provider file cleanup failures are
recorded; the Files API's documented automatic expiry is 48 hours. The adapter
is based on [Google's transcription documentation](https://ai.google.dev/gemini-api/docs/transcribe)
and [data-use terms](https://ai.google.dev/gemini-api/terms).

Asset/library listing, video detail, question/answer, visual processing, and
shared indexing are not implemented yet.