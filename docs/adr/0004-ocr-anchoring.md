# ADR-0004: OCR engine, word-ID anchoring and the 1,600 px page size

- **Status:** accepted (amends DESIGN §4.4, which named docTR as the default OCR engine)
- **Date:** 2026-10-24
- **Spec:** [DESIGN §4.1, §4.4, §5.2](../../DESIGN-InvoiceDesk.md#44-tech-stack-and-justification), §11 ("model-drawn boxes are imprecise")

## Context

The review screen draws a box over the original page for every extracted field, coloured by
calibrated confidence (M5). A clerk fixes a field by looking at that box, so a box in the wrong
place is worse than none. Vision models can return coordinates, but they are approximate, and
they come back relative to the image the model actually saw. An OCR engine gives exact word boxes
but no field semantics. Box hit rate (IoU >= 0.5 against gold boxes) is a published metric.

The spec chose **docTR** for word-level OCR. In practice, `python-doctr[torch]` pulls in PyTorch,
which is several GB, slow to install in CI and too large for the shared Docker image.

## Decision

1. **Default OCR engine: RapidOCR** (onnxruntime with bundled models) behind the `OcrEngine`
   Protocol (`invoicedesk/preprocess/ocr.py`). RapidOCR returns line boxes, so the preprocessor
   splits them into word boxes using character-width proportions snapped to ink gaps.
   **docTR stays available** (`uv sync --extra doctr`, `DocTREngine`) behind the same Protocol,
   and **Azure Document Intelligence** `prebuilt-read` (`AzureDIEngine`) is an optional engine
   for the commercial comparison arm.
2. **Pre-resize every page to a 1,600 px long edge** (`TARGET_LONG_EDGE`). The API maximum is
   2,576 px. Because the model sees exactly the processed image, its coordinates and the OCR
   boxes share one pixel space, with no hidden rescale between them. `PageImage.scale` records the
   factor back to the original.
3. **Anchor fields to OCR word IDs.** The extractor's user turn lists every OCR word as
   `[id] "text" (x0,y0,x1,y1)`, with IDs global across pages. The schema's `WordRef.word_ids`
   points at the words that contain a value, and the box shown in the UI is the union of those
   words' OCR boxes. `WordRef.fallback_box` (a model-drawn box) is used **only** when no OCR word
   covers the value, which in practice means handwriting the OCR missed.
4. **Native PDFs also keep their text layer** (PyMuPDF spans with fill colour, background and font
   size). It is used to corroborate OCR words and, separately, by the hidden-text detector
   ([ADR-0005](0005-guardrails.md)). It never replaces visible OCR words, so hidden text cannot
   pose as visible content.
5. **A second OCR pass on a CLAHE-enhanced copy** (`enhanced_words`) feeds the faint-text detector
   only.

## Consequences

- Box precision depends on how well RapidOCR line boxes split into words. On synthetic renders,
  words land within 8 px of their true positions (a test asserts this). RapidOCR sometimes merges
  tokens (`"WorkorderWO-1180"`), which can leave a field without word refs. Its calibrated
  confidence then falls and R9 holds the document. That is the main cause of the extra holds
  recorded in [GAPS.md](../GAPS.md) (API section).
- The offline oracle run measured a box hit rate of 0.922 (95% CI 0.909-0.936) on native and
  scanned dev documents
  ([evals/reports/2026-10-24-dev_adversarial-D-N-offline.md](../../evals/reports/2026-10-24-dev_adversarial-D-N-offline.md)).
  The word refs come from the oracle in that run, so the figure measures the OCR boxes and the
  anchoring code, not Claude's ability to pick the right words.
- Swapping engines is a configuration change. Comparing docTR and RapidOCR box IoU on the gold
  set is open work (GAPS.md, Foundation and Preprocess sections).
- The CLAUDE.md rule "pre-resize pages to a 1,600 px long edge; anchor boxes to OCR word IDs"
  holds unchanged; only the engine name differs from §4.4.
