"""Project path and default configuration helpers."""

from __future__ import annotations

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
REGISTRY_DIR = ROOT_DIR / "registry"
SCRIPTS_DIR = ROOT_DIR / "scripts"
DOWNLOADS_DIR = REGISTRY_DIR / "downloads"
IMPORTS_DIR = REGISTRY_DIR / "imports"
WORK_DIR = REGISTRY_DIR / "work"

DEFAULT_CANDIDATES_FILE = REGISTRY_DIR / "pair_alignment_candidates.tsv"
DEFAULT_CURATED_FILE = REGISTRY_DIR / "pair_alignments.tsv"
DEFAULT_RECONCILED_FILE = REGISTRY_DIR / "reconciled_mappings.tsv"
DEFAULT_GROUPS_FILE = REGISTRY_DIR / "reconciled_canonical_groups.tsv"
DEFAULT_SQLITE_DB = REGISTRY_DIR / "alignment_curation.sqlite"
DEFAULT_MANIFEST = REGISTRY_DIR / "external_sources.tsv"
DEFAULT_ALIGNMENT_BATCHES = REGISTRY_DIR / "alignment_batches.tsv"
DEFAULT_OLS_ONTOLOGIES_FILE = REGISTRY_DIR / "ols_ontologies.tsv"
DEFAULT_CURATORS_FILE = REGISTRY_DIR / "curators.tsv"
