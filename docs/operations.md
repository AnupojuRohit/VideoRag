# Local operations

The Compose stack binds the API, frontend, PostgreSQL, and Redis to localhost.
PostgreSQL and local media persist across container restarts, but Docker
volumes are not backups.

## Consistent local backup

Stop API and worker writers before backing up so database metadata and original
media come from the same point in time:

```powershell
docker compose -f infra\docker-compose.yml stop api worker scheduler
New-Item -ItemType Directory -Force data\backups | Out-Null
docker compose -f infra\docker-compose.yml exec -T postgres sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /tmp/videorag.dump'
docker compose -f infra\docker-compose.yml cp postgres:/tmp/videorag.dump data\backups\videorag.dump
Compress-Archive -Path data\originals -DestinationPath data\backups\originals.zip -Force
docker compose -f infra\docker-compose.yml start scheduler worker api
```

Keep the database dump and `originals.zip` together and copy them to a separate
local disk. The archive contains user media; keep it private. Temporary work
files can be regenerated from checkpoints and do not replace the original
media backup.

## Restore verification

Restore first into a separate empty database rather than overwriting the active
database:

```powershell
docker compose -f infra\docker-compose.yml cp data\backups\videorag.dump postgres:/tmp/videorag.dump
docker compose -f infra\docker-compose.yml exec -T postgres createdb -U videorag videorag_restore
docker compose -f infra\docker-compose.yml exec -T postgres pg_restore -U videorag -d videorag_restore --no-owner --exit-on-error /tmp/videorag.dump
New-Item -ItemType Directory -Force data\restore | Out-Null
Expand-Archive -Path data\backups\originals.zip -DestinationPath data\restore -Force
```

Verify the restored media under `data\restore\originals` and the database
contents before any cutover. This procedure deliberately restores the database
to a separate name and media to a separate directory; it does not overwrite the
active database or media tree. The example `createdb` command uses the Compose
development user; substitute the configured local database user when it
differs. A production cutover must be planned separately so the app's database
and stored media paths point to the matching restored copies.

## Live provider gate

The application starts without an API key. Live AI calls require the
acknowledgements documented in `.env.example`, a project with no active
Gemini API billing, and current per-asset consent. The region acknowledgement
is manual, not geolocation. Do not enable unpaid Gemini for API clients
available to users in the EEA, UK, or Switzerland; this project has no paid
fallback. Review the current [Gemini API terms](https://ai.google.dev/gemini-api/terms)
before live use.
