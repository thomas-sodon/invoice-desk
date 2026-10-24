"""Pure text normalization used by the rules (vendor names, invoice and work-order numbers).

Two normal forms for names:

* :func:`basic_norm`: NFKC, casefold, ``&`` -> ``and``, punctuation removed, corporate suffixes
  (LLC, Inc, Co, ...) dropped. Two names with the same basic form are the same name.
* :func:`skeleton`: the basic form with diacritics stripped and visually confusable characters
  folded together (Cyrillic/Greek lookalikes, ``0``/``o``, ``1``/``l``/``i``, ``rn``/``m``, ...).
  Two names with the same skeleton but different basic forms are a homoglyph lookalike.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "basic_norm",
    "invoice_number_key",
    "is_mixed_script",
    "skeleton",
    "tokens",
    "work_order_key",
]

_CORP_SUFFIXES = frozenset(
    {
        "llc",
        "lllp",
        "llp",
        "lp",
        "inc",
        "incorporated",
        "co",
        "company",
        "corp",
        "corporation",
        "ltd",
        "limited",
        "pllc",
        "pc",
    }
)

# Characters that render (nearly) identically to a Latin letter, folded to one representative.
# i, l, 1, |, and their lookalikes all fold to "l"; o and 0 to "o".
_CONFUSABLES: dict[str, str] = {
    # Cyrillic
    "а": "a",
    "в": "b",
    "е": "e",
    "ё": "e",
    "к": "k",
    "м": "m",
    "н": "h",
    "о": "o",
    "р": "p",
    "с": "c",
    "т": "t",
    "у": "y",
    "х": "x",
    "і": "l",
    "ї": "l",
    "ј": "j",
    "ѕ": "s",
    "һ": "h",
    "ԁ": "d",
    "ԛ": "q",
    "ԝ": "w",
    "ɡ": "g",
    # Greek
    "α": "a",
    "β": "b",
    "ε": "e",
    "η": "n",
    "ι": "l",
    "κ": "k",
    "ν": "v",
    "ο": "o",
    "ρ": "p",
    "τ": "t",
    "υ": "u",
    "χ": "x",
    "ω": "w",
    # Latin / digits / symbols
    "ı": "l",
    "i": "l",
    "1": "l",
    "|": "l",
    "!": "l",
    "0": "o",
    "5": "s",
    "$": "s",
    "8": "b",
}
_MULTI_CONFUSABLES: tuple[tuple[str, str], ...] = (("rn", "m"), ("vv", "w"), ("cl", "d"))

_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def tokens(text: str) -> list[str]:
    """NFKC-casefolded word tokens (underscores and punctuation split words)."""
    norm = unicodedata.normalize("NFKC", text).casefold().replace("&", " and ")
    return [t for t in _NON_WORD.split(norm.replace("_", " ")) if t]


def basic_norm(name: str) -> str:
    """Name normal form: tokens without corporate suffixes, joined by single spaces."""
    toks = tokens(name)
    while toks and toks[-1] in _CORP_SUFFIXES:
        toks.pop()
    return " ".join(toks)


def _strip_marks(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def skeleton(name: str) -> str:
    """Confusable-folded form of :func:`basic_norm` (spaces removed)."""
    s = _strip_marks(basic_norm(name)).replace(" ", "")
    s = "".join(_CONFUSABLES.get(c, c) for c in s)
    for seq, rep in _MULTI_CONFUSABLES:
        s = s.replace(seq, rep)
    return s


def _script(ch: str) -> str | None:
    name = unicodedata.name(ch, "")
    for script in ("LATIN", "CYRILLIC", "GREEK"):
        if name.startswith(script):
            return script
    return None


def is_mixed_script(text: str) -> bool:
    """True if the letters mix Latin with Cyrillic or Greek (a classic homoglyph attack)."""
    scripts = {_script(c) for c in text if c.isalpha()}
    return "LATIN" in scripts and bool(scripts & {"CYRILLIC", "GREEK"})


def _alnum_upper(text: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", unicodedata.normalize("NFKC", text).upper())


def invoice_number_key(value: str | None) -> str | None:
    """``"INV-002231"``, ``"inv 2231"`` and ``"#2231"`` all map to ``"2231"``."""
    if value is None:
        return None
    key = _alnum_upper(value)
    key = re.sub(r"^(INVOICE|INV|NO|NUMBER)+", "", key)
    key = key.lstrip("0")
    return key or None


def work_order_key(value: str | None) -> str | None:
    """``"WO-1187"``, ``"wo 1187"``, ``"Work Order #01187"`` all map to ``"1187"``."""
    if value is None:
        return None
    key = _alnum_upper(value)
    key = re.sub(r"^(WORKORDER|WO)(NO|NUMBER)?", "", key)
    key = key.lstrip("0")
    return key or None
