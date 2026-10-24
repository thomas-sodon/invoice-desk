"""Checks on the portfolio-facing docs (README, SECURITY, ADRs, architecture, eval methodology).

* Every relative link resolves to a file in the repo, and every ``#anchor`` into a Markdown file
  matches a heading there (GitHub's slug rules).
* README's results numbers are the values in the latest eval report JSON, which the README links;
  the DESIGN doc's bracketed placeholder figures never appear as results.
* No MCP or OWASP jargon above README's technical appendix (CLAUDE.md, Scope discipline).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = [
    ROOT / "README.md",
    ROOT / "SECURITY.md",
    ROOT / "docs" / "architecture.md",
    ROOT / "docs" / "eval-methodology.md",
    ROOT / "docs" / "results" / "README.md",
    ROOT / "docs" / "media" / "README.md",
    ROOT / "integrations" / "n8n" / "workflows" / "README.md",
    ROOT / "infra" / "fly" / "README.md",
    *sorted((ROOT / "docs" / "adr").glob("*.md")),
]
REPORTS = ROOT / "evals" / "reports"
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^```.*?^```", re.S | re.M)


def _strip_code(md: str) -> str:
    md = FENCE.sub("", md)
    return re.sub(r"`[^`\n]*`", "", md)


def github_slug(heading: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # link text only
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(md_file: Path) -> set[str]:
    out: set[str] = set()
    seen: dict[str, int] = {}
    for line in FENCE.sub("", md_file.read_text(encoding="utf-8")).splitlines():
        m = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not m:
            continue
        slug = github_slug(m.group(1))
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.add(slug if n == 0 else f"{slug}-{n}")
    return out


def relative_links(md_file: Path) -> list[str]:
    links = LINK.findall(_strip_code(md_file.read_text(encoding="utf-8")))
    return [
        link
        for link in links
        if not re.match(r"^[a-z][a-z0-9+.-]*:", link)  # http:, https:, mailto:
    ]


@pytest.mark.parametrize("md_file", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_relative_link_resolves(md_file: Path) -> None:
    assert md_file.exists()
    problems: list[str] = []
    for link in relative_links(md_file):
        path_part, _, frag = link.partition("#")
        target = (md_file.parent / path_part).resolve() if path_part else md_file
        if not target.exists():
            problems.append(f"{link}: no such file")
            continue
        try:
            target.relative_to(ROOT)
        except ValueError:
            problems.append(f"{link}: points outside the repo")
            continue
        if frag and target.suffix == ".md" and frag not in anchors(target):
            problems.append(f"{link}: no heading #{frag} in {target.relative_to(ROOT)}")
    assert not problems, problems


def test_all_adrs_are_indexed_and_numbered() -> None:
    adr_dir = ROOT / "docs" / "adr"
    adrs = sorted(p.name for p in adr_dir.glob("[0-9][0-9][0-9][0-9]-*.md"))
    assert adrs[:5] == [
        "0001-scope.md",
        "0002-langgraph.md",
        "0003-claude-routing.md",
        "0004-ocr-anchoring.md",
        "0005-guardrails.md",
    ]
    index = (adr_dir / "README.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in adrs:
        assert f"({name})" in index
        assert f"(docs/adr/{name})" in readme
        text = (adr_dir / name).read_text(encoding="utf-8")
        for heading in ("## Context", "## Decision", "## Consequences"):
            assert heading in text, f"{name} lacks {heading}"


# ----------------------------------------------------------------------------- metrics honesty
def latest_report() -> tuple[Path, dict[str, Any]]:
    reports = sorted(p for p in REPORTS.glob("*.json") if p.name != "badge.json")
    assert reports, "no eval report"
    path = reports[-1]
    return path, json.loads(path.read_text(encoding="utf-8"))


def _metric(report: dict[str, Any], config: str, name: str) -> dict[str, Any]:
    (run,) = [r for r in report["runs"] if r["config"] == config]
    return run["metrics"][name]


def _fmt(v: float, digits: int = 3) -> str:
    return f"{v:.{digits}f}"


def test_readme_results_come_from_the_latest_report() -> None:
    path, report = latest_report()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    rel = path.relative_to(ROOT).as_posix()
    results = readme.split("## Results", 1)[1].split("\n## ", 1)[0]
    assert f"({rel})" in results, "README results must link the report they quote"
    assert "benchmark on synthetic data" in results.lower()

    if report["mode"] == "offline" or report.get("oracle"):
        # an oracle run is not a model measurement: say so, and quote no accuracy/cost/latency
        assert "offline" in results.lower()
        assert "no live run" in results.lower() or "no live model" in results.lower()
        for name in ("field_f1", "usd_per_doc", "p50_s", "p95_s", "line_item_f1"):
            for cfg in ("D", "N"):
                v = _metric(report, cfg, name)["value"]
                if v is not None and v not in (0, 1):
                    assert _fmt(v) not in results, f"{name} {cfg} quoted from an oracle run"

    expect: list[str] = []
    for cfg in ("D", "N"):
        for name in ("false_stp", "stp"):
            m = _metric(report, cfg, name)
            expect.append(_fmt(m["value"]))
            if cfg == "D" or name == "false_stp":
                lo, hi = m["ci"]
                expect.append(f"[{_fmt(lo)}, {_fmt(hi)}]")
        miss = _metric(report, cfg, "adversarial_misses")
        lo, hi = miss["ci"]
        expect.append(f"[{lo:.0f}, {hi:.0f}]")
        for name in ("bank_change_hold_recall", "duplicate_recall"):
            expect.append(_fmt(_metric(report, cfg, name)["value"]))
    for s in expect:
        assert s in results, f"{s} not found in README results (from {rel})"


def test_no_placeholder_design_figures_are_presented_as_results() -> None:
    # DESIGN §9-§10 bracketed numbers are targets to be replaced by measured values.
    for md in (ROOT / "README.md", ROOT / "SECURITY.md", ROOT / "docs" / "eval-methodology.md"):
        text = md.read_text(encoding="utf-8")
        for placeholder in ("97% field accuracy", "76% hands-free", "$0.011 per invoice", "4.1s"):
            assert placeholder not in text, f"{placeholder} in {md.name}"


def test_security_adversarial_table_matches_the_report() -> None:
    path, report = latest_report()
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert f"({path.relative_to(ROOT).as_posix()})" in security
    gold = {
        f"adversarial/{g['id']}": g["category"]
        for g in (
            json.loads(p.read_text(encoding="utf-8"))
            for p in (ROOT / "data" / "gold" / "adversarial").glob("adv-*.json")
        )
    }
    cases = {k for k, v in gold.items() if v != "prior"}
    assert len(cases) == 30
    for cfg, expected_misses in (("D", "**0 of 30**"), ("N", "30 of 30")):
        (run,) = [r for r in report["runs"] if r["config"] == cfg]
        misses = {
            x["example_id"]
            for x in run["results"]
            if x["adversarial_miss"] and x["example_id"] in cases
        }
        if cfg == "D":
            assert not misses
        else:
            assert len(misses) == 30
        assert expected_misses in security


def test_no_mcp_or_owasp_jargon_in_readme_top_section() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Technical appendix" in readme
    top = readme.split("## Technical appendix", 1)[0]
    for word in ("MCP", "OWASP", "Model Context Protocol"):
        assert word not in top, word


def test_readme_uses_works_with_wording_only() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Works with QuickBooks Online" in readme
    assert "intuit-logo" not in readme.lower() and "xero-logo" not in readme.lower()
    for img in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", readme):
        assert "quickbooks" not in img.lower() and "intuit" not in img.lower()
