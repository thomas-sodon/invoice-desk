# ADR-0008: A two-person release needs verified values; held invoices are fixed first

- **Status:** accepted
- **Date:** 2026-10-24
- **Spec:** [DESIGN §4.3, §5.5](../../DESIGN-InvoiceDesk.md) (state machine, R6, two-person approval), M7

## Context

A Flagged document left Flagged only through `Flagged -> ReadyToPay` (two approvers, a callback
attestation, never the uploader) or `Rejected`. The transition guard looked at the votes only.
A document that failed R6 *and* a review-level rule (R9 low-confidence total, R5 arithmetic, R3
over the estimate, R7 over the cap, R8 dates, R2 work order, R1 unknown vendor) was therefore
posted with values nobody had verified once the bank change was approved. Corrections were
refused on Flagged documents, so a reviewer could not fix the total before the vote either.

Options considered:

1. **Release to Validating after the vote.** Re-running the rules after approval puts the
   document straight back in Flagged, because R6 deliberately never reads approvals
   (invariant 3). Making R6 honour earlier approvals would need a "same remit-to fingerprint,
   same hold" carry-over: easy to get wrong, and a weakening of invariant 3.
2. **Approvers acknowledge the open failures.** Keeps one vote, but posts a total that may be
   misread. "Acknowledged" is not "verified".
3. **Fix first, then vote** (chosen).

## Decision

1. **Guard.** `ApprovalEvidence` carries the latest evaluation's rule outcomes.
   `status.transition(Flagged -> ReadyToPay)` refuses unless they cover R1..R10 and every
   failure is `flag`-level (the hold the approvers judge: bank change, duplicate, look-alike,
   injection). `status.release_blockers()` lists what is still open. Missing outcomes fail closed.
2. **New edge `Flagged -> Validating`, human actors only.** On an extracted Flagged document a
   reviewer can apply corrections, and an `over_cap` / `new_vendor` approval that targets an
   open R7 / R1 review failure re-validates, just as in NeedsReview. R6 re-flags the document,
   which opens a new hold; the votes are cast on the corrected values. A quarantined document
   was never read, so it still cannot be corrected or released.
3. **Order is enforced.** `record_approval` refuses a release vote on a Flagged document while
   review-level failures are open (`ReleaseBlocked`, API `409 illegal_transition` with the list).
   `apply_corrections` refuses a correction once a release vote has been cast on the current
   hold, because the correction would void that vote and `approvals` allows one vote per
   approver per kind per document. The remedy then is to reject and ask for a corrected invoice.
4. **UI.** The correction form also shows on a Flagged invoice with open review failures. The
   approval panel shows "fix these first" (or offers the over-limit / new-vendor approval) and
   hides the release vote until only flag-level holds remain. The mock API mirrors this.

## Consequences

- No path posts a held invoice whose total, arithmetic, estimate, cap, dates or vendor are
  unverified. The graph's `review_interrupt -> post` edge only follows a release the guard
  accepted.
- §4.3 gains one edge, `Flagged -> Validating: human corrects fields`. DESIGN is updated to match.
- A genuine overage (R3) on a held invoice cannot be approved away. It is rejected, as it would
  be in NeedsReview.
- An approver who voted and then wants a correction cannot vote again on the same document;
  reject-and-resubmit is the documented path. Allowing re-votes would need a per-hold unique key
  on `approvals` (a migration), which we do not need for the MVP.
