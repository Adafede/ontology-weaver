"""Add missing source terms and optional mapping candidates."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

import pandas as pd
import streamlit as st

from curation_app.config import DEFAULT_OLS_ONTOLOGIES_FILE
from curation_app.context import active_alignment_context, stamp_batch_metadata
from curation_app.helpers import normalize_source_value, read_tsv, to_relpath, utc_now_timestamp, write_tsv
from curation_app.pages.curate_candidates import (
    REQUIRED_COLUMNS,
    _bioportal_search_url,
    _ensure_columns,
    _fetch_ols_metadata_for_entity,
    _infer_ols_entity_kind,
    _load_mapping_relations_from_local_ontologies,
    _lookup_ols_hit_by_iri,
    _manual_match_score,
    _next_alignment_id,
    _normalize_label,
    _ols_search_url,
)

STATE_CURATOR = "active_curator"
STATE_SEARCH_RESULTS = "add_terms_search_results"
STATE_SEARCH_QUERY = "add_terms_search_query"
STATE_SEARCH_ONTOLOGIES = "add_terms_search_ontologies"
STATE_SELECTED_RESULTS = "add_terms_selected_results"
STATE_PAGE = "active_page"
STATE_LEFT_LABEL = "add_terms_left_label"
STATE_LEFT_KIND = "add_terms_left_kind"
STATE_LEFT_IRI = "add_terms_left_iri"
STATE_LEFT_DEFINITION = "add_terms_left_definition"
STATE_LEFT_COMMENT = "add_terms_left_comment"
STATE_LEFT_EXAMPLE = "add_terms_left_example"
STATE_PENDING_PREFILL = "add_terms_pending_prefill"
STATE_CURATOR_NAME = "active_curator_name"

TERM_REQUIRED_COLUMNS = [
    "iri",
    "label",
    "type",
    "term_kind",
    "definition",
    "comment",
    "example",
    "domain_iris",
    "range_iris",
    "parent_iris",
]


def _ols_term_landing_url(ontology: str, iri: str, short_form: str = "") -> str:
    onto = (ontology or "").strip().lower()
    term_iri = (iri or "").strip()
    if not onto or not term_iri:
        return ""
    base = (
        f"https://www.ebi.ac.uk/ols4/ontologies/{urllib.parse.quote(onto)}/terms"
        f"?iri={urllib.parse.quote(term_iri, safe='')}"
    )
    sf = (short_form or "").strip()
    if sf:
        return f"{base}&sf={urllib.parse.quote(sf, safe='')}"
    return base


def _ols_catalog() -> list[str]:
    df = read_tsv(DEFAULT_OLS_ONTOLOGIES_FILE)
    if df.empty or "ontology" not in df.columns:
        return ["chebi", "obi", "ms", "chmo", "edam"]
    options: list[str] = []
    seen: set[str] = set()
    for value in df["ontology"].tolist():
        ontology = str(value or "").strip().lower()
        if not ontology or ontology in seen:
            continue
        seen.add(ontology)
        options.append(ontology)
    return options or ["chebi", "obi", "ms", "chmo", "edam"]


def _search_ols(
    query: str,
    ontologies: list[str],
    rows: int = 8,
    timeout: float = 4.0,
    search_all: bool = False,
    mother_only: bool = True,
) -> list[dict[str, str]]:
    q = query.strip()
    if not q:
        return []

    by_key: dict[tuple[str, str], dict[str, str]] = {}
    query_norm = _normalize_label(q)
    if search_all:
        request_specs = [("", max(25, min(200, rows * 10)))]
    else:
        if not ontologies:
            return []
        request_specs = [(ontology, max(1, rows)) for ontology in ontologies]

    for ontology, row_limit in request_specs:
        params_map = {"q": q, "rows": row_limit}
        if ontology:
            params_map["ontology"] = ontology
        if mother_only:
            params_map["local"] = "true"
        params = urllib.parse.urlencode(params_map)
        url = f"https://www.ebi.ac.uk/ols4/api/search?{params}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        try:
            parsed = json.loads(payload)
        except Exception:
            continue
        docs = parsed.get("response", {}).get("docs", [])
        if not isinstance(docs, list):
            continue
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            iri = str(doc.get("iri", "") or "").strip()
            label = str(doc.get("label", "") or "").strip()
            onto = str(doc.get("ontology_prefix", "") or ontology or "unknown").strip().lower()
            short_form = str(doc.get("short_form", "") or "").strip()
            entity_kind = (
                _infer_ols_entity_kind(
                    doc.get("entity_type"),
                    doc.get("entityType"),
                    doc.get("type"),
                    doc.get("semantic_type"),
                    doc.get("semanticType"),
                )
                or "class"
            )
            if not iri:
                continue
            score = _manual_match_score(q, label or iri, left_kind="", right_kind=entity_kind)
            if query_norm and _normalize_label(label) == query_norm:
                score = 1.0
            key = (onto, iri)
            existing = by_key.get(key)
            row = {
                "ontology": onto,
                "iri": iri,
                "ols_term_page": _ols_term_landing_url(onto, iri, short_form=short_form),
                "label": label or iri,
                "short_form": short_form,
                "entity_kind": entity_kind,
                "is_defining_ontology": str(
                    doc.get("is_defining_ontology", doc.get("isDefiningOntology", ""))
                ).strip(),
                "definition": "",
                "comment": "",
                "example": "",
                "score": f"{score:.2f}",
            }
            if existing is None or float(row["score"]) > float(existing.get("score", "0") or "0"):
                by_key[key] = row

    ranked = sorted(
        by_key.values(),
        key=lambda row: (-float(row.get("score", "0") or "0"), row.get("ontology", ""), row.get("label", "")),
    )
    return ranked[:50]


def _slug_from_label(label: str) -> str:
    value = (label or "").strip()
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    value = re.sub(r"[^A-Za-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "NewTerm"


def _ensure_term_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in TERM_REQUIRED_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    return out


def _type_from_kind(term_kind: str) -> str:
    kind = term_kind.strip().lower()
    if kind == "class":
        return "http://www.w3.org/2002/07/owl#Class"
    if kind == "property":
        return "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"
    return ""


def _append_or_update_term(
    terms_df: pd.DataFrame,
    *,
    iri: str,
    label: str,
    term_kind: str,
    definition: str,
    comment: str,
    example: str,
) -> tuple[pd.DataFrame, str]:
    out = _ensure_term_columns(terms_df)
    mask = out["iri"].astype(str).str.strip() == iri.strip()
    if bool(mask.any()):
        idx = int(out.index[mask][0])
        out.at[idx, "label"] = label
        out.at[idx, "term_kind"] = term_kind
        out.at[idx, "type"] = _type_from_kind(term_kind)
        out.at[idx, "definition"] = definition
        out.at[idx, "comment"] = comment
        out.at[idx, "example"] = example
        return out, "updated"

    new_row = {col: "" for col in out.columns}
    new_row["iri"] = iri
    new_row["label"] = label
    new_row["type"] = _type_from_kind(term_kind)
    new_row["term_kind"] = term_kind
    new_row["definition"] = definition
    new_row["comment"] = comment
    new_row["example"] = example
    out = pd.concat([out, pd.DataFrame([new_row], columns=out.columns)], ignore_index=True)
    return out, "added"


def _new_candidate_row(df_columns: list[str]) -> dict[str, str]:
    return {col: "" for col in df_columns}


def _queue_source_term_prefill(
    *,
    iri: str,
    label: str,
    kind: str,
    definition: str,
    comment: str,
    example: str,
) -> None:
    st.session_state[STATE_PENDING_PREFILL] = {
        "iri": iri.strip(),
        "label": label.strip(),
        "kind": ("property" if kind.strip().lower() == "property" else "class"),
        "definition": definition.strip(),
        "comment": comment.strip(),
        "example": example.strip(),
    }


def render() -> None:
    st.title("Add Terms")
    st.caption("Create missing source terms (class/property) and optional mapping candidates for curation.")

    ctx = active_alignment_context()
    if ctx is None:
        st.warning("No alignment batch found. Configure registry/alignment_batches.tsv first.")
        return

    active_curator = str(st.session_state.get(STATE_CURATOR, "") or "").strip()
    active_curator_name = str(st.session_state.get(STATE_CURATOR_NAME, "") or "").strip()
    if not active_curator or not active_curator_name:
        st.error("Set a valid Curator ORCID with a resolvable public name in the left sidebar before adding terms.")
        return

    st.caption(f"Active batch: `{ctx.batch_label}`")
    st.caption(f"Active curator: `{active_curator_name}` ({active_curator})")
    st.caption(f"Pivot terms file: `{to_relpath(ctx.terms_tsv)}`")
    st.caption(f"Batch review ledger: `{to_relpath(ctx.review_tsv)}`")
    st.caption(f"Batch queue: `{to_relpath(ctx.queue_tsv)}`")

    relation_catalog, catalog_msg = _load_mapping_relations_from_local_ontologies()
    relation_options: list[str] = []
    if relation_catalog is not None:
        relation_options = [
            str(x.get("curie", "")).strip()
            for x in relation_catalog.get("relations", [])
            if str(x.get("curie", "")).strip()
        ]
    if catalog_msg:
        st.caption(catalog_msg)

    namespace_hint = (ctx.namespace_prefix or "").strip()
    if STATE_LEFT_LABEL not in st.session_state:
        st.session_state[STATE_LEFT_LABEL] = ""
    if STATE_LEFT_KIND not in st.session_state:
        st.session_state[STATE_LEFT_KIND] = "class"
    if STATE_LEFT_IRI not in st.session_state:
        st.session_state[STATE_LEFT_IRI] = ""
    if STATE_LEFT_DEFINITION not in st.session_state:
        st.session_state[STATE_LEFT_DEFINITION] = ""
    if STATE_LEFT_COMMENT not in st.session_state:
        st.session_state[STATE_LEFT_COMMENT] = ""
    if STATE_LEFT_EXAMPLE not in st.session_state:
        st.session_state[STATE_LEFT_EXAMPLE] = ""
    pending_prefill = st.session_state.pop(STATE_PENDING_PREFILL, None)
    if isinstance(pending_prefill, dict):
        st.session_state[STATE_LEFT_IRI] = str(pending_prefill.get("iri", "") or "").strip()
        st.session_state[STATE_LEFT_LABEL] = str(pending_prefill.get("label", "") or "").strip()
        st.session_state[STATE_LEFT_KIND] = (
            "property" if str(pending_prefill.get("kind", "")).strip().lower() == "property" else "class"
        )
        st.session_state[STATE_LEFT_DEFINITION] = str(pending_prefill.get("definition", "") or "").strip()
        st.session_state[STATE_LEFT_COMMENT] = str(pending_prefill.get("comment", "") or "").strip()
        st.session_state[STATE_LEFT_EXAMPLE] = str(pending_prefill.get("example", "") or "").strip()

    st.subheader("Fetch source term from OLS")
    ontology_options = _ols_catalog()
    default_onto = "ms" if "ms" in ontology_options else (ontology_options[0] if ontology_options else "")
    fetch_col1, fetch_col2, fetch_col3 = st.columns(3)
    with fetch_col1:
        fetch_ontology = st.selectbox(
            "Ontology ID",
            options=ontology_options,
            index=ontology_options.index(default_onto) if default_onto in ontology_options else 0,
            key="add_terms_fetch_ontology",
        )
    with fetch_col2:
        fetch_iri = st.text_input(
            "Term IRI to fetch",
            value="",
            placeholder="http://purl.obolibrary.org/obo/MS_1003208",
            key="add_terms_fetch_iri",
        )
    with fetch_col3:
        fetch_kind_label = st.selectbox(
            "Entity type",
            options=["Class", "Property", "Individual"],
            index=0,
            key="add_terms_fetch_kind",
        )

    if st.button("Fetch term into form"):
        iri = fetch_iri.strip()
        if not iri.startswith(("http://", "https://")):
            st.error("Provide a valid term IRI (http:// or https://).")
        else:
            entity_kind = fetch_kind_label.lower()
            metadata = _fetch_ols_metadata_for_entity(
                iri=iri,
                ontology=fetch_ontology,
                entity_kind=entity_kind,
            )
            _, hint = _lookup_ols_hit_by_iri(iri)
            for key in ("label", "definition", "comment", "example"):
                if not metadata.get(key, "").strip():
                    metadata[key] = hint.get(key, "")
            fetched_label = metadata.get("label", "").strip()
            if not fetched_label:
                st.error("No term metadata found for this ontology ID + IRI.")
            else:
                _queue_source_term_prefill(
                    iri=iri,
                    label=fetched_label,
                    kind=entity_kind,
                    definition=metadata.get("definition", ""),
                    comment=metadata.get("comment", ""),
                    example=metadata.get("example", ""),
                )
                st.success("Source term form populated from OLS.")
                st.rerun()

    st.subheader("New source term")
    c1, c2 = st.columns(2)
    with c1:
        left_label = st.text_input(
            "Label",
            value=st.session_state[STATE_LEFT_LABEL],
            placeholder="MS1 feature",
            key=STATE_LEFT_LABEL,
        )
        left_kind = st.selectbox(
            "Kind",
            options=["class", "property"],
            index=0 if st.session_state[STATE_LEFT_KIND] == "class" else 1,
            key=STATE_LEFT_KIND,
        )
        suggested_tail = _slug_from_label(left_label)
        suggested_iri = f"{namespace_hint}{suggested_tail}" if namespace_hint else ""
        left_iri = st.text_input(
            "IRI",
            value=st.session_state[STATE_LEFT_IRI] or suggested_iri,
            placeholder="https://w3id.org/emi#MS1Feature",
            help="Provide the full IRI for the term in your source schema.",
            key=STATE_LEFT_IRI,
        )
    with c2:
        left_definition = st.text_area("Definition", value=st.session_state[STATE_LEFT_DEFINITION], height=84, key=STATE_LEFT_DEFINITION)
        left_comment = st.text_area("Comment", value=st.session_state[STATE_LEFT_COMMENT], height=84, key=STATE_LEFT_COMMENT)
        left_example = st.text_area("Example", value=st.session_state[STATE_LEFT_EXAMPLE], height=84, key=STATE_LEFT_EXAMPLE)

    st.subheader("Fetch mappings from existing ontologies")
    default_ontos = [x for x in ["chebi", "obi", "ms", "chmo", "edam"] if x in ontology_options] or ontology_options[:5]
    search_all_ontologies = st.checkbox(
        "Search across ALL OLS ontologies (slower)",
        value=False,
        help="When enabled, ontology selection is ignored and OLS global search is used.",
    )
    mother_only = st.checkbox(
        "Only terms defined in source ontology (exclude imports)",
        value=True,
        help=(
            "Keeps only terms where OLS marks the hit as defined by the ontology "
            "(not imported from another ontology)."
        ),
    )
    selected_ontologies = st.multiselect(
        "Ontologies",
        options=ontology_options,
        default=default_ontos,
        help="Choose OLS ontologies to search for matching terms.",
        disabled=search_all_ontologies,
    )
    search_query = st.text_input(
        "Search query",
        value=(left_label or ""),
        help="Usually your new term label (for example: MS1 feature).",
    )
    search_term_for_link = (search_query or left_label or "").strip()
    if search_term_for_link:
        st.markdown(f"[Open OLS search for this term]({_ols_search_url(search_term_for_link)})")
    rows = st.number_input("Rows per ontology", min_value=1, max_value=30, value=8, step=1)

    if st.button("Search ontology terms"):
        results = _search_ols(
            search_query,
            selected_ontologies,
            rows=int(rows),
            search_all=search_all_ontologies,
            mother_only=mother_only,
        )
        st.session_state[STATE_SEARCH_RESULTS] = results
        st.session_state[STATE_SEARCH_QUERY] = search_query
        st.session_state[STATE_SEARCH_ONTOLOGIES] = ["ALL"] if search_all_ontologies else selected_ontologies
        st.session_state[STATE_SELECTED_RESULTS] = []
        if not results:
            st.warning("No OLS result found for this query/ontology selection.")
        else:
            st.success(f"Loaded {len(results)} candidate mapping term(s).")

    results = st.session_state.get(STATE_SEARCH_RESULTS, [])
    if results:
        st.caption(
            "Search context: "
            f"`{st.session_state.get(STATE_SEARCH_QUERY, '')}` in "
            f"{', '.join(st.session_state.get(STATE_SEARCH_ONTOLOGIES, []))}"
        )
        option_map = {
            f"{r['ontology'].upper()} | {r['label']} | {r['iri']}": r
            for r in results
        }
        selected_labels = st.multiselect(
            "Select mapping terms to add",
            options=list(option_map.keys()),
            default=st.session_state.get(STATE_SELECTED_RESULTS, []),
        )
        st.session_state[STATE_SELECTED_RESULTS] = selected_labels
        action_rows = [option_map[x] for x in selected_labels] if selected_labels else list(results)
        for row in action_rows:
            row["score"] = float(pd.to_numeric(row.get("score", ""), errors="coerce") or 0.0)
        st.caption("Row actions")
        header_cols = st.columns([1.4, 1.2, 2.2, 3.2, 1.3, 1.1])
        header_cols[0].markdown("**Action**")
        header_cols[1].markdown("**Score**")
        header_cols[2].markdown("**Term**")
        header_cols[3].markdown("**Label**")
        header_cols[4].markdown("**Ontology**")
        header_cols[5].markdown("**Kind**")
        for idx, row in enumerate(action_rows[:50]):
            iri = str(row.get("iri", "")).strip()
            ontology = str(row.get("ontology", "")).strip().lower()
            label = str(row.get("label", "")).strip() or iri
            entity_kind = str(row.get("entity_kind", "")).strip().lower() or "class"
            short_form = str(row.get("short_form", "")).strip()
            term_page = str(row.get("ols_term_page", "")).strip()
            score_value = float(row.get("score", 0.0) or 0.0)
            row_cols = st.columns([1.4, 1.2, 2.2, 3.2, 1.3, 1.1])
            with row_cols[0]:
                if st.button(
                    "Use",
                    key=f"populate_source_term_{idx}_{short_form or iri}",
                    disabled=not bool(iri),
                    use_container_width=True,
                ):
                    metadata = _fetch_ols_metadata_for_entity(
                        iri=iri,
                        ontology=ontology,
                        entity_kind=entity_kind,
                    )
                    _queue_source_term_prefill(
                        iri=iri,
                        label=metadata.get("label", "").strip() or label,
                        kind=entity_kind,
                        definition=metadata.get("definition", ""),
                        comment=metadata.get("comment", ""),
                        example=metadata.get("example", ""),
                    )
                    st.success(f"Populated source term form from `{short_form or label}`.")
                    st.rerun()
            with row_cols[1]:
                st.progress(max(0.0, min(1.0, score_value)))
            with row_cols[2]:
                if term_page:
                    st.markdown(f"[{short_form or iri}]({term_page})")
                else:
                    st.markdown(short_form or iri)
            with row_cols[3]:
                st.markdown(label)
            with row_cols[4]:
                st.markdown(ontology.upper())
            with row_cols[5]:
                st.markdown(entity_kind)

    st.subheader("Optional mapping relation")
    if relation_options:
        relation_default_idx = relation_options.index("skos:exactMatch") if "skos:exactMatch" in relation_options else 0
        selected_relation = st.selectbox(
            "Mapping relation for added candidates",
            options=[""] + relation_options,
            index=(relation_default_idx + 1),
            help="This pre-fills the relation on newly created candidate rows.",
        )
    else:
        selected_relation = ""
        st.info("No relation catalog loaded. Candidates will be added with empty relation.")

    st.subheader("Create rows")
    create_placeholder = st.checkbox(
        "Also add placeholder row (no mapping yet)",
        value=not bool(st.session_state.get(STATE_SELECTED_RESULTS)),
        help="Keeps the term visible in Curate candidates even when no mapping term is selected yet.",
    )
    if st.button("Add term and candidates", type="primary"):
        iri = left_iri.strip()
        label = left_label.strip()
        if not iri or not iri.startswith(("http://", "https://")):
            st.error("Term IRI is required and must start with http:// or https://.")
            return
        if not label:
            st.error("Term label is required.")
            return

        terms_df = read_tsv(ctx.terms_tsv)
        terms_df, term_action = _append_or_update_term(
            terms_df,
            iri=iri,
            label=label,
            term_kind=left_kind,
            definition=left_definition.strip(),
            comment=left_comment.strip(),
            example=left_example.strip(),
        )

        cand_df = _ensure_columns(read_tsv(ctx.queue_tsv))
        if cand_df.empty and not ctx.queue_tsv.is_file():
            cand_df = pd.DataFrame(columns=REQUIRED_COLUMNS)
            cand_df = _ensure_columns(cand_df)
        if "date_added" not in cand_df.columns:
            cand_df["date_added"] = ""
        if "ols_term_api_url" not in cand_df.columns:
            cand_df["ols_term_api_url"] = ""
        if "match_method" not in cand_df.columns:
            cand_df["match_method"] = ""
        if "match_score" not in cand_df.columns:
            cand_df["match_score"] = ""
        if "normalized_left_label" not in cand_df.columns:
            cand_df["normalized_left_label"] = ""
        if "normalized_right_label" not in cand_df.columns:
            cand_df["normalized_right_label"] = ""

        selected_rows: list[dict[str, str]] = []
        option_map = {
            f"{r['ontology'].upper()} | {r['label']} | {r['iri']}": r
            for r in st.session_state.get(STATE_SEARCH_RESULTS, [])
        }
        for opt in st.session_state.get(STATE_SELECTED_RESULTS, []):
            row = option_map.get(opt)
            if row:
                selected_rows.append(row)

        new_rows: list[dict[str, str]] = []
        now_ts = utc_now_timestamp()
        left_source = normalize_source_value(ctx.source_id)
        left_norm = _normalize_label(label)

        for hit in selected_rows:
            right_iri = str(hit.get("iri", "")).strip()
            if not right_iri:
                continue
            duplicate_mask = (
                (cand_df["left_source"].astype(str) == left_source)
                & (cand_df["left_term_iri"].astype(str) == iri)
                & (cand_df["right_term_iri"].astype(str) == right_iri)
            )
            if bool(duplicate_mask.any()):
                continue
            entry = _new_candidate_row(cand_df.columns.tolist())
            entry["alignment_id"] = _next_alignment_id(cand_df)
            entry["left_source"] = left_source
            entry["left_term_iri"] = iri
            entry["left_label"] = label
            entry["left_definition"] = left_definition.strip()
            entry["left_comment"] = left_comment.strip()
            entry["left_example"] = left_example.strip()
            entry["left_term_kind"] = left_kind
            entry["right_source"] = normalize_source_value(hit.get("ontology", ""))
            entry["right_term_iri"] = right_iri
            entry["right_label"] = str(hit.get("label", "")).strip() or right_iri
            entry["right_definition"] = str(hit.get("definition", "")).strip()
            entry["right_comment"] = str(hit.get("comment", "")).strip()
            entry["right_example"] = str(hit.get("example", "")).strip()
            entry["right_term_kind"] = str(hit.get("entity_kind", "")).strip().lower()
            entry["relation"] = selected_relation.strip()
            entry["status"] = "needs_review"
            entry["canonical_from"] = ""
            entry["canonical_term_iri"] = ""
            entry["canonical_term_label"] = ""
            entry["canonical_term_source"] = ""
            entry["canonical_term_kind"] = ""
            entry["reviewer"] = ""
            entry["reviewer_name"] = ""
            entry["date_reviewed"] = ""
            entry["date_added"] = now_ts
            entry["curator"] = active_curator
            entry["curator_name"] = active_curator_name
            entry["ols_search_url"] = _ols_search_url(label)
            entry["bioportal_search_url"] = _bioportal_search_url(label)
            entry["ols_term_api_url"] = right_iri
            entry["match_method"] = "manual_add_terms_ols_search"
            score_value = _manual_match_score(
                left_label=label,
                right_label=str(hit.get("label", "")).strip() or right_iri,
                left_kind=left_kind,
                right_kind=str(hit.get("entity_kind", "")).strip().lower(),
            )
            entry["match_score"] = f"{score_value:.2f}"
            entry["suggestion_source"] = "manual_add_terms"
            entry["notes"] = "Added from Add Terms module."
            entry["normalized_left_label"] = left_norm
            entry["normalized_right_label"] = _normalize_label(str(hit.get("label", "")).strip())
            new_rows.append(entry)
            cand_df = pd.concat([cand_df, pd.DataFrame([entry], columns=cand_df.columns)], ignore_index=True)

        if create_placeholder:
            placeholder_mask = (
                (cand_df["left_source"].astype(str) == left_source)
                & (cand_df["left_term_iri"].astype(str) == iri)
                & (cand_df["right_term_iri"].astype(str).str.strip() == "")
            )
            if not bool(placeholder_mask.any()):
                entry = _new_candidate_row(cand_df.columns.tolist())
                entry["alignment_id"] = _next_alignment_id(cand_df)
                entry["left_source"] = left_source
                entry["left_term_iri"] = iri
                entry["left_label"] = label
                entry["left_definition"] = left_definition.strip()
                entry["left_comment"] = left_comment.strip()
                entry["left_example"] = left_example.strip()
                entry["left_term_kind"] = left_kind
                entry["right_source"] = "manual"
                entry["right_term_iri"] = ""
                entry["right_label"] = "(no mapping selected yet)"
                entry["right_definition"] = ""
                entry["right_comment"] = ""
                entry["right_example"] = ""
                entry["right_term_kind"] = left_kind
                entry["relation"] = selected_relation.strip()
                entry["status"] = "needs_review"
                entry["canonical_from"] = ""
                entry["canonical_term_iri"] = ""
                entry["canonical_term_label"] = ""
                entry["canonical_term_source"] = ""
                entry["canonical_term_kind"] = ""
                entry["reviewer"] = ""
                entry["reviewer_name"] = ""
                entry["date_reviewed"] = ""
                entry["date_added"] = now_ts
                entry["curator"] = active_curator
                entry["curator_name"] = active_curator_name
                entry["ols_search_url"] = _ols_search_url(label)
                entry["bioportal_search_url"] = _bioportal_search_url(label)
                entry["ols_term_api_url"] = ""
                entry["match_method"] = "manual_add_terms_placeholder"
                entry["match_score"] = ""
                entry["suggestion_source"] = "manual_add_terms"
                entry["notes"] = "Placeholder row created in Add Terms module."
                entry["normalized_left_label"] = left_norm
                entry["normalized_right_label"] = ""
                new_rows.append(entry)
                cand_df = pd.concat([cand_df, pd.DataFrame([entry], columns=cand_df.columns)], ignore_index=True)

        write_tsv(terms_df, ctx.terms_tsv)
        write_tsv(stamp_batch_metadata(cand_df, ctx), ctx.queue_tsv)

        st.success(
            f"Term {term_action}: `{label}`. Added {len(new_rows)} row(s) to the local queue."
        )
        if st.button("Go to Curate candidates"):
            st.session_state[STATE_PAGE] = "Curate candidates"
            st.rerun()
