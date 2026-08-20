"""Generate charts from measured results.

matplotlib only -- one plotting dependency is enough, and every chart here is a plain bar
or line plot that does not need a second library.

Lab and cloud tracks are never drawn on the same axes. The cloud endpoint carries a few
hundred milliseconds of WAN latency that the loopback containers do not, so a combined
chart would show the network, not the databases.

Usage:
    python scripts/make_charts.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS = REPO_ROOT / "results"
CHARTS = RESULTS / "charts"

WORKLOADS = ["one_hop", "two_hop", "three_hop", "point_lookup", "filtered_lookup", "aggregation"]
LABELS = ["1-hop", "2-hop", "3-hop", "point", "filtered", "aggregation"]

# Colour-blind-safe qualitative palette (Okabe-Ito).
PALETTE = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#D55E00", "#56B4E9"]


def load_all() -> dict:
    merged: dict = {}
    for path in sorted(RESULTS.glob("results*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        merged.update(payload.get("databases", {}))
    return merged


def chart_latency(databases: dict, track: str, percentile: str, out: Path) -> bool:
    targets = [(n, e) for n, e in databases.items() if e.get("track") == track]
    if not targets:
        return False

    fig, ax = plt.subplots(figsize=(11, 5.5))
    width = 0.8 / len(targets)
    positions = range(len(WORKLOADS))

    for index, (name, entry) in enumerate(targets):
        values = []
        for workload in WORKLOADS:
            payload = entry.get("workloads", {}).get(workload, {})
            values.append(payload.get("warm", {}).get(percentile) or 0)
        offsets = [p + index * width - 0.4 + width / 2 for p in positions]
        ax.bar(offsets, values, width, label=entry.get("display_name", name),
               color=PALETTE[index % len(PALETTE)], edgecolor="white", linewidth=0.6)

    ax.set_xticks(list(positions))
    ax.set_xticklabels(LABELS)
    ax.set_ylabel(f"{percentile.replace('_ms','').upper()} latency (ms, log scale)")
    ax.set_yscale("log")
    ax.set_title(
        f"{track.capitalize()} track — {percentile.replace('_ms','').upper()} read latency\n"
        + ("0.5 vCPU / 256 MB per engine, loopback network"
           if track == "lab" else "managed endpoint over WAN — includes ~390 ms transport"),
        fontsize=11,
    )
    ax.grid(axis="y", alpha=0.3, linestyle=":")
    ax.set_axisbelow(True)
    ax.legend(fontsize=9, framealpha=0.9)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return True


def chart_concurrency(databases: dict, track: str, out: Path) -> bool:
    targets = [(n, e) for n, e in databases.items() if e.get("track") == track and e.get("mixed")]
    if not targets:
        return False

    fig, ax = plt.subplots(figsize=(9, 5))
    for index, (name, entry) in enumerate(targets):
        clients = [m["clients"] for m in entry["mixed"]]
        qps = [m["throughput_qps"] for m in entry["mixed"]]
        ax.plot(clients, qps, marker="o", linewidth=2,
                label=entry.get("display_name", name), color=PALETTE[index % len(PALETTE)])

    ax.set_xlabel("Concurrent clients")
    ax.set_ylabel("Sustained throughput (queries/sec)")
    ax.set_title(f"{track.capitalize()} track — mixed workload, 90% reads / 10% writes", fontsize=11)
    ax.grid(alpha=0.3, linestyle=":")
    ax.set_axisbelow(True)
    ax.legend(fontsize=9)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return True


def chart_load(out: Path) -> bool:
    path = RESULTS / "load_results.json"
    if not path.exists():
        return False
    loads = json.loads(path.read_text(encoding="utf-8"))
    entries = [(k, v) for k, v in loads.items() if v.get("relationships_per_second")]
    if not entries:
        return False

    fig, ax = plt.subplots(figsize=(9, 5))
    names = [k for k, _ in entries]
    values = [v["relationships_per_second"] for _, v in entries]
    colours = ["#D55E00" if v.get("track") == "cloud" else "#0072B2" for _, v in entries]
    bars = ax.bar(names, values, color=colours, edgecolor="white", linewidth=0.6)
    for bar, value in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, value, f"{value:,.0f}",
                ha="center", va="bottom", fontsize=9)

    ax.set_ylabel("Relationships loaded / sec")
    ax.set_title("Ingest throughput — identical method everywhere (batched UNWIND, batch=1000)\n"
                 "orange = managed endpoint over WAN; blue = local container at 0.5 vCPU / 256 MB",
                 fontsize=10)
    ax.grid(axis="y", alpha=0.3, linestyle=":")
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()

    CHARTS.mkdir(parents=True, exist_ok=True)
    databases = load_all()
    if not databases:
        print("no results found -- run scripts/run_benchmark.py first", file=sys.stderr)
        return 1

    written = []
    for track in ("lab", "cloud"):
        for percentile in ("p50_ms", "p95_ms"):
            out = CHARTS / f"{track}_{percentile.replace('_ms','')}_latency.png"
            if chart_latency(databases, track, percentile, out):
                written.append(out)
        out = CHARTS / f"{track}_concurrency.png"
        if chart_concurrency(databases, track, out):
            written.append(out)

    out = CHARTS / "load_throughput.png"
    if chart_load(out):
        written.append(out)

    for path in written:
        print("wrote", path.relative_to(REPO_ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
