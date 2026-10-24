"""Eval data types.

* :class:`EvalExample`: one gold document to evaluate (from ``data/gold`` via invoicedesk_data).
* :class:`DocRun`: what one config did with one example in one repeat (harness output).
* :class:`DocScore`: the per-document metrics derived from a run and its gold label. It is what
  ``eval_results.metrics_json`` stores, and every aggregate (with bootstrap CIs) is a pure
  function of a list of these.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["DocRun", "DocScore", "EvalExample", "FieldScore", "LLMCall"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvalExample(_M):
    """``example_id`` is ``"<set>/<doc_id>"`` (the ``eval_examples.external_ref``)."""

    example_id: str
    doc_id: str
    set: str
    split: str  # dev | test | adversarial | public
    medium: str
    category: str | None = None
    priors: list[str] = Field(default_factory=list)  # example ids processed first (support)

    @property
    def is_adversarial_case(self) -> bool:
        """The 30 adversarial cases (the 8 ``prior`` support documents are not cases)."""
        return self.split == "adversarial" and self.category not in (None, "prior")


class LLMCall(_M):
    role: str
    model_id: str
    cost_usd: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    refused: bool = False


class DocRun(_M):
    example_id: str
    config: str
    repeat: int
    status: str  # final DocStatus value ("Posted" for the naive baseline's auto-post)
    auto_posted: bool  # ReadyToPay (InvoiceDesk would post it with no human) / naive posted
    quarantined: bool = False
    extraction: dict[str, Any] | None = None  # final InvoiceExtraction JSON (None if not read)
    calibrated: dict[str, float] = Field(default_factory=dict)
    vendor_resolved: bool = False  # R1 ran and stored a vendor_id row
    vendor_id: str | None = None
    boxes: dict[str, dict[str, Any]] = Field(default_factory=dict)  # field -> {box, page}
    has_boxes: bool = True  # False for pipelines that never anchor boxes (naive baseline)
    failed_rules: dict[str, str] = Field(default_factory=dict)  # rule_id -> severity
    rule_messages: dict[str, str] = Field(default_factory=dict)
    screen: list[dict[str, Any]] = Field(default_factory=list)  # detector, verdict, categories
    tiers: list[str] = Field(default_factory=list)
    escalated_to: str | None = None
    calls: list[LLMCall] = Field(default_factory=list)
    cost_usd: str = "0"
    wall_ms: int = 0
    llm_ms: int = 0
    latency_ms: int = 0
    error: str | None = None


class FieldScore(_M):
    tp: int
    fp: int
    fn: int
    correct: bool | None
    gold: str | int | None = None  # normalized; masked for remit-to before storage
    pred: str | int | None = None


class DocScore(_M):
    example_id: str
    doc_id: str
    set: str
    split: str
    medium: str
    category: str | None
    config: str
    repeat: int

    status: str
    expected_status: str
    auto_posted: bool
    clean_truth: bool
    quarantined: bool
    expected_quarantined: bool
    extracted: bool
    error: str | None = None

    fields: dict[str, FieldScore] = Field(default_factory=dict)
    required_correct: bool | None = None  # all required fields right (doc exact match)
    lines_tp: int = 0
    lines_fp: int = 0
    lines_fn: int = 0
    lines_gold: int = 0
    lines_pred: int = 0

    rules_expected: dict[str, str] = Field(default_factory=dict)
    rules_failed: dict[str, str] = Field(default_factory=dict)
    rules_scored: list[str] = Field(default_factory=list)  # rule ids this doc is scored on

    injection_positive: bool = False
    injection_detected: bool = False
    bank_change_positive: bool = False
    bank_change_held: bool = False
    adversarial_case: bool = False
    adversarial_miss: bool = False
    false_stp: bool = False

    calib: list[tuple[float, bool]] = Field(default_factory=list)
    box_hits: int = 0
    box_total: int = 0

    cost_usd: str = "0"
    #: USD per model tier (haiku / sonnet / opus) from this document's calls (DESIGN §5.6
    #: "$/doc ... per tier"); a non-LLM reader's per-page bill (config G) is not included.
    cost_by_tier: dict[str, str] = Field(default_factory=dict)
    latency_ms: int = 0
    tiers: list[str] = Field(default_factory=list)
    illegible: list[str] = Field(default_factory=list)
    root_cause: str | None = None
