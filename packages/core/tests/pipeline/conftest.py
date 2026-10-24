"""Pipeline integration fixtures: a migrated throwaway Postgres, the real guardrails, extractor,
calibrator and rules engine, and fakes for everything external (LLM, accounting, storage,
events). Documents are described by a :class:`DocSpec`; a stub preprocessor turns the spec into
the ``PreprocessedDocument`` the real screeners and extractor read (OCR words carry the field
values so calibration sees real OCR agreement)."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from invoicedesk.config.models import ModelRole
from invoicedesk.config.settings import REPO_ROOT
from invoicedesk.config.tenant import load_rules_config
from invoicedesk.contracts.extraction import (
    DATE_FIELDS,
    HEADER_FIELDS,
    MONEY_FIELDS,
    InvoiceExtraction,
)
from invoicedesk.contracts.llm import LLMRequest
from invoicedesk.contracts.preprocess import OcrWord, PageImage, PreprocessedDocument, TextLayerSpan
from invoicedesk.db import repo
from invoicedesk.db.connection import Database
from invoicedesk.fakes import FakeAccounting, FakeLLM, InMemoryEventPublisher
from invoicedesk.money import format_cents
from invoicedesk.pipeline.deps import PipelineDeps, build_deps
from invoicedesk.pipeline.graph import PipelineRunner, postgres_checkpointer
from invoicedesk.pipeline.services import PipelineServices
from invoicedesk.remit import remit_fingerprint
from invoicedesk.storage import InMemoryStorage
from PIL import Image
from psycopg import sql

TODAY = dt.date(2026, 10, 24)
MASTER_REMIT = "Routing 021000021 Acct 000123456789"
ATTACKER_REMIT = "Routing 026009593 Acct 000999888777"
_EXTRACT_ROLES = {ModelRole.EXTRACT_T1, ModelRole.EXTRACT_T2, ModelRole.ADJUDICATE_T3}


def _white_png(w: int = 1237, h: int = 1600) -> bytes:
    buf = io.BytesIO()
    Image.new("L", (w, h), 255).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


_PNG = _white_png()


@dataclass
class DocSpec:
    extraction: InvoiceExtraction
    words: list[OcrWord]
    text_layer: list[TextLayerSpan] = field(default_factory=list)
    verdict: dict[str, Any] = field(
        default_factory=lambda: {"verdict": "clean", "categories": ["none"], "spans": []}
    )
    naive_extraction: InvoiceExtraction | None = None
    refuse_extraction: bool = False
    phash: str = "0" * 16


def _display(name: str, f: Any) -> str | None:
    if name in MONEY_FIELDS:
        return None if f.value_cents is None else format_cents(f.value_cents)
    if name in DATE_FIELDS:
        return f.value_iso
    return f.value


def with_ocr_words(ex: InvoiceExtraction) -> tuple[InvoiceExtraction, list[OcrWord]]:
    """OCR words that print every header value, and the extraction citing them."""
    data = ex.model_dump(mode="json")
    words: list[OcrWord] = []
    y = 60
    for name in HEADER_FIELDS:
        if name in ("doc_type", "remit_to_bank_details"):
            continue
        text = _display(name, getattr(ex, name))
        if not text:
            continue
        ids: list[int] = []
        x = 80
        for tok in text.split():
            wid = len(words)
            words.append(
                OcrWord(id=wid, page=0, text=tok, box=(x, y, x + 12 * len(tok), y + 18), conf=0.98)
            )
            ids.append(wid)
            x += 12 * len(tok) + 10
        data[name]["source"]["word_ids"] = ids
        y += 40
    return InvoiceExtraction.model_validate(data), words


class StubPreprocessor:
    """Returns the registered spec's document; stores a white page render like the real one."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[str] = []

    def preprocess(
        self, data: bytes, *, mime: str, document_id: str, filename: str | None = None
    ) -> PreprocessedDocument:
        self.calls.append(document_id)
        spec = self.world.specs[document_id]
        key = f"maple_row/renders/{document_id}/page-0.png"
        self.world.storage.put(key, _PNG, content_type="image/png")
        return PreprocessedDocument(
            document_id=document_id,
            sha256=hashlib.sha256(data).hexdigest(),
            mime=mime,
            is_native_pdf=bool(spec.text_layer),
            pages=[
                PageImage(
                    index=0,
                    width=1237,
                    height=1600,
                    storage_key=key,
                    sha256=hashlib.sha256(_PNG).hexdigest(),
                    scale=1.0,
                )
            ],
            words=spec.words,
            enhanced_words=spec.words,
            text_layer=spec.text_layer,
            phash=spec.phash,
            ocr_engine="stub",
        )


class World:
    TODAY = TODAY
    MASTER_REMIT = MASTER_REMIT
    ATTACKER_REMIT = ATTACKER_REMIT

    @staticmethod
    def hidden_white_text(text: str) -> list[TextLayerSpan]:
        return hidden_white_text(text)

    def __init__(self, db: Database, dsn: str) -> None:
        self.db = db
        self.dsn = dsn
        self.storage = InMemoryStorage()
        self.specs: dict[str, DocSpec] = {}
        self.llm = FakeLLM(self._respond)
        self.publisher = InMemoryEventPublisher()
        self.accounting = FakeAccounting()
        self.preprocessor = StubPreprocessor(self)
        self.deps: PipelineDeps = build_deps(
            db,
            llm=self.llm,
            storage=self.storage,
            publisher=self.publisher,
            accounting=self.accounting,
            preprocessor=self.preprocessor,
            rules_config=load_rules_config("maple_row", REPO_ROOT / "tenants"),
            today=lambda: TODAY,
        )
        self._seed()

    # ---------------------------------------------------------------- fake LLM
    def _respond(self, req: LLMRequest) -> dict[str, Any] | None:
        doc_id = req.metadata.get("document_id", "")
        spec = self.specs[doc_id]
        if req.role == ModelRole.INJECTION_CLASSIFIER:
            return spec.verdict
        if req.metadata.get("pipeline") == "naive":
            return (spec.naive_extraction or spec.extraction).model_dump(mode="json")
        if req.role in _EXTRACT_ROLES:
            if spec.refuse_extraction:
                return None
            return spec.extraction.model_dump(mode="json")
        raise AssertionError(f"unexpected role {req.role}")

    def calls(self, document_id: str, roles: set[ModelRole] | None = None) -> list[LLMRequest]:
        return [
            c
            for c in self.llm.calls
            if c.metadata.get("document_id") == document_id and (roles is None or c.role in roles)
        ]

    def extraction_calls(self, document_id: str) -> list[LLMRequest]:
        return [
            c
            for c in self.calls(document_id, _EXTRACT_ROLES)
            if c.metadata.get("pipeline") != "naive"
        ]

    # ---------------------------------------------------------------- business universe
    def _seed(self) -> None:
        with self.db.tx() as conn:
            repo.ensure_tenant(conn, "maple_row", "Maple Row Property Management")
            self.clerk = repo.create_staff_user(
                conn, tenant_id="maple_row", email="clerk@maplerow.test", display_name="Clerk"
            )
            self.approver_a = repo.create_staff_user(
                conn,
                tenant_id="maple_row",
                email="ann@maplerow.test",
                display_name="Ann Approver",
                role="approver",
            )
            self.approver_b = repo.create_staff_user(
                conn,
                tenant_id="maple_row",
                email="bo@maplerow.test",
                display_name="Bo Owner",
                role="owner",
            )
            prop = conn.execute(
                "insert into properties (tenant_id, name, address) values"
                " ('maple_row', 'Alder Court', '40 Alder Ct, Springfield') returning id"
            ).fetchone()
            assert prop is not None
            unit = conn.execute(
                "insert into units (property_id, label) values (%s, '12B') returning id",
                (prop["id"],),
            ).fetchone()
            assert unit is not None
            v = conn.execute(
                "insert into vendors (tenant_id, legal_name, aliases, address, tax_id_last4,"
                " remit_to_hash, bank_last4, phone_on_file, email, qbo_vendor_ref, status)"
                " values ('maple_row', 'Rapid Rooter Plumbing LLC', '{Rapid Rooter}',"
                " '118 Alder Way, Springfield', '4821', %s, '6789', '555-0100',"
                " 'billing@rapidrooter.test', '101', 'approved') returning id",
                (remit_fingerprint(MASTER_REMIT),),
            ).fetchone()
            assert v is not None
            self.vendor_id = str(v["id"])
            for wo, est in (("WO-1187", 115_000), ("WO-2000", 600_000)):
                conn.execute(
                    "insert into work_orders (tenant_id, wo_number, unit_id, vendor_id, scope,"
                    " status, approved_estimate_cents) values ('maple_row', %s, %s, %s,"
                    " 'Kitchen drain repair', 'open', %s)",
                    (wo, unit["id"], v["id"], est),
                )
            conn.execute(
                "insert into historical_bills (tenant_id, vendor_id, invoice_number,"
                " invoice_date, total_cents) values ('maple_row', %s, 'INV-2231',"
                " '2026-08-01', 124000)",
                (v["id"],),
            )

    # ---------------------------------------------------------------- documents
    def batch(self, label: str = "test") -> str:
        with self.db.tx() as conn:
            return repo.create_batch(conn, tenant_id="maple_row", label=label)

    def add_document(
        self,
        extraction: InvoiceExtraction,
        *,
        text_layer: list[TextLayerSpan] | None = None,
        verdict: dict[str, Any] | None = None,
        naive_extraction: InvoiceExtraction | None = None,
        refuse_extraction: bool = False,
        batch_id: str | None = None,
        session_id: str | None = None,
        uploaded_by: str | None = "clerk",
        filename: str = "invoice.pdf",
        email_body: str | None = None,
        phash: str | None = None,
    ) -> str:
        ex, words = with_ocr_words(extraction)
        data = f"original-{uuid.uuid4()}".encode()
        sha = hashlib.sha256(data).hexdigest()
        with self.db.tx() as conn:
            doc_id = repo.insert_document(
                conn,
                tenant_id="maple_row",
                source="upload",
                sha256=sha,
                storage_path="pending",
                mime="application/pdf",
                filename=filename,
                batch_id=batch_id,
                session_id=session_id,
                uploaded_by=self.clerk if uploaded_by == "clerk" else uploaded_by,
                purge_after=dt.datetime.now(dt.UTC) + dt.timedelta(hours=24)
                if session_id
                else None,
            )
            key = f"maple_row/originals/{doc_id}/{filename}"
            conn.execute("update documents set storage_path = %s where id = %s", (key, doc_id))
            if email_body is not None:
                t = conn.execute(
                    "insert into email_threads (tenant_id, message_id, from_addr, subject,"
                    " body_text) values ('maple_row', %s, 'billing@rapidrooter.test', 'Invoice',"
                    " %s) returning id",
                    (f"<{doc_id}@test>", email_body),
                ).fetchone()
                assert t is not None
                conn.execute(
                    "insert into document_threads (document_id, thread_id) values (%s, %s)",
                    (doc_id, t["id"]),
                )
        self.storage.put(key, data, content_type="application/pdf")
        spec = DocSpec(
            extraction=ex,
            words=words,
            text_layer=text_layer or [],
            naive_extraction=naive_extraction,
            refuse_extraction=refuse_extraction,
            phash=phash or uuid.uuid4().hex[:16],
        )
        if verdict is not None:
            spec.verdict = verdict
        self.specs[doc_id] = spec
        return doc_id

    # ---------------------------------------------------------------- queries
    def status(self, document_id: str) -> str:
        with self.db.tx() as conn:
            return repo.get_document(conn, document_id).status.value

    def count(self, table: str, document_id: str) -> int:
        query = sql.SQL("select count(*) as n from {} where document_id = %s").format(
            sql.Identifier(table)
        )
        with self.db.tx() as conn:
            row = conn.execute(query, (document_id,)).fetchone()
        assert row is not None
        return int(row["n"])

    def statuses(self, document_id: str) -> list[str]:
        with self.db.tx() as conn:
            rows = conn.execute(
                "select after_json->>'status' as s from audit_log where document_id = %s"
                " and action = 'status.changed' order by id",
                (document_id,),
            ).fetchall()
        return [r["s"] for r in rows]

    def failed_rules(self, document_id: str) -> dict[str, str]:
        with self.db.tx() as conn:
            rows = conn.execute(
                "select rule_id, message from rule_results where document_id = %s and not passed"
                " and evaluation = (select max(evaluation) from rule_results"
                " where document_id = %s)",
                (document_id, document_id),
            ).fetchall()
        return {r["rule_id"]: r["message"] for r in rows}

    def audit_ok(self) -> bool:
        from invoicedesk.audit import verify

        with self.db.tx() as conn:
            ok, bad = verify(conn)
        return ok and bad is None


@pytest.fixture
def world(pg_db: Database, pg_dsn: str) -> World:
    return World(pg_db, pg_dsn)


@pytest.fixture
def saver(pg_dsn: str) -> Iterator[Any]:
    with postgres_checkpointer(pg_dsn, max_size=4) as s:
        yield s


@pytest.fixture
def runner(world: World, saver: Any) -> PipelineRunner:
    return PipelineRunner(world.deps, saver)


@pytest.fixture
def services(world: World, runner: PipelineRunner) -> PipelineServices:
    return PipelineServices(world.deps, runner=runner)


def hidden_white_text(text: str) -> list[TextLayerSpan]:
    return [
        TextLayerSpan(
            page=0,
            text=text,
            box=(80, 1500, 1100, 1514),
            font_size_pt=9.0,
            fill_rgb=(255, 255, 255),
            background_rgb=(255, 255, 255),
        )
    ]
