"""Main Streamlit entrypoint for ontology curation workflow."""

from __future__ import annotations

import streamlit as st

from curation_app.auto_sync import STATE_SYNC_LAST_ERROR, auto_sync_sqlite
from curation_app.context import (
    STATE_BATCH_ID,
    enabled_batch_ids,
    enabled_source_ids,
    load_batch_manifest,
    load_manifest,
    source_context,
    source_ids,
    batch_context,
    batch_ids,
)
from curation_app.helpers import fetch_orcid_display_name, is_valid_orcid, normalize_orcid, read_curators, save_curator
from curation_app.pages import (
    add_terms,
    curate_candidates,
    download_sources,
    extract_terms,
    finalize_validate,
    generate_candidates,
    ols_ontologies,
    overview,
    sqlite_inspect,
    view_schema,
)

STATE_SOURCE_ID = "active_source_id"
STATE_CURATOR = "active_curator"
STATE_CURATOR_INPUT = "active_curator_input"
STATE_CURATOR_NAME = "active_curator_name"
STATE_GROUP_MODE = "group_session_active"
STATE_SESSION_CURATORS = "group_session_curators"  # list[tuple[str, str]] (orcid, name)
STATE_PAGE = "active_page"

ALIGNMENT_PAGES = {
    "Generate candidates",
    "Add terms",
    "Curate candidates",
    "Review and export",
}

PAGES = {
    "Overview": overview.render,
    "Fetch schemas": download_sources.render,
    "OLS catalog": ols_ontologies.render,
    "Extract terms": extract_terms.render,
    "Generate candidates": generate_candidates.render,
    "Add terms": add_terms.render,
    "Curate candidates": curate_candidates.render,
    "Review and export": finalize_validate.render,
    "View schema": view_schema.render,
    "Inspect SQLite": sqlite_inspect.render,
}

PAGE_GROUPS = {
    "Overview": ["Overview"],
    "Fetch schemas and ontologies": [
        "Fetch schemas",
        "OLS catalog",
    ],
    "Alignment workflow": [
        "Extract terms",
        "Generate candidates",
        "Add terms",
        "Curate candidates",
        "Review and export",
    ],
    "Data inspection": ["Inspect SQLite", "View schema"],
}


def main() -> None:
    st.set_page_config(
        page_title="Ontology Alignment Curation",
        layout="wide",
    )

    if STATE_PAGE not in st.session_state or st.session_state[STATE_PAGE] not in PAGES:
        st.session_state[STATE_PAGE] = "Overview"
    selected_page = st.session_state[STATE_PAGE]

    manifest_df = load_manifest()
    batch_manifest_df = load_batch_manifest()
    all_source_ids = source_ids(manifest_df)
    enabled_ids = enabled_source_ids(manifest_df)
    available_ids = enabled_ids or all_source_ids
    all_batch_ids = batch_ids(batch_manifest_df)
    enabled_batches = enabled_batch_ids(batch_manifest_df)
    available_batches = enabled_batches or all_batch_ids

    st.sidebar.title("Workflow")
    if available_ids:
        selected_source_id = str(st.session_state.get(STATE_SOURCE_ID, available_ids[0]) or "").strip().lower()
        if selected_source_id not in available_ids:
            selected_source_id = available_ids[0]
        if selected_page not in ALIGNMENT_PAGES:
            default_idx = available_ids.index(selected_source_id) if selected_source_id in available_ids else 0
            selected_source_id = st.sidebar.selectbox(
                "Source ID",
                options=available_ids,
                index=default_idx,
                help="Single source slug reused automatically across source-centric workflow steps.",
            )
            st.session_state[STATE_SOURCE_ID] = selected_source_id
        _NEW_CURATOR_OPTION = "— Add new curator —"
        known_curators = read_curators()
        curator_options = [f"{name} ({orcid})" for orcid, name in known_curators] + [_NEW_CURATOR_OPTION]
        active_curator = str(st.session_state.get(STATE_CURATOR, "") or "").strip()
        # Determine the default selectbox index
        _active_idx = next(
            (i for i, (o, _) in enumerate(known_curators) if o == active_curator),
            len(curator_options) - 1,
        )
        selected_curator_option = st.sidebar.selectbox(
            "Active curator",
            options=curator_options,
            index=_active_idx,
            help="Select an existing curator or add a new one by entering their ORCID.",
        )
        if selected_curator_option == _NEW_CURATOR_OPTION:
            curator_input = st.sidebar.text_input(
                "New curator ORCID",
                value=str(st.session_state.get(STATE_CURATOR_INPUT, "") or "").strip(),
                help="Enter a valid ORCID iD. The public name is fetched automatically from ORCID.",
            )
            st.sidebar.markdown("[Find your ORCID](https://orcid.org/orcid-search/search)")
            st.session_state[STATE_CURATOR_INPUT] = curator_input.strip()
            normalized_orcid = normalize_orcid(curator_input)
            if curator_input.strip():
                if is_valid_orcid(normalized_orcid):
                    display_name, lookup_error = fetch_orcid_display_name(normalized_orcid)
                    if display_name:
                        save_curator(normalized_orcid, display_name)
                        st.session_state[STATE_CURATOR] = normalized_orcid
                        st.session_state[STATE_CURATOR_NAME] = display_name
                        st.sidebar.caption(f"Added: `{display_name}` (`{normalized_orcid}`)")
                        st.rerun()
                    elif lookup_error:
                        st.session_state[STATE_CURATOR] = ""
                        st.session_state[STATE_CURATOR_NAME] = ""
                        st.sidebar.warning(lookup_error)
                else:
                    st.session_state[STATE_CURATOR] = ""
                    st.session_state[STATE_CURATOR_NAME] = ""
                    st.sidebar.error("Enter a valid ORCID iD, for example `0000-0002-1825-0097`.")
            else:
                st.session_state[STATE_CURATOR] = ""
                st.session_state[STATE_CURATOR_NAME] = ""
        else:
            # Resolve selected curator from known list
            _sel_idx = curator_options.index(selected_curator_option)
            sel_orcid, sel_name = known_curators[_sel_idx]
            st.session_state[STATE_CURATOR] = sel_orcid
            st.session_state[STATE_CURATOR_NAME] = sel_name
            st.session_state[STATE_CURATOR_INPUT] = sel_orcid
            st.sidebar.caption(f"ORCID: `{sel_orcid}`")
            st.sidebar.caption(f"Name: `{sel_name}`")

        # --- Group session mode ---
        primary_orcid = str(st.session_state.get(STATE_CURATOR, "") or "").strip()
        group_mode = st.sidebar.checkbox(
            "Group session",
            value=bool(st.session_state.get(STATE_GROUP_MODE, False)),
            help="Enable when multiple curators are working together on a shared screen. All participants are credited.",
            disabled=not primary_orcid,
        )
        st.session_state[STATE_GROUP_MODE] = group_mode
        if group_mode and primary_orcid:
            other_curators = [(o, n) for o, n in known_curators if o != primary_orcid]
            if other_curators:
                prev_co = {o for o, _ in st.session_state.get(STATE_SESSION_CURATORS, []) if o != primary_orcid}
                default_co = [f"{n} ({o})" for o, n in other_curators if o in prev_co]
                co_options = [f"{n} ({o})" for o, n in other_curators]
                selected_co_labels = st.sidebar.multiselect(
                    "Co-curators",
                    options=co_options,
                    default=[d for d in default_co if d in co_options],
                    help="Select the other curators participating in this group session.",
                )
                co_map = {f"{n} ({o})": (o, n) for o, n in other_curators}
                co_selected = [co_map[lbl] for lbl in selected_co_labels if lbl in co_map]
                primary_name = str(st.session_state.get(STATE_CURATOR_NAME, "") or "").strip()
                session_curators = [(primary_orcid, primary_name)] + co_selected
                st.session_state[STATE_SESSION_CURATORS] = session_curators
                if co_selected:
                    names = ", ".join(n for _, n in session_curators)
                    st.sidebar.caption(f"Session: {names}")
            else:
                st.sidebar.info("Add more curators via 'Add new curator' first.")
                st.session_state[STATE_SESSION_CURATORS] = [(primary_orcid, str(st.session_state.get(STATE_CURATOR_NAME, "") or ""))]
        else:
            st.session_state[STATE_SESSION_CURATORS] = []

        if selected_page == "Generate candidates":
            st.sidebar.caption("Generate candidates defines the pivot and target directly on the page.")
            current_batch_id = str(st.session_state.get(STATE_BATCH_ID, "") or "").strip().lower()
            if current_batch_id and current_batch_id in available_batches:
                batch_ctx = batch_context(current_batch_id, batch_manifest_df, manifest_df)
                st.sidebar.caption(f"Current batch: `{batch_ctx.batch_id}`")
        elif available_batches:
            default_batch = str(st.session_state.get(STATE_BATCH_ID, available_batches[0])).strip().lower()
            default_batch_idx = available_batches.index(default_batch) if default_batch in available_batches else 0
            selected_batch_id = st.sidebar.selectbox(
                "Alignment batch",
                options=available_batches,
                index=default_batch_idx,
                help="Frozen pivot-left alignment batch used by Generate, Add, Curate, and Review modules.",
            )
            st.session_state[STATE_BATCH_ID] = selected_batch_id
            batch_ctx = batch_context(selected_batch_id, batch_manifest_df, manifest_df)
            st.sidebar.caption(f"Pivot schema: `{batch_ctx.pivot_source.upper()}`")
            st.sidebar.caption(f"Target: `{batch_ctx.target_label}`")
            st.sidebar.caption(f"Batch ledger: `{batch_ctx.review_tsv.name}`")
            st.sidebar.caption(f"Batch queue: `{batch_ctx.queue_tsv.name}`")
        elif selected_page in ALIGNMENT_PAGES:
            st.sidebar.error("No alignment batch found in registry/alignment_batches.tsv")

        ctx = source_context(selected_source_id, manifest_df)
        if selected_page not in ALIGNMENT_PAGES:
            st.sidebar.caption(f"TTL: `{ctx.download_ttl.name}`")
            st.sidebar.caption(f"Terms: `{ctx.terms_tsv.name}`")
            st.sidebar.caption(f"Review ledger: `{ctx.review_tsv.name}`")
            st.sidebar.caption(f"Local queue: `{ctx.queue_tsv.name}`")
    else:
        st.sidebar.error("No source_id found in registry/external_sources.tsv")

    sync_ok, sync_msg = auto_sync_sqlite(manifest_df, batch_manifest_df)
    if not sync_ok:
        st.sidebar.warning(sync_msg)
        detail = str(st.session_state.get(STATE_SYNC_LAST_ERROR, "")).strip()
        if detail:
            st.sidebar.caption(detail[:300])

    st.sidebar.title("Workflow Modules")
    for section_name, page_names in PAGE_GROUPS.items():
        with st.sidebar.expander(section_name, expanded=(section_name == "Overview")):
            for page_name in page_names:
                is_active = st.session_state[STATE_PAGE] == page_name
                label = f"• {page_name}" if is_active else page_name
                if st.button(label, key=f"nav_{page_name}", use_container_width=True):
                    st.session_state[STATE_PAGE] = page_name
                    st.rerun()
    st.sidebar.caption(f"Current page: `{selected_page}`")
    PAGES[selected_page]()


if __name__ == "__main__":
    main()
