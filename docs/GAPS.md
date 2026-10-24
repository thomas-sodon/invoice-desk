# Gaps: what this build environment could not do

Each entry says what the spec asks for, what was done instead, and what closes the gap. Append;
don't delete entries. Mark them **closed** when resolved.

## Foundation (2026-10-24)

- **No Anthropic API key.** Every Claude call sits behind `invoicedesk.contracts.llm.LLMClient`.
  Tests and the offline demo use `FakeLLM`, `ReplayLLM` and the gold-backed oracle. No accuracy,
  cost or latency number has been measured against real models. *Close by:* setting
  `ANTHROPIC_API_KEY`, `INVOICEDESK_MODE=live`, running `make eval CONFIG=D` (asks before the full
  matrix), and recording replays with `RecordingLLM`.
- **docTR not installed.** `python-doctr[torch]` pulls PyTorch (several GB). The default OCR
  engine is RapidOCR (onnxruntime, bundled models, line-level boxes split into words). docTR stays
  available as `uv sync --extra doctr` behind the same `OcrEngine` Protocol. *Close by:*
  installing the extra and running the OCR comparison in evals.
- **No Supabase project.** Postgres runs locally (docker compose, or a throwaway cluster in
  tests), Storage is `LocalStorage` with HMAC-signed URLs, and Realtime is SSE over Postgres
  `LISTEN/NOTIFY`. The Supabase Storage, Auth and Realtime adapters are written against their
  documented APIs but are not exercised end to end.
- **No QuickBooks credentials.** Posting uses `FakeAccounting`, which has the same idempotency
  semantics. The QBO adapter is unit-tested against recorded HTTP shapes only.
- **No running Docker daemon.** `docker-compose.yml` is validated with `docker compose config`,
  but the stack was not started here.
