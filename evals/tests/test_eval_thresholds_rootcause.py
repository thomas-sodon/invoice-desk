from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from invoicedesk.config.models import ModelRole
from invoicedesk.config.settings import REPO_ROOT
from invoicedesk.config.tenant import load_rules_config
from invoicedesk.contracts.extraction import REQUIRED_FIELDS
from invoicedesk.contracts.llm import LLMCallError, LLMRequest, LLMResponse, Usage
from invoicedesk.fakes import FakeLLM
from invoicedesk_evals.model import DocRun, FieldScore
from invoicedesk_evals.root_cause import breakdown as rc_breakdown
from invoicedesk_evals.root_cause import (
    heuristic_label,
    is_failure,
    label_failures,
    write_spot_check_sheet,
)
from invoicedesk_evals.thresholds import (
    apply_thresholds,
    fit_thresholds,
    load_thresholds,
)
from pydantic import BaseModel


# ----------------------------------------------------------------------------- thresholds
def _pair(i: int, conf: float, correct: bool, make_score: Any) -> tuple[DocRun, Any]:
    run = DocRun(
        example_id=f"synthetic-core/d{i}",
        config="D",
        repeat=0,
        status="ReadyToPay",
        auto_posted=True,
        calibrated=dict.fromkeys(REQUIRED_FIELDS, conf),
    )
    fields = {
        n: FieldScore(
            tp=1 if correct else 0, fp=0 if correct else 1, fn=0 if correct else 1, correct=correct
        )
        for n in REQUIRED_FIELDS
    }
    return run, make_score(example_id=run.example_id, fields=fields)


def test_fit_thresholds_dev_only(make_score: Any) -> None:
    pairs = [_pair(i, 0.97, True, make_score) for i in range(40)]
    pairs += [_pair(100 + i, 0.80, False, make_score) for i in range(10)]
    runs = [r for r, _ in pairs]
    scores = [s for _, s in pairs]
    choice = fit_thresholds(runs, scores, split="dev", required=REQUIRED_FIELDS)
    assert 0.80 < choice["tau_field"] <= 0.97
    assert choice["false_stp_rate"] == 0.0 and choice["n_auto"] == 40
    assert choice["split"] == "dev"
    with pytest.raises(PermissionError):
        fit_thresholds(runs, scores, split="test", required=REQUIRED_FIELDS)
    with pytest.raises(PermissionError):
        fit_thresholds(runs, scores, split="dev+test", required=REQUIRED_FIELDS)


def test_thresholds_file_must_come_from_dev(tmp_path: Path) -> None:
    good = tmp_path / "t.json"
    good.write_text(json.dumps({"split": "dev", "tau_field": 0.91, "tau_hi": 0.96}))
    cfg = apply_thresholds(
        load_rules_config("maple_row", REPO_ROOT / "tenants"), load_thresholds(good)
    )
    assert cfg.confidence.tau_field == 0.91 and cfg.confidence.tau_hi == 0.96
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"split": "test", "tau_field": 0.5, "tau_hi": 0.5}))
    with pytest.raises(PermissionError):
        load_thresholds(bad)


# ----------------------------------------------------------------------------- root cause
def _wrong(name: str, gold: Any, pred: Any) -> dict[str, FieldScore]:
    return {name: FieldScore(tp=0, fp=1, fn=1, correct=False, gold=gold, pred=pred)}


def test_heuristic_labels(make_score: Any) -> None:
    ok = make_score()
    assert not is_failure(ok)
    cases = {
        "pipeline_error": make_score(error="boom", status="Error"),
        "screening_false_positive": make_score(
            quarantined=True, status="Flagged", auto_posted=False
        ),
        "calibration_miss": make_score(fields=_wrong("total", 124000, 124900)),
        "handwriting_illegible": make_score(
            medium="handwritten",
            auto_posted=False,
            status="NeedsReview",
            fields=_wrong("total", 124000, 124900),
        ),
        "ocr_misread": make_score(
            auto_posted=False,
            status="NeedsReview",
            fields=_wrong("invoice_number", "inv 2231", "inv 2281"),
        ),
        "line_item_table_split": make_score(lines_gold=2, lines_pred=3, lines_fp=1),
        "rule_false_positive": make_score(
            auto_posted=False, status="NeedsReview", rules_failed={"R3": "review"}
        ),
        "rule_false_negative": make_score(
            expected_status="Flagged", clean_truth=False, rules_expected={"R4": "flag"}
        ),
    }
    for want, s in cases.items():
        assert is_failure(s), want
        assert heuristic_label(s).label == want, want
    r9 = make_score(auto_posted=False, status="NeedsReview", rules_failed={"R9": "review"})
    assert heuristic_label(r9).label == "calibration_miss"
    mapping = make_score(
        auto_posted=False,
        status="NeedsReview",
        fields={
            **_wrong("due_date", "2026-11-09", "2026-10-10"),
            "invoice_date": FieldScore(
                tp=1, fp=0, fn=0, correct=True, gold="2026-10-10", pred="2026-10-10"
            ),
        },
    )
    assert heuristic_label(mapping).label == "wrong_field_mapping"


class _Batch:
    """Fake client with the Batch API surface (submit/collect), like AnthropicLLMClient."""

    def __init__(self) -> None:
        self.inner = FakeLLM(by_role={"root_cause": {"label": "ocr_misread", "rationale": "x"}})
        self.submitted: list[tuple[str, LLMRequest]] = []
        self.polls = 0

    def upload_file(self, data: bytes, *, filename: str, media_type: str) -> str:
        return "f"

    def parse(self, request: LLMRequest, output_model: type[BaseModel]) -> LLMResponse:
        raise AssertionError("batch client must not be called synchronously")

    def submit_batch(
        self, items: list[tuple[str, LLMRequest]], output_model: type[BaseModel]
    ) -> str:
        self.submitted = items
        return "batch_1"

    def collect_batch(
        self, batch_id: str, role: ModelRole, output_model: type[BaseModel]
    ) -> dict[str, Any]:
        self.polls += 1
        if self.polls == 1:
            raise LLMCallError("status", "batch still processing")
        out: dict[str, Any] = {}
        for cid, req in self.submitted:
            out[cid] = self.inner.parse(req, output_model)
        out[self.submitted[-1][0]] = LLMResponse(
            role=role,
            model_id="m",
            stop_reason="refusal",
            parsed=None,
            usage=Usage(),
            cost_usd=Decimal(0),
            latency_ms=0,
        )
        return out


def test_label_failures_offline_live_and_batch(make_score: Any, tmp_path: Path) -> None:
    remit = _wrong("remit_to_bank_details", "acct ••••••••0885", "acct 000111222333")
    fails = [
        make_score(example_id="a", fields=_wrong("total", 1, 2)),
        make_score(
            example_id="b", auto_posted=False, status="NeedsReview", rules_failed={"R3": "review"}
        ),
        make_score(example_id="c", auto_posted=False, status="NeedsReview", fields=remit),
    ]
    scores = [make_score(example_id="ok"), *fails]
    offline = label_failures(scores)
    assert set(offline) == {("a", 0), ("b", 0), ("c", 0)}
    assert all(src == "heuristic" for _, src in offline.values())
    assert rc_breakdown(offline)["calibration_miss"] == 1
    # synchronous LLM path (FakeLLM), labels come back as "opus"
    fake = FakeLLM(by_role={"root_cause": {"label": "wrong_field_mapping", "rationale": "r"}})
    live = label_failures(scores, llm=fake)
    assert {lbl.label for lbl, _ in live.values()} == {"wrong_field_mapping"}
    assert all(c.role == ModelRole.ROOT_CAUSE for c in fake.calls)
    # the labeler never sees an unmasked account number
    assert all("000111222333" not in c.user[0].text for c in fake.calls)  # type: ignore[union-attr]
    # Batch API path with polling; a refused item falls back to the heuristic
    batch = _Batch()
    res = label_failures(scores, llm=batch, poll_s=0)
    assert batch.polls == 2
    sources = sorted(src for _, src in res.values())
    assert sources == ["heuristic-fallback", "opus", "opus"]
    sheet = write_spot_check_sheet(tmp_path / "s.csv", scores, res, sample=2)
    rows = list(csv.DictReader(sheet.open()))
    assert len(rows) == 2 and rows[0]["human_label"] == ""
    assert "000111222333" not in sheet.read_text()
