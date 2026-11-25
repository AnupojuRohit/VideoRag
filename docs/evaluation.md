# Evaluation status

## Current evidence

No live-provider quality, latency, throughput, or quota measurements have been
collected. This checkout has no `.env` or Gemini API key, and the operator has
not supplied account- and region-specific eligibility confirmation. No user
content has been sent to an external provider during these checks. Mocked
provider tests are software tests, not retrieval, transcription, or answer
quality results.

The automated suite exercises upload validation and exact-byte deduplication,
concurrent duplicate handling, durable jobs, branch-failure isolation, shared
index verification, video-scoped vector/FTS/RRF retrieval, generation parsing
and citations, quota classification, consent gates, and browser behavior for
answer rendering and timestamp seeking.

## Required live evaluation before release

Use a consented, locally stored evaluation set with at least:

1. A slide-heavy lecture.
2. A programming tutorial with code or UI changes.
3. A technical presentation with diagrams or animations.

For each video, create human-reviewed questions with relevant evidence
references, acceptable original-timeline intervals, modality labels, and an
answerable/unanswerable label. Avoid sensitive, confidential, or personal
content when using unpaid provider access.

Report measurements per video and overall:

| Metric | Measurement |
| --- | --- |
| Retrieval Recall@k | Relevant evidence references found among the top-k retrieved chunks |
| Evidence relevance | Human judgment that cited chunks support the answer's claims |
| Timestamp accuracy | Absolute error between citation timestamps and reviewed intervals |
| Visual evidence coverage | Relevant visual questions with at least one supporting visual/OCR citation |
| Groundedness | Human judgment that factual answer claims are supported by cited evidence |
| Abstention quality | Appropriate abstentions on unanswerable questions and answer rate on answerable ones |
| Ingestion duration | End-to-end and per-stage wall time for each media class |
| Retrieval/answer latency | Observed p50/p95 latency, excluding mocked tests |
| Provider behavior | Returned token usage, latency, quota responses, and provider errors |

Record model IDs, index version, sampling policy, configuration, date, and
observed quota behavior with every run. Free quotas are account-specific; do
not infer or promise a quota from successful requests.

## Local checks

Run the backend tests with PostgreSQL integration enabled:

```powershell
docker compose -f infra\docker-compose.yml run --rm -e RUN_DB_INTEGRATION_TESTS=1 api pytest -q
```

Run frontend checks from the frontend directory:

```powershell
Set-Location frontend
npm run lint
npm run typecheck
npm run build
npm run test:e2e
```

Playwright tests currently mock backend responses. They verify UI behavior,
not provider quality or real video ingestion.