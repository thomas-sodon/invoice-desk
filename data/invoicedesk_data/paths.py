"""Filesystem layout of the data component (DESIGN §12, ``data/``).

Committed: ``seed/`` (CSVs), ``gold/`` (labels and manifests), ``demo/`` (the 25-file batch).
Gitignored and reproducible by command: ``generated/`` (synthetic-core and adversarial renders)
and ``public/cache/`` (SROIE and CORD downloads, never redistributed).
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "DATA_DIR",
    "DEMO_DIR",
    "GENERATED_DIR",
    "GOLD_DIR",
    "PUBLIC_CACHE_DIR",
    "REPO_ROOT",
    "SEED_DIR",
    "TEMPLATES_DIR",
    "TENANTS_DIR",
]

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR.parent
REPO_ROOT = DATA_DIR.parent
SEED_DIR = DATA_DIR / "seed"
GOLD_DIR = DATA_DIR / "gold"
DEMO_DIR = DATA_DIR / "demo"
GENERATED_DIR = DATA_DIR / "generated"
PUBLIC_CACHE_DIR = DATA_DIR / "public" / "cache"
TEMPLATES_DIR = PACKAGE_DIR / "generator" / "templates"
TENANTS_DIR = REPO_ROOT / "tenants"
