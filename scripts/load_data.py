"""Load the prepared dataset into one or more targets and record ingest throughput.

Every platform is loaded by the same logical method -- driver-batched UNWIND (or its AQL /
RESP equivalent) with an identical batch size -- so the throughput numbers compare
databases rather than comparing bulk-import tools. Each engine has a faster native
importer; the README says so explicitly rather than quietly using it for some targets.

Usage:
    python scripts/load_data.py --db cognodb
    python scripts/load_data.py --track lab
    python scripts/load_data.py --db all --edges 150000
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from benchmarks.registry import REPO_ROOT, build_adapter, load_config, selected_targets

PREPARED = REPO_ROOT / "data" / "prepared"
RESULTS = REPO_ROOT / "results"

log = logging.getLogger("bench.load")


def read_prepared(edge_limit: int = 0) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Read nodes.csv / edges.csv.

    When --edges truncates the graph, the node set is recomputed from the surviving edges
    so no isolated nodes are loaded that the traversal ground truth does not know about.
    """
    edges: list[tuple[str, str]] = []
    with (PREPARED / "edges.csv").open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        next(reader)
        for src, dst in reader:
            edges.append((src, dst))
            if edge_limit and len(edges) >= edge_limit:
                break

    years: dict[str, str] = {}
    with (PREPARED / "nodes.csv").open(encoding="utf-8") as handle:
        reader = csv.reader(handle)
        next(reader)
        for node_id, year, _date in reader:
            years[node_id] = year

    if edge_limit:
        touched = {node for edge in edges for node in edge}
        nodes = [(node, years.get(node, "")) for node in sorted(touched)]
    else:
        nodes = sorted(years.items())
    return nodes, edges


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="all", help="Target name(s), comma separated, or 'all'.")
    parser.add_argument("--track", choices=["lab", "cloud"], help="Restrict to one track.")
    parser.add_argument("--edges", type=int, default=0, help="Load only the first N edges (0 = all).")
    parser.add_argument("--batch-size", type=int, default=0, help="Override the configured batch size.")
    parser.add_argument("--keep", action="store_true", help="Skip the pre-load reset (append instead).")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv(REPO_ROOT / ".env")

    config = load_config()
    batch_size = args.batch_size or config["defaults"]["batch_size"]
    targets = selected_targets(config, args.db, args.track)
    if not targets:
        log.error("no targets selected -- check --db/--track and your .env")
        return 1

    nodes, edges = read_prepared(args.edges)
    log.info("dataset: %s nodes / %s relationships (batch size %s)",
             f"{len(nodes):,}", f"{len(edges):,}", batch_size)

    RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS / "load_results.json"
    records = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}

    exit_code = 0
    for name, spec in targets.items():
        log.info("=== %s (%s track) ===", name, spec.get("track"))
        adapter = build_adapter(name, spec)
        try:
            adapter.connect()

            indexes = adapter.setup_schema()
            log.info("%s: indexes -> %s", name, indexes)

            if not args.keep:
                log.info("%s: clearing existing data ...", name)
                reset_start = time.perf_counter()
                adapter.reset()
                log.info("%s: cleared in %.1fs", name, time.perf_counter() - reset_start)

            log.info("%s: loading ...", name)
            result = adapter.load(nodes, edges, batch_size)
            payload = result.as_dict()
            payload["indexes"] = indexes
            payload["loaded_utc"] = datetime.now(timezone.utc).isoformat()
            payload["track"] = spec.get("track")
            records[name] = payload

            log.info(
                "%s: loaded %s nodes (%s/s) + %s rels (%s/s) in %.1fs",
                name, f"{result.nodes:,}", payload["nodes_per_second"],
                f"{result.relationships:,}", payload["relationships_per_second"],
                result.total_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - a failed load is a reportable result
            log.exception("%s: LOAD FAILED", name)
            records[name] = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "loaded_utc": datetime.now(timezone.utc).isoformat(),
                "track": spec.get("track"),
            }
            exit_code = 1
        finally:
            try:
                adapter.close()
            except Exception:  # noqa: BLE001
                pass

        out_path.write_text(json.dumps(records, indent=2), encoding="utf-8")

    log.info("wrote %s", out_path)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
