# VideoRAG

Local-first, multimodal video knowledge retrieval. The application is built as a
modular monolith: Next.js + TypeScript, FastAPI, PostgreSQL/pgvector, Redis, and
Celery. Phase 1 establishes the durable data model; Phase 2 validates and stores
local MP4 uploads, with browser-side SHA-256 preflight and duplicate reuse.
Phases 3 and 4 add PostgreSQL-backed job recovery, worker progress controls,
bounded FFmpeg audio preparation, and an opt-in free-tier transcription adapter.
Visual processing, shared indexing, and Q&A are not enabled yet.

## Requirements

- Docker Desktop with the Compose plugin and its Linux container engine running.
- A local checkout with Node.js 24+ and Python 3.11+ for running the frontend and
  backend directly during development.
- FFmpeg/FFprobe validate uploaded media in the local API container.

## Start the local stack

1. Copy `.env.example` to `.env`. Replace the PostgreSQL password in both
   `POSTGRES_PASSWORD` and `DATABASE_URL` with the same local-only value.
2. From the repository root, run:

   ```powershell
   docker compose -f infra/docker-compose.yml up --build
   ```

3. Open <http://localhost:3000>. The API liveness endpoint is
   <http://localhost:8000/health>; readiness is <http://localhost:8000/ready>.
   Alembic migrations run before the API, worker, and Celery Beat scheduler start.

Transcription is disabled until you configure `GEMINI_API_KEY` from a Gemini
project with no active billing account and set
`GEMINI_UNPAID_API_ACKNOWLEDGED=true` after reviewing the data terms. Users must
also consent per asset in the upload UI. Google's unpaid tier may use submitted
content to improve its products and may be human-reviewed; do not send
sensitive, confidential, or personal content. Never add a paid fallback.

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
the verified online Gemini transcription endpoint. The asset remains
`PROCESSING` until visual processing and shared indexing are implemented; it
must not appear `READY` or searchable yet. Real provider calls require local
configuration and an explicit per-asset consent.

See [the API contract](./docs/api.md) and [database notes](./docs/database.md).