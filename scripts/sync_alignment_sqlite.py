#!/usr/bin/env python3
"""Synchronize TSV reconciliation artifacts into a SQLite store.

This script keeps your current TSV workflow while adding a SQLite backend:
1) imports candidate and curated pair alignment TSV files into SQLite tables,
2) derives canonical-centric reconciled mappings from curated rows,
3) stores derived mappings in SQLite,
4) exports reconciled mapping/group TSV files from SQLite.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path


PAIR_ALIGNMENT_COLUMNS = [
    "batch_id",
    "pivot_source",
    "target_id",
    "target_backend",
    "alignment_id",
    "left_source",
    "left_term_iri",
    "left_label",
    "right_source",
    "right_term_iri",
    "right_label",
    "normalized_left_label",
    "normalized_right_label",
    "match_method",
    "match_score",
    "relation",
    "suggestion_source",
    "status",
    "curator",
    "reviewer",
    "date_added",
    "date_reviewed",
    "notes",
    "canonical_from",
    "canonical_term_iri",
    "canonical_term_label",
    "canonical_term_source",
]

RECONCILED_MAPPING_COLUMNS = [
    "mapping_id",
    "alignment_id",
    "batch_id",
    "pivot_source",
    "target_id",
    "target_backend",
    "mapping_origin",
    "source_term_source",
    "source_term_iri",
    "source_term_label",
    "canonical_term_iri",
    "canonical_term_label",
    "canonical_term_source",
    "relation",
    "mapping_status",
    "suggestion_source",
    "curator",
    "reviewer",
    "date_added",
    "date_reviewed",
    "notes",
]

RECONCILED_GROUP_COLUMNS = [
    "canonical_term_iri",
    "canonical_term_label",
    "canonical_term_source",
    "mapped_term_count",
    "mapped_terms",
    "alignment_ids",
]


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Sync pair alignment TSV files into SQLite and export reconciled TSV outputs"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("registry/alignment_curation.sqlite"),
        help="SQLite database path",
    )
    parser.add_argument(
        "--pair-candidates",
        type=Path,
        default=Path("registry/pair_alignment_candidates.tsv"),
        help="Candidate pair alignment TSV",
    )
    parser.add_argument(
        "--pair-alignments",
        type=Path,
        default=None,
        help=(
            "Optional curated/reviewed TSV. "
            "Defaults to --pair-candidates when omitted or absent."
        ),
    )
    parser.add_argument(
        "--status",
        default="approved",
        help="Curated status to include in reconciled export (default: approved)",
    )
    parser.add_argument(
        "--reconciled-output",
        type=Path,
        default=Path("registry/reconciled_mappings.tsv"),
        help="Canonical-centric mapping TSV output",
    )
    parser.add_argument(
        "--grouped-output",
        type=Path,
        default=Path("registry/reconciled_canonical_groups.tsv"),
        help="Canonical group TSV output",
    )
    return parser.parse_args()


def clean(value: str) -> str:
    """Return stripped text with None-safe fallback."""
    return (value or "").strip()


def normalize_source(value: str) -> str:
    """Return normalized lowercase source identifier."""
    return clean(value).lower()


def stable_alignment_id(row: dict[str, str]) -> str:
    """Return queue alignment_id or derive a stable internal ID for shared-ledger rows."""
    explicit = clean(row.get("alignment_id", ""))
    if explicit:
        return explicit
    batch_id = clean(row.get("batch_id", ""))
    source_term_iri = clean(row.get("source_term_iri", "")) or clean(row.get("left_term_iri", ""))
    canonical_term_iri = clean(row.get("canonical_term_iri", ""))
    if batch_id and source_term_iri and canonical_term_iri:
        return f"LEDGER::{batch_id}::{source_term_iri}::{canonical_term_iri}"
    if batch_id and source_term_iri:
        return f"LEDGER::{batch_id}::{source_term_iri}"
    if source_term_iri and canonical_term_iri:
        return f"LEDGER::{source_term_iri}::{canonical_term_iri}"
    if source_term_iri:
        return f"LEDGER::{source_term_iri}"
    return ""


def read_tsv(path: Path) -> list[dict[str, str]]:
    """Read TSV rows as dictionaries."""
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_tsv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    """Write dictionary rows to TSV with explicit header order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=headers,
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def create_schema(conn: sqlite3.Connection) -> None:
    """Create all required SQLite tables and indices."""
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        DROP TABLE IF EXISTS pair_alignment_candidates;
        DROP TABLE IF EXISTS pair_alignments;
        DROP TABLE IF EXISTS reconciled_mappings;
        DROP TABLE IF EXISTS reconciled_canonical_groups;

        CREATE TABLE IF NOT EXISTS pair_alignment_candidates (
            batch_id TEXT,
            pivot_source TEXT,
            target_id TEXT,
            target_backend TEXT,
            alignment_id TEXT PRIMARY KEY,
            left_source TEXT,
            left_term_iri TEXT,
            left_label TEXT,
            right_source TEXT,
            right_term_iri TEXT,
            right_label TEXT,
            normalized_left_label TEXT,
            normalized_right_label TEXT,
            match_method TEXT,
            match_score REAL,
            relation TEXT,
            suggestion_source TEXT,
            status TEXT,
            curator TEXT,
            reviewer TEXT,
            date_added TEXT,
            date_reviewed TEXT,
            notes TEXT,
            canonical_from TEXT,
            canonical_term_iri TEXT,
            canonical_term_label TEXT,
            canonical_term_source TEXT
        );

        CREATE TABLE IF NOT EXISTS pair_alignments (
            batch_id TEXT,
            pivot_source TEXT,
            target_id TEXT,
            target_backend TEXT,
            alignment_id TEXT PRIMARY KEY,
            left_source TEXT,
            left_term_iri TEXT,
            left_label TEXT,
            right_source TEXT,
            right_term_iri TEXT,
            right_label TEXT,
            normalized_left_label TEXT,
            normalized_right_label TEXT,
            match_method TEXT,
            match_score REAL,
            relation TEXT,
            suggestion_source TEXT,
            status TEXT,
            curator TEXT,
            reviewer TEXT,
            date_added TEXT,
            date_reviewed TEXT,
            notes TEXT,
            canonical_from TEXT,
            canonical_term_iri TEXT,
            canonical_term_label TEXT,
            canonical_term_source TEXT
        );

        CREATE TABLE IF NOT EXISTS reconciled_mappings (
            mapping_id TEXT PRIMARY KEY,
            alignment_id TEXT,
            batch_id TEXT,
            pivot_source TEXT,
            target_id TEXT,
            target_backend TEXT,
            mapping_origin TEXT,
            source_term_source TEXT,
            source_term_iri TEXT,
            source_term_label TEXT,
            canonical_term_iri TEXT,
            canonical_term_label TEXT,
            canonical_term_source TEXT,
            relation TEXT,
            mapping_status TEXT,
            suggestion_source TEXT,
            curator TEXT,
            reviewer TEXT,
            date_added TEXT,
            date_reviewed TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS reconciled_canonical_groups (
            canonical_term_iri TEXT PRIMARY KEY,
            canonical_term_label TEXT,
            canonical_term_source TEXT,
            mapped_term_count INTEGER,
            mapped_terms TEXT,
            alignment_ids TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_pair_alignments_status
            ON pair_alignments(status);
        CREATE INDEX IF NOT EXISTS idx_pair_alignments_canonical
            ON pair_alignments(canonical_term_iri);
        CREATE INDEX IF NOT EXISTS idx_pair_alignments_left
            ON pair_alignments(left_term_iri);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_reconciled_source_canonical
            ON reconciled_mappings(source_term_iri, canonical_term_iri);
        """
    )


def to_float_or_none(value: str) -> float | None:
    """Parse float value; return None for empty/invalid."""
    text = clean(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def insert_pair_rows(
    conn: sqlite3.Connection,
    table_name: str,
    rows: list[dict[str, str]],
) -> int:
    """Replace rows in pair table and return inserted row count."""
    conn.execute(f"DELETE FROM {table_name}")

    placeholders = ",".join("?" for _ in PAIR_ALIGNMENT_COLUMNS)
    sql = f"""
        INSERT INTO {table_name} (
            {",".join(PAIR_ALIGNMENT_COLUMNS)}
        ) VALUES ({placeholders})
    """

    inserted = 0
    for row in rows:
        values: list[object] = []
        for col in PAIR_ALIGNMENT_COLUMNS:
            if col == "match_score":
                values.append(to_float_or_none(row.get(col, "")))
            elif col == "alignment_id":
                values.append(stable_alignment_id(row))
            elif col == "batch_id":
                values.append(clean(row.get("batch_id", "")))
            elif col == "pivot_source":
                values.append(clean(row.get("pivot_source", "")))
            elif col == "target_id":
                values.append(clean(row.get("target_id", "")))
            elif col == "target_backend":
                values.append(clean(row.get("target_backend", "")))
            elif col == "left_source":
                values.append(normalize_source(row.get("left_source", "")) or normalize_source(row.get("source_term_source", "")))
            elif col == "right_source":
                values.append(normalize_source(row.get("right_source", "")))
            elif col == "canonical_term_source":
                values.append(normalize_source(row.get("canonical_term_source", "")))
            elif col == "left_term_iri":
                values.append(clean(row.get("left_term_iri", "")) or clean(row.get("source_term_iri", "")))
            elif col == "left_label":
                values.append(clean(row.get("left_label", "")) or clean(row.get("source_term_label", "")))
            else:
                values.append(clean(row.get(col, "")))
        conn.execute(sql, values)
        inserted += 1
    return inserted


def append_source_mapping(
    out_rows: list[dict[str, str]],
    seen_keys: set[tuple[str, str]],
    *,
    alignment_id: str,
    batch_id: str,
    pivot_source: str,
    target_id: str,
    target_backend: str,
    mapping_origin: str,
    source_term_source: str,
    source_term_iri: str,
    source_term_label: str,
    canonical_term_iri: str,
    canonical_term_label: str,
    canonical_term_source: str,
    relation: str,
    suggestion_source: str,
    curator: str,
    reviewer: str,
    date_added: str,
    date_reviewed: str,
    notes: str,
) -> None:
    """Append one canonical-centric source mapping if non-self and non-duplicate."""
    if not source_term_iri:
        return
    if source_term_iri == canonical_term_iri:
        return

    dedupe_key = (source_term_iri, canonical_term_iri)
    if dedupe_key in seen_keys:
        return
    seen_keys.add(dedupe_key)

    out_rows.append(
        {
            "mapping_id": f"REC_{len(out_rows) + 1:04d}",
            "alignment_id": alignment_id,
            "batch_id": batch_id,
            "pivot_source": pivot_source,
            "target_id": target_id,
            "target_backend": target_backend,
            "mapping_origin": mapping_origin,
            "source_term_source": source_term_source,
            "source_term_iri": source_term_iri,
            "source_term_label": source_term_label,
            "canonical_term_iri": canonical_term_iri,
            "canonical_term_label": canonical_term_label,
            "canonical_term_source": canonical_term_source,
            "relation": relation,
            "mapping_status": "approved",
            "suggestion_source": suggestion_source,
            "curator": curator,
            "reviewer": reviewer,
            "date_added": date_added,
            "date_reviewed": date_reviewed,
            "notes": notes,
        }
    )


def is_equivalence_relation(value: str) -> bool:
    """Return True for relation values that safely support derived equivalence."""
    normalized = clean(value).lower()
    return normalized in {
        "exact",
        "owl:equivalentclass",
        "owl:equivalentproperty",
        "owl:sameas",
        "skos:exactmatch",
    }


def derived_relation_for_kind(kind: str) -> str:
    """Choose the most specific derived equivalence relation for a term kind."""
    normalized = clean(kind).lower()
    if normalized == "class":
        return "owl:equivalentClass"
    if normalized == "property":
        return "owl:equivalentProperty"
    if normalized == "individual":
        return "owl:sameAs"
    return "skos:exactMatch"


def build_reconciled_rows(
    curated_rows: list[dict[str, str]],
    status_filter: str,
) -> list[dict[str, str]]:
    """Build direct and derived source->canonical rows from curated batch rows."""
    out_rows: list[dict[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    approved_rows: list[dict[str, str]] = []

    for row in curated_rows:
        if clean(row.get("status", "")).lower() != status_filter.lower():
            continue
        approved_rows.append(row)

        alignment_id = stable_alignment_id(row)
        batch_id = clean(row.get("batch_id", ""))
        pivot_source = clean(row.get("pivot_source", ""))
        target_id = clean(row.get("target_id", ""))
        target_backend = clean(row.get("target_backend", ""))
        relation = clean(row.get("relation", ""))
        suggestion_source = clean(row.get("suggestion_source", "")) or "approved_ledger"
        curator = clean(row.get("curator", ""))
        reviewer = clean(row.get("reviewer", ""))
        date_reviewed = clean(row.get("date_reviewed", ""))
        date_added = clean(row.get("date_added", "")) or date_reviewed
        notes = clean(row.get("notes", "")) or clean(row.get("curation_comment", ""))

        left_source = normalize_source(row.get("source_term_source", "")) or normalize_source(row.get("left_source", ""))
        left_iri = clean(row.get("source_term_iri", "")) or clean(row.get("left_term_iri", ""))
        left_label = clean(row.get("source_term_label", "")) or clean(row.get("left_label", ""))
        canonical_iri = clean(row.get("canonical_term_iri", ""))
        canonical_label = clean(row.get("canonical_term_label", ""))
        canonical_source = normalize_source(row.get("canonical_term_source", ""))
        if not (canonical_iri and canonical_label and canonical_source):
            continue

        append_source_mapping(
            out_rows,
            seen_keys,
            alignment_id=alignment_id,
            batch_id=batch_id,
            pivot_source=pivot_source,
            target_id=target_id,
            target_backend=target_backend,
            mapping_origin="direct",
            source_term_source=left_source,
            source_term_iri=left_iri,
            source_term_label=left_label,
            canonical_term_iri=canonical_iri,
            canonical_term_label=canonical_label,
            canonical_term_source=canonical_source,
            relation=relation,
            suggestion_source=suggestion_source,
            curator=curator,
            reviewer=reviewer,
            date_added=date_added,
            date_reviewed=date_reviewed,
            notes=notes,
        )

    by_left_term: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in approved_rows:
        if not is_equivalence_relation(row.get("relation", "")):
            continue
        canonical_iri = clean(row.get("canonical_term_iri", ""))
        canonical_source = clean(row.get("canonical_term_source", ""))
        if not canonical_iri or not canonical_source:
            continue
        key = (
            clean(row.get("source_term_iri", "")) or clean(row.get("left_term_iri", "")),
            normalize_source(row.get("pivot_source", "")) or normalize_source(row.get("source_term_source", "")),
        )
        by_left_term.setdefault(key, []).append(row)

    for (_, _), rows_for_left in by_left_term.items():
        local_rows = [
            row
            for row in rows_for_left
            if clean(row.get("target_backend", "")) == "source"
            and clean(row.get("canonical_term_iri", ""))
            and clean(row.get("canonical_term_iri", "")) != clean(row.get("source_term_iri", ""))
        ]
        other_targets = [
            row
            for row in rows_for_left
            if clean(row.get("canonical_term_iri", ""))
            and clean(row.get("canonical_term_iri", "")) != clean(row.get("source_term_iri", ""))
        ]
        for local_row in local_rows:
            source_iri = clean(local_row.get("canonical_term_iri", ""))
            source_label = clean(local_row.get("canonical_term_label", ""))
            source_source = normalize_source(local_row.get("canonical_term_source", ""))
            source_kind = clean(local_row.get("canonical_term_kind", "")) or clean(local_row.get("source_term_kind", ""))
            for target_row in other_targets:
                target_iri = clean(target_row.get("canonical_term_iri", ""))
                if not target_iri or target_iri == source_iri:
                    continue
                target_label = clean(target_row.get("canonical_term_label", ""))
                target_source = normalize_source(target_row.get("canonical_term_source", ""))
                derived_alignment_id = (
                    f"DERIVED::{clean(local_row.get('batch_id', ''))}::{source_iri}::{target_iri}"
                )
                support_ids = " | ".join(
                    sorted(
                        {
                            stable_alignment_id(local_row),
                            stable_alignment_id(target_row),
                        }
                    )
                )
                append_source_mapping(
                    out_rows,
                    seen_keys,
                    alignment_id=derived_alignment_id,
                    batch_id=clean(local_row.get("batch_id", "")),
                    pivot_source=clean(local_row.get("pivot_source", "")),
                    target_id=clean(local_row.get("target_id", "")),
                    target_backend=clean(local_row.get("target_backend", "")),
                    mapping_origin="derived",
                    source_term_source=source_source,
                    source_term_iri=source_iri,
                    source_term_label=source_label,
                    canonical_term_iri=target_iri,
                    canonical_term_label=target_label,
                    canonical_term_source=target_source,
                    relation=derived_relation_for_kind(source_kind),
                    suggestion_source="derived_via_pivot",
                    curator=clean(local_row.get("curator", "")),
                    reviewer=clean(local_row.get("reviewer", "")),
                    date_added=clean(local_row.get("date_added", "")) or clean(local_row.get("date_reviewed", "")),
                    date_reviewed=clean(target_row.get("date_reviewed", "")) or clean(local_row.get("date_reviewed", "")),
                    notes=(
                        f"Derived via pivot {clean(local_row.get('pivot_source', ''))}: "
                        f"{support_ids}"
                    ),
                )

    return out_rows


def build_group_rows(mapping_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Group source mappings by canonical term."""
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for row in mapping_rows:
        key = (
            clean(row.get("canonical_term_iri", "")),
            clean(row.get("canonical_term_label", "")),
            clean(row.get("canonical_term_source", "")),
        )
        groups.setdefault(key, []).append(row)

    out: list[dict[str, str]] = []
    for key in sorted(groups.keys()):
        canonical_iri, canonical_label, canonical_source = key
        group_items = groups[key]
        source_entries = sorted(
            {
                (
                    clean(item.get("source_term_source", "")),
                    clean(item.get("source_term_iri", "")),
                    clean(item.get("source_term_label", "")),
                )
                for item in group_items
            }
        )
        out.append(
            {
                "canonical_term_iri": canonical_iri,
                "canonical_term_label": canonical_label,
                "canonical_term_source": canonical_source,
                "mapped_term_count": str(len(source_entries)),
                "mapped_terms": " | ".join(
                    f"{src}:{iri}:{label}" for src, iri, label in source_entries
                ),
                "alignment_ids": " | ".join(
                    sorted({clean(item.get("alignment_id", "")) for item in group_items})
                ),
            }
        )
    return out


def replace_table_rows(
    conn: sqlite3.Connection,
    table_name: str,
    columns: list[str],
    rows: list[dict[str, str]],
) -> int:
    """Replace all rows in table using provided column order."""
    conn.execute(f"DELETE FROM {table_name}")
    placeholders = ",".join("?" for _ in columns)
    sql = f"INSERT INTO {table_name} ({','.join(columns)}) VALUES ({placeholders})"
    for row in rows:
        conn.execute(sql, [clean(row.get(col, "")) for col in columns])
    return len(rows)


def fetch_all(conn: sqlite3.Connection, table_name: str, columns: list[str]) -> list[dict[str, str]]:
    """Fetch complete table as list of dictionaries."""
    query = f"SELECT {','.join(columns)} FROM {table_name} ORDER BY 1"
    cursor = conn.execute(query)
    result: list[dict[str, str]] = []
    for record in cursor.fetchall():
        row: dict[str, str] = {}
        for idx, col in enumerate(columns):
            value = record[idx]
            row[col] = "" if value is None else str(value)
        result.append(row)
    return result


def sync(args: argparse.Namespace) -> int:
    """Run full TSV->SQLite->reconciled-TSV synchronization flow."""
    pair_candidates = read_tsv(args.pair_candidates)
    pair_alignments_path = args.pair_alignments
    if pair_alignments_path is None or not pair_alignments_path.is_file():
        pair_alignments_path = args.pair_candidates
    pair_alignments = read_tsv(pair_alignments_path)

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    try:
        create_schema(conn)
        with conn:
            candidate_count = insert_pair_rows(
                conn, "pair_alignment_candidates", pair_candidates
            )
            curated_count = insert_pair_rows(conn, "pair_alignments", pair_alignments)

            reconciled_rows = build_reconciled_rows(
                pair_alignments, status_filter=args.status
            )
            group_rows = build_group_rows(reconciled_rows)

            reconciled_count = replace_table_rows(
                conn,
                "reconciled_mappings",
                RECONCILED_MAPPING_COLUMNS,
                reconciled_rows,
            )
            grouped_count = replace_table_rows(
                conn,
                "reconciled_canonical_groups",
                RECONCILED_GROUP_COLUMNS,
                group_rows,
            )

        exported_reconciled = fetch_all(
            conn, "reconciled_mappings", RECONCILED_MAPPING_COLUMNS
        )
        exported_groups = fetch_all(
            conn, "reconciled_canonical_groups", RECONCILED_GROUP_COLUMNS
        )
    finally:
        conn.close()

    write_tsv(args.reconciled_output, RECONCILED_MAPPING_COLUMNS, exported_reconciled)
    write_tsv(args.grouped_output, RECONCILED_GROUP_COLUMNS, exported_groups)

    print(f"SQLite DB: {args.db}")
    print(f"Imported candidates: {candidate_count}")
    print(f"Imported curated alignments: {curated_count}")
    print(f"Stored reconciled mappings: {reconciled_count}")
    print(f"Stored canonical groups: {grouped_count}")
    print(f"Wrote: {args.reconciled_output}")
    print(f"Wrote: {args.grouped_output}")
    return 0


def main() -> int:
    """Entry point for CLI."""
    args = parse_args()
    try:
        return sync(args)
    except FileNotFoundError as exc:
        print(f"File not found: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
