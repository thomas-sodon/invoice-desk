"""Shared helpers for the deterministic screeners: geometry, colour difference, text
normalisation, keyword categories and span grouping. Pure functions, no I/O."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from invoicedesk.contracts.preprocess import Box, OcrWord
from invoicedesk.contracts.screening import InjectionCategory

__all__ = [
    "Finding",
    "alnum_len",
    "categorize",
    "delta_e76",
    "group_findings",
    "height_ratio",
    "norm",
    "overlap",
    "text_supported",
    "union_box",
]


# ----------------------------------------------------------------------------- geometry
def overlap(a: Box, b: Box) -> float:
    """Intersection area over the smaller box's area (0..1)."""
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    if ix <= 0 or iy <= 0:
        return 0.0
    area = min(max(1, (a[2] - a[0]) * (a[3] - a[1])), max(1, (b[2] - b[0]) * (b[3] - b[1])))
    return (ix * iy) / area


def height_ratio(a: Box, b: Box) -> float:
    ha, hb = max(1, a[3] - a[1]), max(1, b[3] - b[1])
    return min(ha, hb) / max(ha, hb)


def union_box(boxes: Iterable[Box]) -> Box:
    bs = list(boxes)
    return (
        min(b[0] for b in bs),
        min(b[1] for b in bs),
        max(b[2] for b in bs),
        max(b[3] for b in bs),
    )


# ----------------------------------------------------------------------------- colour
def _srgb_to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    def lin(c: int) -> float:
        v = c / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e76(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """CIE76 ΔE between two sRGB colours (D65). Same formula as the preprocessor uses."""
    la, lb = _srgb_to_lab(a), _srgb_to_lab(b)
    return sum((p - q) ** 2 for p, q in zip(la, lb, strict=True)) ** 0.5


# ----------------------------------------------------------------------------- text
_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)


def norm(s: str) -> str:
    """Lower-case, alphanumerics only (OCR and the text layer disagree on punctuation/spaces)."""
    return _NON_ALNUM.sub("", s).lower()


def alnum_len(s: str) -> int:
    return sum(c.isalnum() for c in s)


def text_supported(candidate: str, visible: str, *, threshold: float) -> bool:
    """Does ``visible`` (OCR text found at the same place) account for ``candidate``?

    A shorter candidate may sit inside a longer OCR chunk (merged words): partial match.
    A longer candidate must match as a whole, so a long hidden word lying on a short visible
    one ("assistant" over "a") is NOT supported."""
    nc, nv = norm(candidate), norm(visible)
    if not nc:
        return True
    if not nv:
        return False
    if len(nc) <= 2:
        return nc in nv
    if len(nc) <= len(nv):
        return fuzz.partial_ratio(nc, nv) >= threshold
    return fuzz.ratio(nc, nv) >= threshold


def words_at(box: Box, page: int, words: Sequence[OcrWord], *, min_overlap: float = 0.3) -> str:
    """OCR text overlapping ``box`` on ``page`` (compatible height), in reading order."""
    hits = [
        w
        for w in words
        if w.page == page and overlap(box, w.box) >= min_overlap and height_ratio(box, w.box) >= 0.4
    ]
    hits.sort(key=lambda w: (w.box[1] // 8, w.box[0]))
    return " ".join(w.text for w in hits)


# ----------------------------------------------------------------------------- keywords
#: Words that address an automated reader. ``_AI_STRONG`` is safe next to an imperative;
#: ``_AI`` adds generic words ("system", "agent") that only count in an address form.
_AI_STRONG = (
    r"(?:a\.i\.|ai|artificial intelligence|ai assistant|assistant|chat ?bot|llm|language model"
    r"|gpt|claude|automated (?:system|processor|agent|reader|clerk|ap clerk)|automation)"
)
_AI = rf"(?:{_AI_STRONG}|system|agent|bot|model|processor)"
_IMPERATIVE = (
    r"(?:ignore|disregard|forget|override|bypass|skip|approve|mark|post|pay|send|update|change"
    r"|set|route|treat|classify|output|return|do not|don't|must|should)"
)
_IGNORE_RULES = (
    r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|your\s+|the\s+)?"
    r"(?:previous|prior|above|earlier|your)?\s*(?:instructions|rules|prompt|checks|validation)"
)
_CHANGE = r"(?:new|updated|update|change|changed|changing|different)"
_BANK = r"(?:bank|account|acct|remit|remittance|routing|iban|wire|payment details)"
_CHANGED = (
    r"(?:has changed|have changed|changed|will be closed|is being closed|is closed|closed"
    r"|frozen|has moved|moved)"
)

_PATTERNS: dict[InjectionCategory, re.Pattern[str]] = {
    "instruction_to_ai": re.compile(
        rf"\b(?:note to|attention|dear|hey|hello)\s+(?:the\s+)?{_AI}\b"
        rf"|\b{_AI}\s*:"
        rf"|\bif you are an? {_AI}\b"
        rf"|\b{_AI_STRONG}\b[^\n]{{0,60}}?\b{_IMPERATIVE}\b"
        rf"|{_IGNORE_RULES}",
        re.IGNORECASE,
    ),
    "approval_override": re.compile(
        r"\bpre-?approved\b|\balready approved\b|\bapprove (?:this|the|it)\b"
        r"|\bmark (?:this|it|the)?\s*(?:invoice|bill)?\s*(?:as\s+)?(?:ready to pay|approved|paid)\b"
        r"|\bready to pay\b"
        r"|\bskip (?:the\s+)?(?:\w+\s+)?(?:check|checks|validation|review|approval)\b"
        r"|\bbypass\b|\boverride\b|\bno (?:need for|further) (?:review|approval)\b",
        re.IGNORECASE,
    ),
    "bank_or_remit_change": re.compile(
        rf"\b{_CHANGE}\b[^.\n]{{0,40}}\b{_BANK}"
        rf"|\b{_BANK}\b[^.\n]{{0,40}}\b{_CHANGED}\b"
        r"|\bchanged banks\b|\bsend (?:the\s+)?payments? to\b",
        re.IGNORECASE,
    ),
    "urgency_pressure": re.compile(
        r"\burgent(?:ly)?\b|\bimmediately\b|\bpay today\b|\basap\b|\bright away\b"
        r"|\bwithin (?:the|one|1) hour\b|\bfinal notice\b",
        re.IGNORECASE,
    ),
}

#: OCR-tolerant keys matched on de-spaced, lower-cased text (faint/photo OCR merges words and
#: reads "AI" as "Al").
_FLAT: dict[InjectionCategory, tuple[str, ...]] = {
    "instruction_to_ai": (
        "notetoai",
        "notetoal",
        "aisystem",
        "alsystem",
        "aiassistant",
        "alassistant",
        "aiagent",
        "alagent",
        "automatedprocessor",
        "automatedsystem",
        "ignoreallprevious",
        "ignoreprevious",
        "ignoreyourinstructions",
        "systemprompt",
    ),
    "approval_override": ("preapproved", "readytopay", "approvethisinvoice", "approvethisbill"),
    "bank_or_remit_change": (
        "newbankaccount",
        "newaccount",
        "updateourbank",
        "bankdetailshavechanged",
        "accounthaschanged",
        "changedbanks",
        "newroutingnumber",
        "updatetheremitto",
        "updateremitto",
    ),
    "urgency_pressure": ("paytoday", "payimmediately"),
}

_ORDER: tuple[InjectionCategory, ...] = (
    "instruction_to_ai",
    "approval_override",
    "bank_or_remit_change",
    "urgency_pressure",
)


def categorize(text: str) -> list[InjectionCategory]:
    """Deterministic keyword categories for revealed hidden/faint text (empty if none).

    This only labels text the deterministic detectors already flagged; the LLM classifier
    makes the general call. It errs towards labelling (a false label on hidden text costs a
    human look; the document is quarantined either way)."""
    flat = norm(text)
    found: set[InjectionCategory] = set()
    for cat, pat in _PATTERNS.items():
        if pat.search(text):
            found.add(cat)
    for cat, keys in _FLAT.items():
        if any(k in flat for k in keys):
            found.add(cat)
    return [c for c in _ORDER if c in found]


# ----------------------------------------------------------------------------- findings
@dataclass
class Finding:
    """One flagged word (raw text; masking happens when it becomes a ScreenSpan)."""

    page: int
    text: str
    box: Box
    reasons: tuple[str, ...]
    order: int = 0  # position in the source list (text-layer / enhanced-OCR order)
    extra: dict[str, float] = field(default_factory=dict)


@dataclass
class Group:
    page: int
    texts: list[str]
    boxes: list[Box]
    reasons: list[str]

    @property
    def text(self) -> str:
        return " ".join(t for t in self.texts if t.strip())

    @property
    def box(self) -> Box:
        return union_box(self.boxes)


def _same_line(a: Box, b: Box) -> bool:
    ca, cb = (a[1] + a[3]) / 2, (b[1] + b[3]) / 2
    h = max(a[3] - a[1], b[3] - b[1], 1)
    return abs(ca - cb) <= 0.6 * h


def group_findings(findings: Sequence[Finding], *, max_chars: int = 2000) -> list[Group]:
    """Merge consecutive findings on the same page and line into spans (reading order kept).
    Never drops text: a group only splits when it would exceed ``max_chars``."""
    groups: list[Group] = []
    for f in sorted(findings, key=lambda f: (f.page, f.order)):
        g = groups[-1] if groups else None
        if (
            g is not None
            and g.page == f.page
            and _same_line(g.boxes[-1], f.box)
            and len(g.text) + len(f.text) + 1 <= max_chars
        ):
            g.texts.append(f.text)
            g.boxes.append(f.box)
            for r in f.reasons:
                if r not in g.reasons:
                    g.reasons.append(r)
        else:
            groups.append(Group(f.page, [f.text], [f.box], list(f.reasons)))
    return groups
