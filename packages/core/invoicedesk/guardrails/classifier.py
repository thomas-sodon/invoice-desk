"""Haiku 5.5 injection / bank-change classifier (DESIGN §5.4, §5.5 "LLM" layer; M8).

Reads every piece of vendor-supplied text, each in its own section marked untrusted:

* ``document_text``: OCR of the visible render (words in reading order, grouped in lines);
* ``hidden_text``: text-layer words the hidden-text detector flagged, with the reason;
* ``faint_text``: words only the contrast-enhanced OCR pass read;
* ``unseen_text_layer``: text-layer words OCR did not read but that were not flagged (so no
  text-layer content is ever dropped before a model sees it);
* ``email_body``: the email the document came with.

Nothing is truncated: text that does not fit one call (:data:`CALL_CHAR_BUDGET`) is split at
line boundaries into ``part="i of n"`` sections across up to :data:`MAX_CLASSIFIER_CALLS` calls
(token cap per call, OWASP #6), and the strictest verdict wins. A document with more text than
that is not screened partially; it fails closed (quarantined for a human).

Calls go through the :class:`~invoicedesk.contracts.llm.LLMClient` Protocol only (this module
never imports ``anthropic``), with :attr:`ModelRole.INJECTION_CLASSIFIER`, whose model, effort
and thinking settings live in :mod:`invoicedesk.config.models`. The system prompt is the frozen
§5.4 text, cache-flagged, with no IDs or timestamps. Every text sent is masked first
(account numbers to the last 4), and angle brackets in untrusted text are escaped so it cannot
close its own section.

The model only labels; code decides. The verdict is normalised so it can only get stricter:
``instruction_to_ai``/``approval_override`` imply ``malicious``, any other category implies at
least ``suspicious``. Haiku 5.5 has no server-side fallback, so a refusal is re-asked of the
Sonnet escalation role when one is configured (``ModelRole.INJECTION_CLASSIFIER_T2``). When no
usable answer comes back (refusal, no output, output that does not fit the schema) the document
was NOT screened, so the result fails closed: verdict ``malicious`` with category ``none`` and a
``classifier_unavailable`` span, which quarantines it for a human (R10). Transport errors
(:class:`~invoicedesk.contracts.llm.LLMCallError` of kind rate_limit/status/connection/timeout)
propagate so the job is retried; screening never silently passes a document it did not screen.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
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
    "CALL_CHAR_BUDGET",
    "MAX_CLASSIFIER_CALLS",
    "OUTPUT_SCHEMA",
    "InjectionClassifier",
    "build_classifier_requests",
    "normalise_verdict",
]

OUTPUT_SCHEMA = "invoicedesk.contracts.screening.InjectionVerdict"
#: Token cap per call (OWASP #6): ~6k tokens of untrusted text per classifier call.
CALL_CHAR_BUDGET = 24_000
#: Most calls one document may take; more text than this fails closed instead of being skipped.
MAX_CLASSIFIER_CALLS = 4
_MAX_SPAN_CHARS = 300
#: Sonnet escalation for a Haiku refusal (CLAUDE.md: Haiku 5.5 has no server-side fallback).
#: Picked up automatically once ``config.models`` defines the role.
DEFAULT_ESCALATION_ROLE: ModelRole | None = getattr(ModelRole, "INJECTION_CLASSIFIER_T2", None)

_RANK: dict[Verdict, int] = {"clean": 0, "suspicious": 1, "malicious": 2}
_MALICIOUS_CATS: frozenset[str] = frozenset({"instruction_to_ai", "approval_override"})
SpanSource = Literal["pdf_text", "ocr", "ocr_enhanced", "email_body"]


# ----------------------------------------------------------------------------- request
def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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


def _split(body: str, limit: int) -> list[str]:
    """Split at line boundaries into pieces of at most ``limit`` characters (a single longer
    line is hard-split). Never drops text."""
    pieces: list[str] = []
    cur = ""
    for raw in body.split("\n"):
        line = raw
        while len(line) > limit:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            pieces.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur or not pieces:
        pieces.append(cur)
    return pieces


def _sections(tag: str, body: str, budget: int, **attrs: str) -> list[str]:
    """Masked, escaped section(s) for one source; split into parts when over ``budget``."""
    content = _escape(mask_text(body) or "")
    overhead = 2 * len(tag) + 80 + sum(len(k) + len(v) + 4 for k, v in attrs.items())
    pieces = _split(content, max(1, budget - overhead))
    out: list[str] = []
    for i, piece in enumerate(pieces, 1):
        a = dict(attrs)
        if len(pieces) > 1:
            a["part"] = f"{i} of {len(pieces)}"
        attr = "".join(f' {k}="{v}"' for k, v in a.items())
        out.append(f'<{tag} untrusted="true"{attr}>\n{piece}\n</{tag}>')
    return out


def _group_lines(groups: Iterable[Group]) -> str:
    return "\n".join(f"[page {g.page + 1}; {'; '.join(g.reasons)}] {g.text}" for g in groups)


def build_classifier_requests(
    doc: PreprocessedDocument,
    *,
    hidden: HiddenTextReport,
    faint: FaintTextReport,
    email_body: str | None,
    budget: int = CALL_CHAR_BUDGET,
) -> list[LLMRequest]:
    """The classifier call(s) covering ALL vendor-supplied text (empty when there is none).

    Each call holds at most ``budget`` characters of untrusted text; sections that do not fit
    are split into parts across calls. Callers must refuse to screen partially when the list
    is longer than :data:`MAX_CLASSIFIER_CALLS`."""
    texts: list[str] = []
    page_count = len({p.index for p in doc.pages}) or 1
    doc_lines = _lines(doc.words)
    if doc_lines:
        body = "\n".join((f"[page {p + 1}] " if page_count > 1 else "") + t for p, t in doc_lines)
        texts += _sections("document_text", body, budget)
    if hidden.flagged:
        texts += _sections("hidden_text", _group_lines(hidden.flagged), budget)
    if faint.flagged:
        texts += _sections("faint_text", _group_lines(faint.flagged), budget)
    if hidden.unconfirmed:
        texts += _sections("unseen_text_layer", _group_lines(hidden.unconfirmed), budget)
    if faint.unconfirmed:
        texts += _sections(
            "faint_text", _group_lines(faint.unconfirmed), budget, note="low-confidence fragments"
        )
    if email_body and email_body.strip():
        texts += _sections("email_body", email_body.strip(), budget)

    calls: list[list[str]] = []
    size = 0
    for t in texts:
        if calls and size + len(t) <= budget:
            calls[-1].append(t)
            size += len(t)
        else:
            calls.append([t])
            size = len(t)
    return [
        LLMRequest(
            role=ModelRole.INJECTION_CLASSIFIER,
            system=(SystemBlock(text=CLASSIFIER_SYSTEM_PROMPT, cache=True),),
            user=tuple(TextBlock(text=t) for t in call),
            output_schema=OUTPUT_SCHEMA,
            metadata={
                "document_id": doc.document_id,
                "detector": "injection_classifier",
                "part": f"{i} of {len(calls)}",
            },
        )
        for i, call in enumerate(calls, 1)
    ]


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
        quoted = (mask_text(text) or "")[:_MAX_SPAN_CHARS]
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

    def __init__(
        self,
        llm: LLMClient,
        *,
        storage: Storage | None = None,
        escalation_role: ModelRole | None = DEFAULT_ESCALATION_ROLE,
    ) -> None:
        self.llm = llm
        self.storage = storage
        self.escalation_role = escalation_role

    def screen(self, doc: PreprocessedDocument, *, email_body: str | None) -> ScreenResult:
        ink = PageInk(self.storage, doc)
        hidden = find_hidden_text(doc, ink=ink)
        faint = find_faint_text(doc, ink=ink)
        requests = build_classifier_requests(doc, hidden=hidden, faint=faint, email_body=email_body)
        if not requests:
            return ScreenResult(
                detector="injection_classifier", verdict="clean", categories=["none"]
            )
        if len(requests) > MAX_CLASSIFIER_CALLS:
            return _undecided(
                f"too much text to screen ({len(requests)} classifier calls needed, cap "
                f"{MAX_CLASSIFIER_CALLS}); quarantined rather than screened partially",
                Decimal(0),
            )

        locate = _Locator(doc, hidden, faint, email_body)
        verdict: Verdict = "clean"
        cats: list[InjectionCategory] = []
        spans: list[ScreenSpan] = []
        cost = Decimal(0)
        for request in requests:
            parsed, call_cost, failure = self._ask(request)
            cost += call_cost
            if parsed is None:
                return _undecided(failure, cost)
            v, c = normalise_verdict(parsed)
            if _RANK[v] > _RANK[verdict]:
                verdict = v
            for x in c:
                if x != "none" and x not in cats:
                    cats.append(x)
            spans += [locate.span(s.text, s.category) for s in parsed.spans if s.text.strip()]
        return ScreenResult(
            detector="injection_classifier",
            verdict=verdict,
            categories=cats or ["none"],
            spans=spans,
            cost_usd=str(cost),
        )

    def _ask(self, request: LLMRequest) -> tuple[InjectionVerdict | None, Decimal, str]:
        """One classifier call (plus the Sonnet escalation on a Haiku refusal).
        Returns (verdict or None, cost, failure note)."""
        parsed, cost, failure, refused = self._call(request)
        if refused and self.escalation_role is not None:
            esc = request.model_copy(update={"role": self.escalation_role})
            parsed, esc_cost, esc_failure, _ = self._call(esc)
            cost += esc_cost
            failure = f"{failure}; escalation: {esc_failure}" if parsed is None else ""
        return parsed, cost, failure

    def _call(self, request: LLMRequest) -> tuple[InjectionVerdict | None, Decimal, str, bool]:
        try:
            response = self.llm.parse(request, InjectionVerdict)
        except LLMCallError as exc:
            if exc.kind != "validation":
                raise
            return None, Decimal(0), "classifier output did not fit the schema", False
        cost = Decimal(response.cost_usd)
        if response.refused:
            note = f"classifier refused to answer (stop_reason=refusal, model {response.model_id})"
            return None, cost, note, True
        if response.parsed is None:
            return (
                None,
                cost,
                f"classifier returned no output (stop_reason={response.stop_reason})",
                False,
            )
        try:
            return InjectionVerdict.model_validate(response.parsed), cost, "", False
        except ValidationError:
            return None, cost, "classifier output did not fit the schema", False


def _undecided(note: str, cost: Decimal) -> ScreenResult:
    """The document was not screened: fail closed (``malicious`` quarantines under R10)."""
    return ScreenResult(
        detector="injection_classifier",
        verdict="malicious",
        categories=["none"],
        spans=[ScreenSpan(text=f"{note}; quarantined to be safe", reason="classifier_unavailable")],
        cost_usd=str(cost),
    )
