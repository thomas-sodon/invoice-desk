from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from invoicedesk_data.gold import load_gold
from invoicedesk_evals.datasets import (
    assert_not_test,
    filter_media,
    load_split,
    smoke_dev_ids,
    split_names,
)
from invoicedesk_evals.matrix import load_matrix, resolve_config_name
from invoicedesk_evals.runner import (
    MoneyGuardError,
    check_money_guard,
    estimate_usd,
    main,
    parse_configs,
)


def test_matrix_has_every_design_config() -> None:
    m = load_matrix()
    assert list(m.configs) == ["A", "B", "C", "D", "D'", "E", "F", "G", "N"]
    assert m.default_repeats == 3
    assert m.get("dp").name == "D'" and resolve_config_name("d'") == "D'"
    d = m.get("D")
    assert d.routed and d.max_tier == "opus" and d.caching and d.images
    a = m.get("A")
    assert a.tiers == ["opus"] and a.max_tier == "haiku"  # one call, role remapped to Opus
    assert m.get("D'").caching is False
    assert m.get("E").images is False and m.get("F").media == ["native"]
    assert m.get("G").extractor == "azure_di" and m.get("N").pipeline == "naive"
    with pytest.raises(KeyError):
        m.get("Z")


def test_smoke_set_is_stratified_and_deterministic() -> None:
    dev = load_gold("synthetic-core", split="dev")
    ids = smoke_dev_ids(dev)
    assert ids == smoke_dev_ids(dev)
    assert len(ids) == len(set(ids)) == 30
    by_id = {r.id: r for r in dev}
    media = Counter(by_id[i].medium for i in ids)
    full = Counter(r.medium for r in dev)
    for m, n in full.items():  # proportional within one document
        assert abs(media[m] - n / 2) <= 1
    statuses = Counter(str(by_id[i].expected.status) for i in ids)
    assert statuses["ReadyToPay"] > 0 and sum(statuses.values()) - statuses["ReadyToPay"] > 0
    smoke = load_split("smoke")
    assert len(smoke) == 60
    assert sum(e.example.is_adversarial_case for e in smoke) == 30
    assert all(e.example.split in ("dev", "adversarial") for e in smoke)


def test_splits() -> None:
    dev = load_split("dev")
    assert len(dev) == 60 and {e.example.split for e in dev} == {"dev"}
    adv = load_split("adversarial")
    assert len(adv) == 38
    assert len(load_split("adversarial-cases")) == 30
    assert len(load_split("dev+adversarial")) == 98
    assert len(filter_media(dev, ["native"])) == 20
    with pytest.raises(ValueError):
        split_names("dev+holdout")


def test_test_split_is_never_tuned_on() -> None:
    assert_not_test("dev")
    for bad in ("test", "dev+test", "all"):
        with pytest.raises(PermissionError):
            assert_not_test(bad)
    # The CLI refuses before running anything.
    assert main(["fit-thresholds", "--split", "test", "--offline"]) == 4
    assert main(["fit-thresholds", "--split", "dev+test", "--offline"]) == 4
    # choose_thresholds accepts "dev" only: other splits are refused before any run too.
    assert main(["fit-thresholds", "--split", "adversarial", "--offline"]) == 4
    assert main(["fit-thresholds", "--split", "dev+adversarial", "--offline"]) == 4


def test_money_guard() -> None:
    exs = load_split("smoke")
    d = parse_configs("D")
    usd, lines = estimate_usd(d, exs, repeats=1)
    assert usd > 0 and "D" in lines[0]
    assert (
        check_money_guard(
            live=False,
            specs=d,
            examples=exs,
            repeats=1,
            i_know=False,
            full_matrix=False,
            root_cause_live=False,
        )
        == 0
    )
    with pytest.raises(MoneyGuardError, match="i-know-this-costs-money"):
        check_money_guard(
            live=True,
            specs=d,
            examples=exs,
            repeats=1,
            i_know=False,
            full_matrix=False,
            root_cause_live=False,
        )
    with_rc = check_money_guard(
        live=True,
        specs=d,
        examples=exs,
        repeats=1,
        i_know=True,
        full_matrix=False,
        root_cause_live=True,
    )
    assert with_rc > usd  # root-cause labeling is included in the estimate
    full = parse_configs("all")
    with pytest.raises(MoneyGuardError, match="full-matrix"):
        check_money_guard(
            live=True,
            specs=full,
            examples=load_split("test"),
            repeats=3,
            i_know=True,
            full_matrix=False,
            root_cause_live=False,
        )
    total, _ = estimate_usd(full, load_split("test"), repeats=3)
    assert 20 < float(total) < 60  # the "about $40" full matrix (DESIGN §7.3)
    # Every config but N is nearly the full matrix: it needs the same confirmation.
    near_full = parse_configs("A,B,C,D,D',E,F")
    with pytest.raises(MoneyGuardError, match="full-matrix"):
        check_money_guard(
            live=True,
            specs=near_full,
            examples=load_split("test"),
            repeats=3,
            i_know=True,
            full_matrix=False,
            root_cause_live=False,
        )
    # The nightly C + D run (all 158 docs, n=3, Opus root causes) stays below the bar.
    nightly = check_money_guard(
        live=True,
        specs=parse_configs("C,D"),
        examples=load_split("all"),
        repeats=3,
        i_know=True,
        full_matrix=False,
        root_cause_live=True,
    )
    assert 0 < nightly < 15


def test_live_cli_refuses_without_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("INVOICEDESK_MODE", "offline")
    rc = main(
        [
            "run",
            "--config",
            "D",
            "--split",
            "smoke",
            "--live",
            "--repeats",
            "1",
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert rc == 3
    err = capsys.readouterr().err
    assert "cost estimate" in err and "total ~$" in err
    assert not list(tmp_path.iterdir())  # nothing ran
    rc = main(
        [
            "run",
            "--config",
            "all",
            "--split",
            "test",
            "--live",
            "--i-know-this-costs-money",
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert rc == 3
    assert "full-matrix" in capsys.readouterr().err


def test_estimate_and_smoke_list_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["estimate", "--config", "C,D", "--split", "smoke", "--repeats", "1"]) == 0
    assert "total ~$" in capsys.readouterr().out
    assert main(["smoke-list"]) == 0
    assert len(capsys.readouterr().out.split()) == 60
