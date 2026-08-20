### Data loading (ingest throughput)

| Database | Track | Nodes/sec | Relationships/sec | Total load time (s) | Method |
|---|---|---|---|---|---|
| cognodb | cloud | 1,322 | 1,484 | 310.3 | driver batched UNWIND (identical batch size on every platform) |
| neo4j | lab | 2,176 | 4,819 | 103.4 | driver batched UNWIND (identical batch size on every platform) |
| memgraph | lab | 16,701 | 15,714 | 28.9 | driver batched UNWIND (identical batch size on every platform) |
| falkordb | lab | 16,756 | 7,394 | 59.1 | driver batched UNWIND (identical batch size on every platform) |
| arangodb | lab | 18,509 | 15,706 | 28.7 | driver batched AQL INSERT (identical batch size on every platform) |

### Network baseline (transport cost, measured separately from engine time)

| Database | Track | TCP p50 (ms) | TCP p95 (ms) | Resolved endpoint |
|---|---|---|---|---|
| Neo4j Community 5 (Docker, capped) | lab | 0.48 | 13.46 | 127.0.0.1 |
| Memgraph (Docker, capped) | lab | 0.44 | 15.50 | 127.0.0.1 |
| FalkorDB (Docker, capped) | lab | 0.55 | 13.54 | 127.0.0.1 |
| ArangoDB (Docker, capped) | lab | 0.53 | 15.53 | 127.0.0.1 |
| CognoDB Cloud (free c0) | cloud | 471.89 | 759.58 | 136.70.132.96 |

### Lab track — read latency, p50 / p95 in ms

| Workload | Neo4j Community 5 (Docker, capped) | Memgraph (Docker, capped) | FalkorDB (Docker, capped) | ArangoDB (Docker, capped) |
|---|---|---|---|---|
| 1-hop | 5.17 / 65.09 | 1.38 / 2.32 | 4.39 / 38.88 | 16.70 / 31.08 |
| 2-hop | 4.70 / 63.55 | 1.47 / 2.93 | 4.67 / 42.38 | 21.14 / 35.83 |
| 3-hop | 4.95 / 81.97 | 2.39 / 10.29 | 12.95 / 168.74 | 28.77 / 73.81 |
| Point lookup | 4.03 / 67.24 | 1.07 / 1.72 | 4.17 / 32.76 | 15.39 / 27.95 |
| Filtered lookup (indexed) | 12.12 / 69.39 | 1.39 / 1.96 | 4.55 / 38.19 | 15.58 / 29.19 |
| Aggregation (group-by) | 8.85 / 82.87 | 9.16 / 44.55 | 9.41 / 60.79 | 30.37 / 60.50 |

### Cloud track — read latency, p50 / p95 in ms

| Workload | CognoDB Cloud (free c0) |
|---|---|
| 1-hop | 322.38 / 480.25 |
| 2-hop | 323.45 / 361.48 |
| 3-hop | 363.74 / 803.10 |
| Point lookup | 319.36 / 330.81 |
| Filtered lookup (indexed) | 324.23 / 339.11 |
| Aggregation (group-by) | 400.31 / 438.14 |

### Mixed read/write workload — lab track

| Database | Clients | Sustained QPS | Read p50 / p95 (ms) | Write p50 / p95 (ms) | Errors |
|---|---|---|---|---|---|
| Neo4j Community 5 (Docker, capped) | 1 | 56.3 | 5.22 / 73.69 | 7.74 / 71.53 | 0 |
| Neo4j Community 5 (Docker, capped) | 10 | 125.4 | 93.93 / 105.53 | 98.83 / 196.32 | 0 |
| Neo4j Community 5 (Docker, capped) | 40 | 176.6 | 181.59 / 307.88 | 233.41 / 1308.93 | 26 |
| Memgraph (Docker, capped) | 1 | 839.6 | 1.11 / 1.75 | 1.15 / 1.82 | 0 |
| Memgraph (Docker, capped) | 10 | 1345.6 | 6.37 / 14.45 | 6.43 / 15.43 | 0 |
| Memgraph (Docker, capped) | 40 | 1437.3 | 25.68 / 48.92 | 25.89 / 50.14 | 0 |
| FalkorDB (Docker, capped) | 1 | 144.9 | 4.59 / 36.33 | 1.39 / 20.80 | 0 |
| FalkorDB (Docker, capped) | 10 | 131.7 | 96.09 / 109.52 | 91.63 / 102.41 | 0 |
| FalkorDB (Docker, capped) | 40 | 119.3 | 301.31 / 500.42 | 496.44 / 1203.00 | 0 |
| ArangoDB (Docker, capped) | 1 | 66.7 | 15.38 / 29.98 | 15.08 / 30.08 | 0 |
| ArangoDB (Docker, capped) | 10 | 397.3 | 22.25 / 40.12 | 21.75 / 37.26 | 0 |
| ArangoDB (Docker, capped) | 40 | 388.6 | 99.69 / 152.64 | 97.70 / 152.76 | 0 |

### Mixed read/write workload — cloud track

| Database | Clients | Sustained QPS | Read p50 / p95 (ms) | Write p50 / p95 (ms) | Errors |
|---|---|---|---|---|---|
| CognoDB Cloud (free c0) | 1 | 3.1 | 318.90 / 348.65 | 329.33 / 340.47 | 0 |
| CognoDB Cloud (free c0) | 10 | 28.2 | 315.77 / 473.72 | 328.60 / 371.19 | 0 |
| CognoDB Cloud (free c0) | 40 | 117.9 | 316.09 / 344.94 | 335.42 / 415.75 | 0 |

### Correctness against ground truth

| Database | 1-hop | 2-hop | 3-hop | Point lookup | Filtered lookup (indexed) | Aggregation (group-by) |
|---|---|---|---|---|---|---|
| Neo4j Community 5 (Docker, capped) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Memgraph (Docker, capped) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| FalkorDB (Docker, capped) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| ArangoDB (Docker, capped) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| CognoDB Cloud (free c0) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

### Resource footprint

| Database | Nodes | Relationships | Memory / store size | Server version |
|---|---|---|---|---|
| Neo4j Community 5 (Docker, capped) | 34546 | 421578 | not observable (procedure not available on this platform) | Neo4j Kernel 5.26.29 |
| Memgraph (Docker, capped) | 34546 | 421578 | not observable (procedure not available on this platform) | Memgraph 5.9.0 |
| FalkorDB (Docker, capped) | 34546 | 421578 | 32.5 MB | 7.2.4 |
| ArangoDB (Docker, capped) | 34546 | 421578 | not observable | 3.11.10 |
| CognoDB Cloud (free c0) | 34546 | 421578 | not observable (procedure not available on this platform) | not observable (procedure not available on this platform) |
