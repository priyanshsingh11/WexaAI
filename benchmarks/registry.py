"""Build adapters from config/databases.yaml plus the environment.

Keeping target definitions in YAML rather than in code means adding a database is a config
edit, and -- more importantly for this assignment -- it puts every platform's advertised
tier spec in one auditable place that the README tables are generated from.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from benchmarks.common import GraphAdapter

log = logging.getLogger("bench.registry")

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config" / "databases.yaml"


def load_config(path: Path | None = None) -> dict[str, Any]:
    return yaml.safe_load((path or CONFIG_PATH).read_text(encoding="utf-8"))


def _resolve_env(env_map: dict[str, str]) -> dict[str, str]:
    """Map config keys to their environment values. Missing vars become empty strings so a
    partially configured target reports a clear skip rather than a KeyError mid-run."""
    return {key: os.environ.get(var, "") for key, var in env_map.items()}


def build_adapter(name: str, spec: dict[str, Any]) -> GraphAdapter:
    settings = _resolve_env(spec.get("env", {}))
    settings["flavour"] = spec.get("flavour", "neo4j")

    adapter_kind = spec["adapter"]
    if adapter_kind == "bolt":
        from benchmarks.adapters.bolt import BoltAdapter

        return BoltAdapter(name, settings)
    if adapter_kind == "falkordb":
        from benchmarks.adapters.falkordb import FalkorDBAdapter

        return FalkorDBAdapter(name, settings)
    if adapter_kind == "arangodb":
        from benchmarks.adapters.arangodb import ArangoDBAdapter

        return ArangoDBAdapter(name, settings)
    raise ValueError(f"unknown adapter kind {adapter_kind!r} for target {name!r}")


def selected_targets(
    config: dict[str, Any], requested: str = "all", track: str | None = None
) -> dict[str, dict[str, Any]]:
    """Resolve --db / --track into a concrete target set.

    An optional target with no credentials configured (Neo4j Aura, unless the user
    provisioned one) is skipped silently rather than counted as a failure.
    """
    targets: dict[str, dict[str, Any]] = config["targets"]

    if requested != "all":
        wanted = [name.strip() for name in requested.split(",")]
        unknown = [name for name in wanted if name not in targets]
        if unknown:
            raise SystemExit(f"unknown target(s): {', '.join(unknown)}. "
                             f"Known: {', '.join(targets)}")
        chosen = {name: targets[name] for name in wanted}
    else:
        chosen = dict(targets)

    if track:
        chosen = {n: s for n, s in chosen.items() if s.get("track") == track}

    resolved: dict[str, dict[str, Any]] = {}
    for name, spec in chosen.items():
        env_values = _resolve_env(spec.get("env", {}))
        primary = env_values.get("uri") or env_values.get("host") or env_values.get("url")
        if not primary:
            if spec.get("optional"):
                log.info("skipping optional target %s (no credentials configured)", name)
                continue
            log.warning("skipping %s: required environment variables are not set", name)
            continue
        resolved[name] = spec
    return resolved
