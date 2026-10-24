# ADR-0002: LangGraph with a Postgres checkpointer for the document pipeline

- **Status:** accepted
- **Date:** 2026-10-24
- **Spec:** [DESIGN §4.1, §4.3, §4.4](../../DESIGN-InvoiceDesk.md#4-system-architecture-and-tech-stack), §8 day 9

## Context

Each document runs through the same steps: preprocess, screen, extract and score, validate,
optionally wait for a human, then post. The pipeline has to:

- survive a worker crash mid-document without re-charging LLM calls or double-posting;
- pause for human review (corrections, two-person approval) for minutes or days, then resume;
- be replayable, so the quarantine path can be shown and debugged after the fact;
- keep the money decisions deterministic and outside any model (CLAUDE.md invariant 1).

Two designs were considered.

| | Plain Postgres state machine | LangGraph `StateGraph` + `PostgresSaver` |
|---|---|---|
| State | `documents.status` + a job table | the same, plus a checkpoint per node |
| Crash recovery | hand-written per step | resume from the last completed node |
| Human wait | poll the status column | `interrupt()` and resume on the same thread |
| Replay and time travel | none unless built | checkpoint history per `thread_id` |
| Extra dependency | none | `langgraph`, `langgraph-checkpoint-postgres` |
| Market signal | none | LangGraph is named in higher-value agent posts (§2) |

## Decision

Use **LangGraph** (`packages/core/invoicedesk/pipeline/graph.py`) with `PostgresSaver`
(`thread_id = document_id`), but **keep the database as the source of truth for status**:

```text
preprocess -> screen --quarantined--> review_interrupt
                 \--> extract_and_score -> validate --ReadyToPay--> post -> END
                                               \--NeedsReview/Flagged--> review_interrupt
review_interrupt --(human acted; status re-read from the DB)--> validate | post | END
```

- **Status changes never come from graph state.** Every transition goes through
  `invoicedesk.db.repo.set_status`, which locks the row and calls `invoicedesk.status.transition()`
  with evidence (the recomputed `RoutingDecision`, `ApprovalEvidence` or `PostingReceipt`). The
  graph is orchestration, not authority.
- **Nodes are idempotent beyond the checkpointer.** LangGraph saves a checkpoint *after* a node
  returns, so a crash between "node committed" and "checkpoint saved" re-runs the node. Each node
  therefore writes its effects, its transition and a `pipeline_steps` marker (migration
  `0300_pipeline_steps.sql`) in one transaction, and a re-run returns the stored output
  (`invoicedesk/pipeline/steps.py`).
- **Human review is `interrupt()`.** The services layer records the human action and its status
  change first, then resumes the thread. The review node re-reads the status from the database,
  so a resume payload can never move a document.
- **The queue is a plain Postgres `SKIP LOCKED` table** (`invoicedesk/db/queue.py`), one job per
  document, concurrency 8, with `requeue_stale` for jobs held by a dead worker.

## Why the plain state machine lost

It would have needed hand-built crash recovery and resume for every step, and it would still have
had no replay or checkpoint history. The `pipeline_steps` markers we wrote anyway are a small
part of that work; LangGraph supplies the rest.

## Consequences

- Two layers of durability (checkpoints and step markers). The step markers are what make
  re-runs safe, and they are tested by killing a run between commit and checkpoint.
- LangGraph's checkpoint tables are created by `PostgresSaver.setup()`, outside our migrations.
  The demo reset does not clear them (see [GAPS.md](../GAPS.md)).
- The worker kill test is in-process: a `BaseException` raised inside a node, then a resume with a
  fresh saver. An OS-level `SIGKILL` chaos run waits for a Docker daemon (GAPS.md).
- `/agent/extract` runs the whole graph synchronously, because the graph has no
  stop-after-extraction edge (GAPS.md, API section).
