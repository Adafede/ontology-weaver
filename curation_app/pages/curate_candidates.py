"""Interactive candidate curation page."""

from __future__ import annotations

import html
import json
import re
from urllib.error import HTTPError, URLError
import urllib.request
from difflib import SequenceMatcher
from urllib.parse import quote, quote_plus, urlencode, urlparse

import pandas as pd
import streamlit as st
from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from curation_app.config import DEFAULT_OLS_ONTOLOGIES_FILE
from curation_app.context import active_source_context
from curation_app.helpers import (
    dataframe_to_tsv_bytes,
    normalize_notes_for_approval,
    read_tsv,
    render_clickable_dataframe,
    sync_review_ledger,
    to_path,
    to_relpath,
    utc_now_timestamp,
    write_tsv,
)


REQUIRED_COLUMNS = [
    "alignment_id",
    "left_source",
    "left_term_iri",
    "left_label",
    "left_definition",
    "left_comment",
    "left_example",
    "left_term_kind",
    "right_source",
    "right_term_iri",
    "right_label",
    "right_definition",
    "right_comment",
    "right_example",
    "right_term_kind",
    "relation",
    "status",
    "canonical_from",
    "canonical_term_iri",
    "canonical_term_label",
    "canonical_term_source",
    "canonical_term_kind",
    "reviewer",
    "reviewer_name",
    "co_curators",
    "co_curator_names",
    "curator_name",
    "date_reviewed",
    "logs",
    "curation_comment",
    "ols_search_url",
    "bioportal_search_url",
    "suggestion_source",
]

RELATIONS = ["exact", "close", "broad", "narrow", "related"]
MAPPING_RELATIONS_DIR = to_path("registry/downloads/ontologies")
MAPPING_RELATION_ONTOLOGY_IDS = ("owl", "rdfs", "skos")
MAPPING_RELATION_PLACEHOLDER = "(select mapping relation)"
STATUSES = ["needs_review", "approved", "rejected", "deprecated"]
CANONICAL_FROM = ["", "left", "right", "manual"]
KIND_MISMATCH_SCORE_FACTOR = 0.75
OLS_ENTITY_KIND_VALUES = ("class", "property", "individual")

MAPPING_GUIDANCE: dict[str, dict[str, str]] = {
    "owl:equivalentClass": {
        "tier": "recommended",
        "when": "Use when both terms denote the same class meaning across ontologies.",
        "example": "ex:Automobile owl:equivalentClass ex:Car",
    },
    "owl:equivalentProperty": {
        "tier": "recommended",
        "when": "Use when both terms are properties with the same intended semantics.",
        "example": "ex:birthDate owl:equivalentProperty schema:birthDate",
    },
    "owl:sameAs": {
        "tier": "recommended",
        "when": "Use when both named individuals denote the same real-world entity.",
        "example": "ex:EntityA owl:sameAs ex:EntityB",
    },
    "rdfs:subClassOf": {
        "tier": "recommended",
        "when": "Use when source class is narrower than target class.",
        "example": "ex:GraduateStudent rdfs:subClassOf ex:Student",
    },
    "rdfs:subPropertyOf": {
        "tier": "recommended",
        "when": "Use when source property is a specialization of target property.",
        "example": "ex:hasBiologicalMother rdfs:subPropertyOf ex:hasParent",
    },
    "skos:exactMatch": {
        "tier": "recommended",
        "when": "Use for near-equivalent cross-scheme concepts when you avoid OWL commitment.",
        "example": "ex:ConceptA skos:exactMatch ex:ConceptB",
    },
    "skos:closeMatch": {
        "tier": "recommended",
        "when": "Use when concepts are very close but not strictly identical in all contexts.",
        "example": "ex:ConceptA skos:closeMatch ex:ConceptB",
    },
    "skos:broadMatch": {
        "tier": "recommended",
        "when": "Use when source concept is broader than target concept.",
        "example": "ex:ConceptA skos:broadMatch ex:ConceptB",
    },
    "skos:narrowMatch": {
        "tier": "recommended",
        "when": "Use when source concept is narrower than target concept.",
        "example": "ex:ConceptA skos:narrowMatch ex:ConceptB",
    },
    "skos:relatedMatch": {
        "tier": "recommended",
        "when": "Use for associative cross-scheme relation without hierarchy or equivalence.",
        "example": "ex:ConceptA skos:relatedMatch ex:ConceptC",
    },
    "skos:mappingRelation": {
        "tier": "advanced",
        "when": "Generic parent mapping relation; prefer a specific SKOS mapping relation above.",
        "example": "ex:ConceptA skos:mappingRelation ex:ConceptB",
    },
    "owl:disjointUnionOf": {
        "tier": "advanced",
        "when": "Modeling axiom for class decomposition, not a routine alignment mapping.",
        "example": "ex:Vehicle owl:disjointUnionOf (ex:Car ex:Bus ex:Bicycle)",
    },
}

MAPPING_GUIDANCE_ORDER = [
    "owl:equivalentClass",
    "owl:equivalentProperty",
    "owl:sameAs",
    "rdfs:subClassOf",
    "rdfs:subPropertyOf",
    "skos:exactMatch",
    "skos:closeMatch",
    "skos:broadMatch",
    "skos:narrowMatch",
    "skos:relatedMatch",
    "skos:mappingRelation",
    "owl:disjointUnionOf",
]

STATE_PATH = "curation_path"
STATE_DF = "curation_df"
STATE_DIRTY = "curation_dirty"
STATE_MTIME = "curation_mtime"
STATE_SELECTED_ALIGNMENT = "curation_selected_alignment_id"
STATE_KEPT_LEFT_TERMS = "curation_kept_left_terms"
STATE_LEFT_TERM_INDEX = "curation_left_term_index"
STATE_CURATOR = "active_curator"
STATE_CURATOR_NAME = "active_curator_name"
STATE_GROUP_MODE = "group_session_active"
STATE_SESSION_CURATORS = "group_session_curators"  # list[tuple[str, str]] (orcid, name)
STATE_REVIEW_SYNC_IRIS = "curation_review_sync_iris"
STATE_PENDING_APPROVALS = "curation_pending_approvals"


def _prepare_review_display_df(review_df: pd.DataFrame) -> pd.DataFrame:
    out = review_df.copy()
    for col in [
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
    ]:
        if col not in out.columns:
            out[col] = ""
    display = out[
        [
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
            "reviewer_name",
            "reviewer",
            "date_reviewed",
            "curation_comment",
        ]
    ].copy()
    display = display.rename(
        columns={
            "source_term_source": "source",
            "source_term_iri": "source_term_iri",
            "source_term_label": "source_term_label",
            "source_term_kind": "source_term_kind",
            "canonical_term_iri": "canonical_term_iri",
            "canonical_term_label": "canonical_term_label",
            "canonical_term_source": "canonical_term_source",
            "canonical_term_kind": "canonical_term_kind",
            "relation": "relation",
            "status": "status",
            "reviewer_name": "reviewer_name",
            "reviewer": "reviewer_orcid",
            "date_reviewed": "date_reviewed",
            "curation_comment": "curation_comment",
        }
    )
    return display.sort_values(by=["date_reviewed", "source_term_label"], ascending=[False, True], na_position="last")


def _ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "logs" not in out.columns:
        if "notes" in out.columns:
            out["logs"] = out["notes"].fillna("")
        else:
            out["logs"] = ""
    if "curation_comment" not in out.columns:
        out["curation_comment"] = ""
    for col in REQUIRED_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    return out


def _load_df(path_text: str) -> pd.DataFrame:
    df = read_tsv(path_text)
    return _ensure_columns(df)


def _file_mtime(path_text: str) -> float | None:
    resolved = to_path(path_text)
    if not resolved.is_file():
        return None
    return resolved.stat().st_mtime


def _save_queue_and_sync_review(queue_file: str, review_file: str) -> None:
    queue_df = st.session_state[STATE_DF]
    write_tsv(queue_df, queue_file)
    review_df = read_tsv(review_file)
    touched_source_iris = set(str(x or "").strip() for x in st.session_state.get(STATE_REVIEW_SYNC_IRIS, set()))
    synced_review_df = sync_review_ledger(review_df, queue_df, touched_source_iris=touched_source_iris)
    write_tsv(synced_review_df, review_file)
    st.session_state[STATE_MTIME] = _file_mtime(queue_file)
    st.session_state[STATE_REVIEW_SYNC_IRIS] = set()


def _autosave_if_dirty(queue_file: str, review_file: str) -> None:
    if not st.session_state.get(STATE_DIRTY):
        return
    _save_queue_and_sync_review(queue_file, review_file)
    st.session_state[STATE_DIRTY] = False
    st.caption("Auto-saved local queue and synced reviewed decisions.")


def _mark_review_sync_iri(source_term_iri: str) -> None:
    iri = str(source_term_iri or "").strip()
    if not iri:
        return
    pending = set(str(x or "").strip() for x in st.session_state.get(STATE_REVIEW_SYNC_IRIS, set()))
    pending.add(iri)
    st.session_state[STATE_REVIEW_SYNC_IRIS] = pending


def _set_review_fields(df: pd.DataFrame, idx: int, reviewer: str) -> None:
    if reviewer.strip():
        df.at[idx, "reviewer"] = reviewer.strip()
        reviewer_name = str(st.session_state.get(STATE_CURATOR_NAME, "") or "").strip()
        df.at[idx, "reviewer_name"] = reviewer_name
    df.at[idx, "date_reviewed"] = utc_now_timestamp()
    # Group session attribution
    session_curators: list[tuple[str, str]] = st.session_state.get(STATE_SESSION_CURATORS, []) or []
    if session_curators and len(session_curators) > 1:
        co_orcids = "|".join(o for o, _ in session_curators)
        co_names = ", ".join(n for _, n in session_curators)
        df.at[idx, "co_curators"] = co_orcids
        df.at[idx, "co_curator_names"] = co_names
    else:
        df.at[idx, "co_curators"] = ""
        df.at[idx, "co_curator_names"] = ""


def _apply_approve_left(df: pd.DataFrame, idx: int, reviewer: str, relation: str, logs: str) -> None:
    df.at[idx, "status"] = "approved"
    df.at[idx, "canonical_from"] = "left"
    df.at[idx, "canonical_term_iri"] = df.at[idx, "left_term_iri"]
    df.at[idx, "canonical_term_label"] = df.at[idx, "left_label"]
    df.at[idx, "canonical_term_source"] = df.at[idx, "left_source"]
    df.at[idx, "canonical_term_kind"] = df.at[idx, "left_term_kind"]
    df.at[idx, "relation"] = relation
    df.at[idx, "suggestion_source"] = "manual_curated"
    df.at[idx, "logs"] = normalize_notes_for_approval(logs)
    _set_review_fields(df, idx, reviewer)


def _apply_approve_right(df: pd.DataFrame, idx: int, reviewer: str, relation: str, logs: str) -> None:
    df.at[idx, "status"] = "approved"
    df.at[idx, "canonical_from"] = "right"
    df.at[idx, "canonical_term_iri"] = df.at[idx, "right_term_iri"]
    df.at[idx, "canonical_term_label"] = df.at[idx, "right_label"]
    df.at[idx, "canonical_term_source"] = df.at[idx, "right_source"]
    df.at[idx, "canonical_term_kind"] = df.at[idx, "right_term_kind"]
    df.at[idx, "relation"] = relation
    df.at[idx, "suggestion_source"] = "manual_curated"
    df.at[idx, "logs"] = normalize_notes_for_approval(logs)
    _set_review_fields(df, idx, reviewer)


def _apply_approve_manual(
    df: pd.DataFrame,
    idx: int,
    reviewer: str,
    relation: str,
    logs: str,
    manual_iri: str,
    manual_label: str,
    manual_source: str,
    manual_kind: str,
) -> bool:
    if not (manual_iri.strip() and manual_label.strip() and manual_source.strip() and manual_kind.strip()):
        return False

    df.at[idx, "status"] = "approved"
    df.at[idx, "canonical_from"] = "manual"
    df.at[idx, "canonical_term_iri"] = manual_iri.strip()
    df.at[idx, "canonical_term_label"] = manual_label.strip()
    df.at[idx, "canonical_term_source"] = manual_source.strip()
    df.at[idx, "canonical_term_kind"] = manual_kind.strip()
    df.at[idx, "relation"] = relation
    df.at[idx, "suggestion_source"] = "manual_curated"
    df.at[idx, "logs"] = normalize_notes_for_approval(logs)
    _set_review_fields(df, idx, reviewer)
    return True


def _apply_reject(df: pd.DataFrame, idx: int, reviewer: str, logs: str) -> None:
    df.at[idx, "status"] = "rejected"
    df.at[idx, "canonical_from"] = ""
    df.at[idx, "canonical_term_iri"] = ""
    df.at[idx, "canonical_term_label"] = ""
    df.at[idx, "canonical_term_source"] = ""
    df.at[idx, "canonical_term_kind"] = ""
    df.at[idx, "logs"] = logs.strip()
    _set_review_fields(df, idx, reviewer)


def _append_log(existing_log: str, new_log: str) -> str:
    existing = existing_log.strip()
    entry = new_log.strip()
    if not entry:
        return existing
    if not existing:
        return entry
    if entry.lower() in existing.lower():
        return existing
    return f"{existing} | {entry}"


def _filtered_df(df: pd.DataFrame, statuses: list[str], search: str) -> pd.DataFrame:
    out = df.copy()
    if statuses:
        out = out[out["status"].isin(statuses)]

    token = search.strip().lower()
    if token:
        haystack = (
            out["alignment_id"].str.lower()
            + " "
            + out["left_label"].str.lower()
            + " "
            + out["right_label"].str.lower()
            + " "
            + out["logs"].str.lower()
            + " "
            + out["curation_comment"].str.lower()
        )
        out = out[haystack.str.contains(token, na=False)]
    return out


def _is_http(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://")


def _format_link_or_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    escaped = html.escape(text)
    if _is_http(text):
        return f'<a href="{escaped}" target="_blank">{escaped}</a>'
    return escaped


def _compact_text(value: str, limit: int = 220) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "-", ""
    if len(text) <= limit:
        return text, ""
    short = text[:limit].rstrip() + "..."
    return short, text


def _display_source(value: object) -> str:
    text = str(value or "").strip()
    return text.upper() if text else "-"


def _display_kind(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text else "-"


def _kind_mismatch(left_kind: object, right_kind: object) -> bool:
    left = str(left_kind or "").strip().lower()
    right = str(right_kind or "").strip().lower()
    return bool(left and right and left != right)


def _ols_search_url(query: str) -> str:
    return "https://www.ebi.ac.uk/ols4/search?q=" + quote_plus(query or "")


def _bioportal_search_url(query: str) -> str:
    return "https://bioportal.bioontology.org/search?query=" + quote_plus(query or "")


def _ols_term_landing_url(ontology: str, iri: str, short_form: str = "") -> str:
    onto = (ontology or "").strip().lower()
    term_iri = (iri or "").strip()
    if not onto or not term_iri:
        return ""
    base = (
        f"https://www.ebi.ac.uk/ols4/ontologies/{quote(onto)}/terms"
        f"?iri={quote(term_iri, safe='')}"
    )
    sf = (short_form or "").strip()
    if sf:
        return f"{base}&sf={quote(sf, safe='')}"
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
    *,
    left_label: str,
    left_kind: str,
    rows: int = 8,
    timeout: float = 4.0,
    search_all: bool = False,
    mother_only: bool = True,
) -> tuple[list[dict[str, str]], str]:
    q = query.strip()
    if not q:
        return [], ""

    by_key: dict[tuple[str, str], dict[str, str]] = {}
    query_norm = _normalize_label(q)
    request_failures: list[str] = []
    successful_responses = 0
    if search_all:
        request_specs = [("", max(25, min(200, rows * 10)))]
    else:
        if not ontologies:
            return [], ""
        request_specs = [(ontology, max(1, rows)) for ontology in ontologies]

    for ontology, row_limit in request_specs:
        params_map: dict[str, object] = {"q": q, "rows": row_limit}
        if ontology:
            params_map["ontology"] = ontology
        if mother_only:
            params_map["local"] = "true"
        params = urlencode(params_map)
        url = f"https://www.ebi.ac.uk/ols4/api/search?{params}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
            successful_responses += 1
        except HTTPError as exc:
            request_failures.append(f"{ontology or 'all'}: HTTP {exc.code}")
            continue
        except URLError as exc:
            reason = str(getattr(exc, "reason", exc) or "").strip() or exc.__class__.__name__
            request_failures.append(f"{ontology or 'all'}: {reason}")
            continue
        except Exception as exc:
            request_failures.append(f"{ontology or 'all'}: {exc.__class__.__name__}")
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
            score = _manual_match_score(
                left_label or q,
                label or iri,
                left_kind=left_kind,
                right_kind=entity_kind,
            )
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
                "score": f"{score:.2f}",
            }
            if existing is None or float(row["score"]) > float(existing.get("score", "0") or "0"):
                by_key[key] = row

    ranked = sorted(
        by_key.values(),
        key=lambda row: (
            -float(row.get("score", "0") or "0"),
            row.get("ontology", ""),
            row.get("label", ""),
        ),
    )
    if ranked:
        return ranked[:50], ""
    if successful_responses == 0 and request_failures:
        if all("HTTP 503" in failure for failure in request_failures):
            return [], "OLS search is currently unavailable (HTTP 503 from www.ebi.ac.uk)."
        return [], "OLS search failed: " + "; ".join(request_failures[:3])
    return [], ""


def _infer_label_from_iri(iri: str) -> str:
    trimmed = (iri or "").rstrip("/#")
    if "#" in trimmed:
        return trimmed.rsplit("#", 1)[1]
    if "/" in trimmed:
        return trimmed.rsplit("/", 1)[1]
    return iri


def _next_alignment_id(df: pd.DataFrame) -> str:
    max_id = 0
    if "alignment_id" not in df.columns:
        return "CAND_0001"
    for value in df["alignment_id"].astype(str):
        match = re.fullmatch(r"CAND_(\d+)", value.strip())
        if not match:
            continue
        max_id = max(max_id, int(match.group(1)))
    return f"CAND_{max_id + 1:04d}"


def _normalize_label(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    value = re.sub(r"\([^)]*\)", " ", value)
    value = value.replace("_", " ").replace("-", " ")
    value = value.lower()
    value = re.sub(r"^(a|an|the)\s+", "", value)
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _tokenize(normalized_label: str) -> set[str]:
    if not normalized_label:
        return set()
    return {part for part in normalized_label.split() if part}


def _jaccard_score(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _manual_match_score(
    left_label: str,
    right_label: str,
    left_kind: str = "",
    right_kind: str = "",
) -> float:
    left_norm = _normalize_label(left_label)
    right_norm = _normalize_label(right_label)
    if left_norm and left_norm == right_norm:
        score = 1.0
    else:
        fuzzy = SequenceMatcher(None, left_norm, right_norm).ratio()
        jac = _jaccard_score(_tokenize(left_norm), _tokenize(right_norm))
        score = max(fuzzy, jac)
    if _kind_mismatch(left_kind, right_kind):
        score *= KIND_MISMATCH_SCORE_FACTOR
    return score


def _relation_for_score(score: float) -> str:
    return ""


def _normalize_mapping_relation(value: object, allowed: set[str]) -> str:
    rel = str(value or "").strip()
    if rel in allowed:
        return rel
    return ""


def _normalize_kind(value: object) -> str:
    kind = str(value or "").strip().lower()
    if kind in {"class", "property", "individual"}:
        return kind
    return ""


def _derived_export_mapping_labels(relation: str, left_kind: object, right_kind: object) -> list[str]:
    del left_kind, right_kind
    rel_bucket = relation.strip()
    if rel_bucket:
        return [rel_bucket]
    return []


def _short_local_name(iri: str) -> str:
    if "#" in iri:
        return iri.rsplit("#", 1)[-1]
    if "/" in iri:
        return iri.rsplit("/", 1)[-1]
    return iri


def _curie_for_iri(iri: str) -> str:
    if iri.startswith("http://www.w3.org/2002/07/owl#"):
        return "owl:" + _short_local_name(iri)
    if iri.startswith("http://www.w3.org/2000/01/rdf-schema#"):
        return "rdfs:" + _short_local_name(iri)
    if iri.startswith("http://www.w3.org/2004/02/skos/core#"):
        return "skos:" + _short_local_name(iri)
    return iri


def _build_mapping_relation_entries(merged: Graph) -> list[dict[str, str]]:
    skos_mapping_root = SKOS.mappingRelation
    skos_candidates: set[URIRef] = {skos_mapping_root}
    changed = True
    while changed:
        changed = False
        for subj, _, obj in merged.triples((None, RDFS.subPropertyOf, None)):
            if isinstance(subj, URIRef) and isinstance(obj, URIRef) and obj in skos_candidates and subj not in skos_candidates:
                skos_candidates.add(subj)
                changed = True

    keyword_tokens = ("equivalent", "same as", "subclass", "subproperty", "see also", "mapping", "match")
    structural_candidates: set[URIRef] = set()
    for subj in set(merged.subjects(RDF.type, RDF.Property)) | set(merged.subjects(RDF.type, OWL.ObjectProperty)) | set(
        merged.subjects(RDF.type, OWL.AnnotationProperty)
    ):
        if not isinstance(subj, URIRef):
            continue
        iri = str(subj)
        if iri.startswith("http://www.w3.org/2002/07/owl#") or iri.startswith("http://www.w3.org/2000/01/rdf-schema#"):
            text_parts = [iri.lower()]
            for obj in merged.objects(subj, RDFS.label):
                text_parts.append(str(obj).lower())
            for obj in merged.objects(subj, RDFS.comment):
                text_parts.append(str(obj).lower())
            blob = " ".join(text_parts)
            if any(tok in blob for tok in keyword_tokens):
                structural_candidates.add(subj)

    candidates = structural_candidates | skos_candidates

    entries: list[dict[str, str]] = []
    for iri_ref in sorted(
        candidates,
        key=lambda u: (
            0 if str(u).startswith("http://www.w3.org/2002/07/owl#")
            else 1 if str(u).startswith("http://www.w3.org/2000/01/rdf-schema#")
            else 2 if str(u).startswith("http://www.w3.org/2004/02/skos/core#")
            else 3,
            _curie_for_iri(str(u)).lower(),
        ),
    ):
        iri = str(iri_ref)
        label = ""
        definition = ""
        for obj in merged.objects(iri_ref, RDFS.label):
            label = str(obj).strip()
            if label:
                break
        for obj in merged.objects(iri_ref, RDFS.comment):
            definition = str(obj).strip()
            if definition:
                break
        if not definition:
            for obj in merged.objects(iri_ref, SKOS.definition):
                definition = str(obj).strip()
                if definition:
                    break
        entries.append(
            {
                "iri": iri,
                "curie": _curie_for_iri(iri),
                "label": label or _short_local_name(iri),
                "definition": definition or "",
            }
        )

    return entries


def _guess_rdf_format(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in {".ttl", ".turtle"}:
        return "turtle"
    if suffix in {".rdf", ".owl", ".xml"}:
        return "xml"
    if suffix in {".json", ".jsonld"}:
        return "json-ld"
    if suffix == ".nt":
        return "nt"
    if suffix == ".nq":
        return "nquads"
    if suffix == ".trig":
        return "trig"
    return None


def _find_mapping_relation_files() -> list[Path]:
    if not MAPPING_RELATIONS_DIR.is_dir():
        return []
    files = [
        path
        for path in MAPPING_RELATIONS_DIR.iterdir()
        if path.is_file() and path.stem.strip().lower() in MAPPING_RELATION_ONTOLOGY_IDS
    ]
    files.sort(key=lambda p: p.name.lower())
    return files


def _load_mapping_relations_from_local_ontologies() -> tuple[dict | None, str]:
    source_files = _find_mapping_relation_files()
    if not source_files:
        return (
            None,
            "No local mapping ontologies found. Download `owl`, `rdfs`, and `skos` first "
            "in `Fetch schemas and ontologies`.",
        )

    merged = Graph()
    parse_failures: list[str] = []
    for path in source_files:
        try:
            guessed = _guess_rdf_format(path)
            if guessed:
                merged.parse(path, format=guessed)
            else:
                merged.parse(path)
        except Exception as exc:
            parse_failures.append(f"{path.name}: {exc}")

    entries = _build_mapping_relation_entries(merged)
    if not entries:
        msg = "No mapping relation terms found in local ontology files."
        if parse_failures:
            msg = msg + " Parse issues: " + "; ".join(parse_failures[:3])
        return None, msg

    payload = {
        "catalog_version": "local-files-v1",
        "sources": [to_relpath(path) for path in source_files],
        "relations": entries,
    }
    msg = f"Loaded {len(entries)} relation term(s) from local ontology files."
    if parse_failures:
        msg = msg + " Some files could not be parsed."
    return payload, msg


def _first_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    if isinstance(value, dict):
        for subvalue in value.values():
            text = _first_text(subvalue)
            if text:
                return text
    return ""


def _extract_annotation_value(annotations: object, keys: list[str]) -> str:
    if not isinstance(annotations, dict):
        return ""
    for key in keys:
        if key in annotations:
            text = _first_text(annotations.get(key))
            if text:
                return text
    return ""


def _extract_annotation_by_substring(annotations: object, needle: str) -> str:
    if not isinstance(annotations, dict):
        return ""
    target = needle.strip().lower()
    if not target:
        return ""
    for key, value in annotations.items():
        key_text = str(key).strip().lower()
        if target in key_text:
            text = _first_text(value)
            if text:
                return text
    return ""


def _empty_ols_metadata() -> dict[str, str]:
    return {
        "label": "",
        "definition": "",
        "comment": "",
        "example": "",
        "term_api_url": "",
        "entity_kind": "",
    }


def _infer_ols_entity_kind(*values: object) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, dict):
            parts.extend(f"{key} {subvalue}" for key, subvalue in value.items())
        elif isinstance(value, (list, tuple, set)):
            parts.extend(str(item or "") for item in value)
        else:
            parts.append(str(value or ""))
    text = " ".join(parts).lower()
    if "property" in text:
        return "property"
    if "individual" in text:
        return "individual"
    if "class" in text or "concept" in text:
        return "class"
    return ""


def _mapping_guidance_text(rel: str, fallback_definition: str) -> str:
    entry = MAPPING_GUIDANCE.get(rel, {})
    when = str(entry.get("when", "")).strip()
    example = str(entry.get("example", "")).strip()
    if when and example:
        return f"{when} Example: `{example}`"
    if when:
        return when
    return fallback_definition


def _infer_ontology_from_iri(iri: str) -> str:
    parsed = urlparse(iri)
    path = parsed.path or ""
    if "/obo/" in path:
        tail = path.rsplit("/", 1)[-1]
        if "_" in tail:
            return tail.split("_", 1)[0].strip().lower()
    if "/ontologies/" in path:
        after = path.split("/ontologies/", 1)[1]
        onto = after.split("/", 1)[0].strip().lower()
        if onto:
            return onto
    return ""


def _lookup_ols_hit_by_iri(
    iri: str, timeout: float = 4.0
) -> tuple[str, dict[str, str]]:
    """Best-effort OLS lookup by IRI/short form; returns (ontology_prefix, metadata_hint)."""
    clean_iri = iri.strip()
    if not clean_iri:
        return "", _empty_ols_metadata()

    short_form = _infer_label_from_iri(clean_iri)
    search_params = [
        {"q": clean_iri, "rows": 50, "exact": "true"},
        {"q": clean_iri, "rows": 50},
    ]
    if short_form:
        search_params.extend(
            [
                {"q": short_form, "rows": 50, "exact": "true"},
                {"q": short_form, "rows": 50},
            ]
        )

    def fetch_docs(params: dict[str, object]) -> list[dict[str, object]]:
        url = f"https://www.ebi.ac.uk/ols4/api/search?{urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception:
            return []
        docs = payload.get("response", {}).get("docs", [])
        if not isinstance(docs, list):
            return []
        return [doc for doc in docs if isinstance(doc, dict)]

    def doc_to_hint(doc: dict[str, object]) -> dict[str, str]:
        definition = _first_text(doc.get("description")) or _first_text(doc.get("definition"))
        comment = _first_text(doc.get("comment"))
        example = _first_text(doc.get("example"))
        return {
            "label": _first_text(doc.get("label")),
            "definition": definition,
            "comment": comment,
            "example": example,
            "term_api_url": "",
            "entity_kind": _infer_ols_entity_kind(
                doc.get("entity_type"),
                doc.get("entityType"),
                doc.get("type"),
                doc.get("semantic_type"),
                doc.get("semanticType"),
            ),
        }

    def normalize_iri(text: str) -> str:
        value = str(text or "").strip()
        if not value:
            return ""
        value = value.rstrip("/")
        value = value.replace("https://", "http://", 1)
        return value

    def score_doc(doc: dict[str, object]) -> int:
        score = 0
        doc_iri = str(doc.get("iri", "") or "").strip()
        doc_short = str(doc.get("short_form", "") or "").strip()
        onto_prefix = str(doc.get("ontology_prefix", "") or "").strip().lower()
        if not onto_prefix:
            return -1

        clean_norm = normalize_iri(clean_iri)
        doc_norm = normalize_iri(doc_iri)
        if doc_norm and clean_norm and doc_norm == clean_norm:
            score += 100

        if short_form:
            short_lower = short_form.lower()
            if doc_short and doc_short.lower() == short_lower:
                score += 35
            doc_iri_lower = doc_iri.lower()
            if doc_iri_lower.endswith("/" + short_lower) or doc_iri_lower.endswith("#" + short_lower):
                score += 20
            if short_lower in doc_iri_lower:
                score += 5
        return score

    best_doc: dict[str, object] | None = None
    best_score = -1
    for params in search_params:
        docs = fetch_docs(params)
        for doc in docs:
            score = score_doc(doc)
            if score > best_score:
                best_score = score
                best_doc = doc

    if best_doc is not None and best_score > 0:
        onto_prefix = str(best_doc.get("ontology_prefix", "") or "").strip().lower()
        if onto_prefix:
            return onto_prefix, doc_to_hint(best_doc)
    return "", _empty_ols_metadata()


def _fetch_ols_metadata_for_iri(iri: str, ontology: str, timeout: float = 4.0) -> dict[str, str]:
    if not iri.strip() or not ontology.strip():
        return _empty_ols_metadata()
    iri_encoded = quote(iri.strip(), safe="")
    iri_double_encoded = quote(iri_encoded, safe="")
    ontology_encoded = quote(ontology.strip().lower(), safe="")
    terms_global_direct_url = f"https://www.ebi.ac.uk/ols4/api/terms/{iri_double_encoded}"
    terms_global_fallback_url = f"https://www.ebi.ac.uk/ols4/api/terms?iri={iri_encoded}"
    terms_direct_url = f"https://www.ebi.ac.uk/ols4/api/ontologies/{ontology_encoded}/terms/{iri_encoded}"
    terms_fallback_url = (
        f"https://www.ebi.ac.uk/ols4/api/ontologies/{ontology_encoded}/terms?iri={iri_encoded}"
    )
    props_direct_url = f"https://www.ebi.ac.uk/ols4/api/ontologies/{ontology_encoded}/properties/{iri_encoded}"
    props_fallback_url = (
        f"https://www.ebi.ac.uk/ols4/api/ontologies/{ontology_encoded}/properties?iri={iri_encoded}"
    )
    indiv_scoped_direct_url = (
        f"https://www.ebi.ac.uk/ols4/api/ontologies/{ontology_encoded}/individuals/{iri_double_encoded}"
    )
    indiv_scoped_fallback_url = (
        f"https://www.ebi.ac.uk/ols4/api/ontologies/{ontology_encoded}/individuals?iri={iri_encoded}"
    )
    indiv_global_direct_url = f"https://www.ebi.ac.uk/ols4/api/individuals/{iri_double_encoded}"
    indiv_global_fallback_url = f"https://www.ebi.ac.uk/ols4/api/individuals?iri={iri_encoded}"

    def fetch_direct(url: str) -> dict[str, object] | None:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                return data
        except Exception:
            return None
        return None

    def fetch_embedded_first(url: str) -> dict[str, object] | None:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                wrapper = json.loads(response.read().decode("utf-8", errors="replace"))
            if not isinstance(wrapper, dict):
                return None
            embedded = wrapper.get("_embedded", {})
            if not isinstance(embedded, dict):
                return None
            for key in ("terms", "properties", "individuals"):
                entries = embedded.get(key, [])
                if isinstance(entries, list) and entries:
                    first = entries[0]
                    if isinstance(first, dict):
                        return first
        except Exception:
            return None
        return None

    payload: dict[str, object] | None = None
    used_url = terms_fallback_url
    for url in (
        # Ontology Term Controller (robust default when ontology id is known)
        terms_fallback_url,
        terms_direct_url,
        # Global term endpoints as fallback
        terms_global_direct_url,
        terms_global_fallback_url,
        # Property and individual fallbacks
        props_direct_url,
        props_fallback_url,
        indiv_scoped_direct_url,
        indiv_scoped_fallback_url,
        indiv_global_direct_url,
        indiv_global_fallback_url,
    ):
        if (
            url.endswith(f"/terms/{iri_double_encoded}")
            or url.endswith(f"/terms/{iri_encoded}")
            or url.endswith(f"/properties/{iri_encoded}")
            or url.endswith(f"/individuals/{iri_double_encoded}")
        ):
            payload = fetch_direct(url)
        else:
            payload = fetch_embedded_first(url)
        if payload is not None:
            used_url = url
            break

    if payload is None:
        return _empty_ols_metadata()

    annotations = payload.get("annotation", payload.get("annotations", {}))
    label = _first_text(payload.get("label"))
    definition = (
        _first_text(payload.get("description"))
        or _first_text(payload.get("definition"))
        or _extract_annotation_value(
            annotations,
            [
                "http://www.w3.org/2004/02/skos/core#definition",
                "http://purl.obolibrary.org/obo/IAO_0000115",
                "definition",
            ],
        )
        or _extract_annotation_by_substring(annotations, "definition")
    )
    comment = (
        _first_text(payload.get("comment"))
        or _extract_annotation_value(
            annotations,
            ["http://www.w3.org/2000/01/rdf-schema#comment", "comment"],
        )
        or _extract_annotation_by_substring(annotations, "comment")
    )
    example = (
        _first_text(payload.get("example"))
        or _extract_annotation_value(
            annotations,
            [
                "http://purl.org/vocab/vann/example",
                "http://www.w3.org/2004/02/skos/core#example",
                "example",
            ],
        )
        or _extract_annotation_by_substring(annotations, "example")
    )
    return {
        "label": label,
        "definition": definition,
        "comment": comment,
        "example": example,
        "term_api_url": used_url,
        "entity_kind": _infer_ols_entity_kind(
            payload.get("entity_type"),
            payload.get("entityType"),
            payload.get("type"),
            payload.get("semantic_type"),
            payload.get("semanticType"),
        ),
    }


def _fetch_ols_metadata_for_entity(
    *,
    iri: str,
    ontology: str,
    entity_kind: str,
    timeout: float = 4.0,
) -> dict[str, str]:
    """Fetch OLS metadata prioritizing endpoint type requested by user."""
    clean_kind = (entity_kind or "").strip().lower()
    if clean_kind in {"class", "term"}:
        return _fetch_ols_metadata_for_iri(iri, ontology, timeout=timeout)

    # Reuse the robust parser by selecting endpoint priority via temporary URL ordering.
    iri_encoded = quote(iri.strip(), safe="")
    iri_double_encoded = quote(iri_encoded, safe="")
    ontology_encoded = quote(ontology.strip().lower(), safe="")
    terms_global_direct_url = f"https://www.ebi.ac.uk/ols4/api/terms/{iri_double_encoded}"
    terms_global_fallback_url = f"https://www.ebi.ac.uk/ols4/api/terms?iri={iri_encoded}"
    terms_direct_url = f"https://www.ebi.ac.uk/ols4/api/ontologies/{ontology_encoded}/terms/{iri_encoded}"
    terms_fallback_url = (
        f"https://www.ebi.ac.uk/ols4/api/ontologies/{ontology_encoded}/terms?iri={iri_encoded}"
    )
    props_direct_url = f"https://www.ebi.ac.uk/ols4/api/ontologies/{ontology_encoded}/properties/{iri_encoded}"
    props_fallback_url = (
        f"https://www.ebi.ac.uk/ols4/api/ontologies/{ontology_encoded}/properties?iri={iri_encoded}"
    )
    indiv_scoped_direct_url = (
        f"https://www.ebi.ac.uk/ols4/api/ontologies/{ontology_encoded}/individuals/{iri_double_encoded}"
    )
    indiv_scoped_fallback_url = (
        f"https://www.ebi.ac.uk/ols4/api/ontologies/{ontology_encoded}/individuals?iri={iri_encoded}"
    )
    indiv_global_direct_url = f"https://www.ebi.ac.uk/ols4/api/individuals/{iri_double_encoded}"
    indiv_global_fallback_url = f"https://www.ebi.ac.uk/ols4/api/individuals?iri={iri_encoded}"

    if clean_kind == "property":
        ordered = (
            props_direct_url,
            props_fallback_url,
            terms_fallback_url,
            terms_direct_url,
            terms_global_direct_url,
            terms_global_fallback_url,
            indiv_scoped_direct_url,
            indiv_scoped_fallback_url,
            indiv_global_direct_url,
            indiv_global_fallback_url,
        )
    elif clean_kind == "individual":
        ordered = (
            indiv_scoped_direct_url,
            indiv_scoped_fallback_url,
            indiv_global_direct_url,
            indiv_global_fallback_url,
            terms_fallback_url,
            terms_direct_url,
            terms_global_direct_url,
            terms_global_fallback_url,
            props_direct_url,
            props_fallback_url,
        )
    else:
        ordered = (
            terms_fallback_url,
            terms_direct_url,
            terms_global_direct_url,
            terms_global_fallback_url,
            props_direct_url,
            props_fallback_url,
            indiv_scoped_direct_url,
            indiv_scoped_fallback_url,
            indiv_global_direct_url,
            indiv_global_fallback_url,
        )

    # Inline minimal fetch logic to preserve the same extraction behavior.
    def fetch_direct(url: str) -> dict[str, object] | None:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
            if isinstance(data, dict):
                return data
        except Exception:
            return None
        return None

    def fetch_embedded_first(url: str) -> dict[str, object] | None:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                wrapper = json.loads(response.read().decode("utf-8", errors="replace"))
            if not isinstance(wrapper, dict):
                return None
            embedded = wrapper.get("_embedded", {})
            if not isinstance(embedded, dict):
                return None
            for key in ("terms", "properties", "individuals"):
                entries = embedded.get(key, [])
                if isinstance(entries, list) and entries:
                    first = entries[0]
                    if isinstance(first, dict):
                        return first
        except Exception:
            return None
        return None

    payload: dict[str, object] | None = None
    used_url = ordered[0]
    for url in ordered:
        if (
            "/terms/" in url
            or "/properties/" in url
            or "/individuals/" in url
        ):
            payload = fetch_direct(url)
        else:
            payload = fetch_embedded_first(url)
        if payload is not None:
            used_url = url
            break

    if payload is None:
        return _empty_ols_metadata()

    annotations = payload.get("annotation", payload.get("annotations", {}))
    label = _first_text(payload.get("label"))
    definition = (
        _first_text(payload.get("description"))
        or _first_text(payload.get("definition"))
        or _extract_annotation_value(
            annotations,
            [
                "http://www.w3.org/2004/02/skos/core#definition",
                "http://purl.obolibrary.org/obo/IAO_0000115",
                "definition",
            ],
        )
        or _extract_annotation_by_substring(annotations, "definition")
    )
    comment = (
        _first_text(payload.get("comment"))
        or _extract_annotation_value(
            annotations,
            ["http://www.w3.org/2000/01/rdf-schema#comment", "comment"],
        )
        or _extract_annotation_by_substring(annotations, "comment")
    )
    example = (
        _first_text(payload.get("example"))
        or _extract_annotation_value(
            annotations,
            [
                "http://purl.org/vocab/vann/example",
                "http://www.w3.org/2004/02/skos/core#example",
                "example",
            ],
        )
        or _extract_annotation_by_substring(annotations, "example")
    )
    return {
        "label": label,
        "definition": definition,
        "comment": comment,
        "example": example,
        "term_api_url": used_url,
        "entity_kind": _infer_ols_entity_kind(
            payload.get("entity_type"),
            payload.get("entityType"),
            payload.get("type"),
            payload.get("semantic_type"),
            payload.get("semanticType"),
        ),
    }


def _resolve_ols_entity_metadata(
    *,
    iri: str,
    ontology_candidates: list[str],
    search_hint: dict[str, str] | None = None,
) -> tuple[dict[str, str], str]:
    hint = dict(search_hint or {})
    search_entity_kind = _normalize_kind(hint.get("entity_kind", ""))
    kind_candidates: list[str] = []
    for kind in (search_entity_kind,) + OLS_ENTITY_KIND_VALUES:
        normalized = _normalize_kind(kind)
        if normalized and normalized not in kind_candidates:
            kind_candidates.append(normalized)

    metadata = _empty_ols_metadata()
    metadata_ontology = ontology_candidates[0] if ontology_candidates else ""
    for candidate_ontology in ontology_candidates:
        for candidate_kind in kind_candidates:
            metadata = _fetch_ols_metadata_for_entity(
                iri=iri,
                ontology=candidate_ontology,
                entity_kind=candidate_kind,
            )
            metadata_ontology = candidate_ontology
            for key in ("label", "definition", "comment", "example", "term_api_url", "entity_kind"):
                if not metadata.get(key, "").strip():
                    metadata[key] = hint.get(key, "")
            if metadata.get("term_api_url", "").strip() and metadata.get("label", "").strip():
                return metadata, metadata_ontology

    for key in ("label", "definition", "comment", "example", "term_api_url", "entity_kind"):
        if not metadata.get(key, "").strip():
            metadata[key] = hint.get(key, "")
    return metadata, metadata_ontology


def _render_term_card(
    *,
    side: str,
    title: str,
    fields: list[tuple[str, str]],
    selected: bool | None = None,
    score: float | None = None,
) -> None:
    classes = [f"term-card {side}"]
    if selected is not None:
        classes.append("selected" if selected else "unselected")
    elif side == "right":
        classes.append("neutral")
    score_html = ""
    if score is not None and not pd.isna(score):
        pct = max(0, min(100, int(round(score * 100))))
        if score >= 0.90:
            ring_color = "#16a34a"
        elif score >= 0.75:
            ring_color = "#d97706"
        else:
            ring_color = "#dc2626"
        score_html = (
            f'<div class="score-ring" style="--p:{pct}; --ring-color:{ring_color};">'
            f'<div class="score-ring-inner">{pct}%</div>'
            "</div>"
        )
    row_html_parts: list[str] = []
    for label, value in fields:
        compact, full = _compact_text(str(value))
        row_html_parts.append(
            (
                '<div class="card-row">'
                f'<strong>{html.escape(label)}:</strong> '
                f'<span title="{html.escape(full)}">{_format_link_or_text(compact)}</span>'
                "</div>"
            )
        )
    rows = "".join(row_html_parts)
    st.markdown(
        (
            f'<div class="{" ".join(classes)}">'
            '<div class="term-card-head">'
            f'<div class="term-card-title">{html.escape(title)}</div>'
            f"{score_html}"
            "</div>"
            f"{rows}"
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def render() -> None:
    st.title("Curate Candidate Alignments")
    st.markdown(
        """
        <style>
          .term-card {
            border-radius: 12px;
            padding: 0.55rem 0.65rem;
            margin-bottom: 0.45rem;
            border: 1px solid #d6dbe1;
            background: #f8fafc;
            font-size: 0.88rem;
          }
          .term-card.left {
            border-left: 6px solid #1f77b4;
            background: #f4f8ff;
          }
          .term-card.left.selected {
            border-left: 6px solid #2f9e44;
            background: #ebfff1;
            border-color: #86efac;
          }
          .term-card.right {
            border-left: 6px solid #2f9e44;
            background: #f4fff7;
          }
          .term-card.right.neutral {
            border-left: 6px solid #94a3b8;
            background: #f8fafc;
            border-color: #cbd5e1;
          }
          .term-card.right.selected {
            border-left: 6px solid #2f9e44;
            background: #ebfff1;
            border-color: #86efac;
          }
          .term-card.right.unselected {
            border-left: 6px solid #c92a2a;
            background: #fff5f5;
            border-color: #fecaca;
          }
          .term-card-title {
            font-weight: 700;
            margin-bottom: 0.2rem;
            font-size: 0.92rem;
          }
          .term-card-head {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.45rem;
            margin-bottom: 0.2rem;
          }
          .card-row {
            margin: 0.12rem 0;
            line-height: 1.18;
          }
          .score-ring {
            width: 31px;
            height: 31px;
            border-radius: 50%;
            background: conic-gradient(var(--ring-color, #16a34a) calc(var(--p) * 1%), #e2e8f0 0);
            display: grid;
            place-items: center;
            flex: 0 0 auto;
          }
          .score-ring-inner {
            width: 22px;
            height: 22px;
            border-radius: 50%;
            background: #ffffff;
            display: grid;
            place-items: center;
            font-size: 0.52rem;
            font-weight: 700;
            color: #1f2937;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )

    ctx = active_source_context()
    if ctx is None:
        st.warning("No source slug available. Configure sources in Download External Sources first.")
        return

    queue_file = to_relpath(ctx.queue_tsv)
    review_file = to_relpath(ctx.review_tsv)
    st.caption(f"Local queue: `{queue_file}`")
    st.caption(f"Review ledger: `{review_file}`")
    active_curator = str(st.session_state.get(STATE_CURATOR, "") or "").strip()
    active_curator_name = str(st.session_state.get(STATE_CURATOR_NAME, "") or "").strip()
    if active_curator and active_curator_name:
        st.caption(f"Active curator: `{active_curator_name}` ({active_curator})")
    else:
        st.error("Set a valid Curator ORCID with a resolvable public name in the left sidebar before starting curation.")
        return

    if (
        STATE_PATH not in st.session_state
        or st.session_state.get(STATE_PATH) != queue_file
        or STATE_DF not in st.session_state
    ):
        st.session_state[STATE_PATH] = queue_file
        st.session_state[STATE_DF] = _load_df(queue_file)
        st.session_state[STATE_DIRTY] = False
        st.session_state[STATE_MTIME] = _file_mtime(queue_file)
        st.session_state[STATE_KEPT_LEFT_TERMS] = []
        st.session_state[STATE_REVIEW_SYNC_IRIS] = set()
        st.session_state[STATE_PENDING_APPROVALS] = {}

    if STATE_KEPT_LEFT_TERMS not in st.session_state:
        st.session_state[STATE_KEPT_LEFT_TERMS] = []
    if STATE_REVIEW_SYNC_IRIS not in st.session_state:
        st.session_state[STATE_REVIEW_SYNC_IRIS] = set()
    if STATE_PENDING_APPROVALS not in st.session_state:
        st.session_state[STATE_PENDING_APPROVALS] = {}

    current_mtime = _file_mtime(queue_file)
    loaded_mtime = st.session_state.get(STATE_MTIME)
    if current_mtime != loaded_mtime:
        if st.session_state.get(STATE_DIRTY):
            st.warning(
                "Candidates file changed on disk, but you have unsaved edits in memory. "
                "Use 'Reload from disk' to refresh."
            )
        else:
            st.session_state[STATE_DF] = _load_df(queue_file)
            st.session_state[STATE_MTIME] = current_mtime
            st.session_state[STATE_PENDING_APPROVALS] = {}
            st.session_state[STATE_SELECTED_ALIGNMENT] = ""
            st.info("Reloaded latest local queue from disk.")

    col_reload, col_save = st.columns(2)
    with col_reload:
        if st.button("Reload from disk"):
            st.session_state[STATE_DF] = _load_df(queue_file)
            st.session_state[STATE_DIRTY] = False
            st.session_state[STATE_MTIME] = _file_mtime(queue_file)
            st.session_state[STATE_REVIEW_SYNC_IRIS] = set()
            st.session_state[STATE_PENDING_APPROVALS] = {}
    with col_save:
        if st.button("Save local queue", type="primary"):
            _save_queue_and_sync_review(queue_file, review_file)
            st.session_state[STATE_DIRTY] = False
            st.success(f"Saved `{queue_file}` and synced `{review_file}`")

    relation_catalog, catalog_msg = _load_mapping_relations_from_local_ontologies()
    relations = relation_catalog.get("relations", []) if relation_catalog else []
    relation_options = [str(x.get("curie", "")).strip() for x in relations if str(x.get("curie", "")).strip()]
    relation_definitions = {
        str(x.get("curie", "")).strip(): str(x.get("definition", "")).strip() or str(x.get("label", "")).strip()
        for x in relations
        if str(x.get("curie", "")).strip()
    }
    relation_allowed = set(relation_options)
    if catalog_msg:
        if relation_options:
            st.caption(catalog_msg)
        else:
            st.info(f"{catalog_msg} Mapping relation selection remains optional on this page.")

    df = st.session_state[STATE_DF]
    review_df = read_tsv(review_file)
    if df.empty and not to_path(queue_file).is_file():
        st.warning("No local queue found for this source. Please run Generate Pairwise Candidates first.")
        return

    term_groups = (
        df.groupby(["left_source", "left_term_iri"], dropna=False)["status"]
        .apply(lambda series: any(str(value) == "needs_review" for value in series))
        .reset_index(name="has_needs_review")
    )
    total_terms = len(term_groups)
    needs_review_terms = int(term_groups["has_needs_review"].sum()) if total_terms else 0
    curated_terms = max(0, total_terms - needs_review_terms)
    progress = (curated_terms / total_terms) if total_terms else 0.0

    st.subheader("Local Queue Progress")
    st.caption("Counts source terms in the local queue that no longer have any `needs_review` rows.")
    st.progress(progress, text=f"{curated_terms}/{total_terms} term(s) processed locally ({progress * 100:.1f}%)")

    shared_terms = int(review_df["source_term_iri"].astype(str).str.strip().ne("").sum()) if not review_df.empty else 0
    with st.expander(f"Shared Approved Decisions ({shared_terms})", expanded=False):
        st.caption("Read-only view of the versioned shared ledger. Use this to see what is already approved and by whom.")
        if review_df.empty:
            st.info("No approved shared decisions recorded yet for this source.")
        else:
            shared_search = st.text_input(
                "Search shared decisions",
                value="",
                key="shared_review_search",
                help="Filter the shared approved ledger by source/canonical labels, IRIs, reviewer, or comment.",
            )
            shared_display_df = _prepare_review_display_df(review_df)
            if shared_search.strip():
                token = shared_search.strip().lower()
                hay = (
                    shared_display_df["source_term_label"].astype(str).str.lower()
                    + " "
                    + shared_display_df["canonical_term_label"].astype(str).str.lower()
                    + " "
                    + shared_display_df["source_term_iri"].astype(str).str.lower()
                    + " "
                    + shared_display_df["canonical_term_iri"].astype(str).str.lower()
                    + " "
                    + shared_display_df["reviewer_name"].astype(str).str.lower()
                    + " "
                    + shared_display_df["reviewer_orcid"].astype(str).str.lower()
                    + " "
                    + shared_display_df["curation_comment"].astype(str).str.lower()
                )
                shared_display_df = shared_display_df[hay.str.contains(token, na=False)]
            st.caption(f"Shared ledger rows: {len(shared_display_df)}")
            render_clickable_dataframe(shared_display_df, use_container_width=True, hide_index=True)

    st.subheader("Filter")
    available_statuses = sorted(df["status"].dropna().unique().tolist())
    default_statuses = ["needs_review"] if "needs_review" in available_statuses else available_statuses
    selected_statuses = st.multiselect(
        "Status",
        options=available_statuses,
        default=default_statuses,
    )
    search_text = st.text_input("Search (left/right labels, logs, curation comments)", value="")

    filtered = _filtered_df(df, selected_statuses, search_text)
    st.caption(f"Filtered rows: {len(filtered)}")

    left_terms_df = (
        filtered[
            [
                "left_source",
                "left_term_iri",
                "left_label",
                "left_definition",
                "left_comment",
                "left_example",
                "left_term_kind",
            ]
        ]
        .drop_duplicates()
        .copy()
    )

    token = search_text.strip().lower()
    if token:
        label_series = left_terms_df["left_label"].astype(str).str.lower()
        left_terms_df = left_terms_df[label_series.str.contains(token, na=False)]
    left_terms_df = left_terms_df.drop_duplicates(subset=["left_source", "left_term_iri", "left_label"]).reset_index(
        drop=True
    )

    if left_terms_df.empty:
        st.info("No left terms available for current filters. Generate or reload candidates first.")
    else:
        left_keys = list(left_terms_df[["left_source", "left_term_iri", "left_label"]].itertuples(index=False, name=None))
        if STATE_LEFT_TERM_INDEX not in st.session_state:
            st.session_state[STATE_LEFT_TERM_INDEX] = 0
        left_idx = int(st.session_state.get(STATE_LEFT_TERM_INDEX, 0))
        left_idx = max(0, min(left_idx, len(left_keys) - 1))
        selected_left = st.selectbox(
            "Left term",
            options=left_keys,
            index=left_idx,
            format_func=lambda item: f"{item[2]} ({item[0]})",
            help="Select a left ontology term to review all candidate matches on the right.",
        )
        selected_idx = left_keys.index(selected_left)
        st.session_state[STATE_LEFT_TERM_INDEX] = selected_idx
        left_source, left_iri, left_label = selected_left
        left_term_key = f"{left_source}|{left_iri}"
        pending_approvals = st.session_state.get(STATE_PENDING_APPROVALS, {})
        shared_left_rows = pd.DataFrame()
        if not review_df.empty and "source_term_iri" in review_df.columns:
            shared_left_rows = review_df[review_df["source_term_iri"].astype(str) == str(left_iri)].copy()
        if not shared_left_rows.empty:
            shared_left_rows = shared_left_rows.sort_values(by="date_reviewed", ascending=False, na_position="last")
            st.info(f"**{len(shared_left_rows)} shared approved mapping(s)** for this term in the review ledger:")
            for _, approved_row in shared_left_rows.iterrows():
                canonical_label = str(approved_row.get("canonical_term_label", "") or "").strip()
                canonical_source = str(approved_row.get("canonical_term_source", "") or "").strip()
                relation = str(approved_row.get("relation", "") or "").strip()
                reviewer_name = str(approved_row.get("reviewer_name", "") or "").strip()
                date_reviewed = str(approved_row.get("date_reviewed", "") or "").strip()
                summary_bits = [f"`{canonical_label or '(no label)'}`"]
                if canonical_source:
                    summary_bits.append(f"from `{canonical_source}`")
                if relation:
                    summary_bits.append(f"via `{relation}`")
                if reviewer_name:
                    summary_bits.append(f"by {reviewer_name}")
                if date_reviewed:
                    summary_bits.append(f"on `{date_reviewed[:10]}`")
                st.caption(" | ".join(summary_bits))
        left_row_series = left_terms_df[
            (left_terms_df["left_source"] == left_source) & (left_terms_df["left_term_iri"] == left_iri)
        ].iloc[0]

        group_df = filtered[
            (filtered["left_source"] == left_source) & (filtered["left_term_iri"] == left_iri)
        ].copy()
        if not group_df.empty:
            group_df["_match_score_num"] = pd.to_numeric(group_df["match_score"], errors="coerce").fillna(-1.0)
            group_df = group_df.sort_values(by=["_match_score_num", "right_label"], ascending=[False, True])
        kept_left_terms = set(str(x) for x in st.session_state.get(STATE_KEPT_LEFT_TERMS, []))
        left_is_kept = left_term_key in kept_left_terms
        if not group_df.empty and (group_df["status"] == "rejected").all():
            left_is_kept = True

        selected_alignment_id = str(st.session_state.get(STATE_SELECTED_ALIGNMENT, ""))
        if not group_df.empty:
            available_alignment_ids = group_df["alignment_id"].astype(str).tolist()
            if selected_alignment_id not in available_alignment_ids:
                st.session_state[STATE_SELECTED_ALIGNMENT] = ""
                selected_alignment_id = ""
        else:
            selected_alignment_id = ""
            st.session_state[STATE_SELECTED_ALIGNMENT] = ""

        if relation_options:
            with st.expander("Mapping guidance", expanded=False):
                recommended_ordered = [
                    rel
                    for rel in MAPPING_GUIDANCE_ORDER
                    if rel in relation_options and MAPPING_GUIDANCE.get(rel, {}).get("tier") != "advanced"
                ]
                advanced_ordered = [
                    rel
                    for rel in MAPPING_GUIDANCE_ORDER
                    if rel in relation_options and MAPPING_GUIDANCE.get(rel, {}).get("tier") == "advanced"
                ]
                remaining = [rel for rel in relation_options if rel not in set(recommended_ordered + advanced_ordered)]

                st.markdown("**Recommended first (ontology reconciliation)**")
                for rel in recommended_ordered:
                    st.markdown(f"- `{rel}`: {_mapping_guidance_text(rel, relation_definitions.get(rel, ''))}")
                if not recommended_ordered:
                    st.markdown("- No recommended relations available from local mapping ontologies.")

                st.markdown("**Advanced / specific modeling cases**")
                for rel in advanced_ordered:
                    st.markdown(f"- `{rel}`: {_mapping_guidance_text(rel, relation_definitions.get(rel, ''))}")
                for rel in remaining:
                    st.markdown(f"- `{rel}`: {relation_definitions.get(rel, '')}")
        else:
            st.caption("Mapping relation metadata is unavailable locally and remains optional for validation.")

        st.subheader("Side-by-side context")
        left_col, right_col = st.columns(2)
        with left_col:
            st.markdown(f"**{_display_source(left_source)} term**")
            _render_term_card(
                side="left",
                title=left_label,
                selected=left_is_kept,
                fields=[
                    ("Source", _display_source(left_row_series["left_source"])),
                    ("Kind", _display_kind(left_row_series.get("left_term_kind", ""))),
                    ("IRI", str(left_row_series["left_term_iri"])),
                    ("Definition", str(left_row_series.get("left_definition", "") or "-")),
                    ("Comment", str(left_row_series.get("left_comment", "") or "-")),
                    ("Example", str(left_row_series.get("left_example", "") or "-")),
                ],
            )
            relation_bound_pending = [
                item
                for item in pending_approvals.get(left_term_key, [])
                if str(item.get("relation", "") or "").strip()
            ]
            relation_missing_pending = [
                item
                for item in pending_approvals.get(left_term_key, [])
                if not str(item.get("relation", "") or "").strip()
            ]
            left_button_label = "Remove left selection" if left_is_kept else "Keep current term"
            if st.button(left_button_label, key=f"keep_left_{left_term_key}"):
                if left_is_kept:
                    if relation_bound_pending:
                        st.warning("Left term must stay selected while any queued right term has a relationship.")
                    else:
                        kept_left_terms.discard(left_term_key)
                else:
                    if relation_missing_pending:
                        st.warning(
                            "Selecting the left term also requires a relationship for every queued right term."
                        )
                    else:
                        kept_left_terms.add(left_term_key)
                st.session_state[STATE_KEPT_LEFT_TERMS] = sorted(kept_left_terms)
                st.session_state[STATE_SELECTED_ALIGNMENT] = ""
                if not ((left_is_kept and relation_bound_pending) or ((not left_is_kept) and relation_missing_pending)):
                    st.rerun()
        with right_col:
            st.markdown("**Matched terms**")
            if group_df.empty:
                st.info("No right-side candidates found for this left term yet.")
            else:
                for _, right_row in group_df.iterrows():
                    current_alignment_id = str(right_row["alignment_id"])
                    pending_for_term = pending_approvals.get(left_term_key, [])
                    pending_by_alignment = {item["alignment_id"]: item.get("relation", "") for item in pending_for_term}
                    is_pending = current_alignment_id in pending_by_alignment
                    is_recent = current_alignment_id == selected_alignment_id
                    is_kind_mismatch = _kind_mismatch(
                        left_row_series.get("left_term_kind", ""),
                        right_row.get("right_term_kind", ""),
                    )
                    if is_pending:
                        card_state = True
                    elif left_is_kept:
                        card_state = False
                    else:
                        card_state = None
                    title = str(right_row["right_label"] or right_row["right_term_iri"] or "(no label)")
                    if is_pending:
                        title = f"{title} [queued]"
                    elif is_recent:
                        title = f"{title} [recent]"
                    _render_term_card(
                        side="right",
                        title=title,
                        selected=card_state,
                        score=float(pd.to_numeric(right_row["match_score"], errors="coerce"))
                        if str(right_row.get("match_score", "")).strip()
                        else None,
                        fields=[
                            ("Source", _display_source(right_row["right_source"])),
                            ("Kind", _display_kind(right_row.get("right_term_kind", ""))),
                            ("Kind warning", "mismatch" if is_kind_mismatch else "-"),
                            ("IRI", str(right_row["right_term_iri"])),
                            ("Match score", str(right_row["match_score"])),
                            ("Status", str(right_row["status"])),
                            ("Definition", str(right_row["right_definition"] or "-")),
                            ("Comment", str(right_row["right_comment"] or "-")),
                            ("Example", str(right_row["right_example"] or "-")),
                        ],
                    )
                    cand_rel_key = f"cand_rel_{current_alignment_id}"
                    if cand_rel_key not in st.session_state:
                        preferred_relation = pending_by_alignment.get(
                            current_alignment_id,
                            _normalize_mapping_relation(str(right_row.get("relation", "") or ""), relation_allowed),
                        )
                        st.session_state[cand_rel_key] = preferred_relation

                    if relation_options:
                        relation_select_options = [MAPPING_RELATION_PLACEHOLDER] + relation_options
                        chosen_relation = str(st.session_state.get(cand_rel_key, "") or "")
                        relation_index = (
                            relation_select_options.index(chosen_relation)
                            if chosen_relation in relation_select_options
                            else 0
                        )
                        chosen_relation = st.selectbox(
                            "Relation",
                            options=relation_select_options,
                            index=relation_index,
                            key=cand_rel_key,
                            label_visibility="collapsed",
                            help="Optional mapping relation for this specific candidate.",
                            disabled=is_pending,
                        )
                        chosen_relation = chosen_relation if chosen_relation in relation_allowed else ""
                    else:
                        chosen_relation = ""

                    action_col = st.columns([1])[0]
                    relation_requires_left = bool(chosen_relation) and not left_is_kept
                    left_with_right_requires_relation = left_is_kept and not bool(chosen_relation)
                    right_only_allows_one = (not left_is_kept) and bool(pending_for_term)
                    if is_pending:
                        if action_col.button(
                            "Unselect",
                            key=f"unapprove_{current_alignment_id}",
                            use_container_width=True,
                        ):
                            pending_for_term = [
                                item for item in pending_for_term if item["alignment_id"] != current_alignment_id
                            ]
                            pending_approvals[left_term_key] = pending_for_term
                            st.session_state[STATE_PENDING_APPROVALS] = pending_approvals
                            st.rerun()
                    else:
                        if action_col.button(
                            "Select",
                            key=f"approve_match_{current_alignment_id}",
                            use_container_width=True,
                            disabled=relation_requires_left or left_with_right_requires_relation or right_only_allows_one,
                        ):
                            pending_for_term = [
                                item for item in pending_for_term if item["alignment_id"] != current_alignment_id
                            ]
                            pending_for_term.append(
                                {"alignment_id": current_alignment_id, "relation": chosen_relation}
                            )
                            pending_approvals[left_term_key] = pending_for_term
                            st.session_state[STATE_PENDING_APPROVALS] = pending_approvals
                            st.session_state[STATE_SELECTED_ALIGNMENT] = current_alignment_id
                            st.rerun()
                        if relation_requires_left:
                            st.caption("Select the left term as well if you want to assign a relationship.")
                        elif left_with_right_requires_relation:
                            st.caption("Select a relationship if you want to approve this right term with the left term.")
                        elif right_only_allows_one:
                            st.caption("Without the left term selected, you can queue only one right term.")

            st.markdown("**Add manual candidate**")
            manual_search_query_key = f"manual_candidate_search_query_{left_term_key}"
            manual_search_query_used_key = f"manual_candidate_search_query_used_{left_term_key}"
            manual_search_results_key = f"manual_candidate_search_results_{left_term_key}"
            manual_search_ontologies_key = f"manual_candidate_search_ontologies_{left_term_key}"
            manual_search_all_key = f"manual_candidate_search_all_{left_term_key}"
            manual_mother_only_key = f"manual_candidate_mother_only_{left_term_key}"
            manual_rows_key = f"manual_candidate_rows_{left_term_key}"
            manual_selected_ontologies_key = f"manual_candidate_selected_ontologies_{left_term_key}"
            manual_url_key = f"manual_candidate_url_{left_term_key}"
            manual_ontology_key = f"manual_candidate_ontology_{left_term_key}"
            if manual_search_query_key not in st.session_state:
                st.session_state[manual_search_query_key] = left_label
            ontology_options = _ols_catalog()
            default_ontos = [x for x in ["chebi", "obi", "ms", "chmo", "edam"] if x in ontology_options] or ontology_options[:5]
            if manual_search_all_key not in st.session_state:
                st.session_state[manual_search_all_key] = False
            if manual_mother_only_key not in st.session_state:
                st.session_state[manual_mother_only_key] = True
            if manual_rows_key not in st.session_state:
                st.session_state[manual_rows_key] = 8
            if manual_selected_ontologies_key not in st.session_state:
                st.session_state[manual_selected_ontologies_key] = default_ontos
            if manual_url_key not in st.session_state:
                st.session_state[manual_url_key] = ""
            if manual_ontology_key not in st.session_state:
                st.session_state[manual_ontology_key] = ""

            def _add_manual_candidate_row(iri_value: str, ontology_id_value: str, *, source: str) -> None:
                iri_clean = iri_value.strip()
                ontology_id_clean = ontology_id_value.strip().lower()
                if not iri_clean:
                    st.error("Manual candidate URL/IRI is required.")
                    return
                if not _is_http(iri_clean):
                    st.error("Manual candidate must be a valid IRI starting with http:// or https://.")
                    return

                existing_mask = (
                    (df["left_source"] == left_source)
                    & (df["left_term_iri"] == left_iri)
                    & (df["right_term_iri"] == iri_clean)
                )
                if bool(existing_mask.any()):
                    existing_alignment_id = str(df.loc[existing_mask].iloc[0]["alignment_id"])
                    st.warning("This candidate URL already exists for the selected left term.")
                    st.session_state[STATE_SELECTED_ALIGNMENT] = existing_alignment_id
                    st.rerun()

                resolved_ontology, search_hint = _lookup_ols_hit_by_iri(iri_clean)
                ontology_candidates: list[str] = []
                for candidate_ontology in (ontology_id_clean, resolved_ontology):
                    clean_ontology = str(candidate_ontology or "").strip().lower()
                    if clean_ontology and clean_ontology not in ontology_candidates:
                        ontology_candidates.append(clean_ontology)
                if not ontology_candidates:
                    st.error("Unable to resolve ontology for this IRI. Provide a valid ontology ID.")
                    return

                metadata, metadata_ontology = _resolve_ols_entity_metadata(
                    iri=iri_clean,
                    ontology_candidates=ontology_candidates,
                    search_hint=search_hint,
                )

                if not metadata.get("term_api_url", "").strip() or not metadata.get("label", "").strip():
                    st.error(
                        "No OLS match found for this ontology ID + IRI. "
                        "Card was not created."
                    )
                    return

                resolved_kind = _normalize_kind(metadata.get("entity_kind", ""))
                right_label_value = metadata["label"].strip()
                match_score_value = _manual_match_score(
                    left_label,
                    right_label_value,
                    left_kind=str(left_row_series.get("left_term_kind", "") or ""),
                    right_kind=resolved_kind,
                )

                new_row = {col: "" for col in df.columns}
                new_row["alignment_id"] = _next_alignment_id(df)
                new_row["left_source"] = left_source
                new_row["left_term_iri"] = left_iri
                new_row["left_label"] = left_label
                new_row["left_definition"] = str(left_row_series.get("left_definition", "") or "")
                new_row["left_comment"] = str(left_row_series.get("left_comment", "") or "")
                new_row["left_example"] = str(left_row_series.get("left_example", "") or "")
                new_row["left_term_kind"] = str(left_row_series.get("left_term_kind", "") or "")
                new_row["right_source"] = metadata_ontology.upper()
                new_row["right_term_iri"] = iri_clean
                new_row["right_label"] = right_label_value
                new_row["right_definition"] = metadata["definition"]
                new_row["right_comment"] = metadata["comment"]
                new_row["right_example"] = metadata["example"]
                new_row["right_term_kind"] = resolved_kind
                new_row["ols_term_api_url"] = metadata["term_api_url"] or iri_clean
                new_row["match_method"] = "manual_url_lexical"
                new_row["match_score"] = f"{match_score_value:.2f}"
                new_row["relation"] = _relation_for_score(match_score_value)
                new_row["suggestion_source"] = "manual_search"
                new_row["canonical_from"] = ""
                new_row["canonical_term_iri"] = ""
                new_row["canonical_term_label"] = ""
                new_row["canonical_term_source"] = ""
                new_row["canonical_term_kind"] = ""
                new_row["ols_search_url"] = _ols_search_url(left_label)
                new_row["bioportal_search_url"] = _bioportal_search_url(left_label)
                new_row["status"] = "needs_review"
                new_row["curator"] = "auto"
                new_row["curator_name"] = ""
                new_row["reviewer"] = active_curator
                new_row["reviewer_name"] = active_curator_name
                new_row["date_added"] = utc_now_timestamp()
                new_row["date_reviewed"] = ""
                if source == "search":
                    new_row["logs"] = (
                        f"Manual {resolved_kind or 'term'} candidate added from OLS search result."
                    )
                else:
                    new_row["logs"] = (
                        f"Manual {resolved_kind or 'term'} candidate added by URL with ontology id "
                        f"'{ontology_id_clean}'."
                    )
                new_row["curation_comment"] = ""
                if "normalized_left_label" in df.columns:
                    new_row["normalized_left_label"] = str(left_label).strip().lower()
                if "normalized_right_label" in df.columns:
                    new_row["normalized_right_label"] = str(right_label_value).strip().lower()

                st.session_state[STATE_DF] = pd.concat(
                    [st.session_state[STATE_DF], pd.DataFrame([new_row], columns=df.columns)],
                    ignore_index=True,
                )
                st.session_state[STATE_DIRTY] = True
                st.session_state[STATE_SELECTED_ALIGNMENT] = str(new_row["alignment_id"])
                st.success("Manual candidate added.")
                st.rerun()

            manual_search_query = st.text_input(
                "Keyword search",
                key=manual_search_query_key,
                help="Search OLS candidates by keyword (usually the source term label).",
            )
            ols_link = _ols_search_url(manual_search_query or left_label)
            st.markdown(f"[Open OLS search for this term]({ols_link})")

            add_manual_candidate_clicked = False
            with st.expander("Advanced options", expanded=False):
                st.checkbox(
                    "Search across ALL OLS ontologies (slower)",
                    key=manual_search_all_key,
                    help="When enabled, ontology selection is ignored and OLS global search is used.",
                )
                st.checkbox(
                    "Only terms defined in source ontology (exclude imports)",
                    key=manual_mother_only_key,
                    help=(
                        "Keeps only terms where OLS marks the hit as defined by the ontology "
                        "(not imported from another ontology)."
                    ),
                )
                st.multiselect(
                    "Ontologies",
                    options=ontology_options,
                    key=manual_selected_ontologies_key,
                    help="Choose OLS ontologies to search for matching terms.",
                    disabled=bool(st.session_state.get(manual_search_all_key, False)),
                )
                st.number_input(
                    "Rows per ontology",
                    min_value=1,
                    max_value=30,
                    step=1,
                    key=manual_rows_key,
                )
                st.markdown("**Direct IRI entry**")
                manual_url = st.text_input(
                    "Manual candidate term IRI",
                    value="",
                    key=manual_url_key,
                    help="Paste a full term IRI (must start with http:// or https://).",
                )
                manual_ontology_id = st.text_input(
                    "Ontology ID (required)",
                    value="",
                    key=manual_ontology_key,
                    help="OLS ontology id (for example: chebi, obi, mesh, biolink, uniprotrdfs).",
                )
                add_manual_candidate_clicked = st.button(
                    "Add manual candidate",
                    key=f"add_manual_candidate_{left_term_key}",
                )

            search_col, clear_col = st.columns([1, 1])
            if search_col.button("Search ontology terms", key=f"search_manual_candidates_{left_term_key}"):
                results, search_error = _search_ols(
                    manual_search_query,
                    list(st.session_state.get(manual_selected_ontologies_key, [])),
                    left_label=left_label,
                    left_kind=str(left_row_series.get("left_term_kind", "") or ""),
                    rows=int(st.session_state.get(manual_rows_key, 8) or 8),
                    search_all=bool(st.session_state.get(manual_search_all_key, False)),
                    mother_only=bool(st.session_state.get(manual_mother_only_key, True)),
                )
                st.session_state[manual_search_results_key] = results
                st.session_state[manual_search_query_used_key] = manual_search_query
                st.session_state[manual_search_ontologies_key] = (
                    ["ALL"]
                    if st.session_state.get(manual_search_all_key, False)
                    else list(st.session_state.get(manual_selected_ontologies_key, []))
                )
                if search_error:
                    st.error(search_error)
                elif not results:
                    st.warning("No OLS result found for this query/ontology selection.")
                else:
                    st.success(f"Loaded {len(results)} candidate mapping term(s).")
            if clear_col.button("Clear search results", key=f"clear_manual_candidates_{left_term_key}"):
                st.session_state[manual_search_results_key] = []
                st.session_state[manual_search_query_used_key] = ""
                st.session_state[manual_search_ontologies_key] = []
                st.rerun()

            manual_results = st.session_state.get(manual_search_results_key, [])
            if manual_results:
                st.caption(
                    "Search context: "
                    f"`{st.session_state.get(manual_search_query_used_key, '')}` in "
                    f"{', '.join(st.session_state.get(manual_search_ontologies_key, []))}"
                )
                header_cols = st.columns([1.3, 1.2, 2.2, 3.2, 1.3, 1.1])
                header_cols[0].markdown("**Action**")
                header_cols[1].markdown("**Score**")
                header_cols[2].markdown("**Term**")
                header_cols[3].markdown("**Label**")
                header_cols[4].markdown("**Ontology**")
                header_cols[5].markdown("**Kind**")
                for idx, result_row in enumerate(manual_results[:50]):
                    iri = str(result_row.get("iri", "")).strip()
                    ontology = str(result_row.get("ontology", "")).strip().lower()
                    label = str(result_row.get("label", "")).strip() or iri
                    entity_kind = str(result_row.get("entity_kind", "")).strip().lower() or "class"
                    short_form = str(result_row.get("short_form", "")).strip()
                    term_page = str(result_row.get("ols_term_page", "")).strip()
                    score_value = float(pd.to_numeric(result_row.get("score", 0.0), errors="coerce") or 0.0)
                    row_cols = st.columns([1.3, 1.2, 2.2, 3.2, 1.3, 1.1])
                    with row_cols[0]:
                        if st.button(
                            "Use",
                            key=f"use_manual_candidate_hit_{left_term_key}_{idx}_{short_form or iri}",
                            disabled=not bool(iri),
                            use_container_width=True,
                        ):
                            _add_manual_candidate_row(
                                iri,
                                ontology if ontology != "unknown" else "",
                                source="search",
                            )
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

            if add_manual_candidate_clicked:
                iri = manual_url.strip()
                ontology_id = manual_ontology_id.strip().lower()
                if not ontology_id:
                    st.error("Ontology ID is required.")
                else:
                    _add_manual_candidate_row(iri, ontology_id, source="manual")
        pending_for_term = st.session_state.get(STATE_PENDING_APPROVALS, {}).get(left_term_key, [])
        if left_is_kept:
            st.caption("Current source term is selected for approval.")
        if pending_for_term:
            st.markdown(f"**{len(pending_for_term)} mapping(s) queued for approval:**")
            for pending_item in pending_for_term:
                pending_alignment_id = pending_item["alignment_id"]
                pending_relation = pending_item.get("relation", "") or "(no relation)"
                pending_rows = group_df[group_df["alignment_id"].astype(str) == pending_alignment_id]
                if not pending_rows.empty:
                    pending_row = pending_rows.iloc[0]
                    pending_label = str(
                        pending_row.get("right_label")
                        or pending_row.get("right_term_iri")
                        or pending_alignment_id
                    )
                    pending_source = str(pending_row.get("right_source", "") or "")
                    st.caption(f"→ `{pending_relation}` → **{pending_label}** ({pending_source})")
                else:
                    st.caption(f"→ `{pending_relation}` → alignment {pending_alignment_id}")
        elif not left_is_kept:
            st.info("Approve one or more candidate matches above, keep the current term, or combine both.")

        decision_made = left_is_kept or bool(pending_for_term)

        st.markdown("**Curation comment**")
        curation_comment = st.text_area(
            "curation_comment (optional)",
            value="",
            key=f"curation_comment_{left_term_key}",
            help="Saved separately from automated logs for this validation step.",
        )
        validate_col = st.columns([1, 2, 1])[1]
        if validate_col.button(
            "Validate",
            key=f"validate_selection_{left_term_key}",
            type="primary",
            disabled=not decision_made,
            use_container_width=True,
        ):
            pending_approvals = st.session_state.get(STATE_PENDING_APPROVALS, {})
            approved_ids = {
                item["alignment_id"]: item.get("relation", "")
                for item in pending_approvals.get(left_term_key, [])
            }
            if not left_is_kept and any(str(relation or "").strip() for relation in approved_ids.values()):
                st.error("A queued right-term relationship requires the left term to be selected as well.")
                st.stop()
            if not left_is_kept and len(approved_ids) > 1:
                st.error("Without the left term selected, only one right term can be validated.")
                st.stop()
            if left_is_kept and approved_ids and any(not str(relation or "").strip() for relation in approved_ids.values()):
                st.error("When the left term and right term are both selected, each right-term mapping needs a relationship.")
                st.stop()
            mask = (df["left_source"] == left_source) & (df["left_term_iri"] == left_iri)
            idxs = df.index[mask].tolist()

            carrier_idx = None
            if left_is_kept:
                candidate_carriers = [idx for idx in idxs if str(df.at[idx, "alignment_id"] or "") not in approved_ids]
                carrier_idx = candidate_carriers[0] if candidate_carriers else None

            for idx in idxs:
                existing_logs = str(df.at[idx, "logs"] or "").strip()
                alignment_id = str(df.at[idx, "alignment_id"] or "")
                if left_is_kept and idx == carrier_idx:
                    df.at[idx, "status"] = "approved"
                    df.at[idx, "canonical_from"] = "left"
                    df.at[idx, "canonical_term_iri"] = df.at[idx, "left_term_iri"]
                    df.at[idx, "canonical_term_label"] = df.at[idx, "left_label"]
                    df.at[idx, "canonical_term_source"] = df.at[idx, "left_source"]
                    df.at[idx, "canonical_term_kind"] = df.at[idx, "left_term_kind"]
                    df.at[idx, "relation"] = ""
                    df.at[idx, "suggestion_source"] = "manual_curated"
                    log_entry = "Kept current source term; recorded source term as canonical."
                elif alignment_id in approved_ids:
                    df.at[idx, "status"] = "approved"
                    df.at[idx, "canonical_from"] = "right"
                    df.at[idx, "canonical_term_iri"] = df.at[idx, "right_term_iri"]
                    df.at[idx, "canonical_term_label"] = df.at[idx, "right_label"]
                    df.at[idx, "canonical_term_source"] = df.at[idx, "right_source"]
                    df.at[idx, "canonical_term_kind"] = df.at[idx, "right_term_kind"]
                    df.at[idx, "relation"] = approved_ids.get(alignment_id, "")
                    df.at[idx, "suggestion_source"] = "manual_curated"
                    log_entry = "Validated right-side match (multi-mapping)."
                else:
                    df.at[idx, "status"] = "rejected"
                    df.at[idx, "canonical_from"] = ""
                    df.at[idx, "canonical_term_iri"] = ""
                    df.at[idx, "canonical_term_label"] = ""
                    df.at[idx, "canonical_term_source"] = ""
                    df.at[idx, "canonical_term_kind"] = ""
                    df.at[idx, "relation"] = ""
                    log_entry = "Not selected for this left term."
                df.at[idx, "logs"] = _append_log(existing_logs, log_entry)
                df.at[idx, "curation_comment"] = curation_comment.strip()
                _set_review_fields(df, idx, active_curator)

            if left_is_kept and carrier_idx is None:
                new_row = {col: "" for col in df.columns}
                new_row["alignment_id"] = _next_alignment_id(df)
                new_row["left_source"] = left_source
                new_row["left_term_iri"] = left_iri
                new_row["left_label"] = left_label
                new_row["left_definition"] = str(left_row_series.get("left_definition", "") or "")
                new_row["left_comment"] = str(left_row_series.get("left_comment", "") or "")
                new_row["left_example"] = str(left_row_series.get("left_example", "") or "")
                new_row["left_term_kind"] = str(left_row_series.get("left_term_kind", "") or "")
                new_row["status"] = "approved"
                new_row["canonical_from"] = "left"
                new_row["canonical_term_iri"] = left_iri
                new_row["canonical_term_label"] = left_label
                new_row["canonical_term_source"] = left_source
                new_row["canonical_term_kind"] = str(left_row_series.get("left_term_kind", "") or "")
                new_row["relation"] = ""
                new_row["suggestion_source"] = "manual_curated"
                new_row["curator"] = "auto"
                new_row["curator_name"] = ""
                new_row["reviewer"] = active_curator
                new_row["reviewer_name"] = active_curator_name
                new_row["date_added"] = utc_now_timestamp()
                new_row["logs"] = "Kept current source term; recorded source term as canonical."
                new_row["curation_comment"] = curation_comment.strip()
                df = pd.concat([df, pd.DataFrame([new_row], columns=df.columns)], ignore_index=True)
                _set_review_fields(df, int(df.index[-1]), active_curator)

            pending_approvals[left_term_key] = []
            st.session_state[STATE_PENDING_APPROVALS] = pending_approvals
            st.session_state[STATE_DF] = df
            st.session_state[STATE_DIRTY] = True
            _mark_review_sync_iri(left_iri)
            st.session_state[STATE_LEFT_TERM_INDEX] = min(selected_idx + 1, len(left_keys) - 1)
            st.session_state[STATE_SELECTED_ALIGNMENT] = ""
            st.rerun()

        skip_col = st.columns([1, 2, 1])[1]
        if skip_col.button("Skip term", key=f"skip_term_{left_term_key}", use_container_width=True):
            st.session_state[STATE_LEFT_TERM_INDEX] = min(selected_idx + 1, len(left_keys) - 1)
            st.session_state[STATE_SELECTED_ALIGNMENT] = ""
            st.rerun()

    st.subheader("Working table preview")
    table_status = st.radio(
        "Table status filter",
        options=["Needs review", "Curated", "All"],
        horizontal=True,
        index=0,
        help="Curated means status is not needs_review.",
    )
    table_df = filtered.copy()
    if table_status == "Needs review":
        table_df = table_df[table_df["status"] == "needs_review"]
    elif table_status == "Curated":
        table_df = table_df[table_df["status"] != "needs_review"]
    st.caption(f"Table rows: {len(table_df)}")
    render_clickable_dataframe(table_df, use_container_width=True, hide_index=True)

    st.download_button(
        label="Download working candidate TSV",
        data=dataframe_to_tsv_bytes(df),
        file_name=to_path(queue_file).name,
        mime="text/tab-separated-values",
        key="download_working_candidates",
    )

    if not review_df.empty or to_path(review_file).is_file():
        st.download_button(
            label="Download review ledger TSV",
            data=dataframe_to_tsv_bytes(review_df),
            file_name=to_path(review_file).name,
            mime="text/tab-separated-values",
            key="download_review_ledger",
        )

    if st.session_state.get(STATE_DIRTY):
        _autosave_if_dirty(queue_file, review_file)
