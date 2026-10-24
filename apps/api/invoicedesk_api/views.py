"""Read models: documents as board cards and detail views (docs/api-contract.md shapes).

All free text leaving here passes through :func:`invoicedesk.masking.mask_text` (bank and
account numbers to the last 4, invariant 9). The ``fields`` table already stores
``remit_to_bank_details`` masked; anonymous (try-it, public demo) callers do not get that field
at all, nor the original file, trace link or approvals (contract, Try-it).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from decimal import Decimal
from functools import lru_cache
from typing import Any

from invoicedesk.config.tenant import RulesConfig, load_rules_config
from invoicedesk.contracts.extraction import DATE_FIELDS, MONEY_FIELDS
from invoicedesk.db.connection import Conn
from invoicedesk.masking import mask_bank_text, mask_payload, mask_text
from invoicedesk.money import format_cents
from invoicedesk.rules.engine import ALL_RULES
from invoicedesk.status import DocStatus, kanban_column

from invoicedesk_api import schemas as sc
from invoicedesk_api.runtime import Runtime

__all__ = [
    "RULE_NAMES",
    "build_cards",
    "build_detail",
    "iso",
    "latest_rule_rows",
    "rule_result_out",
    "rules_config",
    "usd",
]

RULE_NAMES: dict[str, str] = {r.rule_id: r.name for r in ALL_RULES}
TIER_NUMBER = {"haiku": 1, "sonnet": 2, "opus": 3}
FIELD_ORDER: tuple[str, ...] = (
    "vendor_name",
    "vendor_address",
    "vendor_tax_id_last4",
    "invoice_number",
    "invoice_date",
    "due_date",
    "work_order_number",
    "property_or_unit",
    "subtotal",
    "tax",
    "total",
    "remit_to_bank_details",
)
#: Internal bookkeeping fields that are not shown as extracted header fields.
_HIDDEN_FIELDS = frozenset({"vendor_id"})
_BADGE_ORDER = (
    "duplicate",
    "bank_change",
    "hidden_text",
    "faint_text",
    "injection",
    "lookalike",
    "over_cap",
    "new_vendor",
    "over_estimate",
    "arithmetic",
    "date",
    "low_confidence",
    "handwriting",
)
_DETECTOR_HEADLINE = {
    "hidden_text": "hidden text found in the PDF",
    "faint_text": "faint text found in the photo",
    "injection_classifier": "instructions aimed at the AI found",
}


def iso(value: dt.datetime | dt.date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC).isoformat()
    return value.isoformat()


def usd(value: Any) -> str:
    d = Decimal(str(value or 0))
    if d == 0:
        return "0"
    return format(d.normalize(), "f")


@lru_cache(maxsize=8)
def _rules_config_cached(tenant: str, tenants_dir: str) -> RulesConfig:
    from pathlib import Path

    return load_rules_config(tenant, Path(tenants_dir))


def rules_config(rt: Runtime) -> RulesConfig:
    return _rules_config_cached(rt.tenant, str(rt.settings.tenants_dir))


def _rule_sort_key(rule_id: str) -> int:
    try:
        return int(rule_id.lstrip("R"))
    except ValueError:
        return 99


# ---------------------------------------------------------------------------- loaders
def _fields_by_doc(conn: Conn, ids: Sequence[str]) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {i: {} for i in ids}
    for r in conn.execute(
        "select document_id, name, value, raw_conf, calibrated_conf, word_ids, box, page"
        " from fields where final and document_id = any(%s::uuid[])",
        (list(ids),),
    ).fetchall():
        out[str(r["document_id"])][r["name"]] = dict(r)
    return out


def latest_rule_rows(conn: Conn, ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
    for r in conn.execute(
        "select r.document_id, r.rule_id, r.passed, r.severity, r.message, r.details_json"
        " from rule_results r where r.document_id = any(%s::uuid[]) and r.evaluation ="
        " (select max(r2.evaluation) from rule_results r2 where r2.document_id = r.document_id)",
        (list(ids),),
    ).fetchall():
        out[str(r["document_id"])].append(dict(r))
    for rows in out.values():
        rows.sort(key=lambda x: _rule_sort_key(x["rule_id"]))
    return out


def _screens_by_doc(conn: Conn, ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
    for r in conn.execute(
        "select document_id, detector, verdict, categories, spans_json from screen_results"
        " where document_id = any(%s::uuid[]) order by created_at, id",
        (list(ids),),
    ).fetchall():
        out[str(r["document_id"])].append(dict(r))
    return out


def _step_bits(conn: Conn, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Cheap per-document bits of the stored pipeline steps (thumbnail key, handwriting)."""
    out: dict[str, dict[str, Any]] = {i: {} for i in ids}
    for r in conn.execute(
        "select document_id, step,"
        " output_json->'doc'->'pages'->0->>'storage_key' as thumb,"
        " output_json->'extraction'->>'handwriting_present' as handwriting"
        " from pipeline_steps where round = 0 and step in ('preprocess', 'extract')"
        " and document_id = any(%s::uuid[])",
        (list(ids),),
    ).fetchall():
        bits = out[str(r["document_id"])]
        if r["step"] == "preprocess":
            bits["thumb"] = r["thumb"]
        else:
            bits["handwriting"] = r["handwriting"] == "true"
    return out


# ---------------------------------------------------------------------------- card logic
def _int_or_none(v: str | None) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _badges(
    rules: Iterable[dict[str, Any]], screens: Iterable[dict[str, Any]], handwriting: bool
) -> list[str]:
    found: set[str] = set()
    for r in rules:
        if not r["passed"]:
            badge = (r["details_json"] or {}).get("badge")
            if isinstance(badge, str) and badge:
                found.add(badge)
    for s in screens:
        if s["detector"] in ("hidden_text", "faint_text") and s["verdict"] != "clean":
            found.add(s["detector"])
        if s["detector"] == "injection_classifier" and s["verdict"] == "malicious":
            found.add("injection")
        if "bank_or_remit_change" in (s["categories"] or []):
            found.add("bank_change")
    if handwriting:
        found.add("handwriting")
    order = {b: i for i, b in enumerate(_BADGE_ORDER)}
    return sorted(found, key=lambda b: (order.get(b, len(order)), b))


_FLAG_PRIORITY = {"R6": 0, "R10": 1, "R4": 2}
TOTAL_UNREADABLE_HEADLINE = "Total unreadable: please confirm"
HIDDEN_BANK_CHANGE_HEADLINE = "Hidden instruction detected: attempted bank-account change, blocked"


def _total_unreadable(rules: Sequence[dict[str, Any]], fields: dict[str, dict[str, Any]]) -> bool:
    """R9 holds the total and no total was read: the arithmetic (R5) and cap (R7) holds that
    follow are consequences, so the card asks for the total itself."""
    for r in rules:
        if r["rule_id"] == "R9" and not r["passed"]:
            held = (r["details_json"] or {}).get("fields") or []
            return "total" in held and fields.get("total", {}).get("value") is None
    return False


def _headline(
    status: DocStatus,
    rules: Sequence[dict[str, Any]],
    screens: Sequence[dict[str, Any]],
    fields: dict[str, dict[str, Any]] | None = None,
) -> str | None:
    failed = [r for r in rules if not r["passed"]]
    flags = [r for r in failed if r["severity"] == "flag"]
    if not flags and _total_unreadable(failed, fields or {}):
        return TOTAL_UNREADABLE_HEADLINE
    # the money-risk flag the reviewer must act on first: bank change, then injection,
    # then duplicate; otherwise rule order
    pick = sorted(flags, key=lambda r: _FLAG_PRIORITY.get(r["rule_id"], 9)) or failed
    if pick:
        details = pick[0]["details_json"] or {}
        text = details.get("headline") if isinstance(details.get("headline"), str) else None
        return mask_text(text or str(pick[0]["message"]).rstrip("."))
    noisy = [s["detector"] for s in screens if s["verdict"] != "clean"]
    if noisy and status in (DocStatus.FLAGGED, DocStatus.QUARANTINED, DocStatus.REJECTED):
        cats = {
            str(c) for s in screens if s["verdict"] != "clean" for c in s.get("categories") or []
        }
        if "bank_or_remit_change" in cats:
            return HIDDEN_BANK_CHANGE_HEADLINE
        parts = [_DETECTOR_HEADLINE.get(str(d), str(d)) for d in dict.fromkeys(noisy)]
        return "Quarantined: " + "; ".join(parts)
    return None


def _signed(rt: Runtime, key: str | None, ttl: int = 3600) -> str | None:
    if not key:
        return None
    try:
        return rt.storage.signed_url(key, expires_in_s=ttl)
    except (ValueError, KeyError, OSError):
        return None


def _card(
    rt: Runtime,
    *,
    doc: dict[str, Any],
    fields: dict[str, dict[str, Any]],
    rules: list[dict[str, Any]],
    screens: list[dict[str, Any]],
    bits: dict[str, Any],
) -> sc.DocumentCard:
    status = DocStatus(doc["status"])
    vendor = fields.get("vendor_name", {}).get("value")
    number = fields.get("invoice_number", {}).get("value")
    return sc.DocumentCard(
        id=str(doc["id"]),
        batch_id=None if doc["batch_id"] is None else str(doc["batch_id"]),
        filename=mask_text(doc["filename"]) or "invoice",
        source=doc["source"],
        status=status.value,
        column=kanban_column(status),
        vendor_name=mask_text(vendor),
        invoice_number=mask_text(number),
        total_cents=_int_or_none(fields.get("total", {}).get("value")),
        headline=_headline(status, rules, screens, fields),
        badges=_badges(rules, screens, bool(bits.get("handwriting"))),
        cost_usd=usd(doc["total_cost_usd"]),
        thumbnail_url=_signed(rt, bits.get("thumb")),
        created_at=iso(doc["created_at"]) or "",
        updated_at=iso(doc["updated_at"]) or "",
    )


def build_cards(conn: Conn, rt: Runtime, docs: Sequence[dict[str, Any]]) -> list[sc.DocumentCard]:
    ids = [str(d["id"]) for d in docs]
    if not ids:
        return []
    fields = _fields_by_doc(conn, ids)
    rules = latest_rule_rows(conn, ids)
    screens = _screens_by_doc(conn, ids)
    bits = _step_bits(conn, ids)
    return [
        _card(rt, doc=d, fields=fields[i], rules=rules[i], screens=screens[i], bits=bits[i])
        for d, i in zip(docs, ids, strict=True)
    ]


# ---------------------------------------------------------------------------- detail
#: Rule ``details`` keys that carry remit-to bank details (masked); withheld from anonymous
#: callers along with the ``remit_to_bank_details`` field (contract, Conventions).
_REMIT_DETAIL_KEYS = frozenset({"remit_to_masked"})


def rule_result_out(row: dict[str, Any], *, anonymous: bool = False) -> sc.RuleResultOut:
    details = dict(mask_payload(row["details_json"] or {}))
    if anonymous:
        for k in _REMIT_DETAIL_KEYS:
            details.pop(k, None)
    fields = details.pop("fields", [])
    return sc.RuleResultOut(
        rule_id=row["rule_id"],
        name=str(RULE_NAMES.get(row["rule_id"]) or row["rule_id"]),
        passed=bool(row["passed"]),
        severity=row["severity"],
        message=mask_text(row["message"]) or "",
        fields=[str(f) for f in fields] if isinstance(fields, list) else [],
        details=details,
    )


#: Fields that hold bank details: every token with 5+ digits is masked, not only 6+ runs.
BANK_FIELDS = frozenset({"remit_to_bank_details"})


def _display(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if name in MONEY_FIELDS:
        cents = _int_or_none(value)
        return format_cents(cents) if cents is not None else mask_text(value)
    if name in DATE_FIELDS:
        return value
    if name in BANK_FIELDS:
        return mask_bank_text(value)
    return mask_text(value)


def _value(name: str, value: str | None) -> str | None:
    """Money values are integer cents (``"124000"``), not account numbers: keep them exact."""
    if value is not None and name in MONEY_FIELDS and _int_or_none(value) is not None:
        return value
    if name in BANK_FIELDS:
        return mask_bank_text(value)
    return mask_text(value)


def _box(v: Any) -> tuple[int, int, int, int] | None:
    if isinstance(v, (list, tuple)) and len(v) == 4:
        return (int(v[0]), int(v[1]), int(v[2]), int(v[3]))
    return None


def _field_out(
    name: str, row: dict[str, Any], cfg: RulesConfig, rules: Sequence[dict[str, Any]]
) -> sc.Field_:
    conf = row.get("calibrated_conf")
    citing = [
        r
        for r in rules
        if not r["passed"] and name in ((r["details_json"] or {}).get("fields") or [])
    ]
    flagged = any(r["severity"] == "flag" for r in citing)
    threshold = cfg.confidence.threshold(name)
    if conf is None or conf < 0.5 or flagged:
        band = "red"
    elif citing or conf < threshold:
        band = "amber"
    else:
        band = "green"
    reason: str | None = None
    if citing:
        details = citing[0]["details_json"] or {}
        head = details.get("headline")
        reason = mask_text(head if isinstance(head, str) and head else citing[0]["message"])
    elif band != "green":
        reason = "Low confidence: please check this field"
    return sc.Field_(
        name=name,
        value=_value(name, row.get("value")),
        display_value=_display(name, row.get("value")),
        raw_conf=row.get("raw_conf"),
        calibrated_conf=conf,
        band=band,  # type: ignore[arg-type]
        page=row.get("page"),
        box=_box(row.get("box")),
        word_ids=list(row.get("word_ids") or []),
        reason=reason,
    )


def _word_index(doc_json: dict[str, Any] | None) -> dict[int, tuple[int, tuple[int, ...]]]:
    if not doc_json:
        return {}
    idx: dict[int, tuple[int, tuple[int, ...]]] = {}
    for w in [*(doc_json.get("words") or []), *(doc_json.get("enhanced_words") or [])]:
        try:
            idx[int(w["id"])] = (int(w["page"]), tuple(int(x) for x in w["box"]))
        except (KeyError, TypeError, ValueError):
            continue
    return idx


def _ref_box(
    ref: dict[str, Any] | None, words: dict[int, tuple[int, tuple[int, ...]]]
) -> tuple[tuple[int, int, int, int] | None, int | None]:
    if not ref:
        return None, None
    hits = [words[i] for i in ref.get("word_ids") or [] if i in words]
    if hits:
        page = hits[0][0]
        boxes = [b for p, b in hits if p == page]
        return (
            (
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            ),
            page,
        )
    return _box(ref.get("fallback_box")), (0 if ref.get("fallback_box") else None)


def _line_items(
    extraction: dict[str, Any] | None, words: dict[int, tuple[int, tuple[int, ...]]]
) -> list[sc.LineItemOut]:
    out: list[sc.LineItemOut] = []
    for li in (extraction or {}).get("line_items") or []:
        desc = li.get("description") or {}
        box, page = _ref_box(desc.get("source"), words)
        if box is None:
            box, page = _ref_box((li.get("amount") or {}).get("source"), words)
        out.append(
            sc.LineItemOut(
                description=mask_text(desc.get("value")),
                quantity=mask_text((li.get("quantity") or {}).get("value")),
                unit_price_cents=(li.get("unit_price") or {}).get("value_cents"),
                amount_cents=(li.get("amount") or {}).get("value_cents"),
                box=box,
                page=page,
            )
        )
    return out


def _screen_out(row: dict[str, Any]) -> sc.ScreenResultOut:
    spans = []
    for s in row["spans_json"] or []:
        spans.append(
            sc.ScreenSpanOut(
                text=mask_text(str(s.get("text", ""))) or "",
                reason=mask_text(str(s.get("reason", ""))) or "",
                page=s.get("page"),
                box=_box(s.get("box")),
                source=str(s.get("source", "ocr")),
            )
        )
    return sc.ScreenResultOut(
        detector=row["detector"],
        verdict=row["verdict"],
        categories=list(row["categories"] or []),
        spans=spans,
    )


def followup_out(row: dict[str, Any]) -> sc.Followup:
    return sc.Followup(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        to_addr=row["to_addr"] or "",
        draft_subject=mask_text(row["draft_subject"]) or "",
        draft_body=mask_text(row["draft_body"]) or "",
        approved_by=None if row["approved_by"] is None else str(row["approved_by"]),
        sent_at=iso(row["sent_at"]),
    )


def approvals_for(conn: Conn, document_id: str) -> list[sc.ApprovalOut]:
    rows = conn.execute(
        "select a.kind::text as kind, a.approver_id, a.decision::text as decision,"
        " a.callback_attested, a.created_at, s.display_name from approvals a"
        " left join staff_users s on s.id = a.approver_id where a.document_id = %s"
        " order by a.created_at, a.id",
        (document_id,),
    ).fetchall()
    return [
        sc.ApprovalOut(
            kind=r["kind"],
            approver_id=str(r["approver_id"]),
            decision=r["decision"],
            callback_attested=bool(r["callback_attested"]),
            created_at=iso(r["created_at"]) or "",
            approver_name=r["display_name"],
        )
        for r in rows
    ]


def _approvals_required(
    status: DocStatus, rules: Sequence[dict[str, Any]]
) -> sc.ApprovalsRequired | None:
    if status is DocStatus.FLAGGED:
        return sc.ApprovalsRequired(count=2, callback=True)
    if status is DocStatus.NEEDS_REVIEW:
        failed = {r["rule_id"] for r in rules if not r["passed"]}
        if failed & {"R1", "R7"}:
            return sc.ApprovalsRequired(count=1, callback=False)
    return None


def _trace_url(rt: Runtime, conn: Conn, document_id: str) -> str | None:
    host = rt.settings.langfuse_host
    if not host:
        return None
    row = conn.execute(
        "select trace_id from extractions where document_id = %s and trace_id is not null"
        " order by created_at desc limit 1",
        (document_id,),
    ).fetchone()
    if row is None:
        return None
    return f"{host.rstrip('/')}/trace/{row['trace_id']}"


def _redactions(
    fields: dict[str, dict[str, Any]],
    screens: Sequence[dict[str, Any]],
    *,
    anonymous: bool = False,
) -> list[sc.Redaction]:
    """Page regions printing bank/account numbers; the viewer paints a bar over each."""
    out: list[sc.Redaction] = []
    remit = fields.get("remit_to_bank_details")
    if remit and _box(remit.get("box")) is not None:
        out.append(
            sc.Redaction(
                page=int(remit.get("page") or 0),
                box=_box(remit["box"]),  # type: ignore[arg-type]
                label=None if anonymous else mask_bank_text(remit.get("value")),
            )
        )
    for s in screens:
        for span in s["spans_json"] or []:
            box = _box(span.get("box"))
            if box is not None and "•" in str(span.get("text", "")):
                out.append(sc.Redaction(page=int(span.get("page") or 0), box=box, label=None))
    return out


def build_detail(
    conn: Conn, rt: Runtime, doc: dict[str, Any], *, anonymous: bool
) -> sc.DocumentDetail:
    from invoicedesk.pipeline.steps import load_step_conn

    doc_id = str(doc["id"])
    card = build_cards(conn, rt, [doc])[0]
    fields = _fields_by_doc(conn, [doc_id])[doc_id]
    rules = latest_rule_rows(conn, [doc_id])[doc_id]
    screens = _screens_by_doc(conn, [doc_id])[doc_id]
    pre = load_step_conn(conn, doc_id, "preprocess") or {}
    ext = load_step_conn(conn, doc_id, "extract") or {}
    doc_json = pre.get("doc")
    words = _word_index(doc_json)
    cfg = rules_config(rt)
    status = DocStatus(doc["status"])

    shown = [
        n
        for n in fields
        if n not in _HIDDEN_FIELDS and not (anonymous and n == "remit_to_bank_details")
    ]
    order = FIELD_ORDER
    shown.sort(key=lambda n: order.index(n) if n in order else len(order))
    pages = [
        sc.PageOut(
            index=int(p["index"]),
            width=int(p["width"]),
            height=int(p["height"]),
            image_url=_signed(rt, p.get("storage_key")) or "",
        )
        for p in (doc_json or {}).get("pages") or []
    ]
    extraction = ext.get("extraction")
    extractions = [
        sc.ExtractionRunOut(
            tier=TIER_NUMBER.get(str(r["tier"]), 0),
            model_id=r["model_id"],
            cost_usd=usd(r["cost_usd"]),
            latency_ms=int(r["latency_ms"]),
            refused=bool(r["refused"]),
            cache_read_tokens=int(r["cache_read_tokens"]),
        )
        for r in conn.execute(
            "select tier::text as tier, model_id, cost_usd, latency_ms, refused,"
            " cache_read_tokens from extractions where document_id = %s order by created_at, id",
            (doc_id,),
        ).fetchall()
    ]
    bill_row = conn.execute(
        "select system, external_id, status, posted_at from bills where document_id = %s"
        " order by created_at desc limit 1",
        (doc_id,),
    ).fetchone()
    followups = [
        followup_out(dict(r))
        for r in conn.execute(
            "select * from followups where document_id = %s order by created_at, id", (doc_id,)
        ).fetchall()
    ]
    email_row = conn.execute(
        "select t.from_addr, t.subject from document_threads dt join email_threads t"
        " on t.id = dt.thread_id where dt.document_id = %s order by t.received_at limit 1",
        (doc_id,),
    ).fetchone()
    embedded = [
        mask_text(str(t)) or "" for t in (extraction or {}).get("embedded_instructions") or []
    ]
    redactions = _redactions(fields, screens, anonymous=anonymous)
    return sc.DocumentDetail(
        **card.model_dump(),
        pages=pages,
        original_url=None if anonymous else _signed(rt, doc["storage_path"]),
        fields=[_field_out(n, fields[n], cfg, rules) for n in shown],
        line_items=_line_items(extraction, words),
        rule_results=[rule_result_out(r, anonymous=anonymous) for r in rules],
        screen_results=[_screen_out(s) for s in screens],
        embedded_instructions=embedded,
        extractions=extractions,
        approvals=[] if anonymous else approvals_for(conn, doc_id),
        approvals_required=_approvals_required(status, rules),
        bill=None
        if bill_row is None
        else sc.BillOut(
            system=bill_row["system"],
            external_id=bill_row["external_id"],
            status=bill_row["status"],
            posted_at=iso(bill_row["posted_at"]),
        ),
        followups=[] if anonymous else followups,
        email=None
        if email_row is None
        else sc.EmailInfo(
            **{"from": email_row["from_addr"], "subject": mask_text(email_row["subject"]) or ""}
        ),
        trace_url=None if anonymous else _trace_url(rt, conn, doc_id),
        uploaded_by=None if anonymous or doc["uploaded_by"] is None else str(doc["uploaded_by"]),
        redactions=redactions,
    )
