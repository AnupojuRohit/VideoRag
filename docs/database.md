# VideoRAG database

PostgreSQL is the durable source of truth. Alembic enables pgvector first, then
creates the application schema. Large original media is stored on local disk,
never in database blobs.

## Phase 1 tables

- `video_assets`: UUID identity, unique lowercase SHA-256, local path, validated
  media metadata, lifecycle status, pipeline version, and audit timestamps.
- `processing_jobs`: asset foreign key, lifecycle/current stage, retry count,
  JSON checkpoint/error, and a per-asset idempotency key.
- `processing_stages`: independently checkpointable named stages, unique per
  job, with retry/error/timestamp fields.
- `video_chunks`: modality, text, original-timeline bounds, source reference,
  JSON metadata, pgvector embedding, index-version foreign key, and
  PostgreSQL `tsvector`.
- `embedding_index_versions`: explicit provider model ID, version, dimension,
  and active marker. No model is preselected; a row is added only after the
  provider and its free-tier constraints are verified.

Enums constrain asset/job lifecycle and stage/chunk types. Checks cover SHA-256
shape, positive sizes/durations, nonnegative attempts, and ordered timestamps.
Partial unique indexes limit each asset to one active job and each database to
one active embedding index version.

## Index consistency

A PostgreSQL trigger maintains `video_chunks.search_vector` using the `simple`
text configuration. A separate trigger requires every stored embedding to
reference an index version and checks `vector_dims(embedding)` against that
version's registered dimension. The initial schema has no active embedding
version and does not guess a provider model or dimension.

`video_chunks(video_id, source_ref)` is unique for idempotent evidence writes;
the FTS GIN index and `(video_id, start_time)` index support later retrieval.

## Migrations

```powershell
alembic -c backend/alembic.ini upgrade head
alembic -c backend/alembic.ini current
```

The migration runner in Compose applies upgrades before the API/worker start.
The base pgvector extension migration intentionally leaves the extension in
place on downgrade because extensions may be shared by other database objects.