# ADR-0005: Guardrails are architecture, not a filter

- **Status:** accepted
- **Date:** 2026-10-24
- **Spec:** [DESIGN §5.1, §5.5](../../DESIGN-InvoiceDesk.md#55-tools-routing-logic-and-guardrails), CLAUDE.md invariants 1-5, 8, 9

## Context

Vendor invoices are untrusted input from outside the company. An attacker who can send an invoice
can put text on it that a person never sees (white-on-white PDF text, 2 pt fonts, text off the
page, faint grey print in a photo) but a model reads. The payoff is the classic
accounts-payable fraud: "update our bank details" and the next payment goes to the attacker.
Business email compromise is the costliest category in the FBI's 2025 IC3 report, at about
$3.05B ([summary](https://www.mcdonaldhopkins.com/insights/news/the-sobering-truth-of-the-fbis-2025-internet-crime-complaint-center-report)).

Detection alone is not enough: a new injection will eventually get past any classifier. The
design has to make a successful injection harmless.

## Decision

**Containment first, detection second.** The layers below are listed in pipeline order.

1. **Input handling** (`preprocess/`). The file type and size allow-list (JPEG, PNG, HEIC, PDF;
   at most 10 MB and 5 pages), PDF sanitize (JavaScript, embedded files and forms stripped, fail
   closed), and an EXIF strip.
2. **Deterministic screening before any extraction** (`guardrails/`; invariant 8).
   - `hidden_text.py` flags PDF text-layer words that are off-page, below 4 pt, within ΔE < 5 of
     the background, or absent from the OCR of the render.
   - `faint_text.py` flags text that only the CLAHE-enhanced OCR pass can read.
   - `classifier.py` is a Haiku 5.5 injection classifier over the OCR text, the text layer, the
     recovered hidden text and the **email body**. A Haiku refusal is re-asked of Sonnet; if no
     usable answer comes back, the document was not screened, so the result fails closed as
     `malicious` and the document is quarantined for a human.
   - `should_quarantine()` (R10) moves a flagged document Received -> Screening -> Quarantined
     -> Flagged. **The extractor is never called on it**, so hidden text is never escalated to
     higher tiers unflagged.
3. **A tool-less reader** (`extract/`; invariant 2, the dual-LLM or quarantined-reader pattern).
   The only model that sees document text and images has no tools. It returns JSON that must
   validate against `InvoiceExtraction` (`extra="forbid"`). Text addressed to an AI is copied
   into `embedded_instructions` as data.
4. **Code decides** (`rules/`, `status.py`; invariant 1). R1-R10 are pure functions of the
   extraction, tenant data and `rules.yaml`. `status.route()` turns their outcomes into a
   routing decision. `status.transition()` recomputes that decision before allowing
   Validating -> ReadyToPay, and refuses every `llm` actor.
5. **Bank changes always go to people** (R6; invariant 3). R6 fails when the remit-to fingerprint
   differs from the vendor master, when the classifier says `bank_or_remit_change`, or when
   `embedded_instructions` is non-empty. Release from Flagged needs `ApprovalEvidence`: two
   distinct staff approvers, neither of them the uploader, plus a callback attestation to the
   phone number on file. The DB trigger also requires `callback_attested` on every approve row
   for `bank_change`. There is no config key for R6 or R10, and no system or MCP actor can
   produce this transition.
6. **Least-privilege action** (`accounting/`; invariant 4). The adapter's whole public surface is
   `create_bill` and `attach_file`. `BillRequest` has no bank or payment fields
   (`extra="forbid"`). Every call carries a `requestid` idempotency key, and the transport
   refuses any host but the QBO sandbox. A test asserts the module contains no payment, vendor or
   delete endpoints.
7. **The tool-holding side never sees raw document text.** The MCP server and the posting path
   get values, rule results and counts, never OCR words, text-layer spans or screening spans. The
   MCP `post_bill` tool refuses anything that is not `ReadyToPay` (always refusing Flagged)
   before it asks the human. The human must type the vendor name and total, and the API checks
   them against the stored values.
8. **Accountability** (`audit/`; invariant 5). `audit_log` is append-only (a DB trigger forbids
   UPDATE, DELETE and TRUNCATE) and hash-chained: each row stores the SHA-256 of the previous
   row. Every state change, detector verdict, rule result and human action appends a row, and
   `make audit-verify` recomputes the chain.
9. **Masking** (invariant 9). `masking.mask_text`/`mask_payload` cut bank and account numbers to
   the last 4 in logs, traces, the audit log, API responses and the UI.
10. **Cost and abuse limits** on the public try-it page: Turnstile, per-IP and per-session limits,
    a global $3/day spend cap that falls back to a recorded replay, a 24h purge, and visitor
    uploads that can never reach Posted.

The OWASP LLM Top 10 (2026) mapping and the threat model are in [SECURITY.md](../../SECURITY.md).

## Alternatives rejected

- **A classifier-only gate** (screen, then let an agent with tools act). A single bypass then
  pays the attacker.
- **One agent with tools that reads documents and posts bills.** That is the configuration
  indirect prompt injection exploits. The naive baseline (config N, `pipeline/naive.py`) is
  exactly this "read it and post it" design and exists only to show what goes wrong.
- **Letting the model judge legitimacy of a bank-change letter.** Legitimate-looking letters are
  the attack. The classifier labels the category, and policy sends every bank change to a human.

## Consequences

- Legitimate bank-change letters are held too. That is intended: they cost a phone call, not a
  payment.
- The adversarial set (30 cases) is a CI gate with zero misses allowed. The offline oracle run
  held 30 of 30 cases in each of 3 repeats
  ([report](../../evals/reports/2026-10-24-dev_adversarial-D-N-offline.md)). That exercises the
  real detectors, rules and routing, but the classifier's answers came from a keyword oracle, not
  Haiku. Known detector limits are listed in [GAPS.md](../GAPS.md) (Guardrail screening section).
