# VideoRAG database

PostgreSQL is the durable source of truth. Alembic enables pgvector first, then
creates the application schema. Large original media is stored on local disk,
never in database blobs.

## Core tables

- `video_assets`: UUID identity, unique lowercase SHA-256, local path, validated
  media metadata and audio start offset, per-asset external-processing consent,
  lifecycle status, pipeline version, and audit timestamps.
- `processing_jobs`: asset foreign key, lifecycle/current stage, retry count,
  JSON checkpoint/error, heartbeat, dispatch timestamp, lease, quota cooldown,
  and a per-asset idempotency key.
- `processing_stages`: independently checkpointable named stages, unique per
  job, with retry/error/timestamp fields.
- `provider_usage_events`: provider/model operation, status, latency, request
  ID, input bytes, and stable error code. It does not store transcripts,
  credentials, or request payloads.
- `video_chunks`: modality, text, original-timeline bounds, source reference,
  JSON metadata, pgvector embedding, index-version foreign key, and
  PostgreSQL `tsvector`.
- `embedding_index_versions`: explicit provider model ID, version, dimension,
  and active marker. The verified embedding adapter is configured as
  `gemini-embedding-001`, version label `stable`, dimension 768; the active row
  is created when indexing first starts. A model or dimension change is
  rejected until a re-embedding migration is implemented.

Enums constrain asset/job lifecycle and stage/chunk types. Checks cover SHA-256
shape, positive sizes/durations, nonnegative attempts, and ordered timestamps.
Partial unique indexes limit each asset to one active job and each database to
one active embedding index version.

The worker holds a PostgreSQL advisory lock while processing a job. A short
lease plus heartbeat lets the periodic Celery Beat reconciler safely republish
abandoned work after a worker/container restart. Redis stores no durable job
state and the Celery result backend is disabled.

Audio artifacts and transcripts are checkpointed under
`data/work/<asset_id>/audio/`; sampled frames, per-frame OCR, descriptions, and
the visual evidence manifest are under `data/work/<asset_id>/visual/`. Each
branch has independent stage checkpoints, and provider usage records include
token counts when the provider returns them. Each branch submits evidence to
shared indexing as it becomes available, and committed batches survive retries.
Assets are not READY until verification has checked all expected chunks,
embedding dimensions, and FTS content. Retrieval embedding usage is also
recorded against the asset's completed processing job without storing the query
text.

The video-scoped search and answer routes accept only READY assets with current
external-processing consent. Their vector and keyword queries filter to the
same active index-version ID; FTS and vector results are fused in application
code using RRF, with duplicate chunk IDs merged before context assembly.
Generation requests use only the selected evidence context and question; the
Interactions API request is stateless (`store: false`). Question text and
answer content are not written to the database. Provider usage metadata,
including available token counts and request IDs, is recorded against the
asset's processing job.

## Index consistency

A PostgreSQL trigger maintains `video_chunks.search_vector` using the `simple`
text configuration. A separate trigger requires every stored embedding to
reference an index version and checks `vector_dims(embedding)` against that
version's registered dimension. The shared indexer verifies vector dimensions and nonempty
`tsvector` values before readiness.

`video_chunks(video_id, source_ref)` is unique for idempotent evidence writes;
unchanged chunks with a valid active embedding are skipped on retry. Completed
modalities remove stale source references after indexing their full evidence
set. The FTS GIN index and `(video_id, start_time)` index support retrieval.

Embedding and answer-generation calls are serialized across worker processes
with a PostgreSQL advisory lock and paced by
`GEMINI_MIN_REQUEST_INTERVAL_SECONDS` (default one second). This is an
application-side safety setting, not a provider quota claim; Google free-tier
quotas are account-specific. Transcription and vision retain their separate
concurrent processing paths.

## Migrations

```powershell
alembic -c backend/alembic.ini upgrade head
alembic -c backend/alembic.ini current
```

The migration runner in Compose applies upgrades before the API/worker start.
The base pgvector extension migration intentionally leaves the extension in
place on downgrade because extensions may be shared by other database objects.