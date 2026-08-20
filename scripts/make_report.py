"""Turn results.json into results.csv and the Markdown tables used in the README.

Kept separate from run_benchmark.py so reporting can be re-run and re-formatted without
re-measuring anything. Reads only what was measured; it never fills a gap with an estimate.
A workload that failed, or whose results did not match ground truth, is rendered as such
rather than being dropped from the table.

Usage:
    python scripts/make_report.py                       # all runs merged
    python scripts/make_report.py --results results/results-lab.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS = REPO_ROOT / "results"

WORKLOAD_ORDER = ["one_hop", "two_hop", "three_hop", "point_lookup", "filtered_lookup", "aggregation"]
WORKLOAD_LABELS = {
    "one_hop": "1-hop",
    "two_hop": "2-hop",
    "three_hop": "3-hop",
    "point_lookup": "Point lookup",
    "filtered_lookup": "Filtered lookup (indexed)",
    "aggregation": "Aggregation (group-by)",
}


def cell(stats: dict[str, Any], key: str) -> str:
    value = stats.get(key)
    return "n/a" if value is None else f"{value:.2f}"


def load_results(paths: list[Path]) -> dict[str, Any]:
    """Merge one or more result files, keyed by database name."""
    merged: dict[str, Any] = {"databases": {}, "manifests": {}}
    for path in paths:
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        merged["manifests"][payload.get("run_id", path.stem)] = payload.get("manifest", {})
        for name, entry in payload.get("databases", {}).items():
            merged["databases"][name] = entry
    return merged


def write_csv(data: dict[str, Any], out: Path) -> None:
    rows = []
    for name, entry in data["databases"].items():
        for workload, payload in entry.get("workloads", {}).items():
            warm = payload.get("warm", {})
            cold = payload.get("cold", {})
            rows.append({
                "database": name,
                "display_name": entry.get("display_name", name),
                "track": entry.get("track"),
                "dialect": entry.get("dialect"),
                "workload": workload,
                "category": payload.get("category"),
                "correctness": payload.get("correctness"),
                "failures": payload.get("failures"),
                "n": warm.get("n"),
                "warm_min_ms": warm.get("min_ms"),
                "warm_p50_ms": warm.get("p50_ms"),
                "warm_p90_ms": warm.get("p90_ms"),
                "warm_p95_ms": warm.get("p95_ms"),
                "warm_p99_ms": warm.get("p99_ms"),
                "warm_max_ms": warm.get("max_ms"),
                "warm_mean_ms": warm.get("mean_ms"),
                "warm_stdev_ms": warm.get("stdev_ms"),
                "cold_p50_ms": cold.get("p50_ms"),
                "cold_max_ms": cold.get("max_ms"),
            })
    if not rows:
        return
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def latency_table(data: dict[str, Any], track: str) -> str:
    """p50/p95 matrix for one track. Tracks are never mixed in a single table -- the cloud
    endpoint carries a few hundred ms of WAN latency the loopback containers do not."""
    targets = [(n, e) for n, e in data["databases"].items() if e.get("track") == track]
    if not targets:
        return f"_No {track}-track results recorded._\n"

    header = "| Workload | " + " | ".join(e.get("display_name", n) for n, e in targets) + " |"
    divider = "|---|" + "---|" * len(targets)
    lines = [header, divider]
    for workload in WORKLOAD_ORDER:
        cells = []
        for _name, entry in targets:
            payload = entry.get("workloads", {}).get(workload)
            if not payload or not payload.get("warm", {}).get("n"):
                cells.append("not measured")
                continue
            warm = payload["warm"]
            text = f"{cell(warm,'p50_ms')} / {cell(warm,'p95_ms')}"
            if payload.get("correctness") == "MISMATCH":
                text += " ⚠️"
            if payload.get("failures"):
                text += f" ({payload['failures']} failed)"
            cells.append(text)
        lines.append(f"| {WORKLOAD_LABELS[workload]} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def concurrency_table(data: dict[str, Any], track: str) -> str:
    targets = [(n, e) for n, e in data["databases"].items() if e.get("track") == track]
    rows = []
    for name, entry in targets:
        for mixed in entry.get("mixed", []):
            rows.append((entry.get("display_name", name), mixed))
    if not rows:
        return "_No mixed-workload results recorded._\n"

    lines = [
        "| Database | Clients | Sustained QPS | Read p50 / p95 (ms) | Write p50 / p95 (ms) | Errors |",
        "|---|---|---|---|---|---|",
    ]
    for display, mixed in rows:
        read, write = mixed.get("read_latency", {}), mixed.get("write_latency", {})
        lines.append(
            f"| {display} | {mixed['clients']} | {mixed['throughput_qps']:.1f} | "
            f"{cell(read,'p50_ms')} / {cell(read,'p95_ms')} | "
            f"{cell(write,'p50_ms')} / {cell(write,'p95_ms')} | {mixed.get('errors',0)} |"
        )
    return "\n".join(lines) + "\n"


def load_table(load_results: dict[str, Any]) -> str:
    lines = [
        "| Database | Track | Nodes/sec | Relationships/sec | Total load time (s) | Method |",
        "|---|---|---|---|---|---|",
    ]
    for name, payload in load_results.items():
        if payload.get("status") == "failed":
            lines.append(f"| {name} | {payload.get('track','')} | failed | failed | failed | {payload.get('error','')[:60]} |")
            continue
        lines.append(
            f"| {name} | {payload.get('track','')} | {payload['nodes_per_second']:,.0f} | "
            f"{payload['relationships_per_second']:,.0f} | {payload['total_seconds']:.1f} | "
            f"{payload['method']} |"
        )
    return "\n".join(lines) + "\n"


def baseline_table(data: dict[str, Any]) -> str:
    lines = [
        "| Database | Track | TCP p50 (ms) | TCP p95 (ms) | Resolved endpoint |",
        "|---|---|---|---|---|",
    ]
    for name, entry in data["databases"].items():
        base = entry.get("network_baseline_tcp_ms", {})
        if not base or base.get("p50_ms") is None:
            lines.append(f"| {entry.get('display_name',name)} | {entry.get('track','')} | not observable | not observable | - |")
            continue
        lines.append(
            f"| {entry.get('display_name',name)} | {entry.get('track','')} | "
            f"{base['p50_ms']:.2f} | {base['p95_ms']:.2f} | {base.get('resolved_ip','-')} |"
        )
    return "\n".join(lines) + "\n"


def footprint_table(data: dict[str, Any]) -> str:
    lines = ["| Database | Nodes | Relationships | Memory / store size | Server version |", "|---|---|---|---|---|"]
    for name, entry in data["databases"].items():
        fp = entry.get("footprint", {})
        mem = fp.get("memory_used_bytes", fp.get("papers_size_bytes", fp.get("store_size_bytes", "not observable")))
        if isinstance(mem, int):
            mem = f"{mem/1e6:.1f} MB"
        lines.append(
            f"| {entry.get('display_name',name)} | {fp.get('node_count','not observable')} | "
            f"{fp.get('relationship_count','not observable')} | {mem} | {fp.get('server_version','not observable')} |"
        )
    return "\n".join(lines) + "\n"


def correctness_table(data: dict[str, Any]) -> str:
    lines = ["| Database | " + " | ".join(WORKLOAD_LABELS[w] for w in WORKLOAD_ORDER) + " |",
             "|---|" + "---|" * len(WORKLOAD_ORDER)]
    for name, entry in data["databases"].items():
        cells = []
        for workload in WORKLOAD_ORDER:
            payload = entry.get("workloads", {}).get(workload)
            if not payload:
                cells.append("not run")
            elif payload.get("correctness") == "ok":
                cells.append("✅")
            elif payload.get("correctness") == "MISMATCH":
                cells.append("❌")
            else:
                cells.append("—")
        lines.append(f"| {entry.get('display_name',name)} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", nargs="*", default=None,
                        help="Result JSON files to merge (default: every results*.json in results/).")
    args = parser.parse_args()

    paths = [Path(p) for p in args.results] if args.results else sorted(RESULTS.glob("results*.json"))
    paths = [p for p in paths if p.name != "results.csv"]
    data = load_results(paths)
    if not data["databases"]:
        print("no results found -- run scripts/run_benchmark.py first", file=sys.stderr)
        return 1

    write_csv(data, RESULTS / "results.csv")

    load_path = RESULTS / "load_results.json"
    loads = json.loads(load_path.read_text(encoding="utf-8")) if load_path.exists() else {}

    sections = [
        ("Data loading (ingest throughput)", load_table(loads)),
        ("Network baseline (transport cost, measured separately from engine time)", baseline_table(data)),
        ("Lab track — read latency, p50 / p95 in ms", latency_table(data, "lab")),
        ("Cloud track — read latency, p50 / p95 in ms", latency_table(data, "cloud")),
        ("Mixed read/write workload — lab track", concurrency_table(data, "lab")),
        ("Mixed read/write workload — cloud track", concurrency_table(data, "cloud")),
        ("Correctness against ground truth", correctness_table(data)),
        ("Resource footprint", footprint_table(data)),
    ]
    report = "\n".join(f"### {title}\n\n{body}" for title, body in sections)
    (RESULTS / "tables.md").write_text(report, encoding="utf-8")

    print(f"wrote {RESULTS/'results.csv'}")
    print(f"wrote {RESULTS/'tables.md'}")
    print()
    # The tables contain ✅/⚠️; a Windows console defaults to cp1252 and would raise on
    # them. The files are already written as UTF-8, so only this echo needs protecting.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - older/redirected streams
        pass
    print(report.encode("utf-8", "replace").decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
