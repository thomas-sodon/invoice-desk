"""Everything the pipeline needs, assembled once per process (composition for the graph).

:func:`build_deps` wires the real components (preprocessor, guardrail screeners, tiered
extractor, rules engine) around an *injected* ``LLMClient``: ``FakeLLM`` in tests, the data
component's ``OracleLLM`` in the offline demo (passed in by the API, never imported here),
``wiring.llm_client`` (replay offline / Anthropic live) otherwise.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from invoicedesk.config.settings import Settings, get_settings
from invoicedesk.config.tenant import RulesConfig, load_rules_config
from invoicedesk.contracts.accounting import AccountingAdapter
from invoicedesk.contracts.extractor import Extractor
from invoicedesk.contracts.infra import EventPublisher, Storage
from invoicedesk.contracts.llm import LLMClient
from invoicedesk.contracts.preprocess import Preprocessor
from invoicedesk.contracts.rules import RulesEngine
from invoicedesk.contracts.screening import Screener
from invoicedesk.db.connection import Database

__all__ = ["DEFAULT_EXPENSE_ACCOUNT_REF", "PipelineDeps", "build_deps", "utc_today"]

#: QBO expense account for bill lines when the tenant has no mapping (env override).
DEFAULT_EXPENSE_ACCOUNT_REF = "7"


def utc_today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


@dataclass
class PipelineDeps:
    db: Database
    storage: Storage
    llm: LLMClient
    preprocessor: Preprocessor
    screeners: list[Screener]
    extractor: Extractor
    rules_engine: RulesEngine
    #: Rules config per tenant id; loaded lazily from ``tenants/<t>/rules.yaml``.
    rules_configs: dict[str, RulesConfig] = field(default_factory=dict)
    publisher: EventPublisher | None = None
    #: Only used when a document is processed with ``auto_post=True``.
    accounting: AccountingAdapter | None = None
    settings: Settings = field(default_factory=get_settings)
    today: Callable[[], dt.date] = utc_today
    max_tier: Literal["haiku", "sonnet", "opus"] = "opus"
    expense_account_ref: str = field(
        default_factory=lambda: os.environ.get(
            "QBO_EXPENSE_ACCOUNT_REF", DEFAULT_EXPENSE_ACCOUNT_REF
        )
    )

    def rules_config(self, tenant_id: str) -> RulesConfig:
        cfg = self.rules_configs.get(tenant_id)
        if cfg is None:
            cfg = load_rules_config(tenant_id, self.settings.tenants_dir)
            self.rules_configs[tenant_id] = cfg
        return cfg


def build_deps(
    db: Database,
    *,
    llm: LLMClient | None = None,
    storage: Storage | None = None,
    settings: Settings | None = None,
    publisher: EventPublisher | None = None,
    accounting: AccountingAdapter | None = None,
    preprocessor: Preprocessor | None = None,
    screeners: list[Screener] | None = None,
    extractor: Extractor | None = None,
    rules_engine: RulesEngine | None = None,
    rules_config: RulesConfig | None = None,
    today: Callable[[], dt.date] | None = None,
) -> PipelineDeps:
    """Real components around the injected externals. Every argument may be overridden."""
    from invoicedesk import wiring
    from invoicedesk.calibrate import load_calibrator
    from invoicedesk.extract import build_extractor
    from invoicedesk.guardrails import build_screeners
    from invoicedesk.preprocess import build_preprocessor
    from invoicedesk.rules import build_rules_engine

    s = settings or get_settings()
    st = storage or wiring.storage(s)
    client = llm or wiring.llm_client(s)
    configs: dict[str, RulesConfig] = {}
    if rules_config is not None:
        configs[s.tenant] = rules_config
    cfg_for_extractor = rules_config or load_rules_config(s.tenant, s.tenants_dir)
    return PipelineDeps(
        db=db,
        storage=st,
        llm=client,
        preprocessor=preprocessor or build_preprocessor(st, key_prefix=s.tenant),
        screeners=screeners if screeners is not None else build_screeners(client, storage=st),
        extractor=extractor
        or build_extractor(
            client,
            calibrator=load_calibrator(),
            storage=st,
            confidence=cfg_for_extractor.confidence,
            rounding_slack_cents=cfg_for_extractor.arithmetic.rounding_slack_cents,
        ),
        rules_engine=rules_engine or build_rules_engine(),
        rules_configs=configs,
        publisher=publisher,
        accounting=accounting,
        settings=s,
        today=today or utc_today,
    )
