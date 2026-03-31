"""Step 6: sync SQLite and export reconciled outputs."""

from __future__ import annotations

import streamlit as st

from curation_app.config import (
    DEFAULT_GROUPS_FILE,
    DEFAULT_RECONCILED_FILE,
    DEFAULT_SQLITE_DB,
)
from curation_app.context import active_alignment_context
from curation_app.helpers import render_file_download, render_table_preview, run_python_script, show_command_result, to_relpath


def render() -> None:
    st.title("Step 6: Sync SQLite + Export Reconciled Files")
    ctx = active_alignment_context()
    default_review = ctx.review_tsv if ctx else None

    db_path = st.text_input("SQLite DB", value=to_relpath(DEFAULT_SQLITE_DB))
    candidates_file = st.text_input(
        "Review ledger TSV",
        value=to_relpath(default_review) if default_review else "registry/pair_alignment_candidates.tsv",
    )
    curated_file = st.text_input(
        "Approved rows source TSV",
        value=to_relpath(default_review) if default_review else "registry/pair_alignment_candidates.tsv",
    )
    status_filter = st.text_input("Status to export", value="approved")
    reconciled_output = st.text_input("Reconciled mappings TSV", value=to_relpath(DEFAULT_RECONCILED_FILE))
    grouped_output = st.text_input("Canonical groups TSV", value=to_relpath(DEFAULT_GROUPS_FILE))
    st.caption("Recommended setup: point both TSV fields to the same versioned per-batch review ledger.")

    if st.button("Run sync", type="primary"):
        args = [
            "--db",
            db_path,
            "--pair-candidates",
            candidates_file,
            "--pair-alignments",
            curated_file,
            "--status",
            status_filter,
            "--reconciled-output",
            reconciled_output,
            "--grouped-output",
            grouped_output,
        ]
        result = run_python_script("scripts/sync_alignment_sqlite.py", args)
        show_command_result(result)

    st.subheader("Reconciled Mappings")
    render_table_preview(reconciled_output, key="sync_reconciled_preview")

    st.subheader("Canonical Groups")
    render_table_preview(grouped_output, key="sync_groups_preview")

    st.subheader("Exports")
    render_file_download(db_path, label="Download SQLite DB", key="download_db")
