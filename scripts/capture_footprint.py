"""Capture container resource footprint for the lab track.

The engines' own introspection is patchy -- Neo4j and Memgraph do not expose store size
without APOC, and CognoDB's managed endpoint exposes neither. Docker does expose real
memory accounting for the four local containers, so it is recorded here rather than left as
"not observable" when an honest measurement is available.

This is a separate script, not part of run_benchmark.py, because it observes the container
rather than the database and should be runnable without re-measuring anything.

Usage:
    python scripts/capture_footprint.py            # after loading, before or after a run
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS = REPO_ROOT / "results"

# container name -> target name in config/databases.yaml
CONTAINERS = {
    "bench-neo4j": "neo4j",
    "bench-memgraph": "memgraph",
    "bench-falkordb": "falkordb",
    "bench-arangodb": "arangodb",
}


def docker(args: list[str]) -> str:
    return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.DEVNULL).strip()


def main() -> int:
    try:
        docker(["info", "--format", "{{.ServerVersion}}"])
    except Exception:  # noqa: BLE001
        print("docker is not available -- skipping container footprint", file=sys.stderr)
        return 1

    captured: dict[str, dict] = {}
    for container, target in CONTAINERS.items():
        try:
            stats = docker([
                "stats", container, "--no-stream", "--format",
                "{{.MemUsage}}|{{.MemPerc}}|{{.CPUPerc}}",
            ])
            mem_usage, mem_perc, cpu_perc = stats.split("|")
            limits = docker([
                "inspect", container, "--format",
                "{{.HostConfig.NanoCpus}}|{{.HostConfig.Memory}}|{{.Config.Image}}",
            ])
            nano_cpus, memory, image = limits.split("|")
            captured[target] = {
                "container": container,
                "image": image,
                "enforced_vcpu": int(nano_cpus) / 1e9,
                "enforced_memory_mb": int(memory) / 1024 / 1024,
                "memory_in_use": mem_usage.split("/")[0].strip(),
                "memory_percent_of_cap": mem_perc,
                "cpu_percent_at_sample": cpu_perc,
            }
        except Exception as exc:  # noqa: BLE001
            captured[target] = {"container": container, "status": f"not observable ({type(exc).__name__})"}

    payload = {
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "note": (
            "Observed from the Docker daemon, not from the databases themselves. "
            "Disk is not capped per container: --storage-opt size= needs a storage driver "
            "unavailable on Docker Desktop for Windows. The dataset is ~50 MB on disk, well "
            "under the 1 GB free-tier limit on every engine, so disk was never binding."
        ),
        "containers": captured,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "container_footprint.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"wrote {out}\n")
    print(f"{'target':12s} {'vCPU':>6s} {'cap MB':>8s} {'in use':>12s} {'% of cap':>9s}")
    for target, info in captured.items():
        if "status" in info:
            print(f"{target:12s} {info['status']}")
            continue
        print(f"{target:12s} {info['enforced_vcpu']:>6.1f} {info['enforced_memory_mb']:>8.0f} "
              f"{info['memory_in_use']:>12s} {info['memory_percent_of_cap']:>9s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
