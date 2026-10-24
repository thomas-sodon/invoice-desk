"""Haiku 5.5 injection / bank-change classifier (DESIGN §5.4, §5.5 "LLM" layer; M8).

Reads every piece of vendor-supplied text, each in its own section marked untrusted:

* ``document_text``: OCR of the visible render (words in reading order, grouped in lines);
* ``hidden_text``: text-layer words the hidden-text detector flagged, with the reason;
* ``faint_text``: words only the contrast-enhanced OCR pass read;
* ``unseen_text_layer``: text-layer words OCR did not read but that were not flagged (so no
  text-layer content is ever dropped before a model sees it);
* ``email_body``: the email the document came with.

Calls go through the :class:`~invoicedesk.contracts.llm.LLMClient` Protocol only (this module
never imports ``anthropic``), with :attr:`ModelRole.INJECTION_CLASSIFIER`, whose model, effort
and thinking settings live in :mod:`invoicedesk.config.models`. The system prompt is the frozen
§5.4 text, cache-flagged, with no IDs or timestamps. Every text sent is masked first
(account numbers to the last 4), and angle brackets in untrusted text are escaped so it cannot
close its own section.

The model only labels; code decides. The verdict is normalised so it can only get stricter:
``instruction_to_ai``/``approval_override`` imply ``malicious``, any other category implies at
least ``suspicious``. A refusal (``stop_reason == "refusal"``) or an output that does not fit
the schema becomes ``suspicious`` with a span saying so. Transport errors
(:class:`~invoicedesk.contracts.llm.LLMCallError` of kind rate_limit/status/connection/timeout)
propagate so the job is retried; screening never silently passes a document it did not screen.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import ValidationError
from rapidfuzz import fuzz

from invoicedesk.config.models import ModelRole
from invoicedesk.contracts.infra import Storage
from invoicedesk.contracts.llm import LLMCallError, LLMClient, LLMRequest, SystemBlock, TextBlock
from invoicedesk.contracts.preprocess import Box, OcrWord, PreprocessedDocument
from invoicedesk.contracts.screening import (
    InjectionCategory,
    InjectionVerdict,
    ScreenResult,
    ScreenSpan,
    Verdict,
)
from invoicedesk.guardrails._common import Group, norm, union_box
from invoicedesk.guardrails.faint_text import FaintTextReport, find_faint_text
from invoicedesk.guardrails.hidden_text import HiddenTextReport, find_hidden_text
from invoicedesk.guardrails.pixels import PageInk
from invoicedesk.guardrails.prompts import CLASSIFIER_SYSTEM_PROMPT
from invoicedesk.masking import mask_text

__all__ = [
    "MAX_DOCUMENT_CHARS",
    "MAX_SECTION_CHARS",
    "OUTPUT_SCHEMA",
    "InjectionClassifier",
    "build_classifier_request",
    "normalise_verdict",
]

OUTPUT_SCHEMA = "invoicedesk.contracts.screening.InjectionVerdict"
#: Token caps per call (OWASP #6): ~6k tokens of document text, ~2k per other section.
MAX_DOCUMENT_CHARS = 24_000
MAX_SECTION_CHARS = 8_000
_MAX_SPAN_CHARS = 300

_RANK: dict[Verdict, int] = {"clean": 0, "suspicious": 1, "malicious": 2}
_MALICIOUS_CATS: frozenset[str] = frozenset({"instruction_to_ai", "approval_override"})
SpanSource = Literal["pdf_text", "ocr", "ocr_enhanced", "email_body"]


# ----------------------------------------------------------------------------- request
def _escape(text: str) -> str:
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} more characters not shown]"


def _lines(words: Iterable[OcrWord]) -> list[tuple[int, str]]:
    """Group reading-order words into (page, line text)."""
    out: list[tuple[int, list[str], Box]] = []
    for w in words:
        if out:
            page, texts, last = out[-1]
            h = max(last[3] - last[1], w.box[3] - w.box[1], 1)
            same_line = abs((last[1] + last[3]) / 2 - (w.box[1] + w.box[3]) / 2) <= 0.6 * h
            if page == w.page and same_line and w.box[0] >= last[0]:
                texts.append(w.text)
                out[-1] = (page, texts, w.box)
                continue
        out.append((w.page, [w.text], w.box))
    return [(p, " ".join(t)) for p, t, _ in out]


def _section(tag: str, body: str, limit: int, **attrs: str) -> TextBlock:
    attr = "".join(f' {k}="{v}"' for k, v in attrs.items())
    content = _clip(_escape(mask_text(body) or ""), limit)
    return TextBlock(text=f'<{tag} untrusted="true"{attr}>\n{content}\n</{tag}>')


def _group_lines(groups: Iterable[Group]) -> str:
    return "\n".join(f"[page {g.page + 1}; {'; '.join(g.reasons)}] {g.text}" for g in groups)


def build_classifier_request(
    doc: PreprocessedDocument,
    *,
    hidden: HiddenTextReport,
    faint: FaintTextReport,
    email_body: str | None,
) -> LLMRequest | None:
    """The classifier call, or ``None`` when there is no text at all to classify."""
    blocks: list[TextBlock] = []
    page_count = len({p.index for p in doc.pages}) or 1
    doc_lines = _lines(doc.words)
    if doc_lines:
        body = "\n".join((f"[page {p + 1}] " if page_count > 1 else "") + t for p, t in doc_lines)
        blocks.append(_section("document_text", body, MAX_DOCUMENT_CHARS))
    if hidden.flagged:
        blocks.append(_section("hidden_text", _group_lines(hidden.flagged), MAX_SECTION_CHARS))
    if faint.flagged:
        blocks.append(_section("faint_text", _group_lines(faint.flagged), MAX_SECTION_CHARS))
    if hidden.unconfirmed:
        blocks.append(
            _section("unseen_text_layer", _group_lines(hidden.unconfirmed), MAX_SECTION_CHARS)
        )
    if faint.unconfirmed:
        blocks.append(
            _section(
                "faint_text",
                _group_lines(faint.unconfirmed),
                MAX_SECTION_CHARS,
                note="low-confidence fragments",
            )
        )
    if email_body and email_body.strip():
        blocks.append(_section("email_body", email_body.strip(), MAX_SECTION_CHARS))
    if not blocks:
        return None
    return LLMRequest(
        role=ModelRole.INJECTION_CLASSIFIER,
        system=(SystemBlock(text=CLASSIFIER_SYSTEM_PROMPT, cache=True),),
        user=tuple(blocks),
        output_schema=OUTPUT_SCHEMA,
        metadata={"document_id": doc.document_id, "detector": "injection_classifier"},
    )


# ----------------------------------------------------------------------------- response
def normalise_verdict(v: InjectionVerdict) -> tuple[Verdict, list[InjectionCategory]]:
    """Code decides: categories imply a minimum verdict; the model can only make it stricter."""
    cats: list[InjectionCategory] = []
    seen: list[InjectionCategory] = [*v.categories, *(s.category for s in v.spans)]
    for c in seen:
        if c != "none" and c not in cats:
            cats.append(c)
    implied: Verdict = (
        "malicious" if _MALICIOUS_CATS & set(cats) else ("suspicious" if cats else "clean")
    )
    verdict = v.verdict if _RANK[v.verdict] >= _RANK[implied] else implied
    return verdict, cats or ["none"]


class _Locator:
    """Maps a quoted span back to its source (and page/box when it is document text)."""

    def __init__(
        self,
        doc: PreprocessedDocument,
        hidden: HiddenTextReport,
        faint: FaintTextReport,
        email_body: str | None,
    ) -> None:
        self.email = norm(email_body or "")
        self.hidden = [*hidden.flagged, *hidden.unconfirmed]
        self.faint = [*faint.flagged, *faint.unconfirmed]
        self.words = doc.words
        self.offsets: list[int] = []
        parts: list[str] = []
        pos = 0
        for w in doc.words:
            self.offsets.append(pos)
            n = norm(w.text)
            parts.append(n)
            pos += len(n)
        self.flat = "".join(parts)

    @staticmethod
    def _in_groups(n: str, groups: list[Group]) -> Group | None:
        for g in groups:
            gn = norm(g.text)
            if gn and (n in gn or (len(n) >= 8 and fuzz.partial_ratio(n, gn) >= 85)):
                return g
        return None

    def _words_box(self, n: str) -> tuple[int, Box] | None:
        at = self.flat.find(n)
        if at < 0:
            return None
        end = at + len(n)
        hit = [
            w
            for w, off in zip(self.words, self.offsets, strict=True)
            if off < end and off + len(norm(w.text)) > at
        ]
        if not hit:
            return None
        page = hit[0].page
        return page, union_box(w.box for w in hit if w.page == page)

    def span(self, text: str, category: str) -> ScreenSpan:
        n = norm(text)
        quoted = mask_text(text[:_MAX_SPAN_CHARS]) or ""
        source: SpanSource = "ocr"
        page: int | None = None
        box: Box | None = None
        if n:
            if (g := self._in_groups(n, self.hidden)) is not None:
                source, page, box = "pdf_text", g.page, g.box
            elif (g := self._in_groups(n, self.faint)) is not None:
                source, page, box = "ocr_enhanced", g.page, g.box
            elif (loc := self._words_box(n)) is not None:
                page, box = loc
            elif n in self.email:
                source = "email_body"
        return ScreenSpan(text=quoted, reason=category, page=page, box=box, source=source)


# ----------------------------------------------------------------------------- screener
class InjectionClassifier:
    """:class:`~invoicedesk.contracts.screening.Screener` backed by the Haiku classifier."""

    name = "injection_classifier"

    def __init__(self, llm: LLMClient, *, storage: Storage | None = None) -> None:
        self.llm = llm
        self.storage = storage

    def screen(self, doc: PreprocessedDocument, *, email_body: str | None) -> ScreenResult:
        ink = PageInk(self.storage, doc)
        hidden = find_hidden_text(doc, ink=ink)
        faint = find_faint_text(doc, ink=ink)
        request = build_classifier_request(doc, hidden=hidden, faint=faint, email_body=email_body)
        if request is None:
            return ScreenResult(
                detector="injection_classifier", verdict="clean", categories=["none"]
            )
        try:
            response = self.llm.parse(request, InjectionVerdict)
        except LLMCallError as exc:
            if exc.kind != "validation":
                raise
            return _undecided("classifier output did not fit the schema", "0")
        cost = str(response.cost_usd)
        if response.refused:
            return _undecided(
                f"classifier refused to answer (stop_reason=refusal, model {response.model_id})",
                cost,
            )
        if response.parsed is None:
            return _undecided(
                f"classifier returned no output (stop_reason={response.stop_reason})", cost
            )
        try:
            parsed = InjectionVerdict.model_validate(response.parsed)
        except ValidationError:
            return _undecided("classifier output did not fit the schema", cost)

        verdict, cats = normalise_verdict(parsed)
        locate = _Locator(doc, hidden, faint, email_body)
        spans = [locate.span(s.text, s.category) for s in parsed.spans if s.text.strip()]
        return ScreenResult(
            detector="injection_classifier",
            verdict=verdict,
            categories=cats,
            spans=spans,
            cost_usd=cost,
        )


def _undecided(note: str, cost: str) -> ScreenResult:
    return ScreenResult(
        detector="injection_classifier",
        verdict="suspicious",
        categories=["none"],
        spans=[ScreenSpan(text=note, reason="classifier_unavailable")],
        cost_usd=cost,
    )
