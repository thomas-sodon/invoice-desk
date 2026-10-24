"""OracleLLM: a deterministic, offline, gold-backed :class:`~invoicedesk.contracts.llm.LLMClient`.

It powers the offline demo, recorded replays (wrap it in ``RecordingLLM``) and pipeline tests.
It never calls a network API.

How it recognizes a document:

1. by bytes: ``upload_file`` and inline images are hashed and looked up against each gold
   record's original-file SHA-256 and its processed-page SHA-256s (the PNGs
   ``invoicedesk.preprocess`` stores and the extractor uploads);
2. by text: otherwise the OCR/text blocks in the request are matched against each record's
   distinctive printed tokens (invoice number, work order, vendor name, total, date).

What it answers:

* extraction roles (``EXTRACT_T1``, ``EXTRACT_T2``, ``ADJUDICATE_T3``): the gold
  ``InvoiceExtraction`` with per-tier noise (:class:`OracleNoise`): lower tiers report lower
  confidence on photos and handwriting and occasionally misread a digit (with low confidence),
  so the routing's escalation path is exercised. Illegible fields come back empty. Word refs
  point at the request's OCR word ids when the words can be found, else at the gold box;
* ``INJECTION_CLASSIFIER``: an ``InjectionVerdict`` from keyword rules over all request text
  (document text and e-mail body), the same text a real classifier sees;
* any other role: a schema-valid object filled deterministically from the request text.

Usage and cost are synthetic but computed with the real price table (``config.models``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

from invoicedesk.config.models import ModelRole, cost_usd, settings_for
from invoicedesk.contracts.extraction import (
    DATE_FIELDS,
    MONEY_FIELDS,
    REQUIRED_FIELDS,
    InvoiceExtraction,
)
from invoicedesk.contracts.llm import (
    FileImageBlock,
    InlineImageBlock,
    LLMCallError,
    LLMRequest,
    LLMResponse,
    TextBlock,
)
from invoicedesk.contracts.screening import InjectionVerdict
from invoicedesk.fakes.llm import estimate_usage
from pydantic import BaseModel

from invoicedesk_data.gold import GoldRecord, load_gold
from invoicedesk_data.paths import GOLD_DIR

__all__ = ["OracleLLM", "OracleNoise", "TierNoise", "classify_text"]

Tier = Literal["haiku", "sonnet", "opus"]
_TIER_OF_ROLE: dict[ModelRole, Tier] = {
    ModelRole.EXTRACT_T1: "haiku",
    ModelRole.EXTRACT_T2: "sonnet",
    ModelRole.ADJUDICATE_T3: "opus",
}
_LATENCY_MS: dict[Tier, int] = {"haiku": 1100, "sonnet": 2600, "opus": 5200}


@dataclass(frozen=True)
class TierNoise:
    """Per-tier behaviour. ``confidence`` ranges are (low, high) per medium group."""

    error_rate_hard: float  # chance a header field is misread on photo/handwritten docs
    error_rate_easy: float  # same, on native/scanned docs
    conf_easy: tuple[float, float]
    conf_hard: tuple[float, float]
    conf_wrong: tuple[float, float]


@dataclass(frozen=True)
class OracleNoise:
    haiku: TierNoise = TierNoise(0.12, 0.0, (0.95, 0.995), (0.72, 0.93), (0.45, 0.7))
    sonnet: TierNoise = TierNoise(0.02, 0.0, (0.96, 0.997), (0.9, 0.98), (0.5, 0.75))
    opus: TierNoise = TierNoise(0.0, 0.0, (0.97, 0.998), (0.94, 0.99), (0.55, 0.8))

    @classmethod
    def none(cls) -> OracleNoise:
        """Exact gold at every tier with 0.99 confidence (illegible fields still empty)."""
        t = TierNoise(0.0, 0.0, (0.99, 0.99), (0.99, 0.99), (0.99, 0.99))
        return cls(t, t, t)

    def tier(self, tier: Tier) -> TierNoise:
        return getattr(self, tier)


# ----------------------------------------------------------------------------- classifier
_AI_WORDS = (
    r"(?:\bai\b|assistant|\bsystem\b|automated|automation|processor|\bagent\b|\bbot\b|\bllm\b)"
)
_IMPERATIVE = (
    r"\b(?:ignore|disregard|update|approve|post|mark|skip|send|pay|treat|change|override|"
    r"forget)\b"
)
_PATTERNS: dict[str, re.Pattern[str]] = {
    "instruction_to_ai": re.compile(
        rf"{_AI_WORDS}[^.\n]{{0,80}}{_IMPERATIVE}|{_IMPERATIVE}[^.\n]{{0,40}}{_AI_WORDS}", re.I
    ),
    "approval_override": re.compile(
        r"pre-?approved|approve (?:this|it|for)|skip (?:the|all)|ignore (?:all|your|previous|"
        r"prior|the)|disregard|post it immediately|override",
        re.I,
    ),
    "bank_or_remit_change": re.compile(
        r"(?:update|change|changed|new|moved|switch|closed|closing|frozen)[^.\n]{0,60}"
        r"(?:bank|account|remit|routing)|(?:bank|account|remit)[^.\n]{0,40}(?:has changed|"
        r"have changed|changed|will be closed|is being closed|is closed|has moved)",
        re.I,
    ),
    "urgency_pressure": re.compile(r"\burgent\b|\bimmediately\b|\bpay today\b|\basap\b", re.I),
}
_SENTENCE = re.compile(r"[^.\n!?]+[.!?]?")


_TO_AI = re.compile(
    rf"{_AI_WORDS}[^\n]{{0,120}}?{_IMPERATIVE}"
    rf"|(?:note to|attention|dear|hey)\s+(?:the\s+)?{_AI_WORDS}"
    rf"|{_AI_WORDS}(?:\s+\w+){{0,2}}\s*:",
    re.I,
)


_FLAT_KEYS: dict[str, tuple[str, ...]] = {
    "instruction_to_ai": (
        "aisystem",
        "alsystem",
        "aiassistant",
        "alassistant",
        "aiagent",
        "alagent",
        "automatedprocessor",
        "automatedsystem",
        "notetoai",
    ),
    "bank_or_remit_change": (
        "paymenttonew",
        "tonewaccount",
        "newbankaccount",
        "updateourbank",
        "bankdetailshavechanged",
        "accounthaschanged",
        "changedbanks",
    ),
}


def classify_text(text: str) -> InjectionVerdict:
    """Keyword classifier with the classifier's output schema (deterministic).

    ``instruction_to_ai`` is matched across sentence boundaries (an address such as "AI
    assistant:" followed by an imperative); the other categories sentence by sentence."""
    spans: list[InjectionVerdict.Span] = []
    cats: list[str] = []

    def add(cat: str, snippet: str) -> None:
        if cat not in cats:
            cats.append(cat)
        spans.append(InjectionVerdict.Span(category=cat, text=snippet.strip()[:300]))  # type: ignore[arg-type]

    for m in _TO_AI.finditer(text):
        add("instruction_to_ai", m.group(0))
    # OCR-tolerant pass on de-spaced text: faint or photographed text comes back with merged
    # words and "AI" read as "Al" ("Alsystem: send paymentto.newaccoun").
    flat = re.sub(r"[^a-z0-9]", "", text.lower())
    for cat, keys in _FLAT_KEYS.items():
        hit = next((k for k in keys if k in flat), None)
        if hit:
            add(cat, hit)
    for sentence in _SENTENCE.findall(text):
        sent = sentence.strip()
        if not sent:
            continue
        for cat, pat in _PATTERNS.items():
            if cat != "instruction_to_ai" and pat.search(sent):
                add(cat, sent)
    if "instruction_to_ai" in cats or "approval_override" in cats:
        verdict = "malicious"
    elif cats:
        verdict = "suspicious"
    else:
        verdict = "clean"
    return InjectionVerdict(
        verdict=verdict,  # type: ignore[arg-type]
        categories=cats or ["none"],  # type: ignore[arg-type]
        spans=spans,
    )


# ----------------------------------------------------------------------------- helpers
_NORM = re.compile(r"[^a-z0-9]+")
_STOP = frozenset(
    {
        "2025",
        "2026",
        "2027",
        "jan",
        "feb",
        "mar",
        "apr",
        "may",
        "jun",
        "jul",
        "aug",
        "sep",
        "oct",
        "nov",
        "dec",
        "january",
        "february",
        "march",
        "april",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "llc",
        "inc",
        "the",
        "and",
        "springfield",
        "974",
        "unit",
    }
)


def _norm(s: str) -> str:
    return _NORM.sub("", s.lower())


def _toks(s: str) -> list[str]:
    return [t for t in (_norm(x) for x in s.split()) if t]


_WORD_LINE = re.compile(r"^\s*\[?#?(\d+)\]?\s*[\t:|=]\s*(\S.*?)\s*$")


def _parse_words(text: str) -> list[tuple[int, str]]:
    """OCR words as the extractor lists them: ``12: INV-2231``, ``[12] INV-2231`` (tab, pipe or
    colon separated) or a JSON list of ``{"id": .., "text": ..}``."""
    words: list[tuple[int, str]] = []
    stripped = text.strip()
    if stripped.startswith(("[", "{")):
        try:
            obj = json.loads(stripped)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            obj = obj.get("words")
        if isinstance(obj, list):
            for w in obj:
                if isinstance(w, dict) and "id" in w and "text" in w:
                    words.append((int(w["id"]), str(w["text"])))
            if words:
                return words
    for line in text.splitlines():
        m = _WORD_LINE.match(line)
        if m:
            words.append((int(m.group(1)), m.group(2)))
    return words


def _find_ids(words: list[tuple[int, str]], printed: str) -> list[int]:
    target = _toks(printed)
    if not target or not words:
        return []
    normed = [(i, _norm(t)) for i, t in words]
    n = len(target)
    for start in range(len(normed)):
        ids: list[int] = []
        k = 0
        j = start
        while j < len(normed) and k < n:
            if not normed[j][1]:
                j += 1
                continue
            if normed[j][1] == target[k]:
                ids.append(normed[j][0])
                k += 1
                j += 1
            else:
                break
        if k == n:
            return ids
    # joined match (OCR split or merged differently)
    joined = "".join(target)
    for start in range(len(normed)):
        acc = ""
        ids = []
        for i, t in normed[start : start + 8]:
            acc += t
            ids.append(i)
            if acc == joined:
                return ids
            if not joined.startswith(acc):
                break
    return []


def _misread(value: Any, rng: random.Random) -> Any:
    if isinstance(value, int):
        delta = rng.choice((900, -900, 9_000, 100, -100, 18_000))
        return max(0, value + delta)
    if isinstance(value, str) and value:
        chars = list(value)
        digits = [i for i, c in enumerate(chars) if c.isdigit()]
        if digits:
            i = rng.choice(digits)
            swap = {"0": "8", "1": "7", "3": "8", "5": "6", "6": "5", "7": "1", "8": "3", "9": "4"}
            chars[i] = swap.get(chars[i], "0" if chars[i] != "0" else "9")
            return "".join(chars)
        return value[:-1] if len(value) > 3 else value
    return value


def _default_for(annotation: Any, text: str) -> Any:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if annotation is str:
        return text
    if annotation is bool:
        return False
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    if origin in (list, tuple, set):
        return []
    if origin is dict:
        return {}
    if origin is Literal:
        return args[0]
    if args and type(None) in args:
        return None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _fill_model(annotation, text)
    return None


def _fill_model(model: type[BaseModel], text: str) -> dict[str, Any]:
    return {
        name: _default_for(f.annotation, text)
        for name, f in model.model_fields.items()
        if f.is_required()
    }


@dataclass
class _Index:
    by_sha: dict[str, GoldRecord] = field(default_factory=dict)
    records: dict[str, GoldRecord] = field(default_factory=dict)
    signatures: dict[str, list[str]] = field(default_factory=dict)


class OracleLLM:
    """Gold-backed LLM client. See the module docstring."""

    def __init__(
        self,
        records: Iterable[GoldRecord] = (),
        *,
        noise: OracleNoise | None = None,
        unknown: Literal["empty", "refuse", "error"] = "empty",
        refuse_roles: Iterable[str] = (),
        min_text_score: float = 0.6,
    ) -> None:
        self.noise = noise or OracleNoise()
        self.unknown = unknown
        self.refuse_roles = {str(r) for r in refuse_roles}
        self.min_text_score = min_text_score
        self._idx = _Index()
        self._files: dict[str, str] = {}  # file_id -> sha256
        self.calls: list[LLMRequest] = []
        for r in records:
            self.register(r)

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_gold(
        cls,
        sets: Iterable[str] = ("demo", "synthetic-core", "adversarial"),
        *,
        gold_dir: Path = GOLD_DIR,
        **kw: Any,
    ) -> OracleLLM:
        """Load every committed gold record of ``sets`` (missing sets are skipped)."""
        records: list[GoldRecord] = []
        for s in sets:
            if (gold_dir / s / "manifest.json").exists():
                records.extend(load_gold(s, gold_dir=gold_dir))
        return cls(records, **kw)

    def register(self, record: GoldRecord, *, aliases: Iterable[str] = ()) -> None:
        """Index a gold record by its file hash, page hashes, extra ``aliases`` (sha256 hex of
        any other bytes that represent it) and its printed text signature."""
        self._idx.records[record.id] = record
        for sha in (record.sha256, *record.page_sha256, *aliases):
            self._idx.by_sha[sha] = record
        sig: list[str] = []
        for name in (
            "invoice_number",
            "work_order_number",
            "total",
            "invoice_date",
            "vendor_name",
            "subtotal",
            "vendor_address",
        ):
            b = record.boxes.get(name)
            if b is not None and name not in record.truth.illegible_fields:
                sig.extend(t for t in _toks(b.text)[:4] if len(t) >= 3 and t not in _STOP)
        self._idx.signatures[record.id] = list(dict.fromkeys(sig))

    @property
    def records(self) -> dict[str, GoldRecord]:
        return dict(self._idx.records)

    # ------------------------------------------------------------------ LLMClient
    def upload_file(self, data: bytes, *, filename: str, media_type: str) -> str:
        sha = hashlib.sha256(data).hexdigest()
        file_id = f"file_oracle_{sha[:24]}"
        self._files[file_id] = sha
        return file_id

    def parse(self, request: LLMRequest, output_model: type[BaseModel]) -> LLMResponse:
        self.calls.append(request)
        if str(request.role) in self.refuse_roles:
            return self._respond(request, None, "refusal")
        text = self._text(request)
        record = self._match(request, text)
        if request.role == ModelRole.INJECTION_CLASSIFIER:
            parsed: dict[str, Any] | None = classify_text(text).model_dump(mode="json")
        elif request.role in _TIER_OF_ROLE:
            if record is None:
                if self.unknown == "refuse":
                    return self._respond(request, None, "refusal")
                if self.unknown == "error":
                    raise LLMCallError("validation", "oracle: document not in the gold index")
                parsed = self._empty_extraction()
            else:
                parsed = self._extract(record, _TIER_OF_ROLE[request.role], text)
        else:
            parsed = _fill_model(output_model, self._user_text(request)[:2000])
        try:
            validated = output_model.model_validate(parsed).model_dump(mode="json")
        except ValueError as exc:
            raise LLMCallError(
                "validation", f"oracle output does not fit {output_model}: {exc}"
            ) from exc
        return self._respond(request, validated, "end_turn", record)

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _user_text(request: LLMRequest) -> str:
        return "\n".join(b.text for b in request.user if isinstance(b, TextBlock))

    def _text(self, request: LLMRequest) -> str:
        return self._user_text(request)

    def _match(self, request: LLMRequest, text: str) -> GoldRecord | None:
        for b in request.user:
            sha: str | None = None
            if isinstance(b, FileImageBlock):
                sha = self._files.get(b.file_id)
            elif isinstance(b, InlineImageBlock):
                try:
                    sha = hashlib.sha256(base64.b64decode(b.data_b64)).hexdigest()
                except ValueError:
                    sha = None
            if sha and sha in self._idx.by_sha:
                return self._idx.by_sha[sha]
        hint = request.metadata.get("gold_id")
        if hint and hint in self._idx.records:
            return self._idx.records[hint]
        if not text.strip():
            return None
        # Substring presence on the de-spaced text survives OCR merging tokens
        # ("INVOICENO.A-3600") and dropping punctuation.
        parsed = _parse_words(text)
        flat = _norm(text) + "|" + _norm(" ".join(w for _, w in parsed))
        best: tuple[float, str] | None = None
        tie = False
        for rid, sig in self._idx.signatures.items():
            if not sig:
                continue
            score = sum(1 for t in sig if t in flat) / len(sig)
            if best is None or score > best[0]:
                best, tie = (score, rid), False
            elif score == best[0]:
                tie = True
        if best is None or best[0] < self.min_text_score or tie:
            return None
        return self._idx.records[best[1]]

    def _empty_extraction(self) -> dict[str, Any]:
        def tf() -> dict[str, Any]:
            return {
                "value": None,
                "confidence": 0.05,
                "source": {"word_ids": [], "fallback_box": None},
            }

        def mf() -> dict[str, Any]:
            return {
                "value_cents": None,
                "currency": "USD",
                "confidence": 0.05,
                "source": {"word_ids": [], "fallback_box": None},
            }

        def df() -> dict[str, Any]:
            return {
                "value_iso": None,
                "confidence": 0.05,
                "source": {"word_ids": [], "fallback_box": None},
            }

        return {
            "doc_type": "other",
            "vendor_name": tf(),
            "vendor_address": tf(),
            "vendor_tax_id_last4": tf(),
            "invoice_number": tf(),
            "invoice_date": df(),
            "due_date": df(),
            "work_order_number": tf(),
            "property_or_unit": tf(),
            "line_items": [],
            "subtotal": mf(),
            "tax": mf(),
            "total": mf(),
            "remit_to_bank_details": tf(),
            "handwriting_present": False,
            "legibility_issues": ["document could not be read (not in the offline gold index)"],
            "embedded_instructions": [],
        }

    def _extract(self, record: GoldRecord, tier: Tier, text: str) -> dict[str, Any]:
        gold = InvoiceExtraction.model_validate(record.extraction).model_dump(mode="json")
        noise = self.noise.tier(tier)
        hard = record.medium in ("photo", "handwritten")
        words = _parse_words(text)
        seed = int(hashlib.sha256(f"{record.id}:{tier}".encode()).hexdigest()[:16], 16)
        rng = random.Random(seed)  # noqa: S311 - deterministic noise, not security
        illegible = set(record.truth.illegible_fields)

        def conf(lo_hi: tuple[float, float]) -> float:
            return round(rng.uniform(*lo_hi), 3)

        def source(name: str) -> dict[str, Any]:
            b = record.boxes.get(name)
            ids = _find_ids(words, b.text) if b is not None else []
            if ids:
                return {"word_ids": ids, "fallback_box": None}
            return {"word_ids": [], "fallback_box": list(b.box) if b is not None else None}

        def handle(name: str, fld: dict[str, Any], key: str, *, header: bool) -> None:
            fld["source"] = source(name)
            if name in illegible:
                fld[key] = None
                fld["confidence"] = round(rng.uniform(0.08, 0.25), 3)
                return
            if fld[key] is None:
                fld["confidence"] = conf(noise.conf_easy)
                return
            rate = noise.error_rate_hard if hard else noise.error_rate_easy
            if header and rng.random() < rate:
                fld[key] = _misread(fld[key], rng)
                fld["confidence"] = conf(noise.conf_wrong)
                return
            fld["confidence"] = conf(noise.conf_hard if hard else noise.conf_easy)

        for name in (
            "vendor_name",
            "vendor_address",
            "vendor_tax_id_last4",
            "invoice_number",
            "work_order_number",
            "property_or_unit",
            "remit_to_bank_details",
        ):
            handle(name, gold[name], "value", header=name in REQUIRED_FIELDS)
        for name in sorted(DATE_FIELDS):
            handle(name, gold[name], "value_iso", header=name in REQUIRED_FIELDS)
        for name in sorted(MONEY_FIELDS):
            handle(name, gold[name], "value_cents", header=True)
        for i, li in enumerate(gold["line_items"]):
            handle(f"line_items.{i}.description", li["description"], "value", header=False)
            handle(f"line_items.{i}.quantity", li["quantity"], "value", header=False)
            handle(f"line_items.{i}.unit_price", li["unit_price"], "value_cents", header=False)
            handle(f"line_items.{i}.amount", li["amount"], "value_cents", header=False)
        # Instructions: visible ones always; hidden ones only if the reader was given them.
        visible = list(record.truth.visible_instructions)
        norm_text = _norm(text)
        hidden = [h.text for h in record.truth.hidden if _norm(h.text)[:40] in norm_text]
        gold["embedded_instructions"] = visible + hidden
        gold["legibility_issues"] = [f"{f} is illegible" for f in record.truth.illegible_fields]
        return gold

    def _respond(
        self,
        request: LLMRequest,
        parsed: dict[str, Any] | None,
        stop_reason: str,
        record: GoldRecord | None = None,
    ) -> LLMResponse:
        rs = settings_for(request.role)
        usage = estimate_usage(request, parsed)
        tier: Tier = rs.tier
        key = request.replay_key()
        latency = _LATENCY_MS[tier] + int(key[:4], 16) % 400
        cost: Decimal = cost_usd(rs.model, **usage.model_dump(), batch=rs.batch)
        return LLMResponse(
            role=request.role,
            model_id=rs.model,
            stop_reason=stop_reason,
            parsed=parsed,
            usage=usage,
            cost_usd=cost,
            latency_ms=latency,
            request_id=f"oracle-{key[:12]}",
            trace_id=request.metadata.get("trace_id"),
        )
