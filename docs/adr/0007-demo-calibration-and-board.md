# ADR-0007: Demo numbers, heuristic calibration and board headlines

- **Status:** accepted (amends the WO-1187 estimate in DESIGN §6.2/§9)
- **Date:** 2026-10-24
- **Spec:** [DESIGN §5.4, §5.5, §6.2, §9](../../DESIGN-InvoiceDesk.md)

## Context

The offline end-to-end run of the 25-invoice demo through the real preprocessor first settled
to 10 Ready to pay / 12 Needs review / 3 Flagged instead of the designed 18 / 4 / 3. Every extra
hold was R9 (low calibrated confidence), from three causes: Chromium-printed PDFs switch Type3
subset fonts every few glyphs and the text layer split words on every switch; RapidOCR glues
values to their labels (`Number:1043`), so no OCR word matched a value exactly; and the
heuristic calibrator multiplied every handwritten field by 0.9, which kept a legible,
cross-checked handwritten invoice under `tau_field` 0.90. Separately, R4's image check flagged
the e-mailed invoice A-3601 as a duplicate of A-3600 because both came from one vendor template.

## Decision

1. **WO-1187's approved estimate is $3,200** (not $3,400). R3 compares the pre-tax amount and its
   tolerance `max(10%, $100)` is inclusive, so a $340 overage on $3,400 would pass; on $3,200 it
   is held: "Rapid Rooter Plumbing invoice is $340.00 over the approved estimate ($3,200.00) for
   Unit 12B, WO-1187."
2. **Text-layer words split only on a visible style change** (size, colour, visibility), never on
   a font-resource change. A white run inside a visible word still becomes its own span for the
   hidden-text detector (tested).
3. **OCR agreement** credits a value that appears inside a cited OCR token at token boundaries
   (text edge, punctuation or a lower-to-upper case change), folds the O/0 and I/l/1 confusions
   (0.97), and finds a date inside a label run. Digits glued to more digits never match.
4. **Heuristic calibrator:** the handwriting x0.9 factor applies only when two tiers did not
   independently agree on the value. §5.5 escalates handwriting to tier 2 precisely so that the
   reads can be compared; an agreed handwritten value is no longer penalised twice.
5. **R4 image hits need the same (or an unreadable) total.** A re-photographed duplicate keeps
   its total; two different invoices from one template do not.
6. **Board headlines** follow the §9 captions (see docs/api-contract.md, "Additions").

## Consequences

- The demo settles to 18 / 4 / 3 through the full offline pipeline (`make test-e2e`).
- Offline smoke gate (config D, before -> after): field F1 1.000 -> 1.000, false-STP 0 -> 0,
  adversarial misses 0 -> 0, $/doc $0.0189 -> $0.0121. These are oracle numbers: the calibrator
  changes are unmeasured against real model output until a live dev run exists, and a fitted
  calibrator (dev split only) should replace heuristic-v1 then.
- R4 no longer catches a re-photographed duplicate whose total was misread differently; the
  vendor + invoice-number and near-duplicate checks still apply.
