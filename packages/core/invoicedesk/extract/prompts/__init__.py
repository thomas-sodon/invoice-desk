"""Frozen extraction prompt prefix (DESIGN §5.4).

The prefix is tier-agnostic and identical for every document: system prompt, schema notes and
two text-only few-shot examples. It carries exactly one cache breakpoint at its end, and it never
contains timestamps, document IDs, trace IDs or anything else that varies per call (that would
break the cached prefix). Caches are per model, so each tier warms its own copy.

Changing any text here is a prompt change: it needs a before/after smoke eval (CLAUDE.md).
"""

from __future__ import annotations

import hashlib
from functools import cache

from invoicedesk.contracts.llm import SystemBlock
from invoicedesk.extract.prompts.few_shot import FEW_SHOT_EXAMPLES, render_few_shots
from invoicedesk.extract.prompts.system import SCHEMA_NOTES, SYSTEM_PROMPT

__all__ = [
    "FEW_SHOT_EXAMPLES",
    "PROMPT_VERSION",
    "SCHEMA_NOTES",
    "SYSTEM_PROMPT",
    "estimate_tokens",
    "extraction_prefix_text",
    "extraction_system_blocks",
]


@cache
def extraction_prefix_text() -> str:
    return "\n\n".join([SYSTEM_PROMPT, SCHEMA_NOTES, render_few_shots()])


@cache
def extraction_system_blocks() -> tuple[SystemBlock, ...]:
    """The frozen, cache-flagged system prefix shared by all three tiers."""
    return (SystemBlock(text=extraction_prefix_text(), cache=True),)


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (~4 characters per token for English prose and JSON)."""
    return len(text) // 4


#: Short content hash of the prefix; recorded with eval runs so prompt changes are visible.
PROMPT_VERSION = "extract-" + hashlib.sha256(extraction_prefix_text().encode()).hexdigest()[:10]
