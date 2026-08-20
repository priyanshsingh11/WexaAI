"""Turn the raw SNAP cit-HepPh files into the exact dataset every database loads.

Why this script exists at all: the benchmark is only fair if all six targets receive
byte-identical input. So the raw SNAP files are normalised *once*, written to CSV, and
hashed. Every loader reads those CSVs; nobody re-parses the raw files. The manifest
records the hashes so a reviewer can prove they rebuilt the same dataset.

Two non-obvious details are handled here, both verified against the real files:

1.  ID normalisation. Node IDs in Cit-HepPh.txt have had leading zeros stripped
    (observed lengths 4/5/6/7), while cit-HepPh-dates.txt keeps the full 7 digits and
    prefixes cross-listed papers with "11". A naive string join therefore matches only
    62.9% of nodes and silently drops every paper from 2000-2002 -- the year range
    appears to end at 1999. Stripping the "11" prefix and zero-padding to 7 lifts
    coverage to 88.5% and restores the true 1992-2002 range.

2.  Nodes with no date are kept. The ~11.5% of papers with no entry in the dates file
    are cited from outside the hep-ph category. They carry the citation topology that
    the traversal workloads measure, so dropping them would change the graph. They get
    a null year instead, and the property-based workloads only cover the dated 88.5%.

Usage:
    python scripts/prepare_dataset.py                 # full graph, 421,578 edges
    python scripts/prepare_dataset.py --edges 150000  # deterministic seeded subset
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw"
OUT_DIR = REPO_ROOT / "data" / "prepared"

EDGES_RAW = RAW_DIR / "Cit-HepPh.txt"
DATES_RAW = RAW_DIR / "cit-HepPh-dates.txt"

SOURCE_URL = "https://snap.stanford.edu/data/cit-HepPh.html"

# Number of start nodes drawn for the traversal workloads. 200 gives every workload a
# wide spread of fan-outs while keeping a 100-iteration run from reusing a node twice.
START_NODE_COUNT = 200


def normalise_id(paper_id: str) -> str:
    """Canonicalise an arXiv paper ID to 7 digits.

    Cross-listed papers appear in the dates file as 11<true_id>; the edge file stores
    IDs with leading zeros stripped. Both are folded onto the same 7-digit key.
    """
    if len(paper_id) > 7 and paper_id.startswith("11"):
        paper_id = paper_id[2:]
    return paper_id.zfill(7)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_dates() -> dict[str, str]:
    """Map normalised paper ID -> ISO date.

    Where a paper appears both plain and "11"-prefixed, the plain entry wins: it is the
    paper's own submission date rather than its cross-listing date.
    """
    dates: dict[str, str] = {}
    with DATES_RAW.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            paper_id, date = line.split()
            key = normalise_id(paper_id)
            is_cross_listed = len(paper_id) > 7 and paper_id.startswith("11")
            if key not in dates or not is_cross_listed:
                dates[key] = date
    return dates


def read_edges() -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    with EDGES_RAW.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            source, target = line.split()
            edges.append((normalise_id(source), normalise_id(target)))
    return edges


def sample_edges(edges: list[tuple[str, str]], limit: int, seed: int) -> list[tuple[str, str]]:
    """Deterministically take `limit` edges.

    Sorted first so the sample depends only on the seed, never on file read order.
    Sampling edges (not nodes) keeps the degree distribution roughly intact; sampling
    nodes would shred the connectivity the multi-hop workloads depend on.
    """
    if limit >= len(edges):
        return edges
    ordered = sorted(edges)
    return sorted(random.Random(seed).sample(ordered, limit))


def reachable_within(adjacency: dict[str, set[str]], start: str, hops: int) -> set[str]:
    """OTHER nodes reachable from `start` in 1..hops steps -- the start node is excluded.

    The exclusion matches the `WHERE m <> n` clause the adapters use, and exists because
    the engines disagree about cycles. Measured on CognoDB: the explicit two-step pattern
    finds a path that returns to the origin, but the variable-length `*1..2` form does not,
    while Neo4j keeps such paths. With 44 self-loops and 9 of the 200 start nodes sitting on
    a <=3-hop cycle, that disagreement changed the answer on ~5% of start nodes.

    Asking "which OTHER papers are reachable" sidesteps the ambiguity entirely and is the
    more natural question, so ground truth and every engine now agree exactly.
    """
    frontier = {start}
    seen: set[str] = set()
    for _ in range(hops):
        nxt: set[str] = set()
        for node in frontier:
            nxt |= adjacency.get(node, set())
        nxt -= seen
        seen |= nxt
        frontier = nxt
        if not frontier:
            break
    return seen - {start}


def compute_expected(
    adjacency: dict[str, set[str]],
    start_nodes: list[str],
    node_years: dict[str, str],
    year_histogram: Counter,
) -> dict[str, object]:
    """Precompute the correct answer for every read workload.

    This is the harness's correctness backbone. Each database's result is checked against
    this independently-computed ground truth rather than merely against the other
    databases -- five engines agreeing on a wrong answer would otherwise look like a pass.
    A mismatch means the query for that engine is not logically equivalent, and its
    latency numbers are meaningless until it is fixed.
    """
    hops = {
        str(depth): {
            node: len(reachable_within(adjacency, node, depth)) for node in start_nodes
        }
        for depth in (1, 2, 3)
    }
    return {
        "traversal_counts": hops,
        "point_lookup_year": {node: node_years.get(node, "") for node in start_nodes},
        "filtered_lookup_counts": dict(sorted(year_histogram.items())),
        "aggregation_year_histogram": dict(sorted(year_histogram.items())),
    }


def pick_start_nodes(
    out_degree: Counter, seed: int, count: int
) -> list[dict[str, object]]:
    """Draw the traversal start nodes once, for every database and every run.

    This is the single most important fairness control in the harness: if each database
    were queried from a different set of start nodes, their fan-outs would differ and
    the p95 numbers would be measuring the graph, not the engine.

    Selection is uniform over nodes with at least one outgoing citation. Uniform (rather
    than degree-weighted) sampling means the start set mirrors the real degree
    distribution -- mostly modest nodes with a few hubs -- which is what a real
    application would hit. The chosen out-degrees are recorded so the README can show
    the fan-out spread behind the latency numbers.
    """
    candidates = sorted(node for node, degree in out_degree.items() if degree > 0)
    chosen = random.Random(seed).sample(candidates, min(count, len(candidates)))
    return [{"id": node, "out_degree": out_degree[node]} for node in sorted(chosen)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--edges",
        type=int,
        default=0,
        help="Cap the edge count (deterministic seeded sample). 0 = use the full graph.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=int(os.environ.get("BENCH_SEED", 42)),
        help="Seed for edge sampling and start-node selection (default: $BENCH_SEED or 42).",
    )
    args = parser.parse_args()

    for path in (EDGES_RAW, DATES_RAW):
        if not path.exists():
            print(f"ERROR: missing {path}", file=sys.stderr)
            print(f"Download the dataset from {SOURCE_URL} into data/raw/", file=sys.stderr)
            return 1

    print(f"Reading {EDGES_RAW.name} ...")
    edges = read_edges()
    total_edges = len(edges)

    if args.edges:
        edges = sample_edges(edges, args.edges, args.seed)
        print(f"  sampled {len(edges):,} of {total_edges:,} edges (seed={args.seed})")
    else:
        print(f"  {total_edges:,} edges")

    print(f"Reading {DATES_RAW.name} ...")
    dates = read_dates()
    print(f"  {len(dates):,} dated papers")

    # Node set is derived from the edges actually kept, so a sampled run stays internally
    # consistent: no node exists without at least one incident edge.
    out_degree: Counter = Counter()
    nodes: set[str] = set()
    for source, target in edges:
        nodes.add(source)
        nodes.add(target)
        out_degree[source] += 1

    dated = sum(1 for node in nodes if node in dates)
    coverage = 100.0 * dated / len(nodes)
    year_histogram = Counter(dates[node][:4] for node in nodes if node in dates)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    nodes_csv = OUT_DIR / "nodes.csv"
    with nodes_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "year", "date"])
        for node in sorted(nodes):
            date = dates.get(node)
            writer.writerow([node, date[:4] if date else "", date or ""])

    edges_csv = OUT_DIR / "edges.csv"
    with edges_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["src", "dst"])
        writer.writerows(edges)

    start_nodes = pick_start_nodes(out_degree, args.seed, START_NODE_COUNT)
    start_path = OUT_DIR / "start_nodes.json"
    start_path.write_text(json.dumps(start_nodes, indent=2), encoding="utf-8")

    degrees = [entry["out_degree"] for entry in start_nodes]
    degrees_sorted = sorted(degrees)

    print("Computing ground-truth answers for every read workload ...")
    adjacency: dict[str, set[str]] = defaultdict(set)
    for source, target in edges:
        adjacency[source].add(target)
    start_ids = [str(entry["id"]) for entry in start_nodes]
    node_years = {node: dates[node][:4] for node in nodes if node in dates}
    expected = compute_expected(adjacency, start_ids, node_years, year_histogram)
    expected_path = OUT_DIR / "expected.json"
    expected_path.write_text(json.dumps(expected, indent=2), encoding="utf-8")

    manifest = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source": {
            "name": "SNAP cit-HepPh (arXiv High Energy Physics citation network)",
            "url": SOURCE_URL,
            "files": {
                EDGES_RAW.name: {"sha256": sha256_of(EDGES_RAW), "bytes": EDGES_RAW.stat().st_size},
                DATES_RAW.name: {"sha256": sha256_of(DATES_RAW), "bytes": DATES_RAW.stat().st_size},
            },
        },
        "seed": args.seed,
        "edge_limit": args.edges or None,
        "graph": {
            "nodes": len(nodes),
            "edges": len(edges),
            "edges_in_full_source": total_edges,
            "nodes_with_year": dated,
            "year_coverage_pct": round(coverage, 2),
            "year_histogram": dict(sorted(year_histogram.items())),
        },
        "start_nodes": {
            "count": len(start_nodes),
            "selection": "uniform over nodes with out_degree > 0",
            "out_degree_min": degrees_sorted[0],
            "out_degree_median": degrees_sorted[len(degrees_sorted) // 2],
            "out_degree_max": degrees_sorted[-1],
        },
        "outputs": {
            path.name: {"sha256": sha256_of(path), "bytes": path.stat().st_size}
            for path in (nodes_csv, edges_csv, start_path, expected_path)
        },
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print()
    print(f"  nodes            : {len(nodes):,}")
    print(f"  edges            : {len(edges):,}")
    print(f"  nodes with year  : {dated:,} ({coverage:.1f}%)")
    print(f"  year range       : {min(year_histogram)} -> {max(year_histogram)}")
    print(
        f"  start nodes      : {len(start_nodes)} "
        f"(out-degree {degrees_sorted[0]}/{degrees_sorted[len(degrees_sorted)//2]}/{degrees_sorted[-1]} min/med/max)"
    )
    print(f"\nWrote {OUT_DIR}/ (nodes.csv, edges.csv, start_nodes.json, manifest.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
