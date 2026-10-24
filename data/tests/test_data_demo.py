"""The 25-file demo batch and its expected outcomes."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections import Counter

import pytest
from invoicedesk.contracts.preprocess import ALLOWED_MIME, MAX_BYTES
from invoicedesk.preprocess import images as pim
from invoicedesk.preprocess import pdf as ppdf
from invoicedesk_data.demo import (
    DEMO_TODAY,
    bank_change_email,
    build_demo,
    demo_files,
)
from invoicedesk_data.paths import DEMO_DIR


def test_demo_files_and_expected_board() -> None:
    files = demo_files()
    assert len(files) == 25
    assert len({f.filename for f in files}) == 25
    board = Counter(str(f.expected.status) for f in files if f.expected)
    assert board == Counter({"ReadyToPay": 18, "NeedsReview": 4, "Flagged": 3})
    for f in files:
        assert f.mime in ALLOWED_MIME
        assert len(f.data) <= MAX_BYTES
        assert f.email is None
        assert hashlib.sha256(f.data).hexdigest() == next(
            e["sha256"]
            for e in json.loads((DEMO_DIR / "expected.json").read_text())["files"]
            if e["filename"] == f.filename
        )
    media = Counter(f.medium for f in files)
    assert media["native"] >= 8 and media["photo"] >= 4 and media["handwritten"] >= 3


def test_demo_beats_are_present() -> None:
    heads = {f.expected.headline for f in demo_files() if f.expected and f.expected.headline}
    assert "$340.00 over approved estimate for Unit 12B (WO-1187)" in heads
    assert "Duplicate of INV-2231 (same vendor, amount, week)" in heads
    assert "Total unreadable: please confirm" in heads
    assert "Hidden instruction detected: attempted bank-account change, blocked" in heads
    flagged = [f for f in demo_files() if f.expected and str(f.expected.status) == "Flagged"]
    assert sorted(f.medium for f in flagged) == ["native", "photo", "photo"]


def test_bank_change_email() -> None:
    mail = bank_change_email()
    assert "update our bank details" in mail.body_text
    assert mail.from_addr.endswith(".example")
    assert len(mail.attachments) == 1
    att = mail.attachments[0]
    assert att.mime == "application/pdf"
    assert att.expected is not None and str(att.expected.status) == "Flagged"
    assert att.expected.rules == {"R6": "flag"}
    extra = demo_files(include_email=True)
    assert len(extra) == 26 and extra[-1].email is not None
    payload = json.loads((DEMO_DIR / "bank-change-email.json").read_text())
    assert payload["body_text"] == mail.body_text


def test_demo_as_of_is_recorded() -> None:
    payload = json.loads((DEMO_DIR / "expected.json").read_text())
    assert payload["as_of"] == DEMO_TODAY.isoformat()
    assert payload["board"] == {"Flagged": 3, "NeedsReview": 4, "ReadyToPay": 18}


def _phash(data: bytes, mime: str) -> int:
    if mime == "application/pdf":
        doc, _ = ppdf.open_sanitized(data)
        img = ppdf.render_page(doc[0]).image
        ppdf.close_quietly(doc)
    else:
        img, _, _ = pim.load_raster(data)
        img, _ = pim.resize_long_edge(img)
    return int(pim.phash_hex(img), 16)


def test_no_two_demo_pages_look_alike_to_r4_phash() -> None:
    """R4 flags pHash distance <= 6 as the same image; distinct demo invoices must stay apart."""
    hashes = [(f.filename, _phash(f.data, f.mime)) for f in demo_files()]
    worst = min(((a ^ b).bit_count(), x, y) for (x, a), (y, b) in itertools.combinations(hashes, 2))
    assert worst[0] >= 8, worst


@pytest.mark.slow
def test_demo_rebuild_is_byte_identical(tmp_path) -> None:
    docs, att = build_demo(tmp_path / "demo", gold_dir=tmp_path / "gold")
    for d in [*docs, att]:
        committed = (DEMO_DIR / d.filename).read_bytes()
        assert hashlib.sha256(committed).hexdigest() == d.gold.sha256, d.filename
    for name in ("expected.json", "bank-change-email.json"):
        assert (tmp_path / "demo" / name).read_text() == (DEMO_DIR / name).read_text()
