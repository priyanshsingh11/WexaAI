"""The database-agnostic core: adapter contract, timing loop, and correctness checking.

Design intent -- every database sees the *same logical workload*, never the same query
string. The engines here speak three different dialects (Cypher over Bolt, Cypher over
RESP, AQL over HTTP), so forcing identical syntax would mean writing unnatural queries for
somebody. Instead each adapter implements the same small set of operations idiomatically,
and the harness verifies they return the same answers.

That verification is what makes the latency numbers trustworthy. Every read operation
returns a comparable result value which is checked against ground truth precomputed in
scripts/prepare_dataset.py. A database whose 2-hop query returns a different count is not
running an equivalent workload, and its latency is reported as INVALID rather than being
quietly published alongside the others.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

log = logging.getLogger("bench")

# NOTE ON THE TRAVERSAL RESULT CAP
# An earlier version of this harness appended `LIMIT 5000` to the traversal queries as a
# safety valve. It was removed because it did nothing: the traversals return a single
# aggregated count row, so a row LIMIT caps the result set at one row rather than bounding
# the expansion. Keeping it would have implied a protection that did not exist.
#
# No cap is applied. It is not needed on this dataset: the widest 3-hop neighbourhood
# reaches 3,898 distinct nodes (measured across all 200 start nodes), which every engine
# handles inside 256 MB. A denser graph or a different seed would need a real bound --
# expressed as a genuine traversal limit, not a row limit.

_SECRET_RE = re.compile(r"(?i)(://[^:/@\s]+:)[^@/\s]+(@)")


def redact(text: str) -> str:
    """Strip an embedded password from a URI before it reaches a log line or the manifest."""
    return _SECRET_RE.sub(r"\1***\2", str(text))


@dataclass
class LoadResult:
    """Outcome of ingesting the dataset into one database."""

    nodes: int
    relationships: int
    node_seconds: float
    relationship_seconds: float
    total_seconds: float
    method: str
    batch_size: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "nodes": self.nodes,
            "relationships": self.relationships,
            "node_load_seconds": round(self.node_seconds, 3),
            "relationship_load_seconds": round(self.relationship_seconds, 3),
            "total_seconds": round(self.total_seconds, 3),
            "nodes_per_second": round(self.nodes / self.node_seconds, 1) if self.node_seconds else None,
            "relationships_per_second": (
                round(self.relationships / self.relationship_seconds, 1) if self.relationship_seconds else None
            ),
            "method": self.method,
            "batch_size": self.batch_size,
        }


@dataclass
class Sample:
    """One timed operation."""

    latency_ms: float
    result: Any
    ok: bool = True
    error: str | None = None


@dataclass
class WorkloadResult:
    database: str
    workload: str
    samples: list[Sample] = field(default_factory=list)
    warmup_iterations: int = 0
    cold_samples: list[Sample] = field(default_factory=list)
    correctness: str = "unchecked"      # ok | MISMATCH | unchecked
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def latencies(self) -> list[float]:
        return [s.latency_ms for s in self.samples if s.ok]


class GraphAdapter(ABC):
    """One implementation per database dialect.

    Sessions are thread-local. This matters twice over: opening a session per query costs
    an extra network round trip (measured on CognoDB at roughly 2x the latency of a
    persistent session), and the concurrency workload needs each worker thread to hold its
    own session because Bolt sessions are not thread-safe.
    """

    name: str
    dialect: str

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config
        self._local = threading.local()

    # --- lifecycle ---------------------------------------------------------
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def ping(self) -> Any:
        """Cheapest possible round trip. Establishes the network baseline that separates
        transport cost from engine cost -- essential here, because the managed CognoDB
        endpoint is reached over a WAN link while the competitors run on loopback."""

    # --- schema and ingest -------------------------------------------------
    @abstractmethod
    def reset(self) -> None:
        """Drop all benchmark data so a run always starts from a known-empty database."""

    @abstractmethod
    def setup_schema(self) -> list[str]:
        """Create indexes. Returns the statements actually executed so the README can
        state exactly which properties were indexed on each platform."""

    @abstractmethod
    def load(self, nodes: Sequence[tuple[str, str]], edges: Sequence[tuple[str, str]],
             batch_size: int) -> LoadResult: ...

    # --- read operations ---------------------------------------------------
    @abstractmethod
    def one_hop(self, node_id: str) -> int: ...

    @abstractmethod
    def two_hop(self, node_id: str) -> int: ...

    @abstractmethod
    def three_hop(self, node_id: str) -> int: ...

    @abstractmethod
    def point_lookup(self, node_id: str) -> str: ...

    @abstractmethod
    def filtered_lookup(self, year: str) -> int: ...

    @abstractmethod
    def aggregation(self) -> int: ...

    # --- write / footprint -------------------------------------------------
    @abstractmethod
    def write_op(self, key: str) -> int:
        """Insert one node into an isolated namespace used only by the mixed workload, so
        the measured read dataset is never mutated and runs stay repeatable."""

    @abstractmethod
    def cleanup_writes(self) -> int: ...

    @abstractmethod
    def footprint(self) -> dict[str, Any]:
        """Whatever the platform genuinely exposes. Anything it does not expose is
        reported as 'not observable' rather than estimated."""


def time_call(fn, *args) -> Sample:
    """Time one operation. Failures are recorded, never silently dropped -- a database
    that errors on 30% of queries would otherwise show a flatteringly clean p95."""
    start = time.perf_counter_ns()
    try:
        value = fn(*args)
        return Sample((time.perf_counter_ns() - start) / 1e6, value, True, None)
    except Exception as exc:  # noqa: BLE001 - any driver error is a data point
        return Sample((time.perf_counter_ns() - start) / 1e6, None, False, f"{type(exc).__name__}: {exc}")


def run_workload(
    adapter: GraphAdapter,
    workload_name: str,
    fn,
    args_cycle: Sequence[Any],
    iterations: int,
    warmup: int,
    expected: dict[Any, Any] | None = None,
    cold: int = 0,
) -> WorkloadResult:
    """Cold samples, then warm-up, then the measured run.

    Arguments are cycled deterministically (args_cycle[i % len]) rather than drawn at
    random per iteration, so every database issues the identical query sequence in the
    identical order. Randomising per database would let one engine draw an easier set of
    start nodes than another.
    """
    result = WorkloadResult(database=adapter.name, workload=workload_name, warmup_iterations=warmup)

    # Cold-start samples are captured before warm-up and reported separately; mixing them
    # into the warm percentiles would let a slow first query distort the whole tail.
    for i in range(cold):
        result.cold_samples.append(time_call(fn, args_cycle[i % len(args_cycle)]))

    for i in range(warmup):
        time_call(fn, args_cycle[i % len(args_cycle)])

    for i in range(iterations):
        arg = args_cycle[i % len(args_cycle)]
        sample = time_call(fn, arg)
        result.samples.append(sample)
        if expected is not None and sample.ok:
            want = expected.get(arg) if isinstance(expected, dict) else expected
            if want is not None and sample.result != want:
                if len(result.mismatches) < 10:
                    result.mismatches.append({"arg": arg, "expected": want, "actual": sample.result})

    failures = sum(1 for s in result.samples if not s.ok)
    if failures:
        first = next(s.error for s in result.samples if not s.ok)
        log.warning("%s/%s: %d/%d operations failed (first: %s)",
                    adapter.name, workload_name, failures, iterations, first)
        result.error = first
    if expected is not None:
        result.correctness = "MISMATCH" if result.mismatches else ("ok" if result.samples else "unchecked")
    return result


def iter_batches(rows: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(rows), size):
        yield rows[start:start + size]
