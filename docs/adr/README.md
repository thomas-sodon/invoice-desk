# Architecture decision records

Each ADR records one real decision, the alternatives considered and the consequences. When code
and [DESIGN-InvoiceDesk.md](../../DESIGN-InvoiceDesk.md) disagree, the ADR says which one is
right and why.

| ADR | Decision |
|---|---|
| [0001-scope](0001-scope.md) | MVP is M1-M21, with a fixed cut line; one fictional tenant; sandbox-only accounting; offline-first |
| [0002-langgraph](0002-langgraph.md) | LangGraph + `PostgresSaver` over a plain Postgres state machine; the DB stays the authority for status |
| [0003-claude-routing](0003-claude-routing.md) | Haiku -> Sonnet -> Opus routing and the Claude API specifics (no sampling params, structured outputs, refusals, caching, Files API) |
| [0004-ocr-anchoring](0004-ocr-anchoring.md) | RapidOCR by default (docTR optional), word-ID anchoring, 1,600 px pages |
| [0005-guardrails](0005-guardrails.md) | Containment over detection: screening first, tool-less reader, deterministic rules, least-privilege posting |
| [0006-pipeline-state-and-release](0006-pipeline-state-and-release.md) | Step markers for crash-safe nodes, per-tenant validation lock, what the checkpoint holds, hold-scoped release votes |
| [0007-demo-calibration-and-board](0007-demo-calibration-and-board.md) | WO-1187 estimate $3,200, whole-word text layer, OCR agreement and handwriting calibration, R4 image hits need the total, board headlines |
| [0008-release-needs-verified-values](0008-release-needs-verified-values.md) | A two-person release of a Flagged invoice needs no open review-level failure; held invoices are corrected first (`Flagged -> Validating`), then voted on |

New ADRs take the next number and use the same headings: Status, Date, Spec, Context, Decision,
Consequences.
