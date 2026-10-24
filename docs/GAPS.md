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

## Rules engine R1-R10 (2026-10-24)

- **No before/after smoke eval for the rules.** CLAUDE.md asks for one on any rules change. With no
  API key there is no extraction to feed the rules, so only unit and property tests ran (136
  tests, 100% branch coverage of `invoicedesk.rules`). *Close by:* running the smoke set once the
  extractor and an API key exist, and recording the result for R1-R10.
- **Homoglyph table is curated, not full Unicode TR39.** R1's `skeleton()` folds the common
  Cyrillic and Greek lookalikes plus `0/o`, `1/l/i`, `rn/m`, `vv/w` and `cl/d`, and flags any name
  that mixes Latin with Cyrillic or Greek. It does not ship the full `confusables.txt`. *Close
  by:* vendoring TR39 data if the adversarial set shows a miss.
- **Demo-data dependency (R3, R6).** R3 compares the **pre-tax** amount with the approved
  estimate and its tolerance is inclusive, so "$340 over" is held only when the estimate is
  below $3,400. R6 flags any remit-to details on an invoice whose vendor does not resolve. The
  demo seed must use an estimate below $3,400 for the Unit 12B beat, and must keep remit-to
  details off the new-vendor invoice if that card is meant to land in Needs review rather than
  Flagged.

## Preprocess (2026-10-24)

- **No real phone photos or scans.** Preprocess tests generate every fixture (PyMuPDF PDFs,
  Pillow PNG/JPEG with EXIF orientation and GPS tags, a pillow-heif HEIC round trip). The
  "photo" case is simulated: blur, dimming, Gaussian sensor noise and JPEG q88. Measured on these
  synthetic renders: the CLAHE pass (clip 40, 8x8 tiles) recovers grey-on-white text down to 1%
  contrast on clean renders and 8% contrast under the simulated photo noise; normal OCR misses
  both. Real phone photos (uneven lighting, perspective, moiré) are untested, and there is no
  deskew/dewarp step. *Close by:* adding real photo samples to `data/` and measuring the
  faint-text detector's recall on them.
- **OCR word boxes come from RapidOCR line boxes**, split into words by character-width
  proportions snapped to ink gaps (horizontal lines), or by proportions along the baseline
  (slanted lines). On synthetic renders, words land within 8 px of the true positions (the tests
  assert this). RapidOCR sometimes drops spaces; word-sized ink gaps between letters restore
  them, but a missing space between two digits is never re-inserted. docTR (native word boxes)
  is behind the `doctr` extra and untested here (see Foundation). *Close by:* installing the
  extra and comparing box IoU on the gold set.
- **Azure DI engine is not exercised.** `AzureDIEngine` is written against
  `azure-ai-documentintelligence` (`prebuilt-read`) and requires `INVOICEDESK_MODE=live` plus
  `AZURE_DI_ENDPOINT` and `AZURE_DI_KEY`. It is never called in tests, and those env vars are not
  yet in `.env.example`.
- **Latency was measured on this 4-CPU container only.** One native PDF page (render, OCR,
  CLAHE OCR, text layer, pHash) takes about 1.2 s warm and about 2.5 s cold, including model
  load. A test asserts under 3 s warm.

## Extraction, calibration, telemetry (2026-10-24)

- **No live Claude call has been made.** There is no `ANTHROPIC_API_KEY` here. The request
  bodies for every role (model, `output_config.effort`, thinking, `fallbacks`, beta header,
  cache breakpoints, Files API image blocks) are asserted against the official SDK (anthropic
  2.3.0) through a mocked transport, not against the real API. The SDK uses `httpx2`, so the
  mocks use `httpx2.MockTransport`; `respx` (which patches `httpx`) cannot intercept it. The
  only live test, `test_live_haiku_smoke`, is marked `live` and skips without a key. *Close
  by:* one `pytest -m live` run with a key, plus a smoke eval.
- **Prompt caching is not verified.** The frozen prefix (system prompt, schema notes, two
  few-shot examples) is about 3,000 estimated tokens, with one breakpoint at its end. Whether
  `usage.cache_read_input_tokens` is non-zero on the second call per tier has not been
  observed. *Close by:* the first recorded live run.
- **No fitted calibrator yet.** `load_calibrator()` returns the monotone `heuristic-v1`
  combiner until the evals component fits one on the dev split (`fit_calibrator(...,
  split="dev")`, `choose_thresholds(...)`) and points `INVOICEDESK_CALIBRATOR` at the JSON
  artifact. `tau_field` 0.90 and `tau_hi` 0.95 in `rules.yaml` are placeholders, not tuned
  values. The ECE ≤ 0.05 claim is shown only on synthetic signals in unit tests.
- **The offline extractor is a heuristic, not a model.** Offline, `invoicedesk extract` with
  no recording falls back to `extract.offline`, a regex/label reader over the OCR words. It
  exists so the tiered path runs end to end without network access. It must never be used for
  eval numbers. FakeLLM's token counts (and so the offline costs) are estimates.
- **Replay recordings can't match live uploads yet.** `RecordingLLM` keys each recording by
  `LLMRequest.replay_key()`, which includes the Files API `file_id`. A live upload returns
  `file_...` ids, while `ReplayLLM.upload_file` returns `file_replay_<sha>`. So a recorded
  extraction request will miss on replay. *Close by:* a foundation fix that normalizes file
  ids in the replay key (see the extraction component's needs_outside_ownership).
- **Langfuse export is untested against a real Langfuse.** `setup_tracing` builds an
  OTLP/HTTP exporter to `<LANGFUSE_HOST>/api/public/otel/v1/traces` with basic auth, only when
  `INVOICEDESK_MODE=live`. Tests check the configuration and use in-memory exporters.
