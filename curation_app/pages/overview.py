"""Overview dashboard for alignment curation workflow."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from curation_app.context import enabled_source_ids, load_manifest, source_context, source_ids
from curation_app.helpers import read_tsv, read_curators, render_clickable_dataframe, to_relpath

STATE_PAGE = "active_page"


def _nav_button(label: str, page_name: str, help_text: str) -> None:
    if st.button(label, help=help_text, use_container_width=True):
        st.session_state[STATE_PAGE] = page_name
        st.rerun()


def _source_metrics_df() -> pd.DataFrame:
    manifest_df = load_manifest()
    ids = enabled_source_ids(manifest_df) or source_ids(manifest_df)
    rows: list[dict[str, object]] = []
    for source_id in ids:
        ctx = source_context(source_id, manifest_df)

        terms_df = read_tsv(ctx.terms_tsv)
        terms_loaded = len(terms_df) if ctx.terms_tsv.is_file() else 0

        review_df = read_tsv(ctx.review_tsv)
        queue_df = read_tsv(ctx.queue_tsv)
        review_rows = len(review_df) if ctx.review_tsv.is_file() else 0
        queue_rows = len(queue_df) if ctx.queue_tsv.is_file() else 0

        terms_in_scope = 0
        terms_needs_review = 0
        terms_curated = 0
        terms_approved = 0
        progress_pct = 0.0

        review_source_col = "source_term_source" if "source_term_source" in review_df.columns else "left_source"
        review_iri_col = "source_term_iri" if "source_term_iri" in review_df.columns else "left_term_iri"
        if review_rows > 0 and {review_source_col, review_iri_col, "status"}.issubset(review_df.columns):
            grouped = (
                review_df.groupby([review_source_col, review_iri_col], dropna=False)["status"]
                .agg(
                    has_approved=lambda series: any(str(v) == "approved" for v in series),
                )
                .reset_index()
            )
            terms_in_scope = len(grouped)
            terms_curated = terms_in_scope
            terms_approved = int(grouped["has_approved"].sum()) if not grouped.empty else 0
            progress_pct = (100.0 * terms_curated / terms_loaded) if terms_loaded else 0.0
        if queue_rows > 0 and {"status"}.issubset(queue_df.columns):
            terms_needs_review = int((queue_df["status"].astype(str) == "needs_review").sum())

        ttl_status = "yes" if ctx.download_ttl.is_file() else "no"
        rows.append(
            {
                "Source": ctx.source_label,
                "TTL downloaded": ttl_status,
                "Terms loaded": terms_loaded,
                "Review rows": review_rows,
                "Local queue rows": queue_rows,
                "Terms reviewed": terms_curated,
                "Local needs review": terms_needs_review,
                "Approved terms": terms_approved,
                "Progress %": f"{progress_pct:.1f}",
                "TTL file": to_relpath(ctx.download_ttl),
                "Terms file": to_relpath(ctx.terms_tsv),
                "Review file": to_relpath(ctx.review_tsv),
                "Local queue": to_relpath(ctx.queue_tsv),
            }
        )
    return pd.DataFrame(rows)


def _curator_progress_df() -> pd.DataFrame:
    manifest_df = load_manifest()
    ids = enabled_source_ids(manifest_df) or source_ids(manifest_df)
    known_curators = {orcid: name for orcid, name in read_curators()}
    # Accumulate per-curator term counts across all sources
    curator_source_counts: dict[tuple[str, str], dict[str, int]] = {}
    for source_id in ids:
        ctx = source_context(source_id, manifest_df)
        review_df = read_tsv(ctx.review_tsv)
        if review_df.empty:
            continue
        iri_col = "source_term_iri" if "source_term_iri" in review_df.columns else "left_term_iri"
        if "reviewer" not in review_df.columns or iri_col not in review_df.columns:
            continue
        for _, row in review_df.iterrows():
            # Collect all curators: primary reviewer + co_curators in group sessions
            participants: list[tuple[str, str]] = []
            co_orcids_raw = str(row.get("co_curators", "") or "").strip()
            co_names_raw = str(row.get("co_curator_names", "") or "").strip()
            if co_orcids_raw:
                co_orcid_list = [o.strip() for o in co_orcids_raw.split("|") if o.strip()]
                co_name_list = [n.strip() for n in co_names_raw.split(",")]
                for i, o in enumerate(co_orcid_list):
                    n = co_name_list[i] if i < len(co_name_list) else known_curators.get(o, o)
                    participants.append((o, n or o))
            else:
                reviewer = str(row.get("reviewer", "") or "").strip()
                if reviewer and reviewer != "auto":
                    name = str(row.get("reviewer_name", "") or known_curators.get(reviewer, reviewer)).strip() or reviewer
                    participants.append((reviewer, name))
            for orcid, name in participants:
                if not orcid or orcid == "auto":
                    continue
                key = (orcid, name)
                counts = curator_source_counts.setdefault(key, {})
                counts[source_id] = counts.get(source_id, 0) + 1
    if not curator_source_counts:
        return pd.DataFrame()
    rows = []
    for (orcid, name), source_counts in sorted(curator_source_counts.items(), key=lambda x: x[0][1].lower()):
        row: dict[str, object] = {"Curator": name, "ORCID": orcid}
        total = 0
        for source_id in ids:
            count = source_counts.get(source_id, 0)
            row[source_id] = count
            total += count
        row["Total"] = total
        rows.append(row)
    return pd.DataFrame(rows)


def render() -> None:
    st.title("Schema Alignment")
    st.write(
        "Use this dashboard to curate and align local schemas with available ontologies."
    )

    st.subheader("Schema metrics")
    metrics_df = _source_metrics_df()
    if metrics_df.empty:
        st.info("No sources found in manifest yet.")
    else:
        render_clickable_dataframe(metrics_df, use_container_width=True, hide_index=True)
        st.subheader("Shared Approved Coverage")
        for _, row in metrics_df.iterrows():
            source = str(row.get("Source", "") or "-")
            curated = int(row.get("Terms reviewed", 0) or 0)
            total = int(row.get("Terms loaded", 0) or 0)
            pct = (curated / total) if total else 0.0
            st.write(f"**{source}**")
            st.progress(pct, text=f"{curated}/{total} source term(s) approved in shared ledger")

    st.subheader("Curator progress")
    curator_df = _curator_progress_df()
    if curator_df.empty:
        st.info("No curator activity found in review ledgers yet.")
    else:
        st.dataframe(curator_df, use_container_width=True, hide_index=True)

    st.subheader("Recommended flow")
    st.write("1. **Fetch schemas and ontologies**: maintain source manifest, download TTLs, and browse OLS catalog.")
    st.write("2. **Extract terms**: parse local TTL into term TSV with labels and metadata.")
    st.write("3. **Generate candidates**: build left-vs-right or left-vs-OLS candidate matches.")
    st.write("4. **Add terms**: create missing source classes/properties and seed mapping candidates.")
    st.write("5. **Curate candidates**: validate one or more matches (or keep left term) for each left concept.")
    st.write("6. **Review and export**: filter curated dataset and export updated source TTL.")
    st.write("7. **View schema**: inspect ontology documentation before/after curation with pyLODE.")
    st.write("8. **Inspect SQLite**: run table previews and SQL checks on auto-synced reconciliation tables.")

    st.subheader("Open modules")
    c1, c2 = st.columns(2)
    with c1:
        _nav_button(
            "Fetch schemas",
            "Fetch schemas",
            "Manage manifest and download source TTL files.",
        )
        _nav_button(
            "Extract terms",
            "Extract terms",
            "Extract terms from current source TTL.",
        )
        _nav_button(
            "Curate candidates",
            "Curate candidates",
            "Review and validate candidate matches.",
        )
        _nav_button(
            "Add terms",
            "Add terms",
            "Add missing source terms and create candidate mappings.",
        )
        _nav_button(
            "Inspect SQLite",
            "Inspect SQLite",
            "Query auto-synced reconciliation tables.",
        )
    with c2:
        _nav_button(
            "OLS catalog",
            "OLS catalog",
            "Browse/search available OLS ontologies and metadata.",
        )
        _nav_button(
            "Generate candidates",
            "Generate candidates",
            "Generate candidate mappings from extracted terms.",
        )
        _nav_button(
            "Review and export",
            "Review and export",
            "Review curated rows and export updated source TTL.",
        )
        _nav_button(
            "View schema",
            "View schema",
            "Generate and view ontology documentation with pyLODE.",
        )
