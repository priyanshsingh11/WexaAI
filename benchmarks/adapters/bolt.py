"""Bolt/Cypher adapter -- covers CognoDB Cloud, Neo4j Community, Neo4j Aura and Memgraph.

Four of the six targets speak Bolt and Cypher, so they share one adapter rather than four
near-identical ones. The assignment's own setup instructions confirm this is the intended
path for CognoDB: "connect with an official Neo4j driver ... no other code changes are
needed."

The dialect differences that do exist are narrow and handled by flags rather than by
forking the class:

* Index DDL. Neo4j 5 and CognoDB take `CREATE INDEX ... IF NOT EXISTS FOR (n:L) ON (n.p)`;
  Memgraph takes `CREATE INDEX ON :L(p)` and has no IF NOT EXISTS.
* Memgraph does not support `CALL { } IN TRANSACTIONS` and does not need it -- plain
  batched UNWIND is the idiomatic load path everywhere here.

Everything else -- the traversal, lookup and aggregation queries -- is byte-identical
Cypher across all four, which is the strongest possible form of workload equivalence.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Sequence

from neo4j import GraphDatabase

from benchmarks.common import GraphAdapter, LoadResult, iter_batches, redact

log = logging.getLogger("bench.bolt")

# --- the logical workloads, in Cypher ---------------------------------------
# Traversals count DISTINCT papers reachable within N hops, EXCLUDING the start paper.
#
# The exclusion is deliberate and load-bearing. Measured on CognoDB: the explicit pattern
# (a)-[:CITES]->(b)-[:CITES]->(a) finds a 2-hop cycle back to the start node, but the
# variable-length form (a)-[:CITES*1..2]->(a) does not -- CognoDB drops paths that return
# to the origin, where Neo4j keeps them. The graph has 44 self-loops and 9 of the 200 start
# nodes sit on a <=3-hop cycle, so that difference silently changed the answer on ~5% of
# start nodes until the ground-truth check caught it.
#
# Rather than pick a winner between two defensible semantics, the workload asks the
# unambiguous question -- "which OTHER papers are reachable within N hops" -- which every
# engine answers identically and which is the more natural question anyway.
Q_ONE_HOP = "MATCH (n:Paper {id: $id})-[:CITES*1..1]->(m) WHERE m <> n RETURN count(DISTINCT m) AS c"
Q_TWO_HOP = "MATCH (n:Paper {id: $id})-[:CITES*1..2]->(m) WHERE m <> n RETURN count(DISTINCT m) AS c"
Q_THREE_HOP = "MATCH (n:Paper {id: $id})-[:CITES*1..3]->(m) WHERE m <> n RETURN count(DISTINCT m) AS c"
Q_POINT = "MATCH (n:Paper {id: $id}) RETURN n.year AS year"
Q_FILTERED = "MATCH (n:Paper) WHERE n.year = $year RETURN count(n) AS c"
Q_AGGREGATION = "MATCH (n:Paper) WHERE n.year IS NOT NULL RETURN n.year AS year, count(*) AS c ORDER BY year"
Q_WRITE = "CREATE (w:BenchWrite {key: $key}) RETURN 1 AS c"
Q_CLEANUP = "MATCH (w:BenchWrite) DETACH DELETE w RETURN count(w) AS c"


class BoltAdapter(GraphAdapter):
    dialect = "cypher-bolt"

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.uri: str = config["uri"]
        self.user: str = config.get("user") or ""
        self.password: str = config.get("password") or ""
        # Memgraph's default image runs without auth and rejects index DDL written in the
        # Neo4j 5 grammar; both differences are declared in config/databases.yaml.
        self.flavour: str = config.get("flavour", "neo4j")
        self._driver = None

    # --- lifecycle ---------------------------------------------------------
    def connect(self) -> None:
        auth = (self.user, self.password) if self.user else None
        self._driver = GraphDatabase.driver(self.uri, auth=auth)
        self._driver.verify_connectivity()
        log.info("%s: connected to %s", self.name, redact(self.uri))

    def close(self) -> None:
        session = getattr(self._local, "session", None)
        if session is not None:
            session.close()
            self._local.session = None
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    @property
    def session(self):
        """One long-lived session per thread.

        Measured on CognoDB: driver.execute_query() (a fresh session per call) costs a p50
        of ~891 ms versus ~438 ms on a persistent session, because session acquisition adds
        a round trip. Over a 390 ms WAN link that overhead would have doubled every
        reported latency for no reason other than driver usage. Bolt sessions are not
        thread-safe, so each concurrency worker gets its own.
        """
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._driver.session()
            self._local.session = session
        return session

    def _run(self, query: str, **params) -> list[dict[str, Any]]:
        return [record.data() for record in self.session.run(query, **params)]

    def ping(self) -> int:
        return self._run("RETURN 1 AS c")[0]["c"]

    # --- schema and ingest -------------------------------------------------
    def reset(self) -> None:
        # Batched delete: a single unbounded DETACH DELETE over 420k relationships will
        # exhaust the 256 MB transaction memory on every engine tested here.
        while True:
            rows = self._run(
                "MATCH (n) WITH n LIMIT 10000 DETACH DELETE n RETURN count(n) AS c"
            )
            if not rows or rows[0]["c"] == 0:
                break

    def setup_schema(self) -> list[str]:
        if self.flavour == "memgraph":
            statements = ["CREATE INDEX ON :Paper(id)", "CREATE INDEX ON :Paper(year)"]
        else:
            statements = [
                "CREATE INDEX paper_id IF NOT EXISTS FOR (n:Paper) ON (n.id)",
                "CREATE INDEX paper_year IF NOT EXISTS FOR (n:Paper) ON (n.year)",
            ]
        executed = []
        for statement in statements:
            try:
                self._run(statement)
                executed.append(statement)
            except Exception as exc:  # noqa: BLE001
                # An already-existing index is fine; anything else is worth surfacing
                # because an unindexed point lookup would silently become a full scan.
                if "already exist" in str(exc).lower() or "already exists" in str(exc).lower():
                    executed.append(f"{statement}  -- already existed")
                else:
                    log.warning("%s: index statement failed: %s -- %s", self.name, statement, exc)
        return executed

    def load(self, nodes: Sequence[tuple[str, str]], edges: Sequence[tuple[str, str]],
             batch_size: int) -> LoadResult:
        """Batched UNWIND -- the same logical method on every platform.

        Deliberately NOT each platform's fastest native bulk importer. neo4j-admin import
        or arangoimport would post better throughput numbers, but they would be measuring
        four different tools rather than four databases ingesting the same way. The README
        documents that faster native paths exist for each engine.
        """
        overall = time.perf_counter()

        node_start = time.perf_counter()
        for batch in iter_batches(nodes, batch_size):
            self._run(
                "UNWIND $rows AS row CREATE (n:Paper {id: row.id, year: row.year})",
                rows=[{"id": nid, "year": year or None} for nid, year in batch],
            )
        node_seconds = time.perf_counter() - node_start

        rel_start = time.perf_counter()
        for batch in iter_batches(edges, batch_size):
            self._run(
                "UNWIND $rows AS row "
                "MATCH (a:Paper {id: row.src}) MATCH (b:Paper {id: row.dst}) "
                "CREATE (a)-[:CITES]->(b)",
                rows=[{"src": src, "dst": dst} for src, dst in batch],
            )
        rel_seconds = time.perf_counter() - rel_start

        return LoadResult(
            nodes=len(nodes),
            relationships=len(edges),
            node_seconds=node_seconds,
            relationship_seconds=rel_seconds,
            total_seconds=time.perf_counter() - overall,
            method="driver batched UNWIND (identical batch size on every platform)",
            batch_size=batch_size,
        )

    # --- read operations ---------------------------------------------------
    def one_hop(self, node_id: str) -> int:
        return self._run(Q_ONE_HOP, id=node_id)[0]["c"]

    def two_hop(self, node_id: str) -> int:
        return self._run(Q_TWO_HOP, id=node_id)[0]["c"]

    def three_hop(self, node_id: str) -> int:
        return self._run(Q_THREE_HOP, id=node_id)[0]["c"]

    def point_lookup(self, node_id: str) -> str:
        rows = self._run(Q_POINT, id=node_id)
        return (rows[0]["year"] or "") if rows else ""

    def filtered_lookup(self, year: str) -> int:
        return self._run(Q_FILTERED, year=year)[0]["c"]

    def aggregation(self) -> int:
        return len(self._run(Q_AGGREGATION))

    # --- write / footprint -------------------------------------------------
    def write_op(self, key: str) -> int:
        return self._run(Q_WRITE, key=key)[0]["c"]

    def cleanup_writes(self) -> int:
        rows = self._run(Q_CLEANUP)
        return rows[0]["c"] if rows else 0

    def footprint(self) -> dict[str, Any]:
        """Ask the engine what it will tell us; record 'not observable' for the rest.

        CognoDB's managed endpoint does not expose dbms.components() or the store-size
        procedures (verified -- the call returns a SyntaxError for an unregistered
        procedure), so several fields legitimately come back unavailable there.
        """
        out: dict[str, Any] = {}
        probes = {
            "node_count": "MATCH (n) RETURN count(n) AS v",
            "relationship_count": "MATCH ()-[r]->() RETURN count(r) AS v",
        }
        for key, query in probes.items():
            try:
                out[key] = self._run(query)[0]["v"]
            except Exception as exc:  # noqa: BLE001
                out[key] = f"not observable ({type(exc).__name__})"

        introspection = {
            "server_version": "CALL dbms.components() YIELD name, versions RETURN name + ' ' + versions[0] AS v",
            "store_size_bytes": "CALL apoc.monitor.store() YIELD total RETURN total AS v",
        }
        for key, query in introspection.items():
            try:
                out[key] = self._run(query)[0]["v"]
            except Exception:  # noqa: BLE001
                out[key] = "not observable (procedure not available on this platform)"
        return out
