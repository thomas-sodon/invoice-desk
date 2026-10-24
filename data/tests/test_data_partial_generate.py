"""A partial ``generate --limit N`` must not overwrite the frozen synthetic-core gold.

Own module: Playwright's sync API cannot nest inside another module's open ``Renderer``."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.slow


def test_partial_generate_never_touches_the_frozen_gold(tmp_path) -> None:
    from invoicedesk_data.gold import load_manifest
    from invoicedesk_data.paths import GOLD_DIR
    from invoicedesk_data.splits import CORE_SET, build_core

    committed = (GOLD_DIR / CORE_SET / "manifest.json").read_bytes()
    m, docs = build_core(tmp_path / "out", limit=1)
    assert len(docs) == 1 and m.count == 1 and not m.frozen
    assert (GOLD_DIR / CORE_SET / "manifest.json").read_bytes() == committed
    assert load_manifest(CORE_SET).count == 120
    assert (tmp_path / "out" / "partial-gold" / CORE_SET / "manifest.json").exists()
