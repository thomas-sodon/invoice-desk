"""CLI entry points and the "fictional data only" guarantee over every committed artifact."""

from __future__ import annotations

import re

import pytest
from invoicedesk_data.cli import main
from invoicedesk_data.paths import DATA_DIR, DEMO_DIR, GOLD_DIR, SEED_DIR


def test_cli_splits_and_verify(capsys) -> None:
    assert main(["splits"]) == 0
    assert main(["verify", "demo", "adversarial"]) == 0
    out = capsys.readouterr().out
    assert "frozen split OK" in out and "26/26" in out and "38/38" in out


def test_cli_seed_csv_only(capsys, monkeypatch, tmp_path) -> None:
    import invoicedesk_data.seed as seed

    monkeypatch.setattr(seed, "SEED_DIR", tmp_path)
    monkeypatch.setattr(seed.write_csvs, "__defaults__", (tmp_path, None))
    assert main(["seed", "--csv-only"]) == 0
    assert (tmp_path / "vendors.csv").exists()


def test_cli_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        main([])


def _committed_text() -> str:
    parts = []
    for base in (SEED_DIR, GOLD_DIR, DEMO_DIR):
        for p in base.rglob("*"):
            if p.suffix in (".csv", ".json"):
                parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_only_test_routing_numbers_and_fictional_accounts() -> None:
    text = _committed_text()
    routings = set(re.findall(r"[Rr]outing (\d{9})", text))
    assert routings == {"110000000"}
    accounts = set(re.findall(r"(?:Acct|account|Account) (\d{9,17})", text))
    assert accounts and all(a.startswith(("000999", "000555")) for a in accounts), accounts


def test_only_reserved_contact_details() -> None:
    text = _committed_text()
    emails = set(re.findall(r"[\w.+-]+@[\w.-]+\.[a-z]+", text))
    assert emails and all(e.endswith(".example") or e.endswith(".example>") for e in emails), emails
    phones = set(re.findall(r"\(\d{3}\) \d{3}-\d{4}", text))
    assert phones and all(re.fullmatch(r"\(\d{3}\) 555-01\d\d", p) for p in phones), phones


def test_public_images_are_never_committed() -> None:
    gitignore = (DATA_DIR.parent / ".gitignore").read_text()
    assert "data/public/cache/" in gitignore and "data/generated/" in gitignore
    assert not list((DATA_DIR / "public").glob("*.jpg"))
