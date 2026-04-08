"""Shared IO, dataframe, and command helpers for Streamlit pages."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import sqlite3
from io import StringIO
import subprocess
import sys
import urllib.request
from urllib.error import URLError
from typing import Iterable

import pandas as pd
import streamlit as st

from curation_app.config import DEFAULT_CURATORS_FILE, ROOT_DIR

FINAL_REVIEW_STATUSES = {"approved"}
ORCID_RECORD_API = "https://pub.orcid.org/v3.0"
LEDGER_COLUMNS = [
    "batch_id",
    "pivot_source",
    "target_id",
    "target_backend",
    "source_term_source",
    "source_term_iri",
    "source_term_label",
    "source_term_kind",
    "canonical_term_iri",
    "canonical_term_label",
    "canonical_term_source",
    "canonical_term_kind",
    "relation",
    "status",
    "curator",
    "curator_name",
    "reviewer",
    "reviewer_name",
    "date_reviewed",
    "curation_comment",
    "co_curators",
    "co_curator_names",
]


@dataclass(frozen=True)
class CommandResult:
    """Captured execution result for one subprocess call."""

    command: str
    returncode: int
    stdout: str
    stderr: str


def to_path(value: str | Path) -> Path:
    """Return resolved absolute path from relative/absolute user input."""
    path = Path(value)
    if path.is_absolute():
        return path
    return (ROOT_DIR / path).resolve()


def normalize_source_value(value: object) -> str:
    """Normalize ontology/source identifiers to lowercase text."""
    return str(value or "").strip().lower()


def to_relpath(value: str | Path) -> str:
    """Render path relative to repo root when possible."""
    path = to_path(value)
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def run_python_script(script_name: str, args: Iterable[str]) -> CommandResult:
    """Run one repository Python script and capture output."""
    script_path = (ROOT_DIR / script_name).resolve()
    cmd = [sys.executable, str(script_path), *list(args)]
    completed = subprocess.run(
        cmd,
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    command_text = " ".join(shlex.quote(part) for part in cmd)
    return CommandResult(
        command=command_text,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def show_command_result(result: CommandResult) -> None:
    """Render a consistent result block for command execution."""
    if result.returncode == 0:
        st.success("Command completed successfully.")
    else:
        st.error(f"Command failed with exit code {result.returncode}.")

    st.caption(f"Executed: `{result.command}`")
    if result.stdout.strip():
        st.code(result.stdout.strip(), language="text")
    if result.stderr.strip():
        st.code(result.stderr.strip(), language="text")


def list_files(directory: str | Path, pattern: str) -> list[Path]:
    """List matching files sorted by name."""
    root = to_path(directory)
    if not root.is_dir():
        return []
    return sorted(root.glob(pattern))


def read_tsv(path: str | Path) -> pd.DataFrame:
    """Read TSV into a string-typed dataframe with empty-string NA policy."""
    target = to_path(path)
    if not target.is_file():
        return pd.DataFrame()
    return pd.read_csv(target, sep="\t", dtype=str, keep_default_na=False)


def write_tsv(df: pd.DataFrame, path: str | Path) -> None:
    """Write dataframe to TSV and create parent directories if needed."""
    target = to_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    buffer = StringIO()
    df.to_csv(buffer, sep="\t", index=False, lineterminator="\n")
    content = buffer.getvalue()
    if target.is_file():
        existing = target.read_text(encoding="utf-8", errors="replace")
        if existing == content:
            return
    target.write_text(content, encoding="utf-8")


def _is_http_link(value: object) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://")


def link_column_config(df: pd.DataFrame) -> dict[str, object]:
    """Infer Streamlit LinkColumn config for URL/IRI-like columns."""
    config: dict[str, object] = {}
    for column in df.columns:
        name = str(column).lower()
        if not any(token in name for token in ("url", "iri", "link")):
            continue
        series = df[column] if column in df.columns else pd.Series(dtype=str)
        if series.empty:
            continue
        if not any(_is_http_link(value) for value in series.head(1000).tolist()):
            continue
        config[str(column)] = st.column_config.LinkColumn(str(column))
    return config


def render_clickable_dataframe(
    df: pd.DataFrame,
    *,
    use_container_width: bool = True,
    hide_index: bool = True,
) -> None:
    """Render a dataframe with inferred clickable link columns."""
    st.dataframe(
        df,
        use_container_width=use_container_width,
        hide_index=hide_index,
        column_config=link_column_config(df),
    )


def dataframe_to_tsv_bytes(df: pd.DataFrame) -> bytes:
    """Encode a dataframe as UTF-8 TSV bytes."""
    return df.to_csv(sep="\t", index=False).encode("utf-8")


def file_to_bytes(path: str | Path) -> bytes:
    """Read raw bytes from a file."""
    return to_path(path).read_bytes()


def render_table_preview(path: str | Path, *, max_rows: int = 200, key: str) -> pd.DataFrame:
    """Render a TSV preview and return the loaded dataframe."""
    df = read_tsv(path)
    target = to_path(path)
    if not target.is_file():
        st.info(f"File not found: `{to_relpath(path)}`")
        return df

    st.caption(f"Preview: `{to_relpath(path)}` ({len(df)} row(s))")
    render_clickable_dataframe(df.head(max_rows), use_container_width=True, hide_index=True)
    st.download_button(
        label="Download TSV",
        data=file_to_bytes(target),
        file_name=target.name,
        mime="text/tab-separated-values",
        key=f"download_{key}",
    )
    return df


def render_file_download(path: str | Path, *, label: str, key: str) -> None:
    """Render a file download button if the file exists."""
    target = to_path(path)
    if not target.is_file():
        return
    st.download_button(
        label=label,
        data=file_to_bytes(target),
        file_name=target.name,
        mime="application/octet-stream",
        key=key,
    )


def utc_now_timestamp() -> str:
    """Return UTC timestamp in compact ISO-8601 form."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_notes_for_approval(notes: str) -> str:
    """Normalize default notes when an item becomes approved."""
    text = (notes or "").strip()
    lower = text.lower()
    if not text or ("auto-suggested" in lower and "before approval" in lower):
        return "Approved after manual review."
    return text


def normalize_orcid(value: str) -> str:
    """Normalize raw ORCID input to canonical hyphenated form."""
    text = (value or "").strip()
    if not text:
        return ""
    if text.lower().startswith("https://orcid.org/"):
        text = text.rsplit("/", 1)[-1]
    digits = text.replace("-", "").upper()
    if len(digits) != 16:
        return text
    return f"{digits[0:4]}-{digits[4:8]}-{digits[8:12]}-{digits[12:16]}"


def is_valid_orcid(value: str) -> bool:
    """Validate ORCID format and checksum."""
    normalized = normalize_orcid(value)
    parts = normalized.split("-")
    if len(parts) != 4 or any(len(part) != 4 for part in parts):
        return False
    digits = "".join(parts).upper()
    if not digits[:-1].isdigit() or not (digits[-1].isdigit() or digits[-1] == "X"):
        return False

    total = 0
    for char in digits[:-1]:
        total = (total + int(char)) * 2
    remainder = total % 11
    result = (12 - remainder) % 11
    checksum = "X" if result == 10 else str(result)
    return checksum == digits[-1]


@st.cache_data(show_spinner=False, ttl=86400)
def fetch_orcid_display_name(orcid: str) -> tuple[str, str]:
    """Fetch public display name from the ORCID public API.

    Returns (display_name, error_message). `display_name` is empty when not found.
    """
    normalized = normalize_orcid(orcid)
    if not is_valid_orcid(normalized):
        return "", "Invalid ORCID format."

    url = f"{ORCID_RECORD_API}/{normalized}/person"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except URLError:
        return "", "ORCID lookup failed."
    except Exception:
        return "", "ORCID lookup failed."

    name_obj = payload.get("name", {}) if isinstance(payload, dict) else {}
    given = ""
    family = ""
    if isinstance(name_obj, dict):
        given_obj = name_obj.get("given-names", {})
        family_obj = name_obj.get("family-name", {})
        if isinstance(given_obj, dict):
            given = str(given_obj.get("value", "") or "").strip()
        if isinstance(family_obj, dict):
            family = str(family_obj.get("value", "") or "").strip()
    display_name = " ".join(part for part in (given, family) if part).strip()
    if display_name:
        return display_name, ""
    return "", "No public name found for this ORCID."


def read_curators() -> list[tuple[str, str]]:
    """Return list of (orcid, name) pairs from the curators registry file."""
    if not DEFAULT_CURATORS_FILE.is_file():
        return []
    try:
        df = pd.read_csv(DEFAULT_CURATORS_FILE, sep="\t", dtype=str).fillna("")
        if {"orcid", "name"}.issubset(df.columns):
            return [(str(row["orcid"]).strip(), str(row["name"]).strip()) for _, row in df.iterrows() if str(row["orcid"]).strip()]
    except Exception:
        pass
    return []


def save_curator(orcid: str, name: str) -> None:
    """Persist a curator (orcid, name) to the curators registry file if not already present."""
    orcid = orcid.strip()
    name = name.strip()
    if not orcid:
        return
    existing = read_curators()
    known_orcids = {o for o, _ in existing}
    if orcid in known_orcids:
        # Update name if it changed
        rows = [(o, name if o == orcid else n) for o, n in existing]
    else:
        rows = existing + [(orcid, name)]
    df = pd.DataFrame(rows, columns=["orcid", "name"])
    DEFAULT_CURATORS_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(DEFAULT_CURATORS_FILE, sep="\t", index=False)


def should_track_review_row(row: dict[str, object] | pd.Series) -> bool:
    """Return True when a row belongs in the versioned review ledger."""
    status = str((row.get("status", "") if hasattr(row, "get") else "") or "").strip().lower()
    return status in FINAL_REVIEW_STATUSES


def ledger_identity(row: dict[str, object] | pd.Series) -> str:
    """Use (source term IRI, canonical term IRI) as the shared-ledger identity."""
    source_iri = str((row.get("source_term_iri", "") if hasattr(row, "get") else "") or "").strip()
    canonical_iri = str((row.get("canonical_term_iri", "") if hasattr(row, "get") else "") or "").strip()
    return f"{source_iri}\t{canonical_iri}"


def project_review_row(row: dict[str, object] | pd.Series) -> dict[str, str]:
    """Project one approved queue row into the minimal shared ledger schema."""
    canonical_from = str((row.get("canonical_from", "") if hasattr(row, "get") else "") or "").strip().lower()
    left_kind = str((row.get("left_term_kind", "") if hasattr(row, "get") else "") or "").strip()
    right_kind = str((row.get("right_term_kind", "") if hasattr(row, "get") else "") or "").strip()
    canonical_kind = str((row.get("canonical_term_kind", "") if hasattr(row, "get") else "") or "").strip()
    if not canonical_kind:
        if canonical_from == "left":
            canonical_kind = left_kind
        elif canonical_from == "right":
            canonical_kind = right_kind
        elif str((row.get("canonical_term_iri", "") if hasattr(row, "get") else "") or "").strip() == str(
            (row.get("left_term_iri", "") if hasattr(row, "get") else "") or ""
        ).strip():
            canonical_kind = left_kind
        else:
            canonical_kind = right_kind

    return {
        "batch_id": str((row.get("batch_id", "") if hasattr(row, "get") else "") or "").strip(),
        "pivot_source": str((row.get("pivot_source", "") if hasattr(row, "get") else "") or "").strip(),
        "target_id": str((row.get("target_id", "") if hasattr(row, "get") else "") or "").strip(),
        "target_backend": str((row.get("target_backend", "") if hasattr(row, "get") else "") or "").strip(),
        "source_term_source": normalize_source_value(
            (row.get("left_source", "") if hasattr(row, "get") else "") or ""
        ),
        "source_term_iri": str((row.get("left_term_iri", "") if hasattr(row, "get") else "") or "").strip(),
        "source_term_label": str((row.get("left_label", "") if hasattr(row, "get") else "") or "").strip(),
        "source_term_kind": left_kind,
        "canonical_term_iri": str((row.get("canonical_term_iri", "") if hasattr(row, "get") else "") or "").strip(),
        "canonical_term_label": str((row.get("canonical_term_label", "") if hasattr(row, "get") else "") or "").strip(),
        "canonical_term_source": normalize_source_value(
            (row.get("canonical_term_source", "") if hasattr(row, "get") else "") or ""
        ),
        "canonical_term_kind": canonical_kind,
        "relation": str((row.get("relation", "") if hasattr(row, "get") else "") or "").strip(),
        "status": str((row.get("status", "") if hasattr(row, "get") else "") or "").strip(),
        "curator": str((row.get("curator", "") if hasattr(row, "get") else "") or "").strip(),
        "curator_name": str((row.get("curator_name", "") if hasattr(row, "get") else "") or "").strip(),
        "reviewer": str((row.get("reviewer", "") if hasattr(row, "get") else "") or "").strip(),
        "reviewer_name": str((row.get("reviewer_name", "") if hasattr(row, "get") else "") or "").strip(),
        "date_reviewed": str((row.get("date_reviewed", "") if hasattr(row, "get") else "") or "").strip(),
        "curation_comment": str((row.get("curation_comment", "") if hasattr(row, "get") else "") or "").strip(),
        "co_curators": str((row.get("co_curators", "") if hasattr(row, "get") else "") or "").strip(),
        "co_curator_names": str((row.get("co_curator_names", "") if hasattr(row, "get") else "") or "").strip(),
    }


def sync_review_ledger(
    review_df: pd.DataFrame,
    queue_df: pd.DataFrame,
    touched_source_iris: set[str] | None = None,
) -> pd.DataFrame:
    """Append new finalized rows from the local queue into the versioned review ledger.

    Existing approved ledger rows are preserved as-is so normal curation of
    `needs_review` candidates cannot silently overwrite prior review provenance
    for the same (source_term_iri, canonical_term_iri) pair.
    """
    merged_cols = list(LEDGER_COLUMNS)
    if review_df.empty:
        ledger = pd.DataFrame(columns=merged_cols)
    else:
        ledger = review_df.copy()
        for col in merged_cols:
            if col not in ledger.columns:
                ledger[col] = ""
        ledger = ledger.reindex(columns=merged_cols, fill_value="")

    tracked = queue_df[queue_df.apply(should_track_review_row, axis=1)].copy()
    if touched_source_iris is not None:
        touched = {str(value or "").strip() for value in touched_source_iris if str(value or "").strip()}
        if not touched:
            return ledger.reindex(columns=merged_cols, fill_value="")
        tracked = tracked[tracked["left_term_iri"].astype(str).isin(touched)].copy()
    if tracked.empty:
        return ledger.reindex(columns=merged_cols, fill_value="")

    tracked_rows = [project_review_row(row) for _, row in tracked.iterrows()]
    tracked = pd.DataFrame(tracked_rows, columns=merged_cols)

    pair_to_idx: dict[str, int] = {}
    for idx, row in ledger.iterrows():
        pair_to_idx[ledger_identity(row)] = idx

    for _, row in tracked.iterrows():
        pair_key = ledger_identity(row)
        existing_idx = pair_to_idx.get(pair_key)

        row_values = {col: str(row.get(col, "") or "") for col in merged_cols}
        if existing_idx is None:
            ledger = pd.concat([ledger, pd.DataFrame([row_values], columns=merged_cols)], ignore_index=True)
            new_idx = int(ledger.index[-1])
            pair_to_idx[pair_key] = new_idx

    ledger = ledger.reindex(columns=merged_cols, fill_value="")
    if not ledger.empty:
        ledger = ledger.sort_values(
            by=["source_term_source", "source_term_iri"],
            kind="stable",
            na_position="last",
        ).reset_index(drop=True)
    return ledger


def sqlite_tables(db_path: str | Path) -> list[str]:
    """List user tables from a SQLite database."""
    target = to_path(db_path)
    if not target.is_file():
        return []
    with sqlite3.connect(target) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return [row[0] for row in rows]


def sqlite_query(db_path: str | Path, query: str) -> pd.DataFrame:
    """Execute a query and return dataframe output."""
    target = to_path(db_path)
    if not target.is_file():
        return pd.DataFrame()
    with sqlite3.connect(target) as conn:
        return pd.read_sql_query(query, conn)
