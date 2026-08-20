"""FalkorDB adapter -- Cypher over the RESP (Redis) protocol.

FalkorDB represents the graph as sparse adjacency matrices and evaluates traversals as
linear algebra (GraphBLAS), which is a genuinely different execution model from the
pointer-chasing engines. It speaks a Cypher dialect, so the *queries* stay close to the
Bolt ones; only the transport and the client API differ.

Dialect notes that forced a deviation from the Bolt adapter:
* Index DDL uses the older `CREATE INDEX ON :Label(prop)` form.
* There is no `IF NOT EXISTS`, so an existing index raises and is caught.
* Queries run against a named graph key rather than a database.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Sequence

from falkordb import FalkorDB

from benchmarks.common import GraphAdapter, LoadResult, iter_batches

log = logging.getLogger("bench.falkordb")

GRAPH_KEY = "citations"

# DOCUMENTED DIALECT DEVIATION -- FalkorDB ranged variable-length traversal is lossy.
#
# FalkorDB's `*1..k` does not return every node reachable within k hops. Reproduced on the
# loaded dataset (start 0001222, target 9308262 via intermediate 9907378):
#
#     MATCH (:Paper {id:'0001222'})-[:CITES*2..2]->(m:Paper {id:'9308262'})  -> 1   found
#     MATCH (:Paper {id:'0001222'})-[:CITES*1..2]->(m:Paper {id:'9308262'})  -> 0   MISSING
#     MATCH (:Paper {id:'0001222'})-[:CITES]->(:Paper {id:'9907378'})
#                                  -[:CITES]->(m:Paper {id:'9308262'})       -> 1   found
#
# The exact-depth form and the explicit pattern both find the node; only the *range* form
# drops it. It appears to stop expanding a node once that node has been emitted at a
# shallower depth, so anything reachable only through a depth-1 neighbour is lost. On the
# 200 start nodes this understated 2-hop counts by up to 27% (e.g. 23 -> 17) and 3-hop by
# up to 37% (912 -> 595). The data is intact: relationship count and per-node out-degrees
# match the source file exactly.
#
# The traversals below are therefore written as an explicit union of depths, which returns
# results identical to ground truth on every start node. This is a genuinely different
# query shape from the one the Bolt engines run, and it plausibly changes FalkorDB's
# traversal latency -- flagged here and in the README rather than buried.
Q_ONE_HOP = "MATCH (n:Paper {id: $id})-[:CITES]->(m) WHERE m <> n RETURN count(DISTINCT m) AS c"
Q_TWO_HOP = (
    "MATCH (n:Paper {id: $id})-[:CITES]->(a) WITH n, collect(DISTINCT a) AS d1 "
    "OPTIONAL MATCH (n)-[:CITES]->()-[:CITES]->(b) WITH n, d1, collect(DISTINCT b) AS d2 "
    "UNWIND (d1 + d2) AS x WITH n, x WHERE x <> n RETURN count(DISTINCT x) AS c"
)
Q_THREE_HOP = (
    "MATCH (n:Paper {id: $id})-[:CITES]->(a) WITH n, collect(DISTINCT a) AS d1 "
    "OPTIONAL MATCH (n)-[:CITES]->()-[:CITES]->(b) WITH n, d1, collect(DISTINCT b) AS d2 "
    "OPTIONAL MATCH (n)-[:CITES]->()-[:CITES]->()-[:CITES]->(c) "
    "WITH n, d1, d2, collect(DISTINCT c) AS d3 "
    "UNWIND (d1 + d2 + d3) AS x WITH n, x WHERE x <> n RETURN count(DISTINCT x) AS c"
)
Q_POINT = "MATCH (n:Paper {id: $id}) RETURN n.year AS year"
Q_FILTERED = "MATCH (n:Paper) WHERE n.year = $year RETURN count(n) AS c"
Q_AGGREGATION = "MATCH (n:Paper) WHERE n.year IS NOT NULL RETURN n.year AS year, count(*) AS c ORDER BY year"


class FalkorDBAdapter(GraphAdapter):
    dialect = "cypher-resp"

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.host = config.get("host") or "localhost"
        self.port = int(config.get("port") or 6379)
        self.password = config.get("password") or None

    # --- lifecycle ---------------------------------------------------------
    def _client(self):
        """Per-thread client. The concurrency workload runs many threads and a single
        Redis connection would serialise them, which would measure the client rather
        than the server."""
        client = getattr(self._local, "client", None)
        if client is None:
            client = FalkorDB(host=self.host, port=self.port, password=self.password)
            self._local.client = client
        return client

    def _graph(self):
        return self._client().select_graph(GRAPH_KEY)

    def connect(self) -> None:
        self._graph().query("RETURN 1")
        log.info("%s: connected to %s:%s", self.name, self.host, self.port)

    def close(self) -> None:
        self._local.client = None

    def _scalar(self, query: str, **params) -> Any:
        result = self._graph().query(query, params or None)
        rows = result.result_set
        return rows[0][0] if rows and rows[0] else None

    def ping(self) -> int:
        return self._scalar("RETURN 1")

    # --- schema and ingest -------------------------------------------------
    def reset(self) -> None:
        try:
            self._graph().delete()
        except Exception as exc:  # noqa: BLE001 - absent graph key is not an error
            log.debug("%s: nothing to delete (%s)", self.name, exc)

    def setup_schema(self) -> list[str]:
        statements = ["CREATE INDEX ON :Paper(id)", "CREATE INDEX ON :Paper(year)"]
        executed = []
        for statement in statements:
            try:
                self._graph().query(statement)
                executed.append(statement)
            except Exception as exc:  # noqa: BLE001
                if "already" in str(exc).lower():
                    executed.append(f"{statement}  -- already existed")
                else:
                    log.warning("%s: index failed: %s -- %s", self.name, statement, exc)
        return executed

    def load(self, nodes: Sequence[tuple[str, str]], edges: Sequence[tuple[str, str]],
             batch_size: int) -> LoadResult:
        overall = time.perf_counter()
        graph = self._graph()

        node_start = time.perf_counter()
        for batch in iter_batches(nodes, batch_size):
            graph.query(
                "UNWIND $rows AS row CREATE (n:Paper {id: row.id, year: row.year})",
                {"rows": [{"id": nid, "year": year or None} for nid, year in batch]},
            )
        node_seconds = time.perf_counter() - node_start

        rel_start = time.perf_counter()
        for batch in iter_batches(edges, batch_size):
            graph.query(
                "UNWIND $rows AS row "
                "MATCH (a:Paper {id: row.src}) MATCH (b:Paper {id: row.dst}) "
                "CREATE (a)-[:CITES]->(b)",
                {"rows": [{"src": src, "dst": dst} for src, dst in batch]},
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
        return self._scalar(Q_ONE_HOP, id=node_id) or 0

    def two_hop(self, node_id: str) -> int:
        return self._scalar(Q_TWO_HOP, id=node_id) or 0

    def three_hop(self, node_id: str) -> int:
        return self._scalar(Q_THREE_HOP, id=node_id) or 0

    def point_lookup(self, node_id: str) -> str:
        return self._scalar(Q_POINT, id=node_id) or ""

    def filtered_lookup(self, year: str) -> int:
        return self._scalar(Q_FILTERED, year=year) or 0

    def aggregation(self) -> int:
        return len(self._graph().query(Q_AGGREGATION).result_set)

    # --- write / footprint -------------------------------------------------
    def write_op(self, key: str) -> int:
        self._graph().query("CREATE (w:BenchWrite {key: $key})", {"key": key})
        return 1

    def cleanup_writes(self) -> int:
        result = self._graph().query("MATCH (w:BenchWrite) DELETE w RETURN count(w) AS c")
        rows = result.result_set
        return rows[0][0] if rows and rows[0] else 0

    def footprint(self) -> dict[str, Any]:
        """FalkorDB exposes real memory accounting through the Redis INFO command, which is
        more than most managed endpoints give us."""
        out: dict[str, Any] = {}
        try:
            out["node_count"] = self._scalar("MATCH (n) RETURN count(n) AS c")
            out["relationship_count"] = self._scalar("MATCH ()-[r]->() RETURN count(r) AS c")
        except Exception as exc:  # noqa: BLE001
            out["node_count"] = out["relationship_count"] = f"not observable ({type(exc).__name__})"
        try:
            info = self._client().connection.info("memory")
            out["memory_used_bytes"] = info.get("used_memory")
            out["memory_peak_bytes"] = info.get("used_memory_peak")
        except Exception:  # noqa: BLE001
            out["memory_used_bytes"] = "not observable"
        try:
            server = self._client().connection.info("server")
            out["server_version"] = server.get("redis_version")
        except Exception:  # noqa: BLE001
            out["server_version"] = "not observable"
        return out
