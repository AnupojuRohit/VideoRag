# VideoRAG

Local-first, multimodal video knowledge retrieval. The application is built as a
modular monolith: Next.js + TypeScript, FastAPI, PostgreSQL/pgvector, Redis, and
Celery. Phase 1 establishes the durable data model; Phase 2 validates and stores
local MP4 uploads, with browser-side SHA-256 preflight and duplicate reuse.
Phases 3 and 4 add PostgreSQL-backed job recovery, worker progress controls,
bounded FFmpeg audio preparation, and an opt-in free-tier transcription adapter.
Phase 5 adds independent, concurrent visual processing with PySceneDetect,
bounded frame sampling, local Tesseract OCR, and the verified Gemini vision
adapter. Shared indexing, retrieval, and Q&A are not enabled yet.

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

The local stack runs without an API key. Live AI processing requires
`GEMINI_API_KEY` from a Gemini API project with no active billing account,
`GEMINI_UNPAID_API_ACKNOWLEDGED=true`, and
`GEMINI_API_TERMS_ACKNOWLEDGED=true`. The operator must be at least 18 and use
Gemini API for a professional or business purpose; it is not permitted for
consumer-use apps or apps directed to minors. Users must also consent per asset
in the upload UI. Unpaid-tier content may be used to improve Google products
and human-reviewed; do not send sensitive, confidential, or personal content.
Free quotas are account-specific and are not guaranteed. Never add a paid
fallback.

### Environment variables

No environment variables are required to start the local Compose stack with its
development defaults. If you set `POSTGRES_PASSWORD`, also set the matching
password in `DATABASE_URL`. The other infrastructure settings in `.env.example`
have local development defaults. Do not create or commit API keys for tests.

Required only for live Phase 4/5 external inference: `GEMINI_API_KEY`,
`GEMINI_UNPAID_API_ACKNOWLEDGED=true`, and
`GEMINI_API_TERMS_ACKNOWLEDGED=true`, plus per-asset consent in the UI.
Embedding model settings are for Phase 6 and must remain unset until that
provider configuration is enabled. No `.env` file is needed to run offline
unit tests or work on local media processing.

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
both branches checkpoint artifacts independently. Their local evidence is not
yet inserted into the shared pgvector/FTS index. The asset remains
`PROCESSING` and must not appear `READY` or searchable until indexing and
consistency verification are complete.

The current vision adapter uses Gemini 3.8 Flash, inline JPEG image input, and
stateless Interactions requests. Its model support and free standard tier are
documented by [Google](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash);
Google's [unpaid-service terms](https://ai.google.dev/gemini-api/terms) restrict
use and data handling. No API call is made by unit tests.

See [the API contract](./docs/api.md) and [database notes](./docs/database.md).