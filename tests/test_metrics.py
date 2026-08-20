"""Tests for the parts of the harness that would fail silently if they were wrong.

These are deliberately not exhaustive unit tests. Each one guards a specific mistake that
would corrupt published numbers without raising an error:

* a percentile definition that quietly disagrees with the documented one
* a dataset join that drops a third of the graph's date properties
* a start-node sample that is not actually reproducible
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmarks.metrics import percentile, summarize  # noqa: E402
from scripts.prepare_dataset import normalise_id, reachable_within  # noqa: E402

PREPARED = REPO_ROOT / "data" / "prepared"
requires_dataset = pytest.mark.skipif(
    not (PREPARED / "manifest.json").exists(),
    reason="run scripts/prepare_dataset.py first",
)


# --- percentiles ----------------------------------------------------------
def test_percentile_is_nearest_rank_not_interpolated():
    """p50 of 1..10 is 5 under nearest-rank; linear interpolation would say 5.5.

    The README states nearest-rank, so this pins the documented behaviour.
    """
    values = list(range(1, 11))
    assert percentile(values, 50) == 5
    assert percentile(values, 95) == 10
    assert percentile(values, 100) == 10


def test_percentile_returns_an_observed_value():
    values = [1.0, 2.0, 100.0]
    assert percentile(values, 95) in values


def test_summarize_reports_the_full_distribution():
    stats = summarize([5.0, 1.0, 3.0, 2.0, 4.0])
    assert stats["n"] == 5
    assert stats["min_ms"] == 1.0
    assert stats["max_ms"] == 5.0
    assert stats["p50_ms"] == 3.0
    assert stats["mean_ms"] == 3.0


def test_summarize_handles_empty_input():
    """A database that failed every operation must produce nulls, not a crash."""
    stats = summarize([])
    assert stats["n"] == 0
    assert stats["p95_ms"] is None


# --- dataset normalisation ------------------------------------------------
def test_normalise_id_restores_stripped_leading_zeros():
    """The edge file stores 2000-2002 arXiv IDs with leading zeros stripped. Without the
    zero-padding the dates join drops every paper from those years."""
    assert normalise_id("1001") == "0001001"
    assert normalise_id("9203201") == "9203201"


def test_normalise_id_strips_cross_listed_prefix():
    """Cross-listed papers appear in the dates file as 11<true_id>."""
    assert normalise_id("119203201") == "9203201"


def test_reachable_within_excludes_the_start_node():
    """Traversal workloads ask for OTHER reachable papers, because the engines disagree
    about whether a path returning to the origin counts. See bolt.py."""
    adjacency = {"a": {"b"}, "b": {"a", "c"}}
    assert reachable_within(adjacency, "a", 1) == {"b"}
    assert reachable_within(adjacency, "a", 2) == {"b", "c"}  # not "a", despite the cycle


def test_reachable_within_handles_self_loops():
    adjacency = {"a": {"a", "b"}}
    assert reachable_within(adjacency, "a", 2) == {"b"}


# --- prepared dataset -----------------------------------------------------
@requires_dataset
def test_year_join_coverage_did_not_regress():
    """The naive string join matches only 62.9% of nodes and silently loses 2000-2002.
    The normalised join reaches 88.5%. This guards that fix."""
    manifest = json.loads((PREPARED / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["graph"]["year_coverage_pct"] > 85.0
    years = manifest["graph"]["year_histogram"]
    assert "2002" in years, "2000-2002 papers missing -- the zero-padding fix regressed"


@requires_dataset
def test_ground_truth_covers_every_start_node():
    expected = json.loads((PREPARED / "expected.json").read_text(encoding="utf-8"))
    starts = {str(e["id"]) for e in json.loads((PREPARED / "start_nodes.json").read_text(encoding="utf-8"))}
    for depth in ("1", "2", "3"):
        assert set(expected["traversal_counts"][depth]) == starts


@requires_dataset
def test_start_node_selection_is_reproducible():
    """Every database must be queried from the identical start-node list; if this sample
    were not deterministic the comparison would be invalid."""
    from collections import Counter

    from scripts.prepare_dataset import pick_start_nodes

    degrees = Counter({str(i): i % 17 + 1 for i in range(5000)})
    first = pick_start_nodes(degrees, seed=42, count=200)
    second = pick_start_nodes(degrees, seed=42, count=200)
    different = pick_start_nodes(degrees, seed=43, count=200)
    assert first == second
    assert first != different
