#!/usr/bin/env python3
"""Generate a PR review comment with focused before/after Mermaid graphs."""

from __future__ import annotations

import argparse
import csv
import io
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from curation_app.pages.view_schema import _build_mermaid, _write_merged_ttl  # noqa: E402
from scripts.export_updated_ttl import build_exports_for_ledger  # noqa: E402

MARKER = "<!-- pr-mermaid-review -->"
LEDGER_PATTERN = re.compile(r"^registry/pair_alignment_candidates_([A-Za-z0-9_.-]+)\.tsv$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Mermaid PR review markdown for changed shared-ledger rows.")
    parser.add_argument("--base-sha", required=True, help="Base commit SHA to diff against.")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Markdown output path.",
    )
    parser.add_argument(
        "--max-terms",
        type=int,
        default=8,
        help="Maximum number of changed source terms to include.",
    )
    parser.add_argument(
        "--focus-hops",
        type=int,
        default=2,
        help="Connected graph depth for focused Mermaid views.",
    )
    parser.add_argument(
        "--max-nodes",
        type=int,
        default=80,
        help="Maximum Mermaid nodes per graph.",
    )
    return parser.parse_args()


def run_git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout


def changed_ledger_paths(base_sha: str) -> list[Path]:
    out = run_git("diff", "--name-only", base_sha, "--", "registry/pair_alignment_candidates_*.tsv")
    paths: list[Path] = []
    for raw in out.splitlines():
        path = raw.strip()
        if not path:
            continue
        if LEDGER_PATTERN.match(path):
            paths.append(Path(path))
    return sorted(paths)


def read_tsv_text(text: str) -> dict[str, dict[str, str]]:
    text = text.strip()
    if not text:
        return {}
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    rows: dict[str, dict[str, str]] = {}
    for row in reader:
        key = str(row.get("source_term_iri", "") or "").strip()
        if key:
            rows[key] = {k: str(v or "") for k, v in row.items()}
    return rows


def read_tsv_at_revision(base_sha: str, path: Path) -> dict[str, dict[str, str]]:
    try:
        text = run_git("show", f"{base_sha}:{path.as_posix()}", check=True)
    except subprocess.CalledProcessError:
        return {}
    return read_tsv_text(text)


def read_current_tsv(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    return read_tsv_text(path.read_text(encoding="utf-8", errors="replace"))


def source_ttl_candidates(ledger_path: Path, source_slug: str) -> list[Path]:
    slugs: list[str] = []

    def add_slug(value: str) -> None:
        slug = str(value or "").strip().lower()
        if slug and slug not in slugs:
            slugs.append(slug)

    if ledger_path.is_file():
        with ledger_path.open(newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            for row in reader:
                add_slug(row.get("pivot_source", ""))
                add_slug(row.get("source_term_source", ""))
                break

    add_slug(source_slug)
    return [Path("registry/downloads") / f"{slug}.ttl" for slug in slugs]


def resolve_source_ttl_path(ledger_path: Path, source_slug: str) -> Path:
    candidates = source_ttl_candidates(ledger_path, source_slug)
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError(
        f"No downloaded source TTL found for `{ledger_path}`. Tried: "
        + ", ".join(path.as_posix() for path in candidates)
    )


def changed_rows(old_rows: dict[str, dict[str, str]], new_rows: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    changed: list[dict[str, str]] = []
    keys = sorted(set(old_rows) | set(new_rows))
    for iri in keys:
        old = old_rows.get(iri)
        new = new_rows.get(iri)
        if old == new:
            continue
        row = new or old or {}
        payload = dict(row)
        payload["_change_type"] = "modified" if old and new else ("added" if new else "removed")
        changed.append(payload)
    return changed


def build_compare_ttls(source_slug: str, tmpdir: Path) -> tuple[Path, Path]:
    ledger_path = Path("registry") / f"pair_alignment_candidates_{source_slug}.tsv"
    if not ledger_path.is_file():
        raise FileNotFoundError(ledger_path)
    source_ttl_path = resolve_source_ttl_path(ledger_path, source_slug)

    _, mapping_ttl_text = build_exports_for_ledger(
        ledger_path=ledger_path,
        source_ttl_path=source_ttl_path,
        statuses=["approved"],
    )

    mapping_path = tmpdir / f"{source_slug}_mappings.ttl"
    if mapping_ttl_text.strip():
        mapping_path.write_text(mapping_ttl_text, encoding="utf-8")

    merged_path = tmpdir / f"{source_slug}_original_plus_mappings.ttl"
    ok_merge, merge_msg = _write_merged_ttl([source_ttl_path, mapping_path], merged_path)
    if not ok_merge:
        raise RuntimeError(merge_msg)
    return source_ttl_path, merged_path


def parse_mermaid_lines(text: str) -> tuple[list[str], list[str], list[str]]:
    nodes: list[str] = []
    edges: list[str] = []
    clicks: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped == "flowchart LR":
            continue
        if stripped.startswith("click "):
            clicks.append(stripped)
        elif "-->" in stripped:
            edges.append(stripped)
        else:
            nodes.append(stripped)
    return nodes, edges, clicks


def prefix_mermaid_ids(lines: list[str], prefix: str) -> list[str]:
    out: list[str] = []
    id_pattern = re.compile(r"\bn(\d+)\b")
    for line in lines:
        out.append(id_pattern.sub(lambda m: f"{prefix}_n{m.group(1)}", line))
    return out


def first_mermaid_node_id(lines: list[str]) -> str | None:
    node_pattern = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*(?:\[|\(|\{)")
    for line in lines:
        match = node_pattern.match(line)
        if match:
            return match.group(1)
    return None


def combine_mermaid(before_text: str, after_text: str) -> str:
    before_nodes, before_edges, before_clicks = parse_mermaid_lines(before_text)
    after_nodes, after_edges, after_clicks = parse_mermaid_lines(after_text)
    before_prefixed = prefix_mermaid_ids(before_nodes + before_edges + before_clicks, "before")
    after_prefixed = prefix_mermaid_ids(after_nodes + after_edges + after_clicks, "after")
    before_focus = first_mermaid_node_id(before_prefixed)
    after_focus = first_mermaid_node_id(after_prefixed)

    lines = [
        "flowchart LR",
        '  before_layout[" "]',
        '  after_layout[" "]',
        "  style before_layout fill:transparent,stroke:transparent,color:transparent",
        "  style after_layout fill:transparent,stroke:transparent,color:transparent",
        "  before_layout --> after_layout",
    ]
    if before_focus:
        lines.append(f"  before_layout --> {before_focus}")
    if after_focus:
        lines.append(f"  after_layout --> {after_focus}")
    for idx in range(1 + int(bool(before_focus)) + int(bool(after_focus))):
        lines.append(f"  linkStyle {idx} stroke:transparent,fill:none,opacity:0")

    lines.extend(['  subgraph before_graph["**Before curation**"]', "    direction LR"])
    for line in before_prefixed:
        lines.append(f"    {line}")
    lines.extend(['  end', '  subgraph after_graph["**After curation**"]', "    direction LR"])
    for line in after_prefixed:
        lines.append(f"    {line}")
    lines.append("  end")
    return "\n".join(lines) + "\n"


def single_node_mermaid(iri: str, label: str) -> str:
    safe_label = str(label or short_iri(iri) or iri).replace('"', "'")
    safe_iri = str(iri or "").replace('"', "%22")
    return "\n".join(
        [
            "flowchart LR",
            f'  n0["{safe_label}"]',
            f'  click n0 href "{safe_iri}" "Open IRI" _blank',
        ]
    ) + "\n"


def short_iri(iri: str) -> str:
    iri = str(iri or "").strip()
    if not iri:
        return ""
    if "#" in iri:
        return iri.rsplit("#", 1)[-1]
    return iri.rstrip("/").rsplit("/", 1)[-1]


def generate_term_section(
    source_slug: str,
    row: dict[str, str],
    before_ttl: Path,
    after_ttl: Path,
    focus_hops: int,
    max_nodes: int,
) -> str:
    source_iri = str(row.get("source_term_iri", "") or "").strip()
    source_label = str(row.get("source_term_label", "") or "").strip() or short_iri(source_iri)
    canonical_iri = str(row.get("canonical_term_iri", "") or "").strip()
    canonical_label = str(row.get("canonical_term_label", "") or "").strip() or short_iri(canonical_iri)
    canonical_source = str(row.get("canonical_term_source", "") or "").strip()
    relation = str(row.get("relation", "") or "").strip()
    reviewer_name = str(row.get("reviewer_name", "") or "").strip()
    reviewer_orcid = str(row.get("reviewer", "") or "").strip()
    change_type = str(row.get("_change_type", "") or "").strip()
    comment = str(row.get("curation_comment", "") or "").strip()

    left_ok, before_graph, left_msg = _build_mermaid(
        input_ttl=before_ttl,
        mode="schema",
        max_nodes=max_nodes,
        include_external=True,
        focus_entity_iri=source_iri,
        focus_max_hops=focus_hops,
    )
    right_ok, after_graph, right_msg = _build_mermaid(
        input_ttl=after_ttl,
        mode="schema",
        max_nodes=max_nodes,
        include_external=True,
        focus_entity_iri=source_iri,
        focus_max_hops=focus_hops,
    )

    lines = [f"### `{source_label}`", f"- Source: `{source_iri}`"]
    lines.append(
        "- Graph compare: **Before curation** uses the downloaded source TTL; "
        "**After curation** uses the original source TTL plus exported mappings."
    )
    if canonical_iri:
        target_text = f"`{canonical_label}`"
        if canonical_source:
            target_text += f" from `{canonical_source}`"
        target_text += f" (`{canonical_iri}`)"
        lines.append(f"- Canonical: {target_text}")
    if relation:
        lines.append(f"- Relation: `{relation}`")
    if change_type:
        lines.append(f"- Change: `{change_type}`")
    if reviewer_name or reviewer_orcid:
        reviewer_text = reviewer_name or reviewer_orcid
        if reviewer_name and reviewer_orcid:
            reviewer_text = f"{reviewer_name} ({reviewer_orcid})"
        lines.append(f"- Reviewer: {reviewer_text}")
    if comment:
        lines.append(f"- Comment: {comment}")

    before_render = before_graph
    after_render = after_graph
    if before_render.strip() == "flowchart LR" or not before_render.strip():
        before_render = single_node_mermaid(source_iri, source_label)
        left_msg = f"{left_msg} Rendered isolated source-term graph from the downloaded source TTL."
    if after_render.strip() == "flowchart LR" or not after_render.strip():
        after_render = single_node_mermaid(source_iri, source_label)
        right_msg = (
            f"{right_msg} Rendered isolated source-term graph from the original source TTL plus exported mappings."
        )

    if left_ok and right_ok:
        combined = combine_mermaid(before_render, after_render)
        lines.extend(
            [
                "",
                "```mermaid",
                combined.rstrip(),
                "```",
                "",
                f"**Before curation**: {left_msg}",
                f"**After curation**: {right_msg}",
            ]
        )
    else:
        lines.extend(
            [
                "",
                f"- Mermaid generation fallback for `{source_slug}`.",
                f"- **Before curation**: {left_msg}",
                f"- **After curation**: {right_msg}",
            ]
        )
    return "\n".join(lines)


def build_comment(base_sha: str, max_terms: int, focus_hops: int, max_nodes: int) -> str:
    ledger_paths = changed_ledger_paths(base_sha)
    if not ledger_paths:
        return "\n".join(
            [
                MARKER,
                "## Mermaid Review",
                "",
                "No changed shared-ledger TSV rows were detected in this PR.",
            ]
        ) + "\n"

    sections: list[str] = [MARKER, "## Mermaid Review", "", "Auto-generated focused before/after graphs for changed shared-ledger terms."]
    total_changed = 0

    with tempfile.TemporaryDirectory(prefix="pr_mermaid_") as tmp:
        tmpdir = Path(tmp)
        for ledger_path in ledger_paths:
            match = LEDGER_PATTERN.match(ledger_path.as_posix())
            if not match:
                continue
            source_slug = match.group(1)
            old_rows = read_tsv_at_revision(base_sha, ledger_path)
            new_rows = read_current_tsv(ledger_path)
            changed = changed_rows(old_rows, new_rows)
            if not changed:
                continue
            total_changed += len(changed)
            before_ttl, after_ttl = build_compare_ttls(source_slug, tmpdir)
            sections.extend(["", f"## Source `{source_slug}`", ""])
            for row in changed[:max_terms]:
                sections.append(generate_term_section(source_slug, row, before_ttl, after_ttl, focus_hops, max_nodes))
                sections.append("")
            if len(changed) > max_terms:
                sections.append(
                    f"_Truncated_: showing {max_terms} of {len(changed)} changed term(s) for `{source_slug}`."
                )
                sections.append("")

    if total_changed == 0:
        sections.extend(["", "No changed shared-ledger TSV rows were detected in this PR."])
    return "\n".join(sections).rstrip() + "\n"


def main() -> None:
    args = parse_args()
    comment = build_comment(
        base_sha=args.base_sha,
        max_terms=args.max_terms,
        focus_hops=args.focus_hops,
        max_nodes=args.max_nodes,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(comment, encoding="utf-8")


if __name__ == "__main__":
    main()
