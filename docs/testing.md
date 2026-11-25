# Testing

## Backend

The backend tests run in the API container so they use the same Python
dependencies and media tools as the local deployment. The opt-in integration
tests require the Compose PostgreSQL service:

```powershell
docker compose -f infra\docker-compose.yml run --rm -e RUN_DB_INTEGRATION_TESTS=1 api pytest -q
```

Unit tests do not require a Gemini key and must not make live provider calls.
Integration tests use controlled fixtures/fake providers where possible.

## Frontend

From `frontend\`, run:

```powershell
npm run lint
npm run typecheck
npm run build
npm run test:e2e
```

Playwright starts a local Next.js development server when needed. Its current
browser tests mock the API; they do not represent provider or ingestion-quality
measurements.

## Compose and migrations

Validate the local Compose configuration with:

```powershell
docker compose -f infra\docker-compose.yml config --quiet
```

The `migrate` service applies Alembic migrations before the API and worker
start. To inspect the current database revision, use the Compose database
credentials and the backend container rather than assuming host `.env`
credentials match.

## Reporting

Report actual command outcomes and failures. Do not describe mocked API
responses as live integration tests or claim retrieval, accuracy, quota, or
latency results without a measured evaluation run. See
[evaluation.md](./evaluation.md) for the required live metrics and current
blockers.