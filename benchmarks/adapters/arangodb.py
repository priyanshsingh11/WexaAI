"""ArangoDB adapter -- AQL over HTTP.

This is the only target that does not speak Cypher, and it is in the comparison precisely
for that reason: it forces the harness to express each workload as a *logical* operation
rather than a shared query string, which is what the assignment asks for. It is also why
every result is checked against ground truth -- a hand-translated query is exactly the kind
of thing that silently computes something slightly different.

DOCUMENTED QUERY-LANGUAGE DIFFERENCE (README section: query portability)
-----------------------------------------------------------------------
The traversals use `OPTIONS {uniqueVertices: 'global', bfs: true}`. This is the idiomatic
AQL way to ask "which distinct vertices are reachable within k hops", and it returns
exactly the same SET as Cypher's `MATCH (n)-[:CITES*1..k]->(m) RETURN count(DISTINCT m)`.

It is not, however, the same amount of WORK. Global uniqueness lets ArangoDB prune a vertex
the first time it is seen, whereas the Cypher engines enumerate path expansions and
deduplicate at the end. Writing the AQL without global uniqueness would enumerate paths the
way Cypher does, but that is an unnatural query no ArangoDB developer would ship.

We chose the idiomatic form -- the assignment explicitly says not to force a database into
an unnatural query -- and flag it here and in the README so the reader can weigh it. It
plausibly favours ArangoDB on the 3-hop workload.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Sequence

from arango import ArangoClient
from arango.http import DefaultHTTPClient

from benchmarks.common import GraphAdapter, LoadResult, iter_batches

log = logging.getLogger("bench.arangodb")


class NoKeepAliveHTTPClient(DefaultHTTPClient):
    """Force a fresh TCP connection per request.

    MEASURED TRANSPORT ARTIFACT, not an engine property. With python-arango's default
    pooled keep-alive session, every request through Docker Desktop's Windows port
    forwarder stalls on a ~40 ms timer -- the classic delayed-ACK signature:

        pooled keep-alive session   p50 = 44.2 ms   (min 6.0)
        client-side TCP_NODELAY     p50 = 43.9 ms   (no help)
        Connection: close           p50 =  4.1 ms

    A *fresh* connection is 9x faster than a pooled one, which is backwards for any real
    engine cost, so the 40 ms is transport, not ArangoDB. Left uncorrected it would have
    inflated every ArangoDB latency ~10x and made the comparison meaningless.

    The trade-off is honest and stated in the README: ArangoDB now pays a TCP handshake on
    every query (~0.4 ms on loopback, measured) that the Bolt targets amortise over a
    persistent connection. That is a small, known overcharge -- far preferable to a 40 ms
    artifact -- and it means ArangoDB's reported latency is, if anything, slightly
    pessimistic rather than flattering.
    """

    def create_session(self, host: str):
        session = super().create_session(host)
        session.headers["Connection"] = "close"
        return session


VERTEX_COLLECTION = "papers"
EDGE_COLLECTION = "cites"
WRITE_COLLECTION = "bench_writes"

# `FILTER v._key != @key` excludes the start vertex, matching the `WHERE m <> n` clause in
# the Cypher adapters. See bolt.py for why the start node is excluded from all traversals.
Q_TRAVERSE = """
LET reached = (
  FOR v IN 1..@depth OUTBOUND @start cites
    OPTIONS {uniqueVertices: 'global', bfs: true}
    FILTER v._key != @key
    RETURN v._key
)
RETURN LENGTH(reached)
"""
Q_POINT = "RETURN DOCUMENT(@id).year"
Q_FILTERED = "FOR p IN papers FILTER p.year == @year COLLECT WITH COUNT INTO c RETURN c"
Q_AGGREGATION = """
FOR p IN papers
  FILTER p.year != null AND p.year != ""
  COLLECT year = p.year WITH COUNT INTO c
  SORT year
  RETURN {year: year, count: c}
"""


class ArangoDBAdapter(GraphAdapter):
    dialect = "aql-http"

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.url = config.get("url") or "http://localhost:8529"
        self.user = config.get("user") or "root"
        self.password = config.get("password") or ""
        self.database = config.get("database") or "citations"

    # --- lifecycle ---------------------------------------------------------
    def _db(self):
        """Per-thread database handle: python-arango wraps a requests.Session, which is
        not safe to share across the concurrency workload's threads."""
        db = getattr(self._local, "db", None)
        if db is None:
            client = ArangoClient(hosts=self.url, http_client=NoKeepAliveHTTPClient())
            db = client.db(self.database, username=self.user, password=self.password)
            self._local.client = client
            self._local.db = db
        return db

    def connect(self) -> None:
        # The target database may not exist yet on a fresh container; create it via _system.
        client = ArangoClient(hosts=self.url, http_client=NoKeepAliveHTTPClient())
        system = client.db("_system", username=self.user, password=self.password)
        if not system.has_database(self.database):
            system.create_database(self.database)
            log.info("%s: created database %s", self.name, self.database)
        db = self._db()
        for name, is_edge in ((VERTEX_COLLECTION, False), (EDGE_COLLECTION, True), (WRITE_COLLECTION, False)):
            if not db.has_collection(name):
                db.create_collection(name, edge=is_edge)
        log.info("%s: connected to %s/%s", self.name, self.url, self.database)

    def close(self) -> None:
        self._local.db = None
        self._local.client = None

    def _query(self, aql: str, **bind) -> list[Any]:
        return list(self._db().aql.execute(aql, bind_vars=bind or None))

    def ping(self) -> int:
        return self._query("RETURN 1")[0]

    # --- schema and ingest -------------------------------------------------
    def reset(self) -> None:
        db = self._db()
        for name in (EDGE_COLLECTION, VERTEX_COLLECTION, WRITE_COLLECTION):
            if db.has_collection(name):
                db.collection(name).truncate()

    def setup_schema(self) -> list[str]:
        """`_key` carries ArangoDB's primary index automatically, which is the point-lookup
        path; only `year` needs an explicit index. Stated plainly because the assignment
        asks which properties are indexed on each platform."""
        db = self._db()
        executed = ["papers._key -- primary index (automatic, used by point lookup)"]
        try:
            db.collection(VERTEX_COLLECTION).add_persistent_index(fields=["year"], name="paper_year")
            executed.append("papers persistent index on ['year']")
        except Exception as exc:  # noqa: BLE001
            if "duplicate" in str(exc).lower() or "already" in str(exc).lower():
                executed.append("papers persistent index on ['year']  -- already existed")
            else:
                log.warning("%s: index failed -- %s", self.name, exc)
        return executed

    def load(self, nodes: Sequence[tuple[str, str]], edges: Sequence[tuple[str, str]],
             batch_size: int) -> LoadResult:
        overall = time.perf_counter()

        node_start = time.perf_counter()
        for batch in iter_batches(nodes, batch_size):
            self._query(
                "FOR row IN @rows INSERT {_key: row.id, year: row.year} INTO papers",
                rows=[{"id": nid, "year": year or None} for nid, year in batch],
            )
        node_seconds = time.perf_counter() - node_start

        rel_start = time.perf_counter()
        for batch in iter_batches(edges, batch_size):
            self._query(
                "FOR row IN @rows INSERT {_from: row.f, _to: row.t} INTO cites",
                rows=[{"f": f"{VERTEX_COLLECTION}/{src}", "t": f"{VERTEX_COLLECTION}/{dst}"}
                      for src, dst in batch],
            )
        rel_seconds = time.perf_counter() - rel_start

        return LoadResult(
            nodes=len(nodes),
            relationships=len(edges),
            node_seconds=node_seconds,
            relationship_seconds=rel_seconds,
            total_seconds=time.perf_counter() - overall,
            method="driver batched AQL INSERT (identical batch size on every platform)",
            batch_size=batch_size,
        )

    # --- read operations ---------------------------------------------------
    def _traverse(self, node_id: str, depth: int) -> int:
        return self._query(
            Q_TRAVERSE, depth=depth, start=f"{VERTEX_COLLECTION}/{node_id}", key=node_id
        )[0]

    def one_hop(self, node_id: str) -> int:
        return self._traverse(node_id, 1)

    def two_hop(self, node_id: str) -> int:
        return self._traverse(node_id, 2)

    def three_hop(self, node_id: str) -> int:
        return self._traverse(node_id, 3)

    def point_lookup(self, node_id: str) -> str:
        value = self._query(Q_POINT, id=f"{VERTEX_COLLECTION}/{node_id}")[0]
        return value or ""

    def filtered_lookup(self, year: str) -> int:
        return self._query(Q_FILTERED, year=year)[0]

    def aggregation(self) -> int:
        return len(self._query(Q_AGGREGATION))

    # --- write / footprint -------------------------------------------------
    def write_op(self, key: str) -> int:
        self._query("INSERT {k: @key} INTO bench_writes", key=key)
        return 1

    def cleanup_writes(self) -> int:
        count = self._db().collection(WRITE_COLLECTION).count()
        self._db().collection(WRITE_COLLECTION).truncate()
        return count

    def footprint(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        db = self._db()
        try:
            out["node_count"] = db.collection(VERTEX_COLLECTION).count()
            out["relationship_count"] = db.collection(EDGE_COLLECTION).count()
        except Exception as exc:  # noqa: BLE001
            out["node_count"] = f"not observable ({type(exc).__name__})"
        for label, collection in (("papers", VERTEX_COLLECTION), ("cites", EDGE_COLLECTION)):
            try:
                figures = db.collection(collection).figures()
                out[f"{label}_size_bytes"] = figures.get("figures", {}).get("documentsSize")
            except Exception:  # noqa: BLE001
                out[f"{label}_size_bytes"] = "not observable"
        try:
            out["server_version"] = db.version()
        except Exception:  # noqa: BLE001
            out["server_version"] = "not observable"
        return out
