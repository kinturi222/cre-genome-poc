# Reliability Genome — Graph Model

The genome is deliberately split across two stores. Getting this split right is
the single most consequential design decision in Phase 1.

| Store | Holds | Why |
|---|---|---|
| **Neptune** (property graph) | *relationships* — dependency, propagation, ownership edges | Traversal queries (blast radius, critical path) are the whole point. These are multi-hop and cheap in a graph, expensive and unbounded in SQL. |
| **DynamoDB** (`service-dna`) | *attributes* — owner, tier, SLO, RTO/RPO, revenue-per-minute | Single-item reads at incident time must be single-digit millisecond and must not compete with graph traversals for capacity. |
| **S3 + Glue** | *history* — raw discovery events, snapshots, lineage | Cheap retention, replay after a bad ingestion, and the audit trail for how the graph reached its current state. |

**Rule of thumb:** if you would traverse it, it belongs in Neptune. If you would
look it up by service ID, it belongs in DynamoDB.

---

## Node types

```
(:Service   {id, name, type, account, region, environment, tier,
             discovered_at, last_seen_at, sources[]})
(:Resource  {id, arn, type, account, region, service_id, last_seen_at})
(:Team      {id, name, contact, escalation})
(:Slo       {id, service_id, objective, window, target, current})
(:Dependency-less nodes are not created — see "absent edges" below)
```

`Service` is the logical unit an operator reasons about ("payments-api").
`Resource` is the physical AWS object (`arn:aws:ecs:...:service/payments-api`).
One Service typically owns many Resources. Keeping them separate is what lets
the graph survive a re-platforming: the Service node and its history persist
when the underlying Resources are replaced.

## Edge types

```
(:Service)-[:DEPENDS_ON  {kind, confidence, discovered_by, last_seen_at}]->(:Service)
(:Service)-[:OWNS]->(:Resource)
(:Team)-[:OWNS]->(:Service)
(:Service)-[:HAS_SLO]->(:Slo)
(:Resource)-[:CONNECTS_TO {protocol, port}]->(:Resource)
```

`DEPENDS_ON.kind` is one of `sync | async | data | infra`. This matters more
than it looks: a synchronous dependency propagates failure immediately, an
asynchronous one degrades gracefully, and a data dependency may not fail the
caller at all. Blast-radius scoring weights them differently.

`DEPENDS_ON.confidence` (0.0–1.0) records how the edge was learned. Edges from
X-Ray traces are observed and get high confidence; edges inferred from security
groups are structural possibilities and get low confidence. **Never present an
inferred edge to a causal agent as though it were observed.**

## Absent edges

The graph records what was discovered, not what exists. A service with no
`DEPENDS_ON` edges is more likely under-instrumented than genuinely isolated.
`Service.sources[]` tracks which discovery mechanisms have seen the service, and
completeness is measured as the fraction of services seen by two or more
independent sources. This number is the Phase-1 promotion gate — not a vanity
metric.

## Temporal handling

Every node and edge carries `last_seen_at`. Discovery is additive; nothing is
hard-deleted. An edge not observed for `STALE_AFTER_DAYS` (default 14) is
flagged `stale=true` rather than removed, because:

- deleting an edge during an incident destroys the evidence needed to explain it;
- absence of observation is not evidence of absence, especially for failover
  paths that are exercised rarely.

Consumers filter `stale` explicitly. The causal agent excludes stale edges from
hypothesis generation but includes them when explaining a failure that only
makes sense via a dormant path.

---

## Why not one store?

Considered and rejected:

- **Everything in Neptune.** Attribute reads at incident time compete with
  traversal load, and Neptune has no natural TTL for high-churn attributes like
  current SLO attainment.
- **Everything in DynamoDB.** Multi-hop traversal requires either recursive
  reads (latency explodes at depth 3+) or a pre-computed adjacency table that
  must be rebuilt on every topology change.
- **A relational store.** Blast radius is transitive closure with cycle
  detection; recursive CTEs make this possible but not pleasant, and the query
  cost grows unpredictably with graph density.

See `ADR-001-graph-database-choice.md` for the full argument.
