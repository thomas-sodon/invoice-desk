"""Gold labels and manifests (DESIGN §5.6 datasets, §6.2).

Layout (committed)::

    data/gold/<set>/<doc_id>.json     one GoldRecord per document
    data/gold/<set>/manifest.json     ordered entries with split, file and gold hashes

``<set>`` is ``synthetic-core`` (splits ``dev``/``test``), ``adversarial`` or ``demo``. The
binaries live in ``data/generated/<set>/`` (gitignored, rebuilt by ``invoicedesk-data``),
except the demo batch, which is committed in ``data/demo/``.

Evals load gold with :func:`load_gold` / :func:`load_manifest`. ``extraction`` is a valid
``InvoiceExtraction`` (values as printed, confidence 1.0, gold boxes as ``fallback_box``).
CLAUDE.md: never tune on, or edit, the ``test`` split.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from invoicedesk.contracts.extraction import InvoiceExtraction
from pydantic import BaseModel, ConfigDict, Field

from invoicedesk_data.generator.truth import BoxGold, ExpectedOutcome, InvoiceTruth
from invoicedesk_data.paths import GENERATED_DIR, GOLD_DIR

__all__ = [
    "EmailFixture",
    "GoldRecord",
    "Manifest",
    "ManifestEntry",
    "canonical_json",
    "gold_path",
    "load_gold",
    "load_manifest",
    "load_record",
    "sha256_hex",
    "write_set",
]

SetName = Literal["synthetic-core", "adversarial", "demo"]


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


class EmailFixture(BaseModel):
    """An e-mail that carries a document (or stands alone), for the email-ingest path (M2)."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    from_addr: str
    subject: str
    body_text: str
    attachments: list[str] = Field(default_factory=list)  # filenames in the same set


class GoldRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    set: str
    split: str | None = None
    medium: str
    template: str
    filename: str
    mime: str
    sha256: str
    #: SHA-256 of each processed page PNG as ``invoicedesk.preprocess`` produces it (lets the
    #: oracle recognize uploaded page images exactly).
    page_sha256: list[str] = Field(default_factory=list)
    box_eval: bool
    extraction: dict[str, Any]
    boxes: dict[str, BoxGold]
    hidden_boxes: list[BoxGold] = Field(default_factory=list)
    expected: ExpectedOutcome
    truth: InvoiceTruth
    augment: dict[str, Any] = Field(default_factory=dict)
    email: EmailFixture | None = None
    #: Documents (ids in the same set) that must be processed before this one (duplicates).
    priors: list[str] = Field(default_factory=list)
    category: str | None = None  # adversarial category
    description: str = ""

    def invoice_extraction(self) -> InvoiceExtraction:
        return InvoiceExtraction.model_validate(self.extraction)

    def gold_sha256(self) -> str:
        return sha256_hex(canonical_json(self.model_dump(mode="json")))


class ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    split: str | None
    medium: str
    filename: str
    mime: str
    sha256: str
    gold_sha256: str
    expected_status: str
    category: str | None = None
    priors: list[str] = Field(default_factory=list)


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    set: str
    seed: int
    as_of: str
    generator_version: str
    count: int
    counts: dict[str, int]
    files_dir: str  # repo-relative directory holding the binaries
    entries: list[ManifestEntry]
    frozen: bool = False


def gold_path(set_name: str, doc_id: str, gold_dir: Path = GOLD_DIR) -> Path:
    return gold_dir / set_name / f"{doc_id}.json"


def write_set(
    set_name: str,
    records: list[GoldRecord],
    *,
    seed: int,
    as_of: str,
    generator_version: str,
    files_dir: str,
    frozen: bool = False,
    gold_dir: Path = GOLD_DIR,
) -> Manifest:
    out = gold_dir / set_name
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.json"):
        old.unlink()
    counts: dict[str, int] = {}
    entries: list[ManifestEntry] = []
    for r in records:
        (out / f"{r.id}.json").write_text(
            json.dumps(r.model_dump(mode="json"), indent=1, sort_keys=True, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        key = f"{r.split}:{r.medium}" if r.split else r.medium
        counts[key] = counts.get(key, 0) + 1
        entries.append(
            ManifestEntry(
                id=r.id,
                split=r.split,
                medium=r.medium,
                filename=r.filename,
                mime=r.mime,
                sha256=r.sha256,
                gold_sha256=r.gold_sha256(),
                expected_status=str(r.expected.status),
                category=r.category,
                priors=r.priors,
            )
        )
    manifest = Manifest(
        set=set_name,
        seed=seed,
        as_of=as_of,
        generator_version=generator_version,
        count=len(records),
        counts=dict(sorted(counts.items())),
        files_dir=files_dir,
        entries=entries,
        frozen=frozen,
    )
    (out / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def load_manifest(set_name: str, gold_dir: Path = GOLD_DIR) -> Manifest:
    path = gold_dir / set_name / "manifest.json"
    return Manifest.model_validate_json(path.read_text(encoding="utf-8"))


def load_record(set_name: str, doc_id: str, gold_dir: Path = GOLD_DIR) -> GoldRecord:
    return GoldRecord.model_validate_json(
        gold_path(set_name, doc_id, gold_dir).read_text(encoding="utf-8")
    )


def load_gold(
    set_name: str, *, split: str | None = None, gold_dir: Path = GOLD_DIR
) -> list[GoldRecord]:
    """All gold records of a set (optionally one split), in manifest order."""
    m = load_manifest(set_name, gold_dir)
    return [
        load_record(set_name, e.id, gold_dir)
        for e in m.entries
        if split is None or e.split == split
    ]


def file_path(record: GoldRecord, *, base: Path | None = None) -> Path:
    """Where the binary for ``record`` lives (``data/generated/<set>/`` or ``data/demo/``)."""
    if base is not None:
        return base / record.filename
    if record.set == "demo":
        from invoicedesk_data.paths import DEMO_DIR

        return DEMO_DIR / record.filename
    return GENERATED_DIR / record.set / record.filename
