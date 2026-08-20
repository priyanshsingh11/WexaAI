"""Run the benchmark suite and write results.

Measurement order per database, per workload:
  1. cold samples  -- the first N operations after connecting, recorded separately
  2. warm-up       -- untimed, discarded
  3. measured run  -- the numbers that get published

Then the mixed read/write concurrency workload at each requested client count.

Everything is written to results/: raw per-operation samples (so any percentile can be
recomputed independently), an aggregated results.json, and a manifest recording the client
machine, driver versions, dataset hashes and the measured network baseline.

Usage:
    python scripts/run_benchmark.py --db cognodb --workload one_hop --iterations 20
    python scripts/run_benchmark.py --track lab
    python scripts/run_benchmark.py --db all --concurrency 1,10,40
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil
from dotenv import load_dotenv

from benchmarks.common import GraphAdapter, run_workload, time_call
from benchmarks.metrics import summarize
from benchmarks.registry import REPO_ROOT, build_adapter, load_config, selected_targets
from benchmarks.workloads import build_workloads

RESULTS = REPO_ROOT / "results"
PREPARED = REPO_ROOT / "data" / "prepared"

log = logging.getLogger("bench.run")


# --------------------------------------------------------------------------
# Network baseline
# --------------------------------------------------------------------------
def tcp_baseline(target_host: str, port: int, samples: int = 20) -> dict[str, Any]:
    """Raw TCP handshake latency -- no TLS, no protocol, no query.

    This is what separates transport cost from engine cost. It matters enormously here:
    the managed CognoDB endpoint is only offered in us-east while the client runs in India,
    so its floor is a few hundred milliseconds, whereas the Docker targets answer over
    loopback. Without this number the two tracks could be mistaken for comparable.
    """
    try:
        ip = socket.gethostbyname(target_host)
    except Exception as exc:  # noqa: BLE001
        return {"status": f"not observable ({type(exc).__name__})"}

    latencies: list[float] = []
    failures = 0
    for _ in range(samples):
        sock = socket.socket()
        sock.settimeout(10)
        start = time.perf_counter_ns()
        try:
            sock.connect((ip, port))
            latencies.append((time.perf_counter_ns() - start) / 1e6)
        except Exception:  # noqa: BLE001
            failures += 1
        finally:
            sock.close()
    if not latencies:
        return {"status": "unreachable", "failures": failures}
    stats = summarize(latencies)
    stats["failures"] = failures
    stats["resolved_ip"] = ip
    return stats


def endpoint_of(spec: dict[str, Any]) -> tuple[str, int] | None:
    """Host and port for the baseline probe, derived from whichever env var the target uses."""
    import os

    env = spec.get("env", {})
    if "uri" in env:
        uri = os.environ.get(env["uri"], "")
        if not uri:
            return None
        parsed = urlparse(uri)
        return parsed.hostname or "", parsed.port or 7687
    if "url" in env:
        url = os.environ.get(env["url"], "")
        if not url:
            return None
        parsed = urlparse(url)
        return parsed.hostname or "", parsed.port or 8529
    if "host" in env:
        return os.environ.get(env["host"], "localhost"), int(os.environ.get(env.get("port", ""), "") or 6379)
    return None


# --------------------------------------------------------------------------
# Mixed read/write concurrency workload
# --------------------------------------------------------------------------
def run_mixed(
    adapter: GraphAdapter,
    start_nodes: list[str],
    clients: int,
    duration: float,
    read_ratio: float,
) -> dict[str, Any]:
    """Sustained mixed load at a stated client concurrency and read/write ratio.

    Threads rather than asyncio: all three drivers are synchronous, and a thread pool is
    trivially explainable. Each worker holds its own session (adapters keep sessions
    thread-local), so the pool measures server concurrency rather than client contention.

    Writes land in an isolated BenchWrite namespace and are deleted afterwards, so the read
    dataset the other workloads measure is never mutated.
    """
    stop = threading.Event()
    read_latencies: list[list[float]] = [[] for _ in range(clients)]
    write_latencies: list[list[float]] = [[] for _ in range(clients)]
    errors = [0] * clients

    # Reads per 10 operations. Using a fixed 10-op cycle keeps the realised ratio exact at
    # every client count; deriving it from a shared counter stride made the actual mix
    # drift with the number of clients.
    reads_per_cycle = round(read_ratio * 10)

    def worker(index: int) -> None:
        # Each worker walks its own slice of the start-node list so the pool spreads across
        # the graph instead of hammering one node, and the sequence is reproducible.
        counter = 0
        node_cursor = index
        while not stop.is_set():
            if counter % 10 < reads_per_cycle:
                sample = time_call(adapter.one_hop, start_nodes[node_cursor % len(start_nodes)])
                node_cursor += clients
                if sample.ok:
                    read_latencies[index].append(sample.latency_ms)
            else:
                sample = time_call(adapter.write_op, f"c{clients}-w{index}-{counter}")
                if sample.ok:
                    write_latencies[index].append(sample.latency_ms)
            if not sample.ok:
                errors[index] += 1
            counter += 1

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=clients) as pool:
        futures = [pool.submit(worker, i) for i in range(clients)]
        time.sleep(duration)
        stop.set()
        for future in futures:
            future.result()
    elapsed = time.perf_counter() - started

    reads = [x for sub in read_latencies for x in sub]
    writes = [x for sub in write_latencies for x in sub]
    total_ops = len(reads) + len(writes)

    cleaned = 0
    try:
        cleaned = adapter.cleanup_writes()
    except Exception as exc:  # noqa: BLE001
        log.warning("%s: write cleanup failed: %s", adapter.name, exc)

    return {
        "clients": clients,
        "duration_seconds": round(elapsed, 2),
        "read_ratio": read_ratio,
        "operations": total_ops,
        "reads": len(reads),
        "writes": len(writes),
        "errors": sum(errors),
        "throughput_qps": round(total_ops / elapsed, 2) if elapsed else 0,
        "read_latency": summarize(reads),
        "write_latency": summarize(writes),
        "writes_cleaned_up": cleaned,
    }


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------
def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    def git_sha() -> str:
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL
            ).strip()
        except Exception:  # noqa: BLE001
            return "not available (not a git checkout or git missing)"

    import importlib.metadata as md

    versions = {}
    for package in ("neo4j", "falkordb", "python-arango", "redis"):
        try:
            versions[package] = md.version(package)
        except Exception:  # noqa: BLE001
            versions[package] = "not installed"

    dataset_manifest = {}
    manifest_path = PREPARED / "manifest.json"
    if manifest_path.exists():
        dataset_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    return {
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "git_sha": git_sha(),
        "client_machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "logical_cpus": psutil.cpu_count(logical=True),
            "physical_cpus": psutil.cpu_count(logical=False),
            "total_ram_gb": round(psutil.virtual_memory().total / 1e9, 2),
        },
        "driver_versions": versions,
        "settings": {
            "iterations": args.iterations,
            "warmup": args.warmup,
            "cold_samples": args.cold,
            "concurrency_levels": args.concurrency,
            "mixed_duration_seconds": args.duration,
            "read_ratio": args.read_ratio,
        },
        "dataset": dataset_manifest.get("graph", {}),
        "dataset_hashes": dataset_manifest.get("outputs", {}),
    }


# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="all")
    parser.add_argument("--track", choices=["lab", "cloud"])
    parser.add_argument("--workload", default="all", help="Workload name(s), comma separated, or 'all'.")
    parser.add_argument("--iterations", type=int, default=100,
                        help="Measured iterations per read workload (assignment suggests >=100).")
    parser.add_argument("--warmup", type=int, default=20, help="Untimed warm-up iterations.")
    parser.add_argument("--cold", type=int, default=5, help="Cold samples captured before warm-up.")
    parser.add_argument("--concurrency", default="10",
                        help="Client counts for the mixed workload, e.g. 1,10,40. Empty to skip.")
    parser.add_argument("--duration", type=float, default=30.0, help="Mixed workload seconds per level.")
    parser.add_argument("--read-ratio", type=float, default=0.9, dest="read_ratio")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv(REPO_ROOT / ".env")

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_dir = RESULTS / "raw" / run_id
    raw_dir.mkdir(parents=True, exist_ok=True)

    config = load_config()
    targets = selected_targets(config, args.db, args.track)
    if not targets:
        log.error("no targets selected -- check --db/--track and your .env")
        return 1

    workloads = build_workloads()
    if args.workload != "all":
        wanted = {w.strip() for w in args.workload.split(",")}
        workloads = [w for w in workloads if w.name in wanted]
        if not workloads:
            log.error("no workload matched %r", args.workload)
            return 1

    levels = [int(c) for c in args.concurrency.split(",") if c.strip()] if args.concurrency else []
    start_nodes = [str(e["id"]) for e in json.loads((PREPARED / "start_nodes.json").read_text(encoding="utf-8"))]

    manifest = build_manifest(args)
    results: dict[str, Any] = {"run_id": run_id, "manifest": manifest, "databases": {}}

    for name, spec in targets.items():
        log.info("=========== %s (%s track) ===========", name, spec.get("track"))
        entry: dict[str, Any] = {
            "display_name": spec.get("display_name", name),
            "track": spec.get("track"),
            "tier": spec.get("tier", {}),
            "dialect": None,
            "workloads": {},
            "mixed": [],
        }

        endpoint = endpoint_of(spec)
        if endpoint and endpoint[0]:
            log.info("%s: measuring network baseline to %s:%s ...", name, endpoint[0], endpoint[1])
            entry["network_baseline_tcp_ms"] = tcp_baseline(endpoint[0], endpoint[1])
            base = entry["network_baseline_tcp_ms"]
            if base.get("p50_ms") is not None:
                log.info("%s: TCP p50=%.1f ms p95=%.1f ms", name, base["p50_ms"], base["p95_ms"])

        adapter = build_adapter(name, spec)
        try:
            adapter.connect()
            entry["dialect"] = adapter.dialect
        except Exception as exc:  # noqa: BLE001
            log.error("%s: CONNECT FAILED -- %s", name, exc)
            entry["error"] = f"connect failed: {type(exc).__name__}: {exc}"
            results["databases"][name] = entry
            continue

        try:
            for workload in workloads:
                log.info("%s: %s (%d iterations, %d warm-up) ...", name, workload.name,
                         args.iterations, args.warmup)
                result = run_workload(
                    adapter=adapter,
                    workload_name=workload.name,
                    fn=workload.bind(adapter),
                    args_cycle=list(workload.args),
                    iterations=args.iterations,
                    warmup=args.warmup,
                    expected=workload.expected,
                    cold=args.cold,
                )

                stats = summarize(result.latencies)
                payload = {
                    "description": workload.description,
                    "category": workload.category,
                    "warm": stats,
                    "cold": summarize([s.latency_ms for s in result.cold_samples if s.ok]),
                    "correctness": result.correctness,
                    "mismatches": result.mismatches[:5],
                    "failures": sum(1 for s in result.samples if not s.ok),
                    "error": result.error,
                }
                entry["workloads"][workload.name] = payload

                # Raw samples: one line per operation, so percentiles are recomputable.
                raw_path = raw_dir / name
                raw_path.mkdir(parents=True, exist_ok=True)
                with (raw_path / f"{workload.name}.jsonl").open("w", encoding="utf-8") as handle:
                    for phase, samples in (("cold", result.cold_samples), ("warm", result.samples)):
                        for sample in samples:
                            handle.write(json.dumps({
                                "phase": phase,
                                "latency_ms": round(sample.latency_ms, 4),
                                "result": sample.result,
                                "ok": sample.ok,
                                "error": sample.error,
                            }) + "\n")

                flag = "" if result.correctness == "ok" else f"  [{result.correctness}]"
                if stats["n"]:
                    log.info("%s: %s p50=%.2f ms p95=%.2f ms n=%d%s", name, workload.name,
                             stats["p50_ms"], stats["p95_ms"], stats["n"], flag)
                else:
                    log.warning("%s: %s produced no successful samples (%s)", name, workload.name, result.error)

            for clients in levels:
                log.info("%s: mixed workload, %d clients, %.0fs, %.0f%% reads ...",
                         name, clients, args.duration, args.read_ratio * 100)
                mixed = run_mixed(adapter, start_nodes, clients, args.duration, args.read_ratio)
                entry["mixed"].append(mixed)
                log.info("%s: %d clients -> %.1f qps (%d errors)", name, clients,
                         mixed["throughput_qps"], mixed["errors"])

            try:
                entry["footprint"] = adapter.footprint()
            except Exception as exc:  # noqa: BLE001
                entry["footprint"] = {"status": f"not observable ({type(exc).__name__}: {exc})"}

        except Exception as exc:  # noqa: BLE001
            log.exception("%s: RUN FAILED", name)
            entry["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                adapter.close()
            except Exception:  # noqa: BLE001
                pass

        results["databases"][name] = entry
        RESULTS.mkdir(parents=True, exist_ok=True)
        (RESULTS / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    (RESULTS / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    log.info("wrote %s", RESULTS / "results.json")
    log.info("raw samples in %s", raw_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
