"""The logical workloads, defined once and applied to every database.

Each workload names an adapter method plus the argument sequence it is driven with. The
arguments are fixed data read from data/prepared/, so every database issues the identical
query sequence in the identical order -- the single most important fairness control in the
harness. If each engine drew its own random start nodes it would face a different fan-out,
and the resulting p95 values would not be comparable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from benchmarks.common import GraphAdapter

PREPARED = Path(__file__).resolve().parents[1] / "data" / "prepared"


@dataclass
class Workload:
    name: str
    description: str
    method: str                       # adapter method to call
    args: Sequence[Any]               # cycled deterministically across iterations
    expected: dict[Any, Any] | None   # ground truth, keyed by argument
    category: str

    def bind(self, adapter: GraphAdapter) -> Callable[[Any], Any]:
        """Return a one-argument callable so the runner has a single code path.

        The aggregation workload scans the whole label and takes no parameter; rather than
        giving every adapter a dummy argument, its binding just swallows the cycled value.
        """
        fn = getattr(adapter, self.method)
        if list(self.args) == [None]:
            return lambda _arg=None: fn()
        return fn


def _load_json(name: str) -> Any:
    return json.loads((PREPARED / name).read_text(encoding="utf-8"))


def build_workloads() -> list[Workload]:
    """Assemble the workload set from the prepared dataset and its ground truth."""
    start_nodes = [str(entry["id"]) for entry in _load_json("start_nodes.json")]
    expected = _load_json("expected.json")

    years = sorted(expected["filtered_lookup_counts"].keys())
    aggregation_expected = len(expected["aggregation_year_histogram"])

    return [
        Workload(
            name="one_hop",
            description="Count distinct papers cited directly by a start paper (1 hop).",
            method="one_hop",
            args=start_nodes,
            expected={k: v for k, v in expected["traversal_counts"]["1"].items()},
            category="traversal",
        ),
        Workload(
            name="two_hop",
            description="Count distinct papers reachable within 2 citation hops.",
            method="two_hop",
            args=start_nodes,
            expected={k: v for k, v in expected["traversal_counts"]["2"].items()},
            category="traversal",
        ),
        Workload(
            name="three_hop",
            description="Count distinct papers reachable within 3 citation hops.",
            method="three_hop",
            args=start_nodes,
            expected={k: v for k, v in expected["traversal_counts"]["3"].items()},
            category="traversal",
        ),
        Workload(
            name="point_lookup",
            description="Fetch one paper by its indexed id and return its publication year.",
            method="point_lookup",
            args=start_nodes,
            expected=expected["point_lookup_year"],
            category="lookup",
        ),
        Workload(
            name="filtered_lookup",
            description="Count papers published in a given year, using the index on `year`.",
            method="filtered_lookup",
            args=years,
            expected=expected["filtered_lookup_counts"],
            category="lookup",
        ),
        Workload(
            name="aggregation",
            description="Group all papers by year and count them; returns the number of year buckets.",
            method="aggregation",
            # Takes no argument, but the runner cycles an argument sequence uniformly, so a
            # single-element list keeps one code path for every workload.
            args=[None],
            expected={None: aggregation_expected},
            category="aggregation",
        ),
    ]
