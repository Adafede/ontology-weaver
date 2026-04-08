"""Workflow context utilities for source-centric and alignment-batch pages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import pandas as pd
import streamlit as st

from curation_app.config import (
    DEFAULT_ALIGNMENT_BATCHES,
    DEFAULT_MANIFEST,
    DOWNLOADS_DIR,
    IMPORTS_DIR,
    REGISTRY_DIR,
    WORK_DIR,
)
from curation_app.helpers import read_tsv


SOURCE_NAMESPACE_HINTS = {
    "emi": "https://w3id.org/emi#",
}
ALIGNMENT_BATCH_COLUMNS = [
    "batch_id",
    "pivot_source",
    "target_id",
    "target_label",
    "target_backend",
    "enabled",
    "description",
]
STATE_SOURCE_ID = "active_source_id"
STATE_BATCH_ID = "active_batch_id"


@dataclass(frozen=True)
class SourceContext:
    """Derived workflow paths and labels for an active source slug."""

    source_id: str
    source_label: str
    download_ttl: Path
    terms_tsv: Path
    review_tsv: Path
    queue_tsv: Path
    namespace_prefix: str


@dataclass(frozen=True)
class AlignmentBatchContext:
    """Derived workflow paths and labels for one alignment batch."""

    batch_id: str
    batch_label: str
    pivot_source: str
    source_id: str
    source_label: str
    target_id: str
    target_ids: tuple[str, ...]
    target_label: str
    target_backend: str
    description: str
    download_ttl: Path
    terms_tsv: Path
    review_tsv: Path
    queue_tsv: Path
    namespace_prefix: str


def load_manifest() -> pd.DataFrame:
    """Load source manifest."""
    df = read_tsv(DEFAULT_MANIFEST)
    if df.empty:
        return df
    for col in ("source_id", "enabled", "url", "description"):
        if col not in df.columns:
            df[col] = ""
    return df


def load_batch_manifest() -> pd.DataFrame:
    """Load alignment-batch manifest."""
    df = read_tsv(DEFAULT_ALIGNMENT_BATCHES)
    if df.empty:
        return df
    for col in ALIGNMENT_BATCH_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df["batch_id"] = df["batch_id"].astype(str).str.strip().str.lower()
    df["pivot_source"] = df["pivot_source"].astype(str).str.strip().str.lower()
    df["target_backend"] = df["target_backend"].astype(str).str.strip().str.lower()
    return df


def source_ids(manifest_df: pd.DataFrame) -> list[str]:
    """Return manifest source ids in table order."""
    if manifest_df.empty:
        return []
    return [value.strip() for value in manifest_df["source_id"].tolist() if str(value).strip()]


def enabled_source_ids(manifest_df: pd.DataFrame) -> list[str]:
    """Return enabled source ids."""
    if manifest_df.empty:
        return []
    mask = manifest_df["enabled"].astype(str).str.lower().isin(["1", "true", "yes", "y", "on"])
    return [value.strip() for value in manifest_df.loc[mask, "source_id"].tolist() if str(value).strip()]


def batch_ids(batch_df: pd.DataFrame) -> list[str]:
    """Return manifest batch ids in table order."""
    if batch_df.empty:
        return []
    return [value.strip() for value in batch_df["batch_id"].tolist() if str(value).strip()]


def enabled_batch_ids(batch_df: pd.DataFrame) -> list[str]:
    """Return enabled batch ids."""
    if batch_df.empty:
        return []
    mask = batch_df["enabled"].astype(str).str.lower().isin(["1", "true", "yes", "y", "on"])
    return [value.strip() for value in batch_df.loc[mask, "batch_id"].tolist() if str(value).strip()]


def _fallback_ttl(source_id: str) -> Path:
    return DOWNLOADS_DIR / f"{source_id}.ttl"


def _normalize_csv_values(value: str) -> tuple[str, ...]:
    tokens = re.split(r"[,\n|]+", value or "")
    normalized = [token.strip().lower() for token in tokens if token.strip()]
    return tuple(normalized)


def source_context(source_id: str, manifest_df: pd.DataFrame) -> SourceContext:
    """Build source-derived workflow context."""
    slug = source_id.strip().lower()
    return SourceContext(
        source_id=slug,
        source_label=slug.upper(),
        download_ttl=_fallback_ttl(slug),
        terms_tsv=IMPORTS_DIR / f"{slug}_terms.tsv",
        review_tsv=REGISTRY_DIR / f"pair_alignment_candidates_{slug}.tsv",
        queue_tsv=WORK_DIR / f"pair_alignment_candidates_{slug}.tsv",
        namespace_prefix=SOURCE_NAMESPACE_HINTS.get(slug, ""),
    )


def batch_context(batch_id: str, batch_df: pd.DataFrame, manifest_df: pd.DataFrame) -> AlignmentBatchContext:
    """Build batch-derived workflow context."""
    slug = batch_id.strip().lower()
    if batch_df.empty or "batch_id" not in batch_df.columns:
        raise KeyError(f"Unknown batch_id: {batch_id}")
    match = batch_df[batch_df["batch_id"].astype(str).str.strip().str.lower() == slug]
    if match.empty:
        raise KeyError(f"Unknown batch_id: {batch_id}")
    row = match.iloc[0]
    pivot_source = str(row.get("pivot_source", "") or "").strip().lower()
    if not pivot_source:
        raise KeyError(f"Batch {batch_id} is missing pivot_source")
    pivot_ctx = source_context(pivot_source, manifest_df)
    target_id = str(row.get("target_id", "") or "").strip()
    target_label = str(row.get("target_label", "") or "").strip() or target_id or slug
    target_backend = str(row.get("target_backend", "") or "").strip().lower()
    return AlignmentBatchContext(
        batch_id=slug,
        batch_label=f"{pivot_source.upper()} vs {target_label}",
        pivot_source=pivot_source,
        source_id=pivot_source,
        source_label=pivot_ctx.source_label,
        target_id=target_id,
        target_ids=_normalize_csv_values(target_id),
        target_label=target_label,
        target_backend=target_backend,
        description=str(row.get("description", "") or "").strip(),
        download_ttl=pivot_ctx.download_ttl,
        terms_tsv=pivot_ctx.terms_tsv,
        review_tsv=REGISTRY_DIR / f"pair_alignment_candidates_{slug}.tsv",
        queue_tsv=WORK_DIR / f"pair_alignment_candidates_{slug}.tsv",
        namespace_prefix=pivot_ctx.namespace_prefix,
    )


def active_source_context() -> SourceContext | None:
    """Return active source context from Streamlit session state."""
    manifest_df = load_manifest()
    ids = enabled_source_ids(manifest_df) or source_ids(manifest_df)
    if not ids:
        return None

    selected = str(st.session_state.get(STATE_SOURCE_ID, ids[0])).strip().lower()
    if selected not in ids:
        selected = ids[0]
        st.session_state[STATE_SOURCE_ID] = selected
    return source_context(selected, manifest_df)


def active_alignment_context() -> AlignmentBatchContext | None:
    """Return active batch context from Streamlit session state."""
    manifest_df = load_manifest()
    batch_df = load_batch_manifest()
    ids = enabled_batch_ids(batch_df) or batch_ids(batch_df)
    if not ids:
        return None
    selected = str(st.session_state.get(STATE_BATCH_ID, ids[0])).strip().lower()
    if selected not in ids:
        selected = ids[0]
        st.session_state[STATE_BATCH_ID] = selected
    return batch_context(selected, batch_df, manifest_df)


def stamp_batch_record(record: dict[str, object], ctx: AlignmentBatchContext) -> dict[str, str]:
    """Return a row dict stamped with batch metadata."""
    out = {str(k): str(v or "") for k, v in record.items()}
    out["batch_id"] = ctx.batch_id
    out["pivot_source"] = ctx.pivot_source
    out["target_id"] = ctx.target_id
    out["target_backend"] = ctx.target_backend
    return out


def stamp_batch_metadata(df: pd.DataFrame, ctx: AlignmentBatchContext) -> pd.DataFrame:
    """Return dataframe stamped with the active batch metadata."""
    out = df.copy()
    out["batch_id"] = ctx.batch_id
    out["pivot_source"] = ctx.pivot_source
    out["target_id"] = ctx.target_id
    out["target_backend"] = ctx.target_backend
    return out
