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
| Embedding provider returns HTTP 429 | The request is recorded, the job enters `PAUSED_QUOTA`, and already committed chunk batches are retained. Retry skips unchanged, already embedded chunks. No paid fallback is attempted. |
| Query embedding provider returns HTTP 429 | Search returns a structured `429` response. The READY asset is unchanged, and the request never re-runs ingestion. |
| Answer generation provider returns HTTP 429 | Q&A returns a structured `429`; the READY asset is unchanged, the user can retry later, and no paid fallback is attempted. |
| Generation returns malformed JSON or a citation not in the supplied context | The answer is rejected and a sanitized provider error is recorded. No answer with an unsupported citation is returned. |
| Retrieval has no usable evidence or generation abstains/omits citations | The service returns a clear insufficient-evidence response with no citations rather than generating an unsupported answer. |
| Unpaid terms, billing, or region acknowledgement is missing | Provider construction is blocked; live calls are not attempted. The user must not enable paid billing as a workaround. |
| Embedding/index write fails after a provider call | The job retries or fails with a stable stage error. Committed chunks remain idempotent; incomplete stages cannot produce `READY`. |
| Configured embedding model/version/dimension differs from the active index | Processing fails closed and asks for an explicit re-embedding migration; it never mixes vector dimensions or models in the active index. |
| A worker exits during OCR or per-frame vision | Completed OCR and description artifacts remain under the asset work directory. On recovery, the worker reuses valid per-frame artifacts and resumes the incomplete stage. |
| User cancels a running job | PostgreSQL immediately records `CANCELLED`; an active FFmpeg/provider request stops at the next checkpoint. A request already in flight may finish; the worker checks before storing its checkpoint, and a cancellation race can leave only an unindexed local artifact. |
| Provider temporary-file deletion fails | A sanitized cleanup error is recorded in provider usage and the warning is logged. Google documents automatic Files API expiry after 48 hours. |
| Index consistency verification finds missing chunks, dimensions, or FTS data | The verification stage fails and the asset is not marked `READY`; retry or repair must make the indexed rows consistent first. |
| Search or answer targets an asset that is not READY or lacks current consent | The API returns a structured conflict and makes no provider request. |
| Original media is missing or stored outside the expected asset directory | Media streaming returns a not-found error without exposing the stored path; indexed text evidence is preserved. |

Logs include stable IDs, stage names, and error types/codes only. They must not
include API keys, transcript text, provider response bodies, or original media
paths. Local Docker volumes and files are persistence, not backups.
