from pathlib import Path

import pytest
from invoicedesk import cli, wiring
from invoicedesk.config.settings import Settings
from invoicedesk.fakes import FakeAccounting, InMemoryEventPublisher, ReplayLLM
from invoicedesk.storage import InMemoryStorage, LocalStorage


def _settings(tmp_path: Path, **kw: object) -> Settings:
    base: dict[str, object] = {
        "INVOICEDESK_MODE": "offline",
        "STORAGE_LOCAL_DIR": tmp_path,
        "LLM_REPLAY_DIR": tmp_path / "replay",
    }
    base.update(kw)
    return Settings.model_validate(base)


def test_offline_wiring_uses_fakes(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    assert s.mode == "offline" and s.tenant == "maple_row"
    assert isinstance(wiring.llm_client(s), ReplayLLM)
    assert isinstance(wiring.accounting_adapter(s), FakeAccounting)
    assert isinstance(wiring.event_publisher(s), InMemoryEventPublisher)
    assert isinstance(wiring.storage(s), LocalStorage)
    assert isinstance(
        wiring.storage(_settings(tmp_path, STORAGE_BACKEND="memory")), InMemoryStorage
    )


def test_live_requires_key(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        wiring.llm_client(_settings(tmp_path, INVOICEDESK_MODE="live"))


def test_cli_help_and_unknown(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0
    assert "migrate" in capsys.readouterr().out
    assert cli.main([]) == 2
    assert cli.main(["nope"]) == 2


def test_cli_missing_component_module() -> None:
    # The stub packages exist but their cli modules are not built yet.
    assert cli.main(["validate"]) == 3
