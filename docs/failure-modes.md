# Processing failure modes

PostgreSQL is the source of truth for job state, stage checkpoints, leases, and
errors. Redis is only used to deliver Celery messages.

| Failure | Behaviour |
| --- | --- |
| Redis unavailable during upload dispatch | The API leaves the durable job queued, returns its IDs, and logs a sanitized warning. Celery Beat retries dispatch from PostgreSQL. |
| Worker/container exits during a stage | The heartbeat stops; after the lease expires, the reconciler republishes the job. A PostgreSQL advisory lock prevents two workers from processing the same job concurrently. |
| Transient FFmpeg or provider network error | The current stage checkpoint is retained; the job is retried with exponential backoff and jitter, up to `JOB_MAX_ATTEMPTS`. |
| Provider returns HTTP 429 | The current stage and job become `PAUSED_QUOTA`. `Retry-After` is persisted when provided. No paid provider fallback is attempted. |
| Provider credentials are missing or invalid | The job fails with a stable, user-facing error. Credential/configuration failures are not retried automatically; a configured owner can use the retry endpoint when the error is marked retryable. |
| Provider rejects invalid audio or returns unusable word offsets | The transcription stage fails without inventing timestamps. Already completed local artifacts remain available for safe retry. |
| Tesseract or scene detection is unavailable | The visual branch fails at its own stage. Any transcript artifacts completed by the concurrent audio branch remain on local storage. |
| Vision provider returns HTTP 429 | The vision stage records the quota response, the job enters `PAUSED_QUOTA`, and completed audio/OCR/visual checkpoints are retained. No paid fallback is attempted. |
| A worker exits during OCR or per-frame vision | Completed OCR and description artifacts remain under the asset work directory. On recovery, the worker reuses valid per-frame artifacts and resumes the incomplete stage. |
| User cancels a running job | PostgreSQL immediately records `CANCELLED`; an active FFmpeg/provider request stops at the next checkpoint. A request already in flight may finish; the worker checks before storing its checkpoint, and a cancellation race can leave only an unindexed local artifact. |
| Provider temporary-file deletion fails | A sanitized cleanup error is recorded in provider usage and the warning is logged. Google documents automatic Files API expiry after 48 hours. |
| Both evidence branches complete but indexing is not implemented | Transcript and visual evidence artifacts remain local and the asset stays `PROCESSING`; it is never reported as READY or searchable. |

Logs include stable IDs, stage names, and error types/codes only. They must not
include API keys, transcript text, provider response bodies, or original media
paths. Local Docker volumes and files are persistence, not backups.
