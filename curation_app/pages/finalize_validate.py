"""Review curated dataset and export updated source TTL."""

from __future__ import annotations

from pathlib import Path
import re

import pandas as pd
import streamlit as st

from curation_app.context import active_alignment_context, stamp_batch_metadata
from curation_app.helpers import (
    file_to_bytes,
    read_tsv,
    render_clickable_dataframe,
    to_relpath,
)

VIEW_OPTIONS = [
    "All rows",
    "Approved mappings",
    "Recently reviewed",
]

OWL_EQUIVALENT_CLASS = "http://www.w3.org/2002/07/owl#equivalentClass"
OWL_EQUIVALENT_PROPERTY = "http://www.w3.org/2002/07/owl#equivalentProperty"
OWL_SAME_AS = "http://www.w3.org/2002/07/owl#sameAs"
RDFS_SUBCLASS_OF = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
RDFS_SUBPROPERTY_OF = "http://www.w3.org/2000/01/rdf-schema#subPropertyOf"
RDFS_SEE_ALSO = "http://www.w3.org/2000/01/rdf-schema#seeAlso"
SKOS_MAPPING_RELATION = "http://www.w3.org/2004/02/skos/core#mappingRelation"
SKOS_EXACT_MATCH = "http://www.w3.org/2004/02/skos/core#exactMatch"
SKOS_CLOSE_MATCH = "http://www.w3.org/2004/02/skos/core#closeMatch"
SKOS_BROAD_MATCH = "http://www.w3.org/2004/02/skos/core#broadMatch"
SKOS_NARROW_MATCH = "http://www.w3.org/2004/02/skos/core#narrowMatch"
SKOS_RELATED_MATCH = "http://www.w3.org/2004/02/skos/core#relatedMatch"

KNOWN_NAMESPACE_PREFIXES: dict[str, str] = {
    "owl": "http://www.w3.org/2002/07/owl#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "dcterms": "http://purl.org/dc/terms/",
    "dcam": "http://purl.org/dc/dcam/",
    "bibo": "http://purl.org/ontology/bibo/",
    "vann": "http://purl.org/vocab/vann/",
    "schema": "http://schema.org/",
    "foaf": "http://xmlns.com/foaf/0.1/",
    "widoco": "https://w3id.org/widoco/vocab#",
    "prov": "http://www.w3.org/ns/prov-o-20130430#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "sosa": "http://www.w3.org/ns/sosa/",
}


def _safe_text(value: object) -> str:
    return str(value or "").strip()


def _ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "logs" not in out.columns:
        if "notes" in out.columns:
            out["logs"] = out["notes"].fillna("")
        else:
            out["logs"] = ""
    if "curation_comment" not in out.columns:
        out["curation_comment"] = ""
    for col in [
        "source_term_source",
        "source_term_iri",
        "source_term_label",
        "right_source",
        "right_term_iri",
        "right_label",
        "source_term_kind",
        "right_term_kind",
        "relation",
        "status",
        "canonical_from",
        "canonical_term_iri",
        "canonical_term_label",
        "canonical_term_source",
        "canonical_term_kind",
        "match_method",
        "suggestion_source",
        "logs",
        "curation_comment",
    ]:
        if col not in out.columns:
            out[col] = ""
    if "left_source" not in out.columns:
        out["left_source"] = out["source_term_source"]
    if "left_term_iri" not in out.columns:
        out["left_term_iri"] = out["source_term_iri"]
    if "left_label" not in out.columns:
        out["left_label"] = out["source_term_label"]
    if "left_term_kind" not in out.columns:
        out["left_term_kind"] = out["source_term_kind"]
    return out


def _normalize_relation_bucket(value: object) -> str:
    rel = _safe_text(value).lower()
    mapping = {
        "owl:equivalentclass": "owl_equivalent_class",
        "owl:equivalentproperty": "owl_equivalent_property",
        "owl:sameas": "owl_same_as",
        "rdfs:subclassof": "rdfs_subclass_of",
        "rdfs:subpropertyof": "rdfs_subproperty_of",
        "rdfs:seealso": "rdfs_see_also",
        "skos:mappingrelation": "skos_mapping",
        "skos:exactmatch": "exact",
        "exact": "exact",
        "skos:closematch": "close",
        "close": "close",
        "skos:broadmatch": "broad",
        "broad": "broad",
        "skos:narrowmatch": "narrow",
        "narrow": "narrow",
        "skos:relatedmatch": "related",
        "related": "related",
        "mapping": "skos_mapping",
    }
    return mapping.get(rel, "")


def _normalize_kind(value: object) -> str:
    kind = _safe_text(value).lower()
    if kind in {"class", "property", "individual"}:
        return kind
    return ""


def _build_mapping_triples(df: pd.DataFrame) -> tuple[str, int, list[str]]:
    approved = df[df["status"] == "approved"].copy()
    if approved.empty:
        return "", 0, []

    triples: set[tuple[str, str, str]] = set()
    warnings: list[str] = []
    for _, row in approved.iterrows():
        left_iri = _safe_text(row.get("left_term_iri"))
        right_iri = _canonical_target_iri(row)
        if not left_iri or not right_iri or left_iri == right_iri:
            continue

        relation_bucket = _normalize_relation_bucket(row.get("relation"))
        if not relation_bucket:
            continue
        left_kind = _normalize_kind(row.get("left_term_kind"))
        canonical_kind = _normalize_kind(row.get("canonical_term_kind"))
        kind = canonical_kind or left_kind

        if relation_bucket == "owl_equivalent_class":
            triples.add((left_iri, OWL_EQUIVALENT_CLASS, right_iri))
            continue
        if relation_bucket == "owl_equivalent_property":
            triples.add((left_iri, OWL_EQUIVALENT_PROPERTY, right_iri))
            continue
        if relation_bucket == "owl_same_as":
            triples.add((left_iri, OWL_SAME_AS, right_iri))
            continue
        if relation_bucket == "rdfs_subclass_of":
            triples.add((left_iri, RDFS_SUBCLASS_OF, right_iri))
            continue
        if relation_bucket == "rdfs_subproperty_of":
            triples.add((left_iri, RDFS_SUBPROPERTY_OF, right_iri))
            continue
        if relation_bucket == "rdfs_see_also":
            triples.add((left_iri, RDFS_SEE_ALSO, right_iri))
            continue

        if relation_bucket == "exact":
            if kind == "class":
                triples.add((left_iri, OWL_EQUIVALENT_CLASS, right_iri))
            elif kind == "property":
                triples.add((left_iri, OWL_EQUIVALENT_PROPERTY, right_iri))
            else:
                triples.add((left_iri, OWL_SAME_AS, right_iri))
            triples.add((left_iri, SKOS_EXACT_MATCH, right_iri))
            continue

        if relation_bucket == "broad":
            if kind == "class":
                triples.add((left_iri, RDFS_SUBCLASS_OF, right_iri))
            elif kind == "property":
                triples.add((left_iri, RDFS_SUBPROPERTY_OF, right_iri))
            else:
                triples.add((left_iri, RDFS_SEE_ALSO, right_iri))
            triples.add((left_iri, SKOS_BROAD_MATCH, right_iri))
            continue

        if relation_bucket == "narrow":
            if kind == "class":
                triples.add((right_iri, RDFS_SUBCLASS_OF, left_iri))
            elif kind == "property":
                triples.add((right_iri, RDFS_SUBPROPERTY_OF, left_iri))
            else:
                triples.add((left_iri, RDFS_SEE_ALSO, right_iri))
            triples.add((left_iri, SKOS_NARROW_MATCH, right_iri))
            continue

        if relation_bucket == "close":
            triples.add((left_iri, RDFS_SEE_ALSO, right_iri))
            triples.add((left_iri, SKOS_CLOSE_MATCH, right_iri))
            continue

        if relation_bucket == "related":
            triples.add((left_iri, RDFS_SEE_ALSO, right_iri))
            triples.add((left_iri, SKOS_RELATED_MATCH, right_iri))
            continue

        # Generic mapping relation fallback.
        triples.add((left_iri, SKOS_MAPPING_RELATION, right_iri))

    if not triples:
        return "", 0, warnings

    body = [
        "",
        "# --- Generated alignment mappings (OWL/SKOS) ---",
    ]
    for s, p, o in sorted(triples):
        body.append(f"<{s}> <{p}> <{o}> .")
    body.append("")
    return "\n".join(body), len(triples), warnings


def _canonical_target_iri(row: pd.Series) -> str:
    return _safe_text(row.get("canonical_term_iri"))


def _build_replacements(df: pd.DataFrame) -> tuple[dict[str, str], list[str]]:
    approved = df[df["status"] == "approved"].copy()
    by_left: dict[str, set[str]] = {}
    for _, row in approved.iterrows():
        left_iri = _safe_text(row.get("left_term_iri"))
        target_iri = _canonical_target_iri(row)
        if not left_iri or not target_iri or left_iri == target_iri:
            continue
        by_left.setdefault(left_iri, set()).add(target_iri)

    warnings: list[str] = []
    replacements: dict[str, str] = {}
    for left_iri, targets in by_left.items():
        sorted_targets = sorted(targets)
        replacements[left_iri] = sorted_targets[0]
        if len(sorted_targets) > 1:
            warnings.append(
                f"Conflicting approved targets for `{left_iri}`: {', '.join(sorted_targets)}. "
                f"Using `{sorted_targets[0]}`."
            )
    return replacements, warnings


def _apply_iri_replacements(ttl_text: str, replacements: dict[str, str]) -> str:
    updated = ttl_text
    # Longest first avoids partial overlaps when IRIs share prefixes.
    for left_iri in sorted(replacements.keys(), key=len, reverse=True):
        right_iri = replacements[left_iri]
        updated = updated.replace(f"<{left_iri}>", f"<{right_iri}>")
    return updated


def _parse_ttl_prefixes(ttl_text: str) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    in_header = True
    for line in ttl_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if in_header and stripped.startswith("#"):
            continue
        match = re.match(r"@prefix\s+([A-Za-z][\w\-]*)?:\s*<([^>]+)>\s*\.", stripped)
        if match:
            prefix = (match.group(1) or "").strip()
            ns = match.group(2).strip()
            prefixes[prefix] = ns
            continue
        match = re.match(r"PREFIX\s+([A-Za-z][\w\-]*)?:\s*<([^>]+)>", stripped, flags=re.IGNORECASE)
        if match:
            prefix = (match.group(1) or "").strip()
            ns = match.group(2).strip()
            prefixes[prefix] = ns
            continue
        # Header may include @base; keep reading until first non-directive statement.
        if re.match(r"@base\s+<[^>]+>\s*\.", stripped, flags=re.IGNORECASE):
            continue
        if in_header:
            in_header = False
            break
    return prefixes


def _build_qname_candidates(iri: str, prefixes: dict[str, str]) -> list[str]:
    out: list[str] = []
    for prefix, ns in prefixes.items():
        if iri.startswith(ns):
            local = iri[len(ns):]
            if local:
                out.append(f"{prefix}:{local}" if prefix else f":{local}")
    return sorted(set(out), key=len, reverse=True)


def _replace_qname_token(text: str, qname: str, replacement_iri: str) -> str:
    # Replace QName only when it appears as a standalone RDF term token.
    escaped = re.escape(qname)
    pattern = re.compile(rf"(?<![\w:/#.-]){escaped}(?![\w:/#.-])")
    return pattern.sub(f"<{replacement_iri}>", text)


def _apply_iri_and_qname_replacements(ttl_text: str, replacements: dict[str, str]) -> str:
    updated = _apply_iri_replacements(ttl_text, replacements)
    prefixes = _parse_ttl_prefixes(ttl_text)
    for left_iri in sorted(replacements.keys(), key=len, reverse=True):
        right_iri = replacements[left_iri]
        for qname in _build_qname_candidates(left_iri, prefixes):
            updated = _replace_qname_token(updated, qname, right_iri)
    return updated


def _split_namespace_local(iri: str) -> tuple[str, str]:
    if "#" in iri:
        ns, local = iri.rsplit("#", 1)
        return ns + "#", local
    if "/" in iri:
        ns, local = iri.rsplit("/", 1)
        return ns + "/", local
    return "", ""


def _sanitize_prefix_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "", (value or "").strip().lower())
    if not name:
        return "ext"
    if not re.match(r"^[A-Za-z_]", name):
        return f"ext_{name}"
    return name


def _safe_qname_local(local: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$", local or ""))


def _split_ttl_header_body(ttl_text: str) -> tuple[list[str], list[str]]:
    lines = ttl_text.splitlines()
    body_start = len(lines)
    in_header = True
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if in_header and stripped.startswith("#"):
            continue
        if re.match(r"@prefix\s+([A-Za-z][\w\-]*)?:\s*<[^>]+>\s*\.\s*$", stripped, flags=re.IGNORECASE):
            continue
        if re.match(r"PREFIX\s+([A-Za-z][\w\-]*)?:\s*<[^>]+>\s*$", stripped, flags=re.IGNORECASE):
            continue
        if re.match(r"@base\s+<[^>]+>\s*\.\s*$", stripped, flags=re.IGNORECASE):
            continue
        body_start = i
        break
    return lines[:body_start], lines[body_start:]


def _known_prefix_binding(iri: str, prefixes: dict[str, str]) -> tuple[str, str, str] | None:
    for prefix, ns in prefixes.items():
        if iri.startswith(ns):
            local = iri[len(ns):]
            if local and _safe_qname_local(local):
                return prefix, ns, local
    return None


def _stem_prefix_binding(iri: str) -> tuple[str, str, str] | None:
    ns, local = _split_namespace_local(iri)
    if not ns or not local:
        return None
    stem_match = re.match(r"^([A-Za-z][A-Za-z0-9]+)_(.+)$", local)
    if not stem_match:
        return None
    ontology_stem = stem_match.group(1)
    suffix = stem_match.group(2)
    if not _safe_qname_local(suffix):
        return None
    prefix = _sanitize_prefix_name(ontology_stem)
    return prefix, f"{ns}{ontology_stem}_", suffix


def _preferred_prefix_binding(iri: str, source_hint: str = "") -> tuple[str, str, str] | None:
    for prefix, ns in KNOWN_NAMESPACE_PREFIXES.items():
        if iri.startswith(ns):
            local = iri[len(ns):]
            if local and _safe_qname_local(local):
                return prefix, ns, local

    stem_binding = _stem_prefix_binding(iri)
    if stem_binding is not None:
        return stem_binding

    ns, local = _split_namespace_local(iri)
    if not ns or not local:
        return None
    if not _safe_qname_local(local):
        return None
    prefix = _sanitize_prefix_name(source_hint or "ext")
    return prefix, ns, local


def _insert_prefixes_in_header(ttl_text: str, new_prefixes: dict[str, str]) -> str:
    if not new_prefixes:
        return ttl_text
    lines = ttl_text.splitlines()
    insert_at = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"@prefix\s+.*\.\s*$", stripped, flags=re.IGNORECASE):
            insert_at = i + 1
            continue
        if re.match(r"@base\s+<[^>]+>\s*\.\s*$", stripped, flags=re.IGNORECASE):
            insert_at = i + 1
            continue
        if stripped == "":
            continue
        break
    prefix_lines = [f"@prefix {p}: <{ns}> ." for p, ns in sorted(new_prefixes.items())]
    out = lines[:insert_at] + prefix_lines + lines[insert_at:]
    return "\n".join(out) + ("\n" if ttl_text.endswith("\n") else "")


def _prune_unused_prefixes(ttl_text: str) -> str:
    header_lines, body_lines = _split_ttl_header_body(ttl_text)
    body_text = "\n".join(body_lines)
    used_prefixes: set[str] = set()
    for match in re.finditer(r"(?<![\w:/#.-])([A-Za-z][\w\-]*)\:([A-Za-z0-9_][A-Za-z0-9_.-]*)", body_text):
        used_prefixes.add(match.group(1))
    if re.search(r"(?<![\w:/#.-])\:([A-Za-z0-9_][A-Za-z0-9_.-]*)", body_text):
        used_prefixes.add("")

    kept_header: list[str] = []
    for line in header_lines:
        stripped = line.strip()
        match = re.match(r"@prefix\s+([A-Za-z][\w\-]*)?:\s*<[^>]+>\s*\.\s*$", stripped, flags=re.IGNORECASE)
        if not match:
            kept_header.append(line)
            continue
        prefix = (match.group(1) or "").strip()
        if prefix in used_prefixes:
            kept_header.append(line)

    out_lines = kept_header + body_lines
    out = "\n".join(out_lines)
    return out + ("\n" if ttl_text.endswith("\n") else "")


def _compact_ttl_iris_with_prefixes(
    ttl_text: str,
    export_df: pd.DataFrame,
    replacements: dict[str, str],
) -> tuple[str, dict[str, str]]:
    existing_prefixes = _parse_ttl_prefixes(ttl_text)

    new_prefixes: dict[str, str] = {}
    iri_to_qname: dict[str, str] = {}
    _, body_lines = _split_ttl_header_body(ttl_text)
    body_text = "\n".join(body_lines)
    body_iris = set(re.findall(r"<([^>]+)>", body_text))

    reusable_prefixes = dict(existing_prefixes)
    for prefix, ns in KNOWN_NAMESPACE_PREFIXES.items():
        existing_ns = reusable_prefixes.get(prefix)
        if existing_ns is None:
            reusable_prefixes[prefix] = ns

    for iri in sorted(body_iris, key=len, reverse=True):
        binding = _known_prefix_binding(iri, reusable_prefixes)
        if binding is None:
            continue
        prefix, ns, local = binding
        if prefix not in existing_prefixes and prefix not in new_prefixes:
            new_prefixes[prefix] = ns
        iri_to_qname[iri] = f"{prefix}:{local}"

    target_iris = sorted(set(replacements.values()) | set(export_df["canonical_term_iri"].dropna().astype(str)), key=len, reverse=True)
    for iri in target_iris:
        iri = _safe_text(iri)
        if not iri:
            continue
        binding = _preferred_prefix_binding(iri)
        if binding is None:
            continue
        preferred, ns, local = binding
        prefix = preferred
        counter = 2
        while True:
            existing_ns = existing_prefixes.get(prefix) or new_prefixes.get(prefix)
            if existing_ns is None:
                break
            if existing_ns == ns:
                break
            prefix = f"{preferred}{counter}"
            counter += 1
        if prefix not in existing_prefixes and prefix not in new_prefixes:
            new_prefixes[prefix] = ns
        iri_to_qname[iri] = f"{prefix}:{local}"

    updated = ttl_text
    if new_prefixes:
        updated = _insert_prefixes_in_header(updated, new_prefixes)

    header_lines, body_lines = _split_ttl_header_body(updated)
    body_text = "\n".join(body_lines)
    for iri in sorted(iri_to_qname.keys(), key=len, reverse=True):
        body_text = body_text.replace(f"<{iri}>", iri_to_qname[iri])

    out = "\n".join(header_lines + body_lines[:0] + [body_text])
    out += "\n" if ttl_text.endswith("\n") else ""
    out = _prune_unused_prefixes(out)

    kept_prefixes = _parse_ttl_prefixes(out)
    added_prefixes = {p: ns for p, ns in new_prefixes.items() if kept_prefixes.get(p) == ns}
    return out, added_prefixes


def _apply_view(df: pd.DataFrame, view: str) -> pd.DataFrame:
    if df.empty:
        return df
    if view == "Approved mappings":
        return df[df["status"] == "approved"]
    if view == "Recently reviewed":
        out = df[df["date_reviewed"].astype(str).str.strip() != ""].copy()
        return out.sort_values(by="date_reviewed", ascending=False)
    return df


def render() -> None:
    st.title("Review and Export")

    ctx = active_alignment_context()
    if ctx is None:
        st.warning("No alignment batch found. Configure registry/alignment_batches.tsv first.")
        return

    candidates_path = ctx.review_tsv
    df = stamp_batch_metadata(_ensure_columns(read_tsv(candidates_path)), ctx)
    if df.empty and not candidates_path.is_file():
        st.warning("No reviewed ledger file found yet. Please validate items in Curate candidates first.")
        return

    st.caption(f"Batch: `{ctx.batch_label}`")
    st.caption(f"Dataset: `{to_relpath(candidates_path)}`")

    view = st.selectbox("View", options=VIEW_OPTIONS, index=1)
    token = st.text_input("Filter text (labels, logs, curation comments, IRIs)", value="")
    shown = _apply_view(df, view)
    if token.strip():
        t = token.strip().lower()
        hay = (
            shown["left_label"].str.lower()
            + " "
            + shown["canonical_term_label"].str.lower()
            + " "
            + shown["left_term_iri"].str.lower()
            + " "
            + shown["canonical_term_iri"].str.lower()
            + " "
            + shown["logs"].str.lower()
            + " "
            + shown["curation_comment"].str.lower()
        )
        shown = shown[hay.str.contains(t, na=False)]

    st.caption(f"Rows shown: {len(shown)}")
    render_clickable_dataframe(shown, use_container_width=True, hide_index=True)

    st.subheader("Export Updated Source TTL")
    status_options = sorted(df["status"].dropna().unique().tolist())
    default_statuses = ["approved"] if "approved" in status_options else status_options
    export_statuses = st.multiselect(
        "Statuses to export",
        options=status_options,
        default=default_statuses,
        help="Usually keep only approved rows for replacements.",
    )
    export_df = shown[shown["status"].isin(export_statuses)] if export_statuses else shown.iloc[0:0]
    source_ttl_path = ctx.download_ttl
    if not source_ttl_path.is_file():
        st.warning(f"Source TTL not found: `{to_relpath(source_ttl_path)}`")
        return

    st.markdown("**Mapping Triple Export**")
    map_col1, map_col2 = st.columns(2)
    with map_col1:
        emit_mapping_triples = st.checkbox(
            "Generate OWL/SKOS mapping triples",
            value=True,
            help=(
                "Build mapping triples from approved curated rows. "
                "Exact -> OWL equivalents, broad/narrow -> RDFS hierarchy, close/related -> seeAlso, "
                "plus corresponding SKOS mapping relation."
            ),
        )
    with map_col2:
        embed_mapping_triples = st.checkbox(
            "Embed mapping triples in updated TTL",
            value=True,
            help="Keep mappings in the same exported TTL so View schema Mermaid can display them directly.",
            disabled=not emit_mapping_triples,
        )
    write_mapping_file = st.checkbox(
        "Write mapping triples to separate TTL",
        value=True,
        help="Also write mapping triples to a dedicated mapping file.",
        disabled=not emit_mapping_triples,
    )

    source_ttl_text = source_ttl_path.read_text(encoding="utf-8", errors="replace")
    replacements, replacement_warnings = _build_replacements(export_df)
    ttl_text = _apply_iri_and_qname_replacements(source_ttl_text, replacements)
    mapping_ttl_text = ""
    mapping_export_text = ""
    mapping_triple_count = 0
    if emit_mapping_triples:
        mapping_ttl_text, mapping_triple_count, mapping_notes = _build_mapping_triples(export_df)
        for msg in mapping_notes:
            st.warning(msg)
        if embed_mapping_triples and mapping_ttl_text:
            ttl_text = ttl_text.rstrip() + "\n" + mapping_ttl_text
        if mapping_ttl_text:
            mapping_export_text, _ = _compact_ttl_iris_with_prefixes(mapping_ttl_text, export_df, replacements)
    ttl_text, added_prefixes = _compact_ttl_iris_with_prefixes(ttl_text, export_df, replacements)

    for msg in replacement_warnings:
        st.warning(msg)
    st.caption(
        f"Applied {len(replacements)} IRI replacement(s) on `{to_relpath(source_ttl_path)}` "
        f"from {len(export_df)} filtered row(s)."
    )
    if emit_mapping_triples:
        st.caption(f"Generated {mapping_triple_count} mapping triple(s) from approved mappings.")
    if added_prefixes:
        st.caption(
            "Added external prefixes: "
            + ", ".join(f"`{p}: <{ns}>`" for p, ns in sorted(added_prefixes.items()))
        )
    st.code(ttl_text[:12000], language="turtle")

    export_dir = Path("registry/exports")
    export_dir.mkdir(parents=True, exist_ok=True)
    ttl_path = export_dir / f"{ctx.batch_id}_updated.ttl"
    mapping_path = export_dir / f"{ctx.batch_id}_mappings.ttl"

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Write TTL file", type="primary"):
            ttl_path.write_text(ttl_text, encoding="utf-8")
            if emit_mapping_triples and write_mapping_file:
                mapping_path.write_text(mapping_export_text.strip() + "\n", encoding="utf-8")
            st.success(f"Wrote `{to_relpath(ttl_path)}`")
            if emit_mapping_triples and write_mapping_file:
                st.success(f"Wrote `{to_relpath(mapping_path)}`")
    with col2:
        st.download_button(
            label="Download TTL",
            data=ttl_text.encode("utf-8"),
            file_name=ttl_path.name,
            mime="text/turtle",
            key=f"download_ttl_{ctx.batch_id}",
        )

    if ttl_path.is_file():
        st.caption(f"Latest TTL export: `{to_relpath(ttl_path)}`")
        st.download_button(
            label="Download latest written TTL",
            data=file_to_bytes(ttl_path),
            file_name=ttl_path.name,
            mime="text/turtle",
            key=f"download_ttl_written_{ctx.batch_id}",
        )
    if emit_mapping_triples and write_mapping_file and mapping_path.is_file():
        st.caption(f"Latest mapping TTL: `{to_relpath(mapping_path)}`")
        st.download_button(
            label="Download latest mapping TTL",
            data=file_to_bytes(mapping_path),
            file_name=mapping_path.name,
            mime="text/turtle",
            key=f"download_mapping_written_{ctx.batch_id}",
        )
