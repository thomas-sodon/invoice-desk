from __future__ import annotations

from invoicedesk_evals.metrics.fields import Counts
from invoicedesk_evals.metrics.line_items import Line, align_lines, description_similarity


def test_exact_alignment_and_counts() -> None:
    gold = [Line("Drain cleaning, main line", 45000), Line("Camera inspection", 30000)]
    pred = [Line("Camera inspection", 30000), Line("Drain cleaning main line", 45000)]
    a = align_lines(gold, pred)
    assert sorted(a.pairs) == [(0, 1), (1, 0)]
    assert a.counts == Counts(tp=2)


def test_amount_must_match_exactly_and_description_fuzzy() -> None:
    gold = [Line("Water heater install", 120000)]
    assert align_lines(gold, [Line("Water heater install", 120001)]).counts == Counts(fp=1, fn=1)
    assert align_lines(gold, [Line("Labor", 120000)]).counts == Counts(fp=1, fn=1)
    assert description_similarity("Water heater instal", "Water heater install") >= 0.8
    assert align_lines(gold, [Line("Water heater instal", 120000)]).counts == Counts(tp=1)


def test_hungarian_beats_greedy() -> None:
    # gold0 matches both predictions (best: pred0); gold1 matches only pred0. Greedy
    # (each gold line takes its best valid match in order) pairs gold0-pred0 and leaves gold1
    # unmatched; the optimal one-to-one assignment matches both.
    gold = [Line("abcdefghij", 10000), Line("abcdefgzij", 10000)]
    pred = [Line("abcdefghij", 10000), Line("abcdefghxy", 10000)]
    assert description_similarity("abcdefghij", "abcdefghxy") >= 0.8
    assert description_similarity("abcdefgzij", "abcdefghij") >= 0.8
    assert description_similarity("abcdefgzij", "abcdefghxy") < 0.8
    a = align_lines(gold, pred)
    assert a.counts.tp == 2
    assert sorted(a.pairs) == [(0, 1), (1, 0)]


def test_split_table_counts() -> None:
    gold = [Line("Labor 4 hrs", 40000)]
    pred = [Line("Labor 2 hrs", 20000), Line("Labor 2 hrs", 20000)]
    c = align_lines(gold, pred).counts
    assert c == Counts(tp=0, fp=2, fn=1)
    assert align_lines([], pred).counts == Counts(fp=2)
    assert align_lines(gold, []).counts == Counts(fn=1)
