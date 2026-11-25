<aside>
🎬

**Goal:** Build a multimodal video knowledge retrieval and question-answering system. Users upload lectures, coding tutorials, presentations, meetings, or supported YouTube videos, then ask grounded questions with clickable timestamps.

**Budget constraint:** ₹0. No paid APIs, cloud GPUs, paid hosting, paid storage, subscriptions, or silent paid fallbacks. AI inference is online-only through verified free tiers. App infrastructure and media processing run locally.

</aside>

## 1. Product scope

VideoRAG indexes two evidence modalities:

1. **Audio evidence:** timestamped speech transcript.
2. **Visual evidence:** selected frames, OCR text, and AI-generated descriptions of slides, diagrams, code, and UI states.

Example questions:

- Explain everything the speaker said about Redis.
- At what timestamp did they explain consumer groups?
- What does the diagram at that point show?
- Find the section where retry handling was demonstrated.
- Compare the two architectures shown.
- Summarize only the backend architecture portions.

**Scope clarification:** Version one performs visual retrieval through searchable OCR and text descriptions of frames. It does not claim native image-to-image embedding retrieval. YouTube support is limited to videos that can legally and technically be accessed; private, restricted, age-gated, or inaccessible videos cannot be guaranteed.

## 2. Architecture decisions

| Decision | Baseline | Rationale |
| --- | --- | --- |
| Application shape | Modular monolith | Avoid premature microservices; easier to build, test, and explain |
| UI | Next.js + TypeScript | Upload, library, player, and chat |
| API | FastAPI | Python ecosystem for media and AI integrations |
| Database | PostgreSQL | Durable metadata, jobs, chunks, and search |
| Vector search | pgvector | Vectors alongside relational data |
| Keyword search | PostgreSQL Full-Text Search | Exact technical terms, acronyms, and identifiers |
| Queue | Redis + Celery | Asynchronous work and bounded worker execution |
| Media processing | FFmpeg / FFprobe | Media inspection and extraction |
| Scene detection | PySceneDetect | Representative frame selection |
| Retrieval fusion | Reciprocal Rank Fusion (RRF) | Combine vector and keyword rankings |
| AI inference | Online provider adapters | User explicitly prefers online inference; free-tier support must be verified |
| Storage | Local SSD | No paid object storage or cloud media hosting |

## 3. System architecture

Next.js + TypeScript UI → FastAPI backend

FastAPI coordinates:

- PostgreSQL + pgvector + full-text search + metadata and job state
- Redis + Celery queues and workers
- Local SSD media storage

Ingestion workers use FFmpeg for media preparation, then run two concurrent branches:

**Audio branch:** audio extraction → segmentation → online transcription → timestamp normalization → transcript chunking.

**Visual branch:** scene detection → frame selection → near-duplicate removal → OCR → online vision analysis → visual evidence chunks.

Both branches feed a shared indexing pipeline: chunk normalization → batched embeddings → PostgreSQL vector and full-text indexing → consistency verification → asset READY.

At question time: query embedding → vector search and keyword search in parallel → RRF fusion → context assembly → online answer model → grounded answer with timestamps.

## 4. Technology stack

| Layer | Technology |
| --- | --- |
| Frontend | Next.js + TypeScript |
| Backend API | FastAPI + Python |
| Database | PostgreSQL |
| Vector index | pgvector |
| Keyword index | PostgreSQL Full-Text Search |
| ORM | SQLAlchemy |
| Migrations | Alembic |
| Queue / workers | Redis + Celery |
| Media inspection | FFprobe |
| Media extraction | FFmpeg |
| Scene detection | PySceneDetect |
| Transcription | Verified Gemini adapter through Interactions and Files APIs |
| Vision | Gemini 3.8 Flash (`gemini-3.8-flash`), Interactions API |
| Embeddings | Gemini `gemini-embedding-001`, text-only, 768-dimensional active index |
| Answer generation | Gemini 3.8 Flash (`gemini-3.8-flash`), stateless Interactions API |
| Testing | pytest + Playwright |
| Local infrastructure | Docker Compose |

**Provider IDs and availability are verified per adapter and may change.** The vision and generation adapters use documented `gemini-3.8-flash` input/output through stateless Interactions requests (`store: false`); generation uses structured JSON output. Google lists a free Standard tier for the model, with a documented 1,048,576-token input limit and 65,536-token output limit. Account-specific free quotas and rate limits are not guaranteed. Embeddings use the text-only `gemini-embedding-001` model via `batchEmbedContents`, with a configured 768-dimensional index and conservative application input/batch bounds. Unpaid-tier content and responses may be used to improve Google products and human-reviewed. Terms require an 18+ operator and professional/business use, prohibit consumer-use apps or apps directed to minors, and require paid services for API clients available to users in the EEA, UK, or Switzerland. Paid services remain prohibited by this project, so unpaid inference must be disabled in those regions. See the [embedding guide](https://ai.google.dev/gemini-api/docs/embeddings), [vision/generation model documentation](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash), [image understanding guide](https://ai.google.dev/gemini-api/docs/image-understanding), [structured output](https://ai.google.dev/gemini-api/docs/structured-output), [Interactions API](https://ai.google.dev/gemini-api/docs/interactions), [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits), and [terms](https://ai.google.dev/gemini-api/terms). Recheck official documentation before live use or changing model IDs. Do not code against guessed capabilities or quota values.

### Provider abstraction

Keep provider-specific calls behind four interfaces: TranscriptionProvider, VisionProvider, EmbeddingProvider, and GenerationProvider. The current Gemini adapters implement these verified capabilities behind their interfaces. The pipeline must not be coupled to one vendor or silently switch to paid models.

## 5. Upload, validation, and local storage

Initial media policy:

- Recommended input: MP4 with H.264 video and AAC audio.
- Initial target duration: up to 60 minutes.
- Recommended upload size: up to 500 MB.
- A larger hard cap, such as 1 GB, is a possible guardrail but is not finalized; decide after testing.
- Validate actual streams with FFprobe; do not trust extension or browser MIME type alone.
- Reject unsupported, corrupt, or oversized files clearly.
- Sanitize filenames, use application-generated asset IDs, prevent path traversal, and limit FFmpeg runtime/resources.

Suggested local storage layout:

data/
originals/<asset_id>/source.mp4
work/<asset_id>/audio/
work/<asset_id>/frames/
work/<asset_id>/temporary/
logs/

Keep original media separate from temporary artifacts. Large media stays on disk, not in PostgreSQL rows. Define retention and cleanup rules. If the original is deleted but indexed chunks remain, text Q&A may work but playback and timestamp seeking will not.

## 6. SHA-256 exact-file deduplication

SHA-256 is a cryptographic hash function. It processes the exact bytes of a file and produces a fixed 256-bit digest, usually represented as 64 hexadecimal characters.

- Same bytes with a different filename or folder → same hash.
- Same exact file uploaded again later → same hash.
- Re-encoded, compressed, trimmed, or modified video → usually a different hash.
- SHA-256 detects exact file identity; it does not detect visually similar videos.

### Deduplication flow

1. Browser calculates SHA-256, preferably in a Web Worker so hashing does not freeze the UI.
2. Browser sends the hash to a backend preflight endpoint.
3. Backend looks up the hash in PostgreSQL.
4. If a matching asset is READY, return its existing asset ID; skip upload and ingestion.
5. If QUEUED or PROCESSING, attach the user to the existing job and show progress.
6. If PAUSED_QUOTA, preserve the job and resume when the provider permits.
7. If FAILED, offer a safe checkpoint-based retry.
8. If there is no match, accept the upload.
9. Backend independently hashes uploaded bytes while streaming and verifies the browser hash. The server hash is authoritative.
10. Create the canonical asset and enqueue ingestion.

The browser still reads the file to hash it, but a duplicate can avoid transferring the large file and avoid repeated processing.

### Concurrency safety

Add a unique database constraint on sha256_hash. Two identical uploads can race between preflight and insert; the unique constraint is the final authority. On a uniqueness conflict, fetch and reuse the existing asset.

| Existing asset state | Duplicate-upload behaviour |
| --- | --- |
| READY | Reuse existing asset |
| QUEUED / PROCESSING | Attach to existing job |
| PAUSED_QUOTA | Reuse job; resume when allowed |
| FAILED | Retry safely from checkpoint |
| No matching hash | Upload and ingest |

**Future enhancement:** Perceptual video/audio fingerprints could detect re-encoded duplicates, but add complexity and false-positive risk. Keep them out of version one.

## 7. Asynchronous orchestration: Redis + Celery

The upload API returns quickly; heavy processing runs in background workers.

- Redis is the broker/coordination layer.
- Celery executes background tasks.
- PostgreSQL is the durable source of truth for asset status, stage status, checkpoints, and errors.
- Redis is not permanent video memory or the source of truth.
- On restart, reconcile unfinished jobs from PostgreSQL and re-enqueue eligible work.

Job lifecycle:

UPLOADED → QUEUED → PROCESSING → INDEXING → VERIFYING → READY

Other states: PAUSED_QUOTA, FAILED, CANCELLED.

Track stages independently: media preparation, audio extraction, transcription, transcript chunking, scene detection, frame extraction, OCR, visual analysis, embeddings, database indexing, and verification.

### Idempotency and retries

An idempotent task can run more than once without creating duplicate or inconsistent results.

- Use stable asset, segment, and chunk identifiers.
- Add unique constraints for idempotency.
- Persist checkpoints and progress in PostgreSQL.
- Retry temporary network/provider failures with exponential backoff and jitter.
- Respect provider Retry-After instructions.
- Limit attempts.
- Do not retry invalid input or permanent authentication/configuration errors indefinitely.
- Pause on quota exhaustion; do not fall back to paid services.
- A visual-stage failure must not erase successfully processed transcript data, and vice versa.

## 8. Parallel ingestion pipeline

After common media preparation, audio and visual processing run independently and concurrently. Both publish completed evidence to shared indexing.

### 8.1 Common media preparation

1. FFprobe inspects duration, codecs, dimensions, frame rate, and available audio/video streams.
2. FFmpeg extracts audio and prepares video/frame access.
3. Preserve a mapping to the original video timeline. All timestamps refer to the original video, not a temporary segment's local clock.

### 8.2 Audio pipeline

1. Extract audio.
2. Split audio into manageable segments according to verified provider limits.
3. Preserve each segment's original start-time offset.
4. Send segments to online transcription with bounded concurrency.
5. Receive timestamped transcript text.
6. Normalize transcript text and timestamps.
7. Reconcile overlapping words or sentences at segment boundaries.
8. Split transcript into coherent retrieval chunks while preserving time ranges.
9. Send completed chunks to shared indexing.

Example evidence record:

{
"type": "transcript",
"start_time": 125.4,
"end_time": 143.8,
"text": "A Redis consumer group allows multiple consumers to coordinate..."
}

Timestamp quality must be checked. If a provider returns only segment-level timestamps, do not pretend they are word-level timestamps.

### 8.3 Visual pipeline

1. Detect scene/shot transitions using PySceneDetect.
2. Select representative frames rather than sending every frame to a vision model.
3. Use adaptive sampling based on scene changes, slide changes, meaningful UI changes, and a configurable interval.
4. Resize frames while keeping text readable.
5. Remove near-identical frames using perceptual deduplication.
6. Run OCR on selected frames.
7. Send selected frames and relevant OCR context to an online vision model.
8. Store each frame's original timestamp, OCR text, and visual description.
9. Convert visual evidence into searchable text chunks.

Example visual evidence record:

{
"type": "visual",
"start_time": 131.0,
"end_time": 131.0,
"frame_path": "data/work/asset-id/frames/frame_00042.jpg",
"ocr_text": "Redis Streams / Consumer Groups",
"description": "A slide showing a Redis Streams architecture with producers, a stream, and multiple consumers."
}

### 8.4 Incremental producer-consumer indexing

Do not wait for both entire branches to finish before starting all indexing.

- While audio segment 2 is being transcribed, completed chunks from segment 1 can be normalized, embedded, and stored.
- While later frame batches are being analysed, earlier visual descriptions can be embedded and stored.
- A shared indexing stage accepts completed evidence from both branches.
- Batch embedding requests and database writes.
- Use a process-shared PostgreSQL advisory lock and configurable request spacing
  for embedding and generation calls without claiming an account's free quota.
- Mark the video READY only after all required audio and visual stages, embeddings, full-text records, and consistency checks are complete.
- A future partial-search feature must clearly label results as partial; never report a partially indexed asset as READY.

## 9. Free-tier quota and cost management

Free-tier availability and limits can change. Treat them as explicit runtime constraints, not assumptions.

- Keep configurable provider request/token limits based on verified documentation.
- Use provider-level rate limiting shared across videos.
- Use semaphores and bounded concurrency.
- Batch embeddings within provider limits.
- Batch frame descriptions only if the endpoint supports it.
- Track request counts, tokens where available, latency, and quota errors.
- Use exponential backoff, Retry-After, cooldowns, and circuit breakers.
- Show statuses such as “Waiting for free-tier quota” in the UI.
- Never assume multiple API keys or projects make usage unlimited or compliant.
- Never introduce a paid fallback.

When quota is exhausted, pause safely and resume later. Throughput is subordinate to the free-tier limit.

**Privacy note:** Online inference means audio, transcript text, selected frames, OCR, questions, retrieved evidence, and generated answers may leave the laptop and be processed by an external provider. The Interactions API requests use `store: false`, but unpaid-service terms still allow Google to use inputs and outputs to improve products and permit human review. Review current terms before use; do not send sensitive, confidential, or personal content.

## 10. Shared indexing and chunk representation

Normalize audio and visual evidence into a common searchable chunk format.

Each chunk should contain:

- id
- video_id
- chunk_type (transcript or visual)
- searchable content
- start_time and end_time
- source segment or frame reference
- metadata
- embedding model/version
- vector embedding
- PostgreSQL full-text search representation

Chunking rules:

- Transcript chunks should be semantically coherent, not cut mid-sentence where avoidable.
- Preserve the original time span of each chunk.
- Visual chunks should describe one frame or a small group of closely related frames.
- Tune chunk size and overlap using retrieval benchmarks.
- Keep model name and embedding dimension consistent.
- Changing embedding models or dimensions requires a deliberate re-embedding migration.

Two indexes:

1. **Vector index:** semantic similarity using pgvector.
2. **Full-text index:** exact words, technical terms, acronyms, identifiers, and code-like tokens using PostgreSQL FTS.

Benchmark exact vector search against HNSW. HNSW is optional for a small dataset; it trades memory/build work for faster approximate search. Add it only when measured query performance requires it.

## 11. Query-time retrieval and answer generation

A new question must not reprocess the video.

1. Validate the selected video and question.
2. Embed the query with the same embedding model used for chunks.
3. Run vector similarity and PostgreSQL FTS in parallel, filtering by video_id unless the user asks across their library.
4. Combine ranked lists using Reciprocal Rank Fusion (RRF).
5. Deduplicate overlapping evidence.
6. Assemble compact context within the answer model's token budget.
7. Include transcript and visual evidence when relevant, with timestamps.
8. Ask the online generation model to answer only from supplied evidence.
9. Return the answer with evidence references and clickable timestamps.

### Why hybrid search?

- Vector search finds semantically related passages even when wording differs.
- Keyword search catches exact technical names, acronyms, function names, and identifiers.
- RRF combines rankings without treating raw vector and keyword scores as directly comparable.

Baseline uses RRF only. A reranker is not part of the baseline. Consider an online reranker only if a genuinely free endpoint is verified and evaluation shows a meaningful improvement. Do not add local AI reranking because the agreed direction is online inference.

### Grounding rules

The answer model must:

- Never invent facts not supported by retrieved evidence.
- Distinguish what the speaker said from what is visibly shown.
- Include timestamps only when supported by evidence.
- Say when evidence is insufficient.
- Label inference as inference, not as a direct quote or visual observation.
- Return evidence links that seek the video player to the correct timestamp.

Store timestamps in seconds; display as MM:SS (or HH:MM:SS for long videos). Clicking a timestamp seeks the player.

## 12. Caching and repeated-work avoidance

1. **Exact duplicate video:** SHA-256 reuses the existing asset; skip upload and ingestion.
2. **New question on an indexed video:** run query embedding, retrieval, and answer generation only. Do not reprocess media.
3. **Repeated question:** optional answer/retrieval cache keyed by asset ID, normalized question, conversation context, prompt version, and model version.

“Zero reprocessing” means the video ingestion pipeline is not repeated for each question. It does not mean zero answer latency.

## 13. Logical database design

### video_assets

| Column | Purpose |
| --- | --- |
| id UUID primary key | Canonical asset identity |
| sha256_hash UNIQUE | Exact-file deduplication |
| original_path | Local original file path |
| original_filename | Display name |
| file_size_bytes | File size |
| duration_seconds | Media duration |
| mime_type | Detected/validated media type |
| video_codec, audio_codec | Actual codecs |
| width, height, frame_rate | Video metadata |
| status | Overall lifecycle state |
| pipeline_version | Pipeline version used |
| created_at, updated_at | Audit timestamps |

### processing_jobs

- id UUID
- video_id foreign key
- status
- stage
- attempt_count
- checkpoint JSON
- last_error
- started_at, finished_at
- created_at, updated_at

### processing_stages (recommended)

One row per stage per asset/job, with stage status, attempt count, timestamps, checkpoint, and error. This supports independent recovery of audio and visual branches.

### video_chunks

- id
- video_id foreign key
- chunk_type
- content
- start_time, end_time
- embedding vector
- search_vector
- source_ref
- metadata JSON
- embedding_model
- created_at

Additional tables as needed: conversations, messages, answer_evidence, provider_usage, and optionally video_library_items if multiple library references should point to one canonical asset.

Use foreign keys, a unique hash constraint, idempotency constraints, indexes on video_id and timestamps, and a consistent vector dimension. Keep large media files on local disk rather than storing them in database rows.

## 14. User experience

Core screens:

- Video library
- Upload and duplicate-detection flow
- Video player
- Chat panel
- Processing progress and error details

Show meaningful pipeline states:

- Uploading
- Validating media
- Preparing audio/video
- Transcribing audio
- Detecting scenes / selecting frames
- Analysing visual content
- Generating embeddings
- Building search index
- Verifying index
- Ready
- Waiting for free-tier quota
- Failed / retry available

A duplicate upload should clearly say that an existing asset or job is being reused.

## 15. Observability and evaluation

Log structured identifiers such as asset ID, job ID, stage, and attempt. Never log API keys or unnecessary full private transcripts.

### Ingestion metrics

- Upload duration and bytes
- Media preparation time
- Audio seconds processed per wall-clock second
- Transcription latency and timestamp quality
- Scenes detected, frames sampled, near-duplicates removed
- OCR usefulness/coverage
- Vision latency and quota errors
- Embedding throughput
- Database indexing time
- CPU, RAM, disk, and worker concurrency
- Retries, quota pauses, and recovery outcomes

### Retrieval and answer metrics

- Recall@k
- Evidence relevance
- Timestamp accuracy
- Answer faithfulness / groundedness
- Visual evidence coverage
- Abstention quality when evidence is missing
- Time to first token
- Total answer latency

### Evaluation videos

Test at least three video types:

1. Slide-heavy lecture.
2. Programming tutorial with frequent code/editor changes.
3. Technical presentation with diagrams or animations.

Create a small question-and-ground-truth set for each video. Measure retrieval and answer quality; do not rely only on subjective demos.

## 16. Performance targets — hypotheses, not claims

Earlier planning estimates:

- 60-minute video ingestion stretch target: 5–12 minutes.
- More conservative planning expectation: 10–20 minutes.
- Retrieval target: under 500 ms.
- Answer latency planning range: roughly 2–8+ seconds.

These are not guarantees and must not be used as resume claims until measured. Actual performance depends on provider latency and quotas, bitrate, audio quality, selected frame count, scene complexity, retries, network conditions, and local hardware. Benchmark on the actual laptop and free-tier provider.

## 17. Reliability and security

- Store API keys in environment variables.
- Never commit .env; provide .env.example with placeholders.
- Validate uploads and actual media streams.
- Sanitize filenames and prevent path traversal.
- Restrict FFmpeg runtime and resource consumption.
- Bind the local API to localhost initially.
- Avoid logging secrets or unnecessary private transcripts.
- Explain external provider data sharing and review data terms.
- Back up PostgreSQL and original media. Docker volumes provide persistence, not backups.
- Recover unfinished jobs from PostgreSQL after restart.
- Keep audio and visual stage failures independent where possible.

## 18. Repository structure

videorag/
├── frontend/
│   ├── app/
│   ├── components/
│   ├── hooks/
│   └── lib/
├── backend/
│   ├── app/
│   │   ├── api/
│   │   ├── core/
│   │   ├── db/
│   │   ├── models/
│   │   ├── schemas/
│   │   ├── services/
│   │   │   ├── media/
│   │   │   ├── ingestion/
│   │   │   ├── transcription/
│   │   │   ├── vision/
│   │   │   ├── embeddings/
│   │   │   ├── retrieval/
│   │   │   ├── generation/
│   │   │   └── deduplication/
│   │   ├── providers/
│   │   ├── workers/
│   │   └── tasks/
│   └── tests/
├── docs/
│   ├── architecture.md
│   ├── architecture-decisions/
│   ├── pipeline.md
│   ├── retrieval.md
│   ├── evaluation.md
│   ├── database.md
│   ├── api.md
│   ├── failure-modes.md
│   ├── testing.md
│   └── interview-preparation.md
├── infra/docker-compose.yml
├── data/
├── .env.example
├── .gitignore
└── README.md

Create files incrementally as each phase is implemented; do not generate a huge empty structure up front.

## 19. Implementation roadmap

| Phase | Scope | Exit criteria |
| --- | --- | --- |
| 0 | Foundation: repo, Docker Compose, env, frontend/backend skeleton, PostgreSQL/pgvector, Redis, health checks, logging, tests | One command launches the stack; frontend opens; backend connects to DB |
| 1 | Durable schema and migrations: assets, jobs, stages, chunks, indexes | Schema matches the lifecycle and enforces durable constraints |
| 2 | Upload + SHA-256: validation, local storage, browser preflight, server verification, unique constraint | Re-uploading exact bytes reuses the existing asset |
| 3 | Durable job orchestration: Celery dispatch, PostgreSQL reconciliation, leases, retries, quota pause, status/control APIs | Jobs recover safely after restart without treating Redis as durable state |
| 4 | Media preparation and audio: FFmpeg, bounded segments, verified online transcription, original-timeline timestamps, transcript artifacts | Timestamped audio evidence is checkpointed locally; asset stays non-READY |
| 5 | Visual pipeline: scene detection, frame sampling, dedup, OCR, online vision | Timestamped visual evidence is checkpointed locally; audio and visual branches run concurrently; asset stays non-READY |
| 6 | Shared indexing: normalization, online embeddings, batching, pgvector, FTS | Both modalities are indexed and verified |
| 7 | Hybrid retrieval: vector search, keyword search, RRF | Implemented: video-scoped parallel retrieval, active-index filtering, deterministic RRF, and tests |
| 8 | Grounded Q&A: context assembly, generation, evidence, abstention | Implemented: stateless structured generation, citation validation, abstention, and consent/region gates |
| 9 | Complete UI: library, player, chat, progress, errors | Implemented: API-backed library/detail/player, real job progress, Q&A, timestamp seeking, and Playwright journeys |
| 10 | Evaluation: ingestion, retrieval, timestamp, latency benchmarks | Evaluation protocol documented; live measurements blocked on eligible provider configuration and consented evaluation videos |
| 11 | Hardening: security, recovery, resource limits, docs, failure tests | In progress; backend recovery/retry tests, backup/restore runbook, and Playwright checks added; an isolated restore drill remains |

Earlier planning estimate: approximately 28–42 days at 3–4 focused hours/day for an interview-ready build; a narrower prototype may take 2–3 weeks. This is a planning estimate, not a commitment.

## 20. Open decisions to validate during implementation

These are not final decisions and should be resolved with tests or official provider documentation:

- Current account-specific free quotas and rate limits, media limits, retention, and data-use terms must be rechecked before live use.
- Google unpaid inference is not eligible for API clients available to users in the EEA, UK, or Switzerland; this project cannot switch to paid access.
- Final hard upload cap and supported formats.
- Frame sampling policy and maximum vision requests per video.
- Transcript chunk size and overlap.
- Embedding dimension and model migration strategy.
- Whether HNSW is needed at the expected dataset size.
- Whether partial search is worth exposing before both branches finish.
- Retention policy for originals, extracted audio, and frames.
- Whether a verified free online reranker adds enough measured value to justify complexity.

## 21. Definition of done

A credible first release should demonstrate:

- Upload a supported video and validate its actual media streams.
- Calculate and verify SHA-256; reuse exact duplicate uploads without repeating ingestion.
- Run audio and visual ingestion branches concurrently.
- Persist job state and recover from a worker/backend restart.
- Pause safely on provider quota exhaustion without paid fallback.
- Produce timestamped transcript and visual evidence.
- Index both modalities in pgvector and PostgreSQL FTS.
- Retrieve with vector + keyword search and RRF.
- Answer using retrieved evidence, distinguish speech from visuals, and abstain when evidence is insufficient.
- Click a timestamp and seek the player to the correct point.
- Report measured ingestion, retrieval, timestamp, and answer-quality metrics.
- Document provider privacy constraints and the ₹0 cost boundary.

**Engineering principle:** Build the smallest verifiable vertical slice at each phase. Keep the architecture modular, make every stage observable and retry-safe, and measure before adding complexity.

---

## 22. AI coding-agent instructions — canonical project contract

This section contains the two prompts to use with an AI coding agent. The **System Prompt** defines permanent engineering rules. The **Master Build Prompt** starts and drives implementation. Both prompts are subordinate to the architecture above: if a coding agent proposes a conflicting design, it must stop and explain the conflict instead of silently changing the architecture.

### 22.1 System prompt — VideoRAG engineering agent

Copy this into the coding agent's persistent system instructions.

```
You are the principal software architect, senior full-stack engineer, backend engineer, AI/RAG engineer, and test engineer responsible for building VideoRAG.

Your job is to implement the existing VideoRAG Architecture & Engineering Specification exactly, incrementally, and with production-minded engineering quality. You are not free to redesign the system. The Notion specification is the source of truth.

MISSION
Build a local-first, multimodal video knowledge retrieval and question-answering application. A user can upload a supported video or provide a supported accessible YouTube URL, wait for ingestion, then ask questions and receive evidence-grounded answers with clickable timestamps. The system indexes both timestamped speech and selected visual evidence.

NON-NEGOTIABLE PRODUCT AND COST RULES
1. Total budget is ₹0. No paid APIs, paid model endpoints, paid cloud GPUs, paid hosting, paid object storage, paid databases, subscriptions, or paid fallback services.
2. AI inference is ONLINE ONLY through verified free-tier endpoints. Do not run local LLMs, local embedding models, or local transcription/vision models. Local non-AI media processing and OCR are permitted only as specified in the architecture; OCR must not silently become a local generative model.
3. Application infrastructure, PostgreSQL, pgvector, Redis, Celery, media files, and workers run locally using Docker Compose/local SSD.
4. Before implementing a provider integration, verify its current official documentation: exact model ID, API endpoint, supported input types, file/video/audio limits, embedding dimension, free quota, rate limits, regional availability, retention and data-use terms. Never guess a model ID or assume a free tier exists.
5. If no compliant free endpoint supports a required capability, do not substitute a paid service or local AI model. Report the blocker, isolate it behind the provider interface, and pause that capability until an approved free solution is verified.
6. Never add a paid fallback. Quota exhaustion means PAUSED_QUOTA, cooldown, and safe resume—not billing.
7. Do not claim performance, accuracy, free quota, or compatibility without measurements or official evidence.

LOCKED ARCHITECTURE
- Frontend: Next.js + TypeScript.
- Backend API: Python + FastAPI.
- Database: PostgreSQL with pgvector and PostgreSQL Full-Text Search.
- ORM: SQLAlchemy.
- Migrations: Alembic.
- Queue and background execution: Redis + Celery.
- Media inspection and extraction: FFprobe + FFmpeg.
- Scene detection: PySceneDetect.
- OCR: a clearly isolated OCR component; use it to extract visible text from selected frames.
- AI provider interfaces: TranscriptionProvider, VisionProvider, EmbeddingProvider, GenerationProvider.
- Retrieval: vector similarity + PostgreSQL FTS in parallel, fused with Reciprocal Rank Fusion (RRF).
- Storage: local SSD; original media and temporary artifacts are files, never large database blobs.
- Local orchestration: Docker Compose.
- Tests: pytest for backend and Playwright for end-to-end UI.
- Application shape: modular monolith. Do not split into microservices.
- Do not add a reranker, HNSW, Kafka, Kubernetes, LangChain/LlamaIndex, a separate vector database, or other infrastructure unless the owner explicitly approves a measured, documented architecture change. Baseline RRF does not include reranking; benchmark exact vector search before considering HNSW.

REQUIRED END-TO-END PIPELINE
A. Upload and identity:
- Validate extension, MIME, actual media streams with FFprobe, file size, duration, and supported codecs.
- Initial target: up to 60 minutes and recommended upload size up to 500 MB. Treat these as initial product limits, configurable and testable; do not silently raise them. The larger 1 GB hard cap is not finalized.
- Sanitize filenames, generate application-owned UUID asset IDs, prevent path traversal, enforce safe FFmpeg resource/time limits.
- Compute SHA-256 in the browser (prefer a Web Worker) for preflight, then independently hash uploaded bytes on the server while streaming. Server hash is authoritative.
- Store a UNIQUE constraint on sha256_hash. Handle concurrent duplicate uploads safely using the database constraint.
- READY duplicate: reuse the canonical asset; QUEUED/PROCESSING duplicate: attach to the existing job; PAUSED_QUOTA: reuse and resume when allowed; FAILED: offer checkpoint-safe retry.
- SHA-256 identifies exact bytes only. Do not claim it detects re-encoded or visually similar videos.

B. Durable asynchronous processing:
- API returns quickly; workers do heavy processing.
- PostgreSQL is the durable source of truth for job state, stage state, checkpoints, and errors. Redis is a broker/coordination mechanism, never permanent state.
- Lifecycle: UPLOADED → QUEUED → PROCESSING → INDEXING → VERIFYING → READY. Also support PAUSED_QUOTA, FAILED, CANCELLED.
- Track stages independently, including media preparation, audio extraction, transcription, transcript chunking, scene detection, frame extraction, OCR, visual analysis, embeddings, indexing, and verification.
- Use stable IDs, uniqueness constraints, idempotent tasks, persisted checkpoints, bounded concurrency, retry limits, exponential backoff with jitter, Retry-After handling, and clear permanent-vs-transient error classification.
- Recover unfinished work from PostgreSQL after process/container restart.
- Audio and visual branches fail independently; a failure in one must not erase successful work in the other.

C. Common media preparation:
- Use FFprobe to inspect duration, codecs, dimensions, frame rate, and streams.
- Use FFmpeg for extraction/preparation.
- Preserve original-video timeline offsets through every segment and frame. All persisted timestamps refer to the original video timeline.

D. Audio branch:
- Extract audio, segment within verified provider limits, retain each segment's original start offset, and transcribe through the online TranscriptionProvider.
- Normalize provider timestamps and reconcile overlapping segment boundaries.
- Never claim word-level precision if only segment-level timestamps are returned.
- Create coherent transcript chunks with accurate start_time/end_time and submit them incrementally to shared indexing.

E. Visual branch:
- Detect scenes/shots with PySceneDetect.
- Select representative frames using adaptive scene/slide/UI-change sampling and configurable interval limits; never send every frame by default.
- Resize while preserving legibility; remove near-identical frames with perceptual deduplication.
- Run OCR on selected frames and send selected frames plus relevant OCR context to the online VisionProvider.
- Store original timestamp, frame path, OCR text, and model-generated visual description as separate fields.
- Convert each frame or closely related frame group into searchable visual evidence.
- Version and configure sampling policy; do not invent visual observations.

F. Incremental shared indexing:
- Audio and visual producers feed one shared indexing pipeline as evidence becomes available. Do not wait for both complete branches before indexing all results.
- Normalize each evidence item into a common chunk model with id, video_id, chunk_type, content, start_time, end_time, source_ref, metadata, embedding_model/version, embedding, and FTS representation.
- Batch online embeddings within verified provider limits and batch database writes.
- Store vectors in pgvector and exact searchable text in PostgreSQL FTS.
- Enforce one embedding model/dimension per active index version. Model/dimension changes require an explicit re-embedding migration strategy.
- Mark READY only after required branches, embeddings, FTS rows, and consistency checks are complete. Never label incomplete indexing READY.

G. Query and answer:
- A question must never trigger video re-ingestion.
- Embed the question with the same embedding model/version used by the selected chunk index.
- Run pgvector semantic retrieval and PostgreSQL FTS keyword retrieval in parallel, scoped to video_id unless library-wide search is explicitly requested.
- Fuse ranked lists using RRF; deduplicate overlapping evidence; assemble a compact context under the generation model's verified token limit.
- The GenerationProvider must answer only from supplied evidence.
- Preserve evidence modality: distinguish speaker transcript from visible frame/OCR/visual description.
- Include supported timestamps and clickable player seek links. Say when evidence is insufficient. Clearly label inference.
- Never invent quotes, facts, visual details, or timestamp precision.

DATA AND API DESIGN
- Follow the database entities and lifecycle in the Notion specification: video_assets, processing_jobs, processing_stages, video_chunks, and only the additional conversation/message/evidence/provider-usage tables needed by implemented features.
- Use UUID primary keys, foreign keys, indexes, unique constraints, typed Pydantic schemas, explicit transaction boundaries, and Alembic migrations.
- Keep API routers thin. Put business logic in services. Keep provider SDK calls in adapters. Keep Celery task wrappers thin and idempotent.
- Define documented API contracts for upload preflight, upload completion, asset/library listing, job status/progress, retry/cancel, video details, and question/answer.
- Return structured errors with stable codes and useful user-facing messages. Never expose stack traces, secrets, or provider credentials.

FRONTEND EXPERIENCE
Build a polished, responsive, accessible application with:
- Video library and asset status.
- Upload flow with progress, SHA-256 duplicate preflight, validation feedback, and duplicate reuse messaging.
- Video detail/player view with chat beside the player.
- Clickable timestamps that seek the player to the correct original-video time.
- Per-stage processing progress and meaningful statuses: validating, preparing media, transcribing, detecting scenes, analysing visuals, embedding, indexing, verifying, ready, waiting for free-tier quota, failed/retry available.
- Clear empty, loading, error, retry, and quota-paused states.
- Never fake progress or show a video as ready before verification.

SECURITY, PRIVACY, AND OPERATIONS
- Secrets only in environment variables; commit .env.example placeholders, never .env.
- Validate all inputs and media; sanitize paths; constrain FFmpeg; use safe local paths.
- Bind local services to localhost for the initial local deployment unless explicitly approved otherwise.
- Do not log API keys, full private transcripts, or unnecessary sensitive content.
- Explain that online inference may send audio, transcript text, selected frames, OCR, or video-derived content to external providers. Verify data terms before use.
- Docker volumes are persistence, not backups. Document backup/restore for PostgreSQL and media.
- Track provider usage, latency, quota responses, retries, stage durations, frame counts, embedding throughput, retrieval relevance, Recall@k, timestamp accuracy, groundedness, abstention quality, and answer latency.

ENGINEERING WORKFLOW
1. Start by inspecting the existing repository, current branch, files, runtime versions, and existing implementation. Do not overwrite or regenerate working code blindly.
2. Read the architecture specification and this system prompt before every major phase. Treat the spec as authoritative.
3. Work in small, testable vertical slices. Do not create a huge empty scaffold.
4. Before coding a phase, state: goal, files to create/change, data flow, dependencies, acceptance criteria, risks, and tests.
5. Implement the smallest complete slice, run formatting/type checks/tests, fix failures, and report actual results.
6. Never claim a command or test passed unless you ran it and observed success.
7. Keep README, architecture docs, API docs, failure modes, environment example, and setup instructions synchronized with the implementation.
8. Prefer clear, boring, maintainable code over clever abstractions. Use type hints, structured logging, docstrings for non-obvious decisions, and explicit error handling.
9. Do not add dependencies without explaining why they are needed and confirming they are free/open-source and compatible with the locked architecture.
10. Do not skip tests to move faster. Add unit, integration, failure/retry, concurrency/deduplication, and end-to-end tests appropriate to each phase.
11. If requirements conflict, stop and ask. If an implementation detail is unspecified, propose the smallest architecture-compatible choice and label it as a proposal before adopting it.
12. Do not silently change the stack, processing order, storage model, modality scope, cost boundary, or retrieval algorithm.

DEFINITION OF DONE
A release is not complete until a supported video can be uploaded and validated; SHA-256 duplicate uploads reuse the canonical asset; durable jobs run and recover; audio and visual branches execute concurrently; quota pauses safely without paid fallback; timestamped transcript and visual evidence are indexed in pgvector and PostgreSQL FTS; hybrid retrieval uses RRF; answers are grounded and abstain appropriately; timestamps seek the player correctly; and tests plus measured evaluation results are documented.

COMMUNICATION STYLE
Be direct and precise. Explain important technical choices and trade-offs in beginner-understandable language, but do not drown the owner in theory. Do not dump an entire codebase in one response. Keep a concise progress ledger: completed, tested, pending, blockers, and next step. Ask the owner only when a decision materially changes architecture or product behaviour.
```

### 22.2 Master build prompt — implement the entire project end to end

Use this as the first task prompt after setting the system prompt. Give the coding agent access to the project repository and terminal. The agent must execute the work, not merely describe it.

```
We are building VideoRAG. Read and obey the complete VideoRAG Architecture & Engineering Specification and your VideoRAG system instructions. They are the source of truth.

PRIMARY TASK
Build the complete VideoRAG application end to end: frontend, FastAPI backend, PostgreSQL/pgvector schema, Redis/Celery processing, local media handling, online free-tier AI provider adapters, parallel audio/visual ingestion, incremental indexing, hybrid retrieval with RRF, grounded timestamped Q&A, Docker Compose, tests, documentation, and a working local developer experience.

Do not redesign the architecture. Do not turn this into a generic chatbot or a transcript-only RAG application. The product must retrieve both what was spoken and what appeared visually in the video. Do not use local AI inference. Do not use paid services or paid fallbacks.

EXECUTION RULES
- Inspect the repository before changing anything. Identify existing code worth preserving.
- Verify current official provider documentation before selecting exact online model IDs or writing provider-specific calls. Document the source, date checked, free-tier constraints, input limits, and unresolved uncertainty.
- If a provider cannot meet the ₹0 constraint or a required capability, implement the adapter interface and a clear configuration/status blocker; do not fake the integration or silently switch providers.
- Implement in the phases below, in order. Do not skip ahead when a phase's exit criteria are failing.
- For every phase: explain the intended changes briefly, implement them, run relevant tests, fix failures, and report exact results and remaining work.
- Keep the repository runnable at each meaningful milestone.
- Do not create placeholder buttons or fake functionality. Every visible control must work or be explicitly marked unavailable.
- Do not produce a giant untested code dump. Build, run, inspect, and verify.

PHASE 0 — REPOSITORY AUDIT AND FOUNDATION
1. Inspect files, git status, existing dependencies, environment, and installed tool versions.
2. Propose the final repository structure consistent with the architecture specification.
3. Create/complete Next.js + TypeScript frontend and FastAPI backend skeletons.
4. Add Docker Compose for PostgreSQL with pgvector and Redis; add backend, Celery worker, and frontend services as appropriate.
5. Add SQLAlchemy, Alembic, Pydantic settings, structured logging, health/readiness endpoints, and pytest setup.
6. Add .env.example, .gitignore, and clear local setup instructions.
7. Add basic frontend shell and backend health integration.
Acceptance: one documented command starts the local stack; frontend loads; API health endpoint responds; backend connects to PostgreSQL and Redis; migrations run; tests pass.

PHASE 1 — DATA MODEL AND MIGRATIONS
1. Implement video_assets, processing_jobs, processing_stages, and video_chunks based on the Notion schema.
2. Add only necessary conversation, message, answer_evidence, and provider_usage tables when their feature is implemented.
3. Add UUID keys, foreign keys, unique SHA-256 constraint, idempotency constraints, status validation, timestamps, and indexes.
4. Enable pgvector and PostgreSQL FTS through migrations.
5. Define explicit embedding model/version and dimension configuration.
Acceptance: migrations work from an empty database and upgrade a persisted database; schema and model tests pass.

PHASE 2 — UPLOAD, VALIDATION, AND SHA-256 DEDUPLICATION
1. Implement browser-side SHA-256 using a Web Worker where practical.
2. Add preflight API to check whether the hash already exists and return canonical asset/job status.
3. Implement safe streamed upload; independently compute server SHA-256 and compare it to the browser value.
4. Validate actual streams with FFprobe, file size, duration, codecs, and supported formats.
5. Sanitize filenames and write originals under application-generated asset directories on local SSD.
6. Handle concurrent identical uploads using the database unique constraint and recovery lookup.
7. Implement exact duplicate behaviour for READY, QUEUED/PROCESSING, PAUSED_QUOTA, FAILED, and absent assets.
Acceptance: exact same bytes under different filenames reuse the same asset; concurrent duplicate uploads do not create two canonical assets; mismatched client/server hashes are rejected; invalid media is rejected safely.

PHASE 3 — DURABLE JOB ORCHESTRATION
1. Configure Redis and Celery.
2. Implement state transitions and independent processing-stage records.
3. Implement task idempotency, checkpoints, bounded concurrency, retry/backoff/jitter, attempt limits, and provider Retry-After handling.
4. Implement PAUSED_QUOTA and safe resume.
5. Implement restart reconciliation from PostgreSQL; Redis must not be the durable source of truth.
6. Add job status/progress, retry, and cancellation APIs.
Acceptance: simulated worker termination and restart resume eligible work without duplicate chunks or corrupted state; transient failures retry; permanent errors stop; quota errors pause.

PHASE 4 — MEDIA PREPARATION AND AUDIO INGESTION
1. Implement FFprobe inspection and FFmpeg audio extraction.
2. Segment audio according to verified provider limits while preserving original timeline offsets.
3. Define TranscriptionProvider interface and a real online free-tier adapter only after verifying official documentation and terms.
4. Normalize transcript outputs, timestamp precision, overlapping segment boundaries, and error states.
5. Create semantically coherent timestamped transcript chunks.
6. Persist stage checkpoints and usage metrics.
Acceptance: a real supported test video produces transcript evidence with timestamps tied to the original video timeline; failures and segment retries are safe.

PHASE 5 — VISUAL INGESTION
1. Implement scene detection with PySceneDetect.
2. Implement configurable adaptive frame sampling, image resizing, and perceptual near-duplicate removal.
3. Implement OCR as a distinct component and preserve raw OCR output separately from vision descriptions.
4. Define VisionProvider interface and a real online free-tier vision adapter only after verifying image/video input support and official terms.
5. Store frame timestamp, path, OCR text, and visual description; convert these into visual evidence chunks.
6. Ensure visual processing runs concurrently with audio processing after common media preparation.
Acceptance: slide-heavy, code-editor, and diagram test videos yield timestamped visual evidence; sampled frames are not needlessly duplicated; a visual-stage failure does not destroy transcript results.

PHASE 6 — INCREMENTAL SHARED INDEXING
1. Define a common normalized evidence/chunk schema for transcript and visual modalities.
2. Implement EmbeddingProvider interface and verified online free-tier adapter.
3. Implement bounded, batched embedding requests and provider-wide rate limiting.
4. Persist vectors to pgvector and full-text representation to PostgreSQL FTS.
5. Make chunk creation/upsert idempotent and checkpointable.
6. Add consistency verification for chunk count, embeddings, dimensions, and FTS rows.
Acceptance: completed evidence from either branch can be indexed before the other branch finishes; re-running a stage does not duplicate chunks; READY is impossible until verification passes.

PHASE 7 — HYBRID RETRIEVAL AND EVIDENCE ASSEMBLY
1. Implement query embedding using the active chunk embedding model/version.
2. Run vector and PostgreSQL FTS retrieval concurrently, scoped by selected video by default.
3. Implement deterministic RRF fusion; do not compare raw vector and FTS scores as if they shared a scale.
4. Deduplicate overlapping evidence and retain modality, timestamp, source, and ranking metadata.
5. Build context assembly with explicit token/context limits.
6. Add retrieval tests for semantic paraphrases, exact technical terms, acronyms, identifiers, and visual-vs-audio queries.
Acceptance: both modalities can be retrieved; exact keywords and semantic queries work; RRF ordering is deterministic and tested.

PHASE 8 — GROUNDED ANSWER GENERATION
1. Define GenerationProvider interface and verified online free-tier adapter.
2. Create a versioned answer prompt enforcing evidence-only answers, modality separation, timestamp grounding, uncertainty, and abstention.
3. Return structured answer text and evidence references, including timestamp start/end and evidence type.
4. Ensure timestamps are clickable and map to the original media timeline.
5. Do not claim a timestamp more precise than the underlying evidence.
6. Do not re-run ingestion for a new question. Optional answer caching must include asset, normalized question, conversation context, prompt version, and model version.
Acceptance: questions are answered from retrieved evidence; unsupported questions trigger a clear insufficient-evidence response; visual and spoken claims are distinguished; timestamp clicks seek correctly.

PHASE 9 — COMPLETE USER INTERFACE
1. Build the video library, upload flow, duplicate detection, processing progress, asset detail, video player, chat, evidence display, and error/retry states.
2. Connect all screens to real backend APIs.
3. Make progress stage-specific and sourced from persisted job state; do not simulate completion.
4. Add loading, empty, validation, offline, quota-paused, failed, retry, and ready states.
5. Make the interface responsive and keyboard-accessible.
Acceptance: a user can upload a real video, watch processing progress, open it, ask questions, inspect transcript/visual evidence, and seek the player from citations.

PHASE 10 — EVALUATION, RELIABILITY, AND HARDENING
1. Build evaluation sets for at least: slide-heavy lecture, programming tutorial, and technical presentation with diagrams/animations.
2. Measure Recall@k, evidence relevance, timestamp accuracy, visual evidence coverage, answer faithfulness, abstention quality, ingestion duration, retrieval latency, and answer latency.
3. Add tests for duplicate-upload races, task retries, restart recovery, quota pauses, partial branch failures, malformed media, path traversal, provider errors, and invalid timestamps.
4. Add structured logs and operational metrics without logging secrets or unnecessary transcript contents.
5. Document backup/restore for local media and PostgreSQL.
6. Review provider privacy/data-use terms and clearly disclose external processing in the UI/docs.
7. Document known limitations and measured performance; do not present planning targets as measured results.
Acceptance: test suite passes; recovery and failure cases are demonstrated; measured evaluation report exists; setup and troubleshooting documentation is complete.

PHASE 11 — FINAL RELEASE AND HANDOFF
1. Run the full backend test suite, frontend lint/type checks, production build, migrations from clean state, and Playwright critical user journeys.
2. Run the application from a clean checkout using documented commands.
3. Fix all release-blocking defects.
4. Update README, architecture, API, database, pipeline, retrieval, evaluation, failure-modes, testing, and interview-preparation docs.
5. Provide a final report containing: architecture implemented, working user journeys, verified provider/model IDs and dated free-tier evidence, exact commands to run, environment variables required, test results, measured metrics, known limitations, security/privacy notes, and next steps.
6. Do not claim completion if any core acceptance criterion is missing. Label incomplete items clearly.

START NOW
First audit the repository and environment. Then report the current state and the Phase 0 implementation plan. After that, implement Phase 0 and continue sequentially. Do not stop after writing a plan unless a genuine external blocker or architecture decision requires owner input.
```

### 22.3 How to use these prompts

1. Put **22.1 System prompt** in the coding agent's persistent project/system instructions.
2. Open the VideoRAG repository root in the coding agent and provide terminal access.
3. Send **22.2 Master build prompt** as the first implementation task.
4. Keep this Notion specification available in the agent's project context. If it cannot read Notion, export or copy the architecture specification into the repository as `docs/architecture.md` and tell it that file is the source of truth.
5. Require phase-by-phase evidence: changed files, commands run, test output, screenshots where relevant, and unresolved blockers.
6. Do not accept a claimed “complete project” based only on generated code. Verify the acceptance criteria against a real video and the evaluation set.

### 22.4 Architecture guardrails and explicit non-decisions

The prompts intentionally preserve these boundaries:

- **Online-only AI, ₹0:** provider IDs and current free-tier limits remain implementation-time verification tasks, not assumed facts.
- **Local infrastructure:** local Docker Compose, PostgreSQL/pgvector, Redis/Celery, local SSD, FFmpeg and PySceneDetect.
- **Modular monolith:** no premature microservices.
- **Two evidence branches:** timestamped speech and timestamped visual evidence.
- **Incremental shared indexing:** index completed evidence while the other branch continues.
- **Hybrid retrieval:** pgvector + PostgreSQL FTS + RRF; no baseline reranker.
- **Exact-file deduplication:** browser preflight plus authoritative server SHA-256 and database uniqueness.
- **Durable orchestration:** PostgreSQL state/checkpoints; Redis is not the source of truth.
- **No false claims:** performance targets are hypotheses until benchmarked; provider support and quotas must be checked against current official documentation.
- **No silent scope expansion:** YouTube access, larger upload caps, partial search, HNSW, reranking, and additional providers remain subject to explicit validation or approval.