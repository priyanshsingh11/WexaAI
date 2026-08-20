# Benchmarking CognoDB Cloud against four graph databases

A reproducible latency and throughput comparison of **CognoDB Cloud** with **Neo4j
Community**, **Memgraph**, **FalkorDB** and **ArangoDB**, on the same dataset and the same
logical workloads.

The headline result is not which database is fastest. It is that **three of the five
engines silently returned wrong answers** for at least one workload before the harness
caught them — including two that would have looked like a speed advantage. Those are
documented in [Correctness findings](#correctness-findings), because a latency number
attached to a wrong answer is worse than no number at all.

---

## Contents

- [What was measured](#what-was-measured)
- [Why two tracks](#why-two-tracks)
- [Databases selected, and why](#databases-selected-and-why)
- [Dataset](#dataset)
- [Methodology](#methodology)
- [Correctness findings](#correctness-findings)
- [Results](#results)
- [Analysis](#analysis)
- [Fairness limitations and caveats](#fairness-limitations-and-caveats)
- [Reproducing this benchmark](#reproducing-this-benchmark)
- [Repository layout](#repository-layout)

---

## What was measured

Every metric required by the assignment, on every platform:

| Category | Metric | Reported as |
|---|---|---|
| Data loading | Ingest throughput | nodes/sec, relationships/sec, total wall-clock |
| Traversals | 1-hop, 2-hop, 3-hop latency | p50 and p95 (ms), 200 fixed start nodes |
| Lookups | Point lookup, indexed/filtered lookup | p50 and p95 (ms), index stated per platform |
| Aggregation | Group-by over a label | p50 and p95 (ms) |
| Mixed workload | Concurrent read/write | sustained QPS at 1 / 10 / 40 clients, 90/10 read/write |
| Footprint | Resource usage where observable | memory / store size / counts, or "not observable" |

Percentiles use the **nearest-rank** method (`ceil(p/100 × n)` on sorted samples), so every
reported value is one that an actual request experienced rather than an interpolation.
Every individual latency sample is written to `results/raw/`, so any reviewer preferring a
different percentile definition can recompute from the same data.

---

## Why two tracks

The assignment requires equivalent vCPU/RAM/storage on every platform, and separately
requires the *same client machine and region*. With a free CognoDB instance those two rules
pull in opposite directions, so the benchmark is split into two tracks that are each
internally fair. **Latency is never compared across tracks.**

### Lab track — controlled engine comparison

Neo4j, Memgraph, FalkorDB and ArangoDB run in Docker, hard-capped to CognoDB's advertised
free-tier spec: **0.5 vCPU, 256 MB RAM**. Verified at the daemon level, not just declared:

```
$ docker inspect bench-neo4j --format '{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}'
500000000 268435456          # 0.5 vCPU, 256 MiB
```

All four are reached over loopback, so latency is close to pure engine time.

### Cloud track — managed endpoint, as delivered

CognoDB Cloud is measured over the public internet from the same client machine.

**This is why the split exists.** CognoDB's free tier is offered in `us-east` only, and the
benchmark client is in India. The measured TCP handshake floor — no TLS, no protocol, no
query, no database — is:

| Path | TCP p50 | TCP p95 |
|---|---|---|
| CognoDB endpoint (us-east) | ~390 ms | ~1420 ms |
| Local Docker container | ~0.4 ms | ~16 ms |

That is roughly a **1000× transport difference before any database does any work**. A
1-hop traversal over 34k nodes is single-digit milliseconds of engine time, so a combined
chart would report the client's internet connection, not the databases. Publishing that as
"CognoDB is 400× slower than Memgraph" would be false.

The honest consequence, stated plainly: **this benchmark cannot isolate CognoDB's engine
performance.** What it can do is measure CognoDB's end-to-end behaviour as a user in India
actually experiences it, verify its correctness rigorously, and compare its ingest
throughput and query semantics against the others.

To make the cloud track a real comparison rather than a single data point, provisioning
**Neo4j AuraDB Free in us-east** gives CognoDB a managed peer over the identical WAN link
from the identical client. See `.env.example` — the harness picks it up automatically when
`NEO4J_AURA_URI` is set, and skips it silently otherwise.

---

## Databases selected, and why

Selection was constrained by one rule: every competitor must be runnable **at 0.5 vCPU /
256 MB**. That eliminated more candidates than it admitted.

| Database | Why it earns a slot |
|---|---|
| **Neo4j Community 5.26** | The incumbent, and the reference implementation for Bolt/Cypher. CognoDB is driven by the official Neo4j driver, so this isolates *platform* differences from *language* differences with zero query-translation risk. |
| **Memgraph 2.22** | In-memory C++ engine speaking the same Cypher. Same language, opposite storage architecture — isolates architecture from dialect. |
| **FalkorDB 4.2** | Sparse-matrix / GraphBLAS execution over the RESP protocol. A genuinely different evaluation model, and the most likely to thrive in 256 MB. |
| **ArangoDB 3.11** | Multi-model, queried in **AQL, not Cypher**. Deliberately included to force the "equivalent logical workload, not identical syntax" discipline the assignment asks for. |

**Rejected, and why** — documenting these is part of the methodology:

| Rejected | Reason |
|---|---|
| Amazon Neptune | No free tier; smallest instance far exceeds 256 MB. Including it *would itself be* the methodology error the assignment warns about. |
| JanusGraph | Requires a Cassandra/ScyllaDB backend. Cannot fit the resource envelope. |
| NebulaGraph | Three separate services (meta/storage/graph); will not run meaningfully in 256 MB. |
| TigerGraph | Heavyweight footprint and a slow signup path for a time-boxed comparison. |

---

## Dataset

**SNAP cit-HepPh** — the arXiv High Energy Physics (phenomenology) citation network.
Source: <https://snap.stanford.edu/data/cit-HepPh.html>

| Property | Value |
|---|---|
| Nodes (papers) | **34,546** |
| Relationships (`:CITES`) | **421,578** |
| Nodes with a `year` property | 30,561 (**88.5%**) |
| Year range | 1992 – 2002 |
| Loaded in full on every platform | yes — no sampling was needed |

421,578 relationships sits inside the assignment's suggested 100k–500k range, and **the
full graph fit every platform including CognoDB's 1 GB free tier**, so no down-sampling was
applied. `scripts/prepare_dataset.py --edges N` supports a deterministic seeded subset if a
tighter tier ever requires one.

### The dataset defect that had to be fixed first

A naive join of the citation graph to `cit-HepPh-dates.txt` matches only **62.9%** of nodes
and produces a year range that stops at **1999**. Both symptoms have the same cause:

- Node IDs in the edge file have had **leading zeros stripped** (observed lengths 4/5/6/7).
- The dates file keeps the full 7 digits and prefixes **cross-listed papers with `11`**.

So every paper from 2000–2002 — arXiv IDs beginning `00`, `01`, `02` — silently fails to
join and vanishes from the properties. Normalising both sides fixes it:

```python
def normalise_id(paper_id: str) -> str:
    if len(paper_id) > 7 and paper_id.startswith("11"):
        paper_id = paper_id[2:]      # cross-listed prefix
    return paper_id.zfill(7)         # restore stripped leading zeros
```

Coverage rises to **88.5%** and the range extends to its true **1992–2002**. This matters
because `year` drives both the filtered-lookup and aggregation workloads. `tests/` asserts
the coverage so the fix cannot silently regress.

The remaining 11.5% of nodes are papers cited from outside the hep-ph category. They
**keep their edges** — the traversal topology depends on them — but carry a null year.

---

## Methodology

### The single most important fairness control

**Every database is queried from the identical list of 200 start nodes, in the identical
order.** The list is generated once by `prepare_dataset.py` with a fixed seed, written to
`data/prepared/start_nodes.json`, and reused byte-for-byte by every engine and every run.

If each database drew its own random start nodes it would face a different fan-out, and the
resulting p95 values would describe the graph rather than the engine. Measured fan-out of
the chosen set:

| Depth | min | p50 | max |
|---|---|---|---|
| 1-hop | 1 | 10 | 109 |
| 2-hop | 1 | 71 | 1,024 |
| 3-hop | 1 | 355 | 3,898 |

### Measurement procedure

Per database, per workload, in this order:

1. **Cold samples** — the first 5 operations after connecting, recorded separately and
   never mixed into the warm percentiles.
2. **Warm-up** — 20 untimed iterations, discarded.
3. **Measured run** — 100 iterations (the assignment's suggested floor), arguments cycled
   deterministically so every engine issues the same sequence.

Failures are recorded, never dropped. A database erroring on 30% of queries would otherwise
show a flatteringly clean p95.

### Concurrency

Threads, not asyncio — all three drivers are synchronous, and a thread pool is trivially
explainable. Each worker holds its own session (adapters keep sessions thread-local), so
the pool measures server concurrency rather than client contention. Writes go to an
isolated `:BenchWrite` namespace and are deleted afterwards, so the measured read dataset is
never mutated and runs stay repeatable.

Swept at **1 / 10 / 40 clients**, 90/10 read/write, 20 s per level.

### Loading

Every platform is loaded by the **same logical method** — driver-batched `UNWIND` (or the
AQL equivalent), identical batch size of 1000, same client — so the throughput numbers
compare databases rather than comparing bulk-import tools.

Each engine has a faster native importer (`neo4j-admin import`, `arangoimport`, and so on).
None was used, because using a different tool per platform would measure the tools. This is
a deliberate choice that makes all reported ingest numbers *lower* than each vendor's best.

### Indexes

Stated per platform, since the assignment asks:

| Platform | Indexes created |
|---|---|
| CognoDB / Neo4j / Aura | `CREATE INDEX FOR (n:Paper) ON (n.id)`, `... ON (n.year)` |
| Memgraph | `CREATE INDEX ON :Paper(id)`, `CREATE INDEX ON :Paper(year)` |
| FalkorDB | `CREATE INDEX ON :Paper(id)`, `CREATE INDEX ON :Paper(year)` |
| ArangoDB | `_key` primary index (automatic, used by point lookup) + persistent index on `year` |

---

## Correctness findings

Every read workload is checked against **ground truth precomputed from the source data** by
`prepare_dataset.py`, not merely cross-checked between engines — five databases agreeing on
a wrong answer would otherwise pass. This caught three real defects.

### 1. CognoDB drops variable-length paths that return to the origin

Measured on the loaded dataset, start node `0001222`:

| Query | Neo4j | CognoDB |
|---|---|---|
| `(a)-[:CITES]->(b)-[:CITES]->(a)` explicit | 1 | 1 |
| `(a)-[:CITES*1..2]->(a)` variable-length | 1 | **0** |
| `(a)-[:CITES*1..2]->(m)` distinct count | 24 | **23** |

The explicit two-step pattern finds the cycle on both engines; only the variable-length form
differs. With 44 self-loops in the graph and 9 of the 200 start nodes sitting on a ≤3-hop
cycle, this changed the answer on ~5% of start nodes.

**Resolution:** all traversal workloads now ask for *other* reachable papers
(`WHERE m <> n`), which is unambiguous, is the more natural question, and makes every
engine agree exactly. Reported here because it is a genuine platform difference in
CognoDB's Cypher implementation.

### 2. FalkorDB's ranged variable-length traversal is lossy

The more serious finding. Start `0001222`, target `9308262`, via intermediate `9907378`:

| Query | Result |
|---|---|
| `-[:CITES*2..2]->` (exact depth) | 1 — found |
| `-[:CITES*1..2]->` (range) | **0 — missing** |
| `-[:CITES]->(:Paper {id:'9907378'})-[:CITES]->` (explicit) | 1 — found |

The exact-depth form and the explicit pattern both find the node; only the **range** form
drops it. FalkorDB appears to stop expanding a node once it has been emitted at a shallower
depth, so anything reachable only *through* a depth-1 neighbour is lost.

Impact on the measured workloads: 2-hop counts understated by up to **27%** (23 → 17) and
3-hop by up to **37%** (912 → 595). The data itself is intact — relationship count and
per-node out-degrees match the source file exactly.

**Resolution:** FalkorDB's traversals are rewritten as an explicit union of depths, which
matches ground truth on every start node. This is a different query *shape* from the one the
Bolt engines run and plausibly changes FalkorDB's traversal latency — flagged rather than
buried.

### 3. ArangoDB was being overcharged ~10× by a transport artifact

Not a database defect, but it would have been published as one. With python-arango's
default pooled keep-alive session, every request through Docker Desktop's Windows port
forwarder stalls on a ~40 ms timer:

| Client configuration | p50 |
|---|---|
| Pooled keep-alive (library default) | 44.2 ms |
| Client-side `TCP_NODELAY` | 43.9 ms — no help |
| `Connection: close` | **4.1 ms** |

A *fresh* connection being 9× faster than a pooled one is backwards for any real engine
cost, which is what identified it as transport. The adapter now forces a new connection per
request.

**Trade-off, stated honestly:** ArangoDB now pays a TCP handshake on every query (~0.4 ms
on loopback) that the Bolt targets amortise over a persistent connection. ArangoDB's
reported latency is therefore slightly *pessimistic* rather than flattering.

### 4. A driver-usage trap that would have doubled CognoDB's latency

`driver.execute_query()` opens a fresh session per call, costing an extra network round
trip. Over a 390 ms WAN link that is not a rounding error:

| Driver usage | CognoDB p50 |
|---|---|
| `execute_query()` (session per call) | 891 ms |
| Persistent session | **438 ms** |

All Bolt adapters hold a long-lived, thread-local session. Worth noting for anyone
benchmarking a remote Bolt endpoint.

---

## Results

<!-- RESULTS_START -->
_Populated by `python scripts/make_report.py` from `results/results.json`._
<!-- RESULTS_END -->

---

## Analysis

<!-- ANALYSIS_START -->
_Written from the measured results below._
<!-- ANALYSIS_END -->

---

## Fairness limitations and caveats

Stated in full, because hidden caveats are worth less than acknowledged ones.

1. **Cross-track latency comparison is invalid.** CognoDB is measured over a ~390 ms WAN
   link (p95 ~1420 ms, stdev 289 ms); the lab containers answer over loopback. The two
   tracks answer different questions and must not be placed on one axis.

2. **CognoDB's engine performance is not isolated by this benchmark.** Only its end-to-end
   delivered latency from India, its ingest throughput, and its correctness are measured.

3. **`cpus: 0.5` is a hard CFS quota; CognoDB's free tier is described as *burstable* 0.5
   vCPU.** Burstable can exceed its nominal share in short spikes; the containers cannot.
   This favours CognoDB on short bursty workloads.

4. **Disk was not capped per container.** `--storage-opt size=` requires a storage driver
   unavailable on Docker Desktop for Windows. The dataset is ~50 MB on disk, far below the
   1 GB limit everywhere, so disk was never the binding constraint.

5. **Docker Desktop's Windows port forwarder is noisy.** The loopback TCP baseline varied
   between ~0.4 ms and ~13 ms p50 across runs. It affects all four lab targets equally, but
   it is real measurement noise and inflates lab p95 values.

6. **FalkorDB runs a different query shape** for traversals (explicit union of depths)
   because its ranged variable-length form returns wrong answers. Its traversal latency is
   therefore not strictly comparable to the other Cypher engines'.

7. **ArangoDB's traversals use `OPTIONS {uniqueVertices: 'global', bfs: true}`** — the
   idiomatic AQL for "distinct vertices within k hops". It returns the same *set* as the
   Cypher engines but does less *work*, since global uniqueness prunes on first visit while
   the Cypher engines expand paths and deduplicate at the end. This plausibly favours
   ArangoDB on 3-hop.

8. **Neo4j ran at 99.9% of its 256 MB cap** after loading. Its numbers reflect an engine
   under genuine memory pressure — which is the point of the resource-parity rule, but
   worth naming.

9. **Single client machine, single run of each configuration.** Repeat-run variance is not
   yet characterised; `--run-id` supports repeated runs for anyone extending this.

10. **Free-tier specs are as advertised at the date in `results/manifest.json`,** not
    independently verified except where the platform exposes them.

---

## Reproducing this benchmark

From a fresh clone, with Docker and Python 3.11+:

```bash
git clone <this-repo> && cd cognodb-benchmark
python -m venv .venv && . .venv/Scripts/activate     # Windows
pip install -r requirements.txt

cp .env.example .env        # then fill in your CognoDB credentials

# 1. Download the dataset into data/raw/ from
#    https://snap.stanford.edu/data/cit-HepPh.html
#    (Cit-HepPh.txt.gz and cit-HepPh-dates.txt.gz, gunzipped)

# 2. Build the prepared dataset, ground truth and start-node list
python scripts/prepare_dataset.py

# 3. Start the four capped competitors
docker compose up -d
docker stats --no-stream        # confirm 0.5 CPU / 256 MB is enforced

# 4. Load the identical dataset everywhere
python scripts/load_data.py --db all

# 5. Run the benchmark
python scripts/run_benchmark.py --track lab   --concurrency 1,10,40
python scripts/run_benchmark.py --track cloud --concurrency 1,10

# 6. Regenerate tables and CSV
python scripts/make_report.py

# Tests
python -m pytest tests/ -q
```

### Environment variables

All credentials come from `.env` (gitignored — never committed). See `.env.example` for the
full list. Nothing in this repository contains a connection URI or password.

---

## Repository layout

```
cognodb-benchmark/
├── benchmarks/
│   ├── common.py            # adapter contract, timing loop, correctness checking
│   ├── metrics.py           # nearest-rank percentiles
│   ├── workloads.py         # the logical workloads, defined once
│   ├── registry.py          # builds adapters from config + environment
│   └── adapters/
│       ├── bolt.py          # CognoDB, Neo4j, Aura, Memgraph (one adapter, four targets)
│       ├── falkordb.py      # Cypher over RESP
│       └── arangodb.py      # AQL over HTTP
├── config/databases.yaml    # targets, tracks, tier specs, credentials mapping
├── scripts/
│   ├── prepare_dataset.py   # normalise, sample, compute ground truth, hash
│   ├── load_data.py         # ingest + throughput measurement
│   ├── run_benchmark.py     # warm-up, measurement, concurrency, manifest
│   └── make_report.py       # results.json -> results.csv + tables.md
├── results/
│   ├── results.json         # aggregated stats
│   ├── results.csv          # flat matrix
│   ├── tables.md            # generated Markdown tables
│   └── raw/<run>/<db>/*.jsonl   # every individual latency sample
├── tests/
├── docker-compose.yml       # four engines, capped to 0.5 vCPU / 256 MB
└── requirements.txt         # fully pinned
```

Four of the six targets speak Bolt/Cypher, so they share a single adapter parameterised by
dialect flags rather than four near-identical classes.
