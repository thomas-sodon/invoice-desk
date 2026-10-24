"""Public-benchmark field mappings (no network)."""

from __future__ import annotations

import json

import pytest
from invoicedesk.contracts.extraction import InvoiceExtraction
from invoicedesk_data.public.download_cord import records_from_rows
from invoicedesk_data.public.download_sroie import build_index
from invoicedesk_data.public.load import load_public
from invoicedesk_data.public.mappings import (
    map_cord,
    map_sroie,
    normalize_date,
    parse_total_cents,
    to_gold_extraction,
)


@pytest.mark.parametrize(
    ("raw", "iso"),
    [
        ("22 MAR 2018", "2018-03-22"),
        ("25/12/2018", "2018-12-25"),
        ("02-01-2019 14:03", "2019-01-02"),
        ("2018-06-01", "2018-06-01"),
        ("15.01.17", "2017-01-15"),
        ("Mar 5, 2018", "2018-03-05"),
        ("12/31/2018", "2018-12-31"),  # day-first impossible -> month-first
        ("not a date", None),
        (None, None),
    ],
)
def test_normalize_date(raw: str | None, iso: str | None) -> None:
    assert normalize_date(raw) == iso


@pytest.mark.parametrize(
    ("raw", "decimals", "cents"),
    [
        ("$7.60", 2, 760),
        ("RM 9.00", 2, 900),
        ("1,234.50", 2, 123_450),
        ("12", 2, 1_200),
        ("60.000", 0, 6_000_000),
        ("1,591,600", 0, 159_160_000),
        ("-5.10", 2, -510),
        ("", 2, None),
        (None, 2, None),
    ],
)
def test_parse_total_cents(raw: str | None, decimals: int, cents: int | None) -> None:
    assert parse_total_cents(raw, decimals=decimals) == cents


def test_map_sroie_sample(tmp_path) -> None:
    sample = {
        "filepath": "data/X51005442376.jpg",
        "company": "FICTIONAL TEST KEDAI SDN BHD",
        "date": "22 MAR 2018",
        "address": "1, JALAN CONTOH (FICTIONAL)",
        "total": "$7.60",
    }
    r = map_sroie(sample)
    assert (r.id, r.vendor_name, r.invoice_date, r.total_cents) == (
        "X51005442376",
        "FICTIONAL TEST KEDAI SDN BHD",
        "2018-03-22",
        760,
    )
    assert r.vendor_address == "1, JALAN CONTOH (FICTIONAL)"
    ex = to_gold_extraction(r)
    assert isinstance(ex, InvoiceExtraction) and ex.total.value_cents == 760
    assert ex.vendor_address.value == "1, JALAN CONTOH (FICTIONAL)"
    path = tmp_path / "samples.json"
    path.write_text(json.dumps({"samples": [sample, {**sample, "filepath": "data/B.jpg"}]}))
    assert [x.id for x in build_index(path)] == ["X51005442376", "B"]
    assert len(build_index(path, limit=1)) == 1


def test_map_cord_row() -> None:
    gt = {"gt_parse": {"menu": {"nm": "-TICKET CP"}, "total": {"total_price": "60.000"}}}
    payload = {
        "rows": [
            {
                "row_idx": 7,
                "row": {"image": {"src": "https://x/7.jpg"}, "ground_truth": json.dumps(gt)},
            }
        ]
    }
    [(r, url)] = records_from_rows(payload)
    assert (r.id, r.total_cents, r.vendor_name, r.invoice_date) == (
        "cord-test-007",
        6_000_000,
        None,
        None,
    )
    assert url == "https://x/7.jpg" and r.image == "images/007.jpg"
    assert map_cord(0, {"gt_parse": {}}, "a.jpg").total_cents is None


def test_load_public_reads_index(tmp_path) -> None:
    assert load_public("sroie", tmp_path) == []
    (tmp_path / "cord").mkdir()
    rec = map_cord(1, {"gt_parse": {"total": {"total_price": "5.000"}}}, "images/001.jpg")
    (tmp_path / "cord" / "index.jsonl").write_text(json.dumps(rec.to_json()) + "\n")
    assert load_public("cord", tmp_path) == [rec]
