#!/usr/bin/env python3
"""Validate pairwise alignment TSV files.

A pairwise alignment row links two terms:
- left term (from a source ontology),
- right term (from another source ontology, local or external).
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import re
import sys
from pathlib import Path

ALIGNMENT_ID_RE_CURATED = re.compile(r"^ALIGN_\d{4}$")
ALIGNMENT_ID_RE_CANDIDATE = re.compile(r"^CAND_\d{4}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$"
)
STATUS_VALUES = {"needs_review", "approved", "rejected", "deprecated"}
KIND_VALUES = {"class", "property", "individual"}
RELATION_VALUES = {
    "exact",
    "close",
    "broad",
    "narrow",
    "related",
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
}
CANONICAL_FROM_VALUES = {"", "left", "right", "manual"}
ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
QUEUE_REQUIRED_COLUMNS = [
    "alignment_id",
    "left_source",
    "left_term_kind",
    "left_term_iri",
    "left_label",
    "right_source",
    "right_term_kind",
    "right_term_iri",
    "right_label",
    "match_method",
    "match_score",
    "relation",
    "suggestion_source",
    "canonical_from",
    "canonical_term_iri",
    "canonical_term_label",
    "canonical_term_source",
    "canonical_term_kind",
    "status",
    "curator",
    "curator_name",
    "reviewer",
    "reviewer_name",
    "date_added",
]
LEDGER_REQUIRED_COLUMNS = [
    "source_term_source",
    "source_term_kind",
    "source_term_iri",
    "source_term_label",
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
]


def is_work_queue(path: Path) -> bool:
    return "work" in path.parts


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Validate pairwise alignment TSV")
    parser.add_argument(
        "file",
        type=Path,
        help="Path to registry/pair_alignments.tsv or registry/pair_alignment_candidates.tsv",
    )
    parser.add_argument(
        "--kind",
        choices=["auto", "curated", "candidate"],
        default="auto",
        help="Validation mode. auto infers from filename (default: auto)",
    )
    return parser.parse_args()


def resolve_id_pattern(path: Path, kind: str) -> tuple[re.Pattern[str], str]:
    """Resolve alignment ID regex and effective kind from validation mode."""
    if kind == "curated":
        return ALIGNMENT_ID_RE_CURATED, "curated"
    if kind == "candidate":
        return ALIGNMENT_ID_RE_CANDIDATE, "candidate"
    if "candidate" in path.name.lower():
        return ALIGNMENT_ID_RE_CANDIDATE, "candidate"
    return ALIGNMENT_ID_RE_CURATED, "curated"


def is_valid_date(value: str) -> bool:
    """Return True if empty or valid ISO date/time.

    Accepted formats:
    - YYYY-MM-DD
    - YYYY-MM-DDTHH:MM:SSZ
    - YYYY-MM-DDTHH:MM:SS+HH:MM
    """
    if value == "":
        return True
    if DATE_RE.fullmatch(value):
        return True
    if DATETIME_RE.fullmatch(value):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False
    return False


def is_valid_score(value: str) -> bool:
    """Return True if score is numeric and within [0, 1]."""
    try:
        number = float(value)
    except ValueError:
        return False
    return 0.0 <= number <= 1.0


def normalize_orcid(value: str) -> str:
    text = (value or "").strip()
    if text.lower().startswith("https://orcid.org/"):
        text = text.rsplit("/", 1)[-1]
    digits = text.replace("-", "").upper()
    if len(digits) == 16:
        return f"{digits[0:4]}-{digits[4:8]}-{digits[8:12]}-{digits[12:16]}"
    return text


def is_valid_orcid(value: str) -> bool:
    normalized = normalize_orcid(value)
    if not ORCID_RE.fullmatch(normalized):
        return False
    digits = normalized.replace("-", "")
    total = 0
    for char in digits[:-1]:
        total = (total + int(char)) * 2
    remainder = total % 11
    result = (12 - remainder) % 11
    checksum = "X" if result == 10 else str(result)
    return checksum == digits[-1]


def validate_file(path: Path, kind: str = "auto") -> list[str]:
    """Validate pair alignment file and return all errors."""
    errors: list[str] = []
    alignment_id_re, effective_kind = resolve_id_pattern(path, kind)
    queue_mode = is_work_queue(path)
    required_columns = QUEUE_REQUIRED_COLUMNS if queue_mode else LEDGER_REQUIRED_COLUMNS

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    for col in required_columns:
        if col not in fieldnames:
            errors.append(f"Missing required column: {col}")

    if errors:
        return errors

    seen_ids: set[str] = set()
    seen_ledger_pairs: set[tuple[str, str]] = set()
    for line_no, row in enumerate(rows, start=2):
        alignment_id = (row.get("alignment_id", "") or "").strip()
        left_source = (
            (row.get("left_source", "") or "").strip()
            if queue_mode
            else (row.get("source_term_source", "") or row.get("left_source", "")).strip()
        )
        left_kind = (
            (row.get("left_term_kind", "") or "").strip().lower()
            if queue_mode
            else (row.get("source_term_kind", "") or row.get("left_term_kind", "")).strip().lower()
        )
        left_iri = (
            (row.get("left_term_iri", "") or "").strip()
            if queue_mode
            else (row.get("source_term_iri", "") or row.get("left_term_iri", "")).strip()
        )
        left_label = (
            (row.get("left_label", "") or "").strip()
            if queue_mode
            else (row.get("source_term_label", "") or row.get("left_label", "")).strip()
        )
        right_source = (row.get("right_source", "") or "").strip()
        right_kind = (row.get("right_term_kind", "") or "").strip().lower()
        right_iri = (row.get("right_term_iri", "") or "").strip()
        right_label = (row.get("right_label", "") or "").strip()
        match_method = (row.get("match_method", "") or "").strip()
        match_score = (row.get("match_score", "") or "").strip()
        relation = (row.get("relation", "") or "").strip()
        suggestion_source = (row.get("suggestion_source", "") or "").strip()
        canonical_from = (row.get("canonical_from", "") or "").strip().lower()
        canonical_term_iri = (row.get("canonical_term_iri", "") or "").strip()
        canonical_term_label = (row.get("canonical_term_label", "") or "").strip()
        canonical_term_source = (row.get("canonical_term_source", "") or "").strip()
        canonical_term_kind = (row.get("canonical_term_kind", "") or "").strip().lower()
        status = (row.get("status", "") or "").strip()
        curator = (row.get("curator", "") or "").strip()
        curator_name = (row.get("curator_name", "") or "").strip()
        reviewer = (row.get("reviewer", "") or "").strip()
        reviewer_name = (row.get("reviewer_name", "") or "").strip()
        date_added = (row.get("date_added", "") or "").strip()
        date_reviewed = (row.get("date_reviewed", "") or "").strip()
        curation_comment = (row.get("curation_comment", "") or "").strip()
        is_placeholder = queue_mode and status == "needs_review" and not right_iri

        if queue_mode:
            missing_required = (
                not alignment_id
                or not left_source
                or not left_iri
                or not left_label
                or not right_source
                or (not right_iri and not is_placeholder)
                or (not right_label and not is_placeholder)
                or not match_method
                or (not match_score and not is_placeholder)
                or not suggestion_source
                or not status
                or not curator
                or not date_added
            )
        else:
            missing_required = (
                not left_source
                or not left_kind
                or not left_iri
                or not left_label
                or not canonical_term_iri
                or not canonical_term_label
                or not canonical_term_source
                or not canonical_term_kind
                or not status
                or not curator
                or not reviewer
                or not reviewer_name
                or not date_reviewed
            )
        if missing_required:
            errors.append(f"Row {line_no}: required field is empty")

        if queue_mode:
            if not alignment_id_re.fullmatch(alignment_id):
                errors.append(f"Row {line_no}: invalid alignment_id format: {alignment_id}")

            if alignment_id in seen_ids:
                errors.append(f"Row {line_no}: duplicate alignment_id: {alignment_id}")
            seen_ids.add(alignment_id)
        else:
            ledger_pair = (left_iri, canonical_term_iri)
            if ledger_pair in seen_ledger_pairs:
                errors.append(
                    "Row "
                    f"{line_no}: duplicate source_term_iri/canonical_term_iri pair: "
                    f"{left_iri} -> {canonical_term_iri}"
                )
            seen_ledger_pairs.add(ledger_pair)

        if queue_mode and not is_valid_score(match_score):
            errors.append(f"Row {line_no}: match_score must be between 0 and 1")

        if relation and relation not in RELATION_VALUES:
            errors.append(f"Row {line_no}: invalid relation: {relation}")

        if status not in STATUS_VALUES:
            errors.append(f"Row {line_no}: invalid status: {status}")

        if left_kind and left_kind not in KIND_VALUES:
            label = "left_term_kind" if queue_mode else "source_term_kind"
            errors.append(f"Row {line_no}: invalid {label}: {left_kind}")

        if queue_mode and right_kind and right_kind not in KIND_VALUES:
            errors.append(f"Row {line_no}: invalid right_term_kind: {right_kind}")

        if canonical_term_kind and canonical_term_kind not in KIND_VALUES:
            errors.append(f"Row {line_no}: invalid canonical_term_kind: {canonical_term_kind}")

        if curator != "auto" and not is_valid_orcid(curator):
            errors.append(f"Row {line_no}: curator must be 'auto' or a valid ORCID: {curator}")

        if curator and curator != "auto" and not curator_name:
            errors.append(f"Row {line_no}: curator_name is required when curator is an ORCID")

        if reviewer and not is_valid_orcid(reviewer):
            errors.append(f"Row {line_no}: reviewer must be a valid ORCID when set: {reviewer}")

        if reviewer and not reviewer_name:
            errors.append(f"Row {line_no}: reviewer_name is required when reviewer is set")

        if status in {"approved", "rejected", "deprecated"} and not reviewer:
            errors.append(f"Row {line_no}: reviewed rows require reviewer ORCID")

        if status == "approved":
            if left_kind and canonical_term_kind and left_kind != canonical_term_kind:
                errors.append(
                    f"Row {line_no}: approved rows require identical source_term_kind and canonical_term_kind"
                )

        if canonical_from not in CANONICAL_FROM_VALUES:
            errors.append(f"Row {line_no}: invalid canonical_from: {canonical_from}")

        if queue_mode and not is_valid_date(date_added):
            errors.append(
                f"Row {line_no}: invalid date_added (expected YYYY-MM-DD or ISO datetime): {date_added}"
            )

        if not is_valid_date(date_reviewed):
            errors.append(
                f"Row {line_no}: invalid date_reviewed (expected YYYY-MM-DD or ISO datetime): {date_reviewed}"
            )

        has_any_canonical = bool(canonical_term_iri or canonical_term_label or canonical_term_source)
        has_all_canonical = bool(canonical_term_iri and canonical_term_label and canonical_term_source)
        if has_any_canonical and not has_all_canonical:
            errors.append(
                f"Row {line_no}: canonical_term_iri/label/source must be all set or all empty"
            )

        if queue_mode and canonical_from in {"left", "right", "manual"} and not has_all_canonical:
            errors.append(
                f"Row {line_no}: canonical_from={canonical_from} requires canonical_term_iri/label/source"
            )

        if status == "approved" and not has_all_canonical:
            errors.append(
                f"Row {line_no}: status=approved requires canonical_term_iri/label/source"
            )

        if queue_mode and right_iri and left_source.lower() == right_source.lower() and left_iri == right_iri:
            errors.append(
                f"Row {line_no}: left and right term are identical; pairwise alignment should link two distinct terms"
            )

    return errors


def main() -> int:
    """Run CLI validation and return process exit code."""
    args = parse_args()
    if not args.file.is_file():
        print(f"File not found: {args.file}", file=sys.stderr)
        return 1

    errors = validate_file(args.file, kind=args.kind)
    if errors:
        for err in errors:
            print(err)
        return 1

    print(f"Validation passed: {args.file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
