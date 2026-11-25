# VideoRAG

Local-first, multimodal video knowledge retrieval. The application is built as a
modular monolith: Next.js + TypeScript, FastAPI, PostgreSQL/pgvector, Redis, and
Celery. Phase 1 establishes the durable data model; Phase 2 validates and stores
local MP4 uploads, with browser-side SHA-256 preflight and duplicate reuse.
Phases 3 and 4 add PostgreSQL-backed job recovery, worker progress controls,
bounded FFmpeg audio preparation, and an opt-in free-tier transcription adapter.
Phase 5 adds independent, concurrent visual processing with PySceneDetect,
bounded frame sampling, local Tesseract OCR, and the verified Gemini vision
adapter. Phase 6 adds incremental shared embedding, pgvector/FTS indexing, and
verification before an asset can become READY. Phase 7 adds video-scoped hybrid
retrieval with reciprocal-rank fusion. Phase 8 adds stateless grounded answers
with validated citations and abstention. Phase 9 adds the library, player,
timestamp seeking, processing progress, and video Q&A UI.

## Requirements

- Docker Desktop with the Compose plugin and its Linux container engine running.
- A local checkout with Node.js 24+ and Python 3.11+ for running the frontend and
  backend directly during development.
- FFmpeg/FFprobe validate uploaded media in the local API container.

## Start the local stack

1. The default local Compose stack runs without `.env`. To customize local
   credentials, copy `.env.example` to `.env` and use the same local-only
   password in `POSTGRES_PASSWORD` and `DATABASE_URL`.
2. From the repository root, run:

   ```powershell
   docker compose -f infra/docker-compose.yml up --build
   ```

3. Open <http://localhost:3000>. The API liveness endpoint is
   <http://localhost:8000/health>; readiness is <http://localhost:8000/ready>.
   Alembic migrations run before the API, worker, and Celery Beat scheduler start.

The local stack runs without an API key. Live AI processing requires `GEMINI_API_KEY` from a Gemini API project with no
active billing account and all three acknowledgements:
`GEMINI_UNPAID_API_ACKNOWLEDGED=true`,
`GEMINI_API_TERMS_ACKNOWLEDGED=true`, and
`GEMINI_REGION_ELIGIBLE_ACKNOWLEDGED=true`. The operator must be at least 18
and use Gemini API for a professional or business purpose; consumer-use apps
and apps directed to minors are not permitted. Unpaid use is not available for
API clients made available to users in the EEA, UK, or Switzerland; this
project prohibits paid access. Users must also consent per asset in the UI.
Unpaid-tier content may be used to improve Google products and human-reviewed;
do not send sensitive, confidential, or personal content. Free quotas are
account-specific and are not guaranteed. Never add a paid fallback.

### Environment variables

No environment variables are required to start the local Compose stack with its
development defaults. If you set `POSTGRES_PASSWORD`, also set the matching
password in `DATABASE_URL`. The other infrastructure settings in `.env.example`
have local development defaults. Do not create or commit API keys for tests.

Required only for live Phase 4–8 external inference: `GEMINI_API_KEY`,
`GEMINI_UNPAID_API_ACKNOWLEDGED=true`,
`GEMINI_API_TERMS_ACKNOWLEDGED=true`, and
`GEMINI_REGION_ELIGIBLE_ACKNOWLEDGED=true`, plus per-asset consent in the UI.
The region acknowledgement is an operator assertion, not automatic
geolocation. Do not set it unless the current Google terms permit unpaid use in
your deployment context. The answer adapter uses the documented
`gemini-3.8-flash` model through stateless Interactions requests; generation
timeout and output-token bounds have local defaults in `.env.example`.
The verified embedding defaults are `gemini-embedding-001`, the local index
version label `stable`, and 768 dimensions. The provider adapter uses small
batches and a configurable local request interval; those settings are not claims
about account quota. No `.env` file or API key is needed for unit tests.

Stop the stack with `Ctrl+C`, then run
`docker compose -f infra/docker-compose.yml down`. Add `-v` only when you
intentionally want to delete the local PostgreSQL volume. Media is stored under
`data/`; Docker volumes and local files are not backups.

## Run backend checks locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend/requirements.txt
python -m pytest backend/tests
```

Apply migrations against the local Compose database with:

```powershell
alembic -c backend/alembic.ini upgrade head
```

The `.env` file is loaded by the application settings. Do not commit credentials
or media files.

## Current milestone

Phase 1 adds the core relational schema and migrations. Phase 2 adds hash
preflight and validated local uploads. Phase 3 adds durable job dispatch,
reconciliation, leases, status/progress, retry, quota pause, and cancellation.
Phase 4 prepares audio and produces timestamped local transcript artifacts using
the online Gemini transcription endpoint. Phase 5 runs scene detection, bounded
frame extraction, OCR, and online visual description concurrently with audio;
both branches checkpoint artifacts independently. Phase 6 incrementally embeds completed transcript and visual evidence, upserts
it into pgvector and PostgreSQL FTS, removes obsolete per-modality chunks, and
verifies the active model version, vector dimensions, and searchable text
before marking the asset `READY`. Phase 7 embeds each question with that same
index version, runs video-scoped vector and FTS search concurrently, and fuses
the ranked evidence with deterministic RRF. Phase 8 uses the verified
`gemini-3.8-flash` generation adapter with `store: false`, structured JSON,
evidence-ID validation, modality-aware citations, and a fail-closed abstention
path. Phase 9 provides paginated library data, video details, range-based media
streaming, clickable original-timeline citations, stage progress, and the
question/answer interface. Quota exhaustion pauses ingestion or returns a
structured retryable query error; there is no paid fallback.

The current vision and generation adapters use Gemini 3.8 Flash and stateless
Interactions requests. Its text input, structured-output support, token limits,
and free Standard tier are documented by
[Google](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash);
Google's [unpaid-service terms](https://ai.google.dev/gemini-api/terms) restrict
use, region eligibility, and data handling. The embedding adapter uses the documented
[`gemini-embedding-001`](https://ai.google.dev/gemini-api/docs/embeddings)
model; quota availability remains account-specific, and unpaid-tier data terms
apply. No API call is made by unit tests.

Phase 10 evaluation is documented but no live quality or performance results
are claimed until a compliant provider configuration and consented evaluation
set are available. Phase 11 hardening includes the local backup/restore runbook
and Playwright browser checks. The backend integration suite, frontend checks,
Playwright journeys, migration head, and local API/UI readiness have been
verified; an isolated restore drill and live-provider evaluation remain
pending.

See the [API contract](./docs/api.md), [database notes](./docs/database.md),
[testing instructions](./docs/testing.md), [evaluation status](./docs/evaluation.md),
and [local operations runbook](./docs/operations.md).