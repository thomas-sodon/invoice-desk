# ADR-0003: Claude model routing and API usage

- **Status:** accepted
- **Date:** 2026-10-24 (API behaviour as checked on this date; it drifts, so re-check it)
- **Spec:** [DESIGN §5.1-5.2](../../DESIGN-InvoiceDesk.md#5-agent-and-llm-design), §7, CLAUDE.md "Claude API rules"

## Context

Extraction cost and accuracy depend on which model reads which document. Haiku 5.5 is about 25x
cheaper per output token than Sonnet 5.5 and 40x cheaper than Opus 5.5 (§7.2), but the hard
tail (handwriting, crumpled photos, arithmetic that doesn't add up) needs a stronger reader. The
5.5 models also changed several API behaviours that a naive client gets wrong.

## Decision

### Routing (cheapest model that passes evals)

| Role (`ModelRole`) | Model | Settings (`config/models.py:ROLE_SETTINGS`) |
|---|---|---|
| `INJECTION_CLASSIFIER` | `claude-haiku-5-5` | effort `low`, thinking disabled |
| `EXTRACT_T1` (every document) | `claude-haiku-5-5` | effort `low`, thinking disabled |
| `EXTRACT_T2` (escalation) | `claude-sonnet-5-5` | effort `low`, adaptive thinking, `fallbacks="default"` |
| `ADJUDICATE_T3` | `claude-opus-5-5` | effort `medium` set explicitly, thinking omitted, `fallbacks="default"` |
| `WORDING` (variance sentence, follow-up draft) | `claude-haiku-5-5` | effort `low`, thinking disabled |
| `ROOT_CAUSE` (offline eval labelling) | `claude-opus-5-5` | effort `high`, Batch API |

Escalation is decided in code (`invoicedesk/extract/extractor.py`): Sonnet runs when a required
field is below `tau_field`, arithmetic fails, handwriting is present or Haiku refuses. Opus runs
when Haiku and Sonnet disagree on a required field or the merged minimum is below `tau_hi`.
Both thresholds live in `tenants/maple_row/rules.yaml` and are fitted on the dev split only.

**Model IDs live only in `invoicedesk/config/models.py`.** Call sites name a `ModelRole`.

### API specifics

1. **No sampling parameters.** `temperature`, `top_p` and `top_k` return a 400 on these models.
   `RoleSettings.request_kwargs()` never emits them. Because outputs are non-deterministic,
   every eval config runs n=3 with bootstrap CIs ([eval methodology](../eval-methodology.md)).
2. **Structured output through `messages.parse()`** with the Pydantic `InvoiceExtraction` model
   (`output_config.format`). There is no forced `tool_choice` (a 400 on Opus 5.5 and Sonnet 5.5),
   no assistant prefill (also a 400), and no tools at all: the extractor is tool-less by design
   ([ADR-0005](0005-guardrails.md)).
3. **Thinking.** Opus 5.5 cannot disable thinking, so it is controlled by effort and the param is
   omitted. Sonnet 5.5 rejects `{type: "disabled"}`, so tier 2 uses adaptive thinking. Haiku 5.5
   accepts `disabled` at effort `high` or below. Effort is always set explicitly.
4. **Refusals.** `stop_reason == "refusal"` is checked before content is read. Haiku has no
   server-side fallback, so a Haiku refusal escalates to Sonnet. Sonnet and Opus requests go
   through `client.beta.messages` with `fallbacks="default"` and the beta
   `server-side-fallback-2026-07-01`. A refusal is recorded on the `extractions` row (`refused`).
5. **Files API (GA).** Each page image is uploaded once with `client.files.upload` and referenced
   by `file_id` in every tier (`extract/files_api.py`), so escalation does not resend base64.
6. **Prompt caching.** Caches are per model and the minimum cacheable prefix is 512 tokens. Each
   tier keeps a frozen prefix (system prompt, schema notes, two few-shot examples) with a
   `cache_control` breakpoint; the image, OCR word list and email body come after it. No
   timestamps or IDs go in the system prompt. Caching is verified from
   `usage.cache_read_input_tokens`, and config D' (no caching) isolates the saving.
7. **Images.** Pages are pre-resized to a 1,600 px long edge (the API maximum is 2,576 px) so the
   model's coordinates map exactly onto the processed image ([ADR-0004](0004-ocr-anchoring.md)).
8. **Errors.** SDK exceptions map to `LLMCallError` kinds (`rate_limit`, `status`, `connection`,
   `timeout`, `validation`) rather than one broad catch. There is one semaphore per tier, and
   worker concurrency is 8.
9. **Cost and latency on every call.** Each call writes an `extractions` row with token counts,
   `cost_usd` from `usage` x `PRICES` (a `Decimal`, never a float) and `latency_ms`, and emits an
   OpenTelemetry GenAI span (`invoicedesk/telemetry/`) that is masked before export.

`invoicedesk/llm/anthropic_client.py` is the only module allowed to import `anthropic` (ruff
`TID251`).

## Consequences

- The budget in §7.3 (about $0.011 per routed document) assumes 30% Sonnet and 5% Opus
  escalation. Those are assumptions. The only measured escalation rates so far come from the
  **offline oracle run**, where 56% of extracted documents reached Sonnet and 54% reached Opus
  ([evals/reports/2026-10-24-dev_adversarial-D-N-offline.md](../../evals/reports/2026-10-24-dev_adversarial-D-N-offline.md)).
  That run's confidences come from the oracle, not from Claude, so it shows that thresholds and
  the calibrator must be fitted on live dev runs before routing cost can be judged.
- No live call has been made from this repo yet. The request body for every role is asserted
  against the official SDK through a mocked transport, and prompt caching has not been observed
  live (see [GAPS.md](../GAPS.md)).
- When the API changes, update CLAUDE.md, `config/models.py` and this ADR together.
