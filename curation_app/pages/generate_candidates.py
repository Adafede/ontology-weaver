"""Candidate generation page."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys

import pandas as pd
import streamlit as st

from curation_app.config import DEFAULT_ALIGNMENT_BATCHES, DEFAULT_OLS_ONTOLOGIES_FILE, REGISTRY_DIR, ROOT_DIR, WORK_DIR
from curation_app.context import (
    ALIGNMENT_BATCH_COLUMNS,
    AlignmentBatchContext,
    STATE_BATCH_ID,
    active_alignment_context,
    enabled_source_ids,
    load_batch_manifest,
    load_manifest,
    source_context,
    source_ids,
    stamp_batch_metadata,
)
from curation_app.helpers import (
    CommandResult,
    normalize_source_value,
    read_tsv,
    render_clickable_dataframe,
    show_command_result,
    to_relpath,
    write_tsv,
)

DEFAULT_OLS_ONTOLOGIES = ["chebi", "obi", "ms", "chmo", "edam"]
STATE_PAGE = "active_page"
STATE_TARGET_BACKEND = "generate_target_backend"


def _ontology_display(
    ontology: str,
    label_map: dict[str, str],
    desc_map: dict[str, str],
) -> str:
    label = label_map.get(ontology, "").strip()
    description = desc_map.get(ontology, "").strip()
    if description and len(description) > 100:
        description = description[:97].rstrip() + "..."
    if label and description:
        return f"{ontology} - {label}: {description}"
    if label:
        return f"{ontology} - {label}"
    return ontology


def _ols_catalog() -> tuple[list[str], dict[str, str], dict[str, str], dict[str, str]]:
    df = read_tsv(DEFAULT_OLS_ONTOLOGIES_FILE)
    if df.empty or "ontology" not in df.columns:
        return DEFAULT_OLS_ONTOLOGIES.copy(), {}, {}, {}

    options: list[str] = []
    label_map: dict[str, str] = {}
    desc_map: dict[str, str] = {}
    url_map: dict[str, str] = {}
    seen: set[str] = set()

    for _, row in df.iterrows():
        ontology = str(row.get("ontology", "") or "").strip().lower()
        if not ontology or ontology in seen:
            continue
        seen.add(ontology)
        options.append(ontology)
        label_map[ontology] = str(row.get("label", "") or "").strip()
        desc_map[ontology] = str(row.get("description", "") or "").strip()
        url_map[ontology] = str(row.get("ols_url", "") or row.get("url", "") or "").strip()

    if not options:
        return DEFAULT_OLS_ONTOLOGIES.copy(), {}, {}, {}
    return options, label_map, desc_map, url_map


def _run_generate_with_progress(args: list[str]) -> CommandResult:
    script_path = (ROOT_DIR / "scripts/suggest_pairwise_alignments.py").resolve()
    cmd = [sys.executable, str(script_path), *args, "--emit-progress"]
    command_text = " ".join(shlex.quote(part) for part in cmd)

    progress_box = st.progress(0.0, text="Starting candidate generation...")
    log_box = st.empty()
    other_lines: list[str] = []
    stderr_lines: list[str] = []

    with subprocess.Popen(
        cmd,
        cwd=ROOT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    ) as proc:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if line.startswith("PROGRESS\t"):
                parts = line.split("\t", 3)
                if len(parts) >= 4:
                    try:
                        current = int(parts[1])
                        total = int(parts[2])
                    except ValueError:
                        current = 0
                        total = 0
                    phase = parts[3].strip() or "processing"
                    frac = (current / total) if total > 0 else 0.0
                    frac = max(0.0, min(1.0, frac))
                    progress_box.progress(frac, text=f"{phase}: {current}/{total}")
                continue
            if line.strip():
                other_lines.append(line)
                log_box.code("\n".join(other_lines[-8:]), language="text")

        # Drain stderr at end.
        if proc.stderr is not None:
            stderr_text = proc.stderr.read().strip()
            if stderr_text:
                stderr_lines.append(stderr_text)
        returncode = proc.wait()

    if returncode == 0:
        progress_box.progress(1.0, text="Generation complete")
    else:
        progress_box.progress(0.0, text="Generation failed")

    return CommandResult(
        command=command_text,
        returncode=returncode,
        stdout="\n".join(other_lines).strip(),
        stderr="\n".join(stderr_lines).strip(),
    )


def _slugify(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "batch"


def _draft_batch_id(pivot_source: str, target_backend: str, target_id: str) -> str:
    pivot_slug = _slugify(pivot_source)
    if target_backend == "ols":
        return f"{pivot_slug}_ols"
    return f"{pivot_slug}_{_slugify(target_id)}"


def _draft_batch_context(
    pivot_source: str,
    target_backend: str,
    target_id: str,
    target_label: str,
    description: str,
    manifest_df: pd.DataFrame,
) -> AlignmentBatchContext:
    pivot_ctx = source_context(pivot_source, manifest_df)
    batch_id = _draft_batch_id(pivot_source, target_backend, target_id)
    return AlignmentBatchContext(
        batch_id=batch_id,
        batch_label=f"{pivot_source.upper()} vs {target_label}",
        pivot_source=pivot_source,
        source_id=pivot_source,
        source_label=pivot_ctx.source_label,
        target_id=target_id,
        target_ids=tuple(token.strip().lower() for token in target_id.split(",") if token.strip()),
        target_label=target_label,
        target_backend=target_backend,
        description=description.strip(),
        download_ttl=pivot_ctx.download_ttl,
        terms_tsv=pivot_ctx.terms_tsv,
        review_tsv=REGISTRY_DIR / f"pair_alignment_candidates_{batch_id}.tsv",
        queue_tsv=WORK_DIR / f"pair_alignment_candidates_{batch_id}.tsv",
        namespace_prefix=pivot_ctx.namespace_prefix,
    )


def _upsert_batch_manifest(ctx: AlignmentBatchContext) -> None:
    batch_df = load_batch_manifest()
    if batch_df.empty:
        batch_df = pd.DataFrame(columns=ALIGNMENT_BATCH_COLUMNS)
    for col in ALIGNMENT_BATCH_COLUMNS:
        if col not in batch_df.columns:
            batch_df[col] = ""
    row = {
        "batch_id": ctx.batch_id,
        "pivot_source": ctx.pivot_source,
        "target_id": ctx.target_id,
        "target_label": ctx.target_label,
        "target_backend": ctx.target_backend,
        "enabled": "1",
        "description": ctx.description,
    }
    mask = batch_df["batch_id"].astype(str).str.strip().str.lower() == ctx.batch_id
    if bool(mask.any()):
        idx = int(batch_df.index[mask][0])
        for key, value in row.items():
            batch_df.at[idx, key] = value
    else:
        batch_df = pd.concat([batch_df, pd.DataFrame([row], columns=ALIGNMENT_BATCH_COLUMNS)], ignore_index=True)
    write_tsv(batch_df.reindex(columns=ALIGNMENT_BATCH_COLUMNS, fill_value=""), DEFAULT_ALIGNMENT_BATCHES)


def render() -> None:
    st.title("Generate Pairwise Candidates")
    st.caption(
        "This module creates candidate matches between the pivot schema on the left and either a downloaded schema or OLS candidates on the right."
    )
    st.markdown(
        "1. Select the pivot schema on the left.\n"
        "2. Select the target as either a downloaded schema or OLS candidates.\n"
        "3. Set optional filters and click **Generate candidates**."
    )

    manifest_df = load_manifest()
    source_options = enabled_source_ids(manifest_df) or source_ids(manifest_df)
    if not source_options:
        st.warning("No source slug found in manifest.")
        return
    active_ctx = active_alignment_context()

    st.subheader("Comparison Setup")
    left_col, right_col = st.columns(2)
    default_pivot = active_ctx.pivot_source if active_ctx else source_options[0]
    if default_pivot not in source_options:
        default_pivot = source_options[0]
    with left_col:
        pivot_source = st.selectbox(
            "Pivot schema",
            options=source_options,
            index=source_options.index(default_pivot),
            format_func=lambda value: value.upper(),
            help="The schema curated on the left side of the alignment UI.",
        )
        pivot_ctx = source_context(pivot_source, manifest_df)
        st.caption(f"Terms: `{to_relpath(pivot_ctx.terms_tsv)}`")

    right_terms_path = None
    selected_ontologies: list[str] = []
    default_backend = active_ctx.target_backend if active_ctx else "ols"
    if default_backend not in {"source", "ols"}:
        default_backend = "ols"
    if STATE_TARGET_BACKEND not in st.session_state:
        st.session_state[STATE_TARGET_BACKEND] = default_backend
    with right_col:
        target_backend_label = st.radio(
            "Target type",
            options=["Downloaded schema", "OLS candidates"],
            index=0 if st.session_state[STATE_TARGET_BACKEND] == "source" else 1,
            horizontal=True,
            key="generate_target_type",
        )
    target_backend = "source" if target_backend_label == "Downloaded schema" else "ols"
    st.session_state[STATE_TARGET_BACKEND] = target_backend

    target_label = ""
    target_id = ""
    description = ""
    if target_backend == "source":
        right_options = [slug for slug in source_options if slug != pivot_source]
        if not right_options:
            st.warning("Add at least one second source schema before running source-vs-source generation.")
            return
        default_right = active_ctx.target_id.strip().lower() if active_ctx and active_ctx.target_backend == "source" else right_options[0]
        if default_right not in right_options:
            default_right = right_options[0]
        with right_col:
            right_slug = st.selectbox(
                "Batch target",
                options=right_options,
                index=right_options.index(default_right),
                format_func=lambda value: value.upper(),
                help="Downloaded schema used as the right side in this batch.",
            )
        right_ctx = source_context(right_slug, manifest_df)
        right_terms_path = right_ctx.terms_tsv
        st.caption(f"Right terms: `{to_relpath(right_ctx.terms_tsv)}`")
        target_label = right_slug.upper()
        target_id = normalize_source_value(right_slug)
        description = f"{pivot_source.upper()} aligned against downloaded schema {right_slug.upper()}."
    else:
        ols_options, label_map, desc_map, url_map = _ols_catalog()
        active_ontologies = active_ctx.target_ids if active_ctx and active_ctx.target_backend == "ols" else ()
        selected_ontologies = [ontology for ontology in active_ontologies if ontology in ols_options]
        if not selected_ontologies:
            selected_ontologies = [o for o in DEFAULT_OLS_ONTOLOGIES if o in ols_options]
        if not selected_ontologies:
            selected_ontologies = ols_options[: min(5, len(ols_options))]
        with right_col:
            selected_ontologies = st.multiselect(
                "Batch target",
                options=ols_options,
                default=selected_ontologies,
                format_func=lambda ontology: _ontology_display(ontology, label_map, desc_map),
                help="OLS ontologies used as the right-side candidate source for this batch.",
            )
            first_url = next(
                (url_map.get(ontology, "").strip() for ontology in selected_ontologies if url_map.get(ontology, "").strip()),
                "",
            )
            if first_url:
                st.link_button("Open first ontology in OLS", first_url, use_container_width=True)
        target_label = "OLS candidates"
        target_id = ""
        description = f"{pivot_source.upper()} aligned against configured OLS ontology candidates."

    ctx = _draft_batch_context(
        pivot_source=pivot_source,
        target_backend=target_backend,
        target_id=target_id,
        target_label=target_label,
        description=description,
        manifest_df=manifest_df,
    )
    st.caption(f"Batch ID: `{ctx.batch_id}`")
    st.caption(f"Review ledger: `{to_relpath(ctx.review_tsv)}`")
    st.caption(f"Local queue: `{to_relpath(ctx.queue_tsv)}`")

    include_existing_curated = st.checkbox(
        "Include pairs already reviewed",
        value=False,
        help="If off, approved rows already present in the shared review ledger are excluded.",
    )

    st.caption("Curator is fixed to `auto` at generation time.")
    top_n_ols = 3
    fetch_metadata = True
    ols_rows = 5
    timeout = 3.0
    max_left_terms = 0
    with st.expander("Advanced settings", expanded=False):
        focus = st.text_input(
            "Focus filter (normalized label contains)",
            value="",
            help=(
                "Optional substring filter on normalized labels (lowercased, punctuation/formatting removed). "
                "Example: 'chemical entity'."
            ),
        )
        if ctx.target_backend == "ols":
            top_n_ols = st.number_input(
                "Top N output hits per left term",
                min_value=1,
                value=3,
                step=1,
                help=(
                    "How many best OLS matches to keep per left term in output candidates."
                ),
            )
            fetch_metadata = st.checkbox(
                "Fetch OLS metadata",
                value=True,
                help="Fetch definition/comment/example for returned OLS suggestions (slower).",
            )
            ols_rows = st.number_input(
                "OLS fetch depth per ontology",
                min_value=1,
                value=5,
                step=1,
                help="Rows requested from OLS API per ontology before Top N output filtering.",
            )
            timeout = st.number_input(
                "OLS request timeout (seconds)",
                min_value=0.5,
                value=3.0,
                step=0.5,
                help="Network timeout per OLS API request.",
            )

    args = [
        "--left-terms",
        to_relpath(pivot_ctx.terms_tsv),
        "--left-source",
        normalize_source_value(pivot_source),
        "--curated-alignments",
        to_relpath(ctx.review_tsv),
        "--output",
        to_relpath(ctx.queue_tsv),
        "--max-left-terms",
        str(max_left_terms),
        "--curator",
        "auto",
    ]
    if focus.strip():
        args.extend(["--focus", focus.strip()])
    if include_existing_curated:
        args.append("--include-existing-curated")

    if ctx.target_backend == "ols":
        ontologies = ",".join(selected_ontologies)
        ols_rows = max(int(top_n_ols), int(ols_rows))

        args.append("--use-ols-api")
        args.extend(["--ontologies", ontologies])
        args.extend(["--ols-rows", str(ols_rows)])
        args.extend(["--top-n-ols", str(top_n_ols)])
        args.extend(["--request-timeout", str(timeout)])
        if fetch_metadata:
            args.append("--ols-fetch-metadata")
    else:
        min_score = st.number_input(
            "Minimum score",
            min_value=0.0,
            max_value=1.0,
            value=0.82,
            step=0.01,
            help="Local-vs-local similarity threshold (0 to 1). Higher is stricter.",
        )
        args.extend(["--right-terms", to_relpath(right_ctx.terms_tsv)])
        args.extend(["--right-source", normalize_source_value(right_slug)])
        args.extend(["--min-score", str(min_score)])

    submitted = st.button("Generate candidates", type="primary")
    if submitted:
        if not pivot_ctx.terms_tsv.is_file():
            st.error(f"Missing terms TSV for pivot source: `{to_relpath(pivot_ctx.terms_tsv)}`")
            return
        if ctx.target_backend == "ols" and not selected_ontologies:
            st.error("Select at least one OLS ontology.")
            return
        if ctx.target_backend == "source" and right_terms_path is not None and not right_terms_path.is_file():
            st.error(f"Missing terms TSV for right source: `{to_relpath(right_terms_path)}`")
            return
        _upsert_batch_manifest(ctx)
        st.session_state[STATE_BATCH_ID] = ctx.batch_id
        result = _run_generate_with_progress(args)
        show_command_result(result)
        if result.returncode == 0:
            queue_df = read_tsv(ctx.queue_tsv)
            if not queue_df.empty:
                queue_df = stamp_batch_metadata(queue_df, ctx)
                write_tsv(queue_df, ctx.queue_tsv)
            st.success("Next step: move to Curate candidates to review and validate matches.")
            if st.button("Go to Curate candidates", key=f"go_curate_{ctx.batch_id}"):
                st.session_state[STATE_PAGE] = "Curate candidates"
                st.rerun()

    st.subheader("Candidates Preview")
    candidates_df = read_tsv(ctx.queue_tsv)
    if candidates_df.empty and not ctx.queue_tsv.is_file():
        st.info("No local queue file yet. Generate candidates first.")
        return
    st.caption(f"Batch queue: `{to_relpath(ctx.queue_tsv)}`")
    st.caption(f"Rows: {len(candidates_df)}")
    render_clickable_dataframe(candidates_df.head(200), use_container_width=True, hide_index=True)
