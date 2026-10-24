# ADR-0006: Pipeline step markers, validation lock, checkpoint contents and hold-scoped release

- **Status:** accepted
- **Date:** 2026-10-24
- **Spec:** [DESIGN §4.2, §4.3, §5.5](../../DESIGN-InvoiceDesk.md) (state machine, R6, two-person approval)

## Context

The LangGraph pipeline ([ADR-0002](0002-langgraph.md)) checkpoints after each node, but a worker
can die after a node commits its database writes and before LangGraph saves the checkpoint. A
naive re-run would then write the extraction, fields, rule results and audit rows twice and pay
for the model calls twice. Two copies of one invoice validated at the same moment could also miss
each other as duplicates (R4 reads prior documents). And release votes on a Flagged document
needed a precise meaning once a document can be held, released and held again.

## Decision

1. **Step markers.** Each node does its database writes, its status change and a
   `pipeline_steps` row (migration 0300) in one transaction under the document row lock. A node
   that re-runs finds its marker, returns the stored output and writes nothing new.
2. **Per-tenant validation lock.** The validate node takes a Postgres advisory lock per tenant, so
   validations run one at a time and R4 always sees the other copy. (The concurrency test passes
   with or without the lock, so the race is closed by design, not proven by a test.)
3. **Checkpoint contents.** The full model extraction, including the raw remit-to text, stays in
   the LangGraph checkpoint and in `pipeline_steps.output_json`, because re-validation after a
   correction or approval must fingerprint the printed remit-to details for R6. These are internal
   tables like the stored original file. Everything that leaves the pipeline (the `extractions`
   table, `fields`, audit rows, events, traces, eval examples, API responses) is masked to the
   last 4 (invariant 9).
4. **Release votes are scoped to the hold.** Flagged -> ReadyToPay counts only votes cast during
   the current hold (audit-id ordering); an R6 hold counts only `kind=bank_change` votes, each
   with a callback attestation; any reject vote blocks release; only human `user` actors with an
   approver or owner role vote, never the uploader, never `mcp`, `system` or `llm` actors.
5. **A quarantined document is never released.** It was never extracted, so there is nothing
   validated to post. It can only be rejected and re-uploaded clean. The §4.3 Flagged ->
   ReadyToPay edge still exists for documents that were read.

## Consequences

- Crash-and-resume is tested (kill during a node, and after commit before the checkpoint) with no
  duplicate rows or model calls.
- Unmasked remit-to text exists in two internal tables; access to them must stay limited to the
  pipeline's own database role.
- Two jobs for the same document (a `process_document` and a `resume_document`) could still run
  on two worker threads; the step markers keep the database consistent, but LangGraph thread
  state is not serialized per document (recorded in GAPS).
