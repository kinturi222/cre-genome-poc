"""
Blast radius and critical path — the derived intelligence that makes the
genome visibly worth having.

You cannot compute blast radius during an outage. These run continuously and
are cached, so at incident time the answer is a lookup, not a traversal.

WHAT "BLAST RADIUS" MEANS HERE
------------------------------
Not "everything reachable in the graph" — that is usually most of the estate
and therefore useless. Failure attenuates as it propagates: a synchronous
caller fails hard, an async consumer degrades, a data dependency may not notice.

Impact of a downstream service is the product of the propagation scores along
the path, and we stop when it falls below a floor. The result is a ranked,
bounded set an operator can act on.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Iterable

# Below this, a downstream service is not meaningfully affected.
IMPACT_FLOOR = 0.15
MAX_DEPTH = 6


@dataclass(frozen=True)
class Edge:
    to_service: str
    propagation_score: float     # kind_weight * confidence, from models.Dependency
    kind: str
    stale: bool = False


@dataclass
class Impacted:
    service_id: str
    impact: float                # 0..1, attenuated along the path
    depth: int
    path: list[str] = field(default_factory=list)
    tier: str | None = None
    revenue_per_minute: float | None = None

    @property
    def revenue_at_risk(self) -> float:
        """Impact-weighted revenue exposure per minute."""
        return (self.revenue_per_minute or 0.0) * self.impact


class Graph:
    """
    Minimal adjacency view. In production this is backed by Neptune;
    the in-memory form keeps the algorithm unit-testable without a cluster.
    """

    def __init__(self):
        self._out: dict[str, list[Edge]] = {}
        self._in: dict[str, list[Edge]] = {}
        self.attrs: dict[str, dict] = {}

    def add_service(self, service_id: str, **attrs) -> None:
        self.attrs[service_id] = attrs
        self._out.setdefault(service_id, [])
        self._in.setdefault(service_id, [])

    def add_dependency(self, frm: str, to: str, score: float,
                       kind: str = "sync", stale: bool = False) -> None:
        """`frm` DEPENDS_ON `to`  =>  failure of `to` propagates to `frm`."""
        self._out.setdefault(frm, []).append(Edge(to, score, kind, stale))
        self._in.setdefault(to, []).append(Edge(frm, score, kind, stale))
        self._out.setdefault(to, [])
        self._in.setdefault(frm, [])

    def dependents_of(self, service_id: str) -> Iterable[Edge]:
        """Who breaks when this breaks (reverse of DEPENDS_ON)."""
        return self._in.get(service_id, [])

    def dependencies_of(self, service_id: str) -> Iterable[Edge]:
        return self._out.get(service_id, [])


def blast_radius(
    graph: Graph,
    origin: str,
    *,
    include_stale: bool = False,
    impact_floor: float = IMPACT_FLOOR,
    max_depth: int = MAX_DEPTH,
) -> list[Impacted]:
    """
    Who is affected if `origin` fails, ranked by attenuated impact.

    Best-first (max-impact-first) traversal. Because impact is monotonically
    non-increasing along a path, the first time we pop a node we have its
    highest-impact path — so a visited set is safe and cycles terminate.
    """
    if origin not in graph.attrs:
        raise KeyError(f"unknown service: {origin}")

    best: dict[str, Impacted] = {}
    # heap of (-impact, depth, service_id, path) — negated for max-heap
    heap: list[tuple[float, int, str, list[str]]] = [(-1.0, 0, origin, [origin])]

    while heap:
        neg_impact, depth, sid, path = heapq.heappop(heap)
        impact = -neg_impact

        if sid in best:                     # already have a stronger path
            continue
        if sid != origin:
            a = graph.attrs.get(sid, {})
            best[sid] = Impacted(
                service_id=sid, impact=round(impact, 4), depth=depth,
                path=path, tier=a.get("tier"),
                revenue_per_minute=a.get("revenue_per_minute"),
            )
        if depth >= max_depth:
            continue

        for edge in graph.dependents_of(sid):
            if edge.stale and not include_stale:
                continue
            nxt = edge.to_service
            if nxt in best or nxt in path:  # visited, or would cycle
                continue
            nxt_impact = impact * edge.propagation_score
            if nxt_impact < impact_floor:
                continue
            heapq.heappush(heap, (-nxt_impact, depth + 1, nxt, path + [nxt]))

    return sorted(best.values(), key=lambda i: (-i.impact, i.service_id))


def critical_path_score(graph: Graph, service_id: str) -> float:
    """
    How critical is this service to the estate?

    Sum of impact-weighted revenue across everything it can take down. This is
    the number that ranks "what should we harden first" — and, unlike a raw
    dependent count, it is not gamed by a service with many trivial consumers.
    """
    total = 0.0
    for imp in blast_radius(graph, service_id):
        total += imp.revenue_at_risk
    own = graph.attrs.get(service_id, {}).get("revenue_per_minute") or 0.0
    return round(total + own, 2)


def single_points_of_failure(graph: Graph, *, min_tier0_impacted: int = 1) -> list[dict]:
    """
    Services whose failure takes down at least one tier-0 service through a
    synchronous path. These are the architecture review list.
    """
    out = []
    for sid in graph.attrs:
        impacted = blast_radius(graph, sid)
        tier0 = [i for i in impacted if i.tier == "tier0" and i.impact >= 0.5]
        if len(tier0) >= min_tier0_impacted:
            out.append({
                "service_id": sid,
                "tier0_impacted": [i.service_id for i in tier0],
                "revenue_at_risk_per_min": round(sum(i.revenue_at_risk for i in impacted), 2),
                "total_impacted": len(impacted),
            })
    return sorted(out, key=lambda r: -r["revenue_at_risk_per_min"])


# --------------------------------------------------------------------------
# openCypher equivalents — what actually runs against Neptune in production.
# Kept beside the algorithm so the two cannot drift apart unnoticed.
# --------------------------------------------------------------------------

CYPHER_BLAST_RADIUS = """
// Who breaks when $originId breaks, with attenuation along the path.
MATCH path = (impacted:Service)-[deps:DEPENDS_ON*1..6]->(origin:Service {id: $originId})
WHERE ALL(d IN deps WHERE d.stale = false OR $includeStale)
WITH impacted,
     REDUCE(acc = 1.0, d IN deps | acc * d.propagation_score) AS impact,
     LENGTH(path) AS depth,
     [n IN NODES(path) | n.id] AS path_ids
WHERE impact >= $impactFloor
WITH impacted, MAX(impact) AS impact, MIN(depth) AS depth, HEAD(COLLECT(path_ids)) AS path
RETURN impacted.id            AS service_id,
       impacted.tier          AS tier,
       impacted.revenue_per_minute AS revenue_per_minute,
       impact, depth, path
ORDER BY impact DESC, service_id
"""

CYPHER_UPSERT_SERVICE = """
MERGE (s:Service {id: $id})
ON CREATE SET s.discovered_at = $now, s.sources = $sources
ON MATCH  SET s.sources = apoc.coll.toSet(coalesce(s.sources, []) + $sources)
SET s.name = $name, s.environment = $environment, s.account = $account,
    s.region = $region, s.type = $type, s.tier = $tier,
    s.last_seen_at = $now
RETURN s.id AS id
"""

CYPHER_UPSERT_DEPENDENCY = """
MATCH (a:Service {id: $fromId}), (b:Service {id: $toId})
MERGE (a)-[d:DEPENDS_ON {kind: $kind}]->(b)
ON CREATE SET d.discovered_by = $source, d.observed_count = 1
ON MATCH  SET d.observed_count = coalesce(d.observed_count, 0) + 1
SET d.confidence        = $confidence,
    d.propagation_score = $propagationScore,
    d.last_seen_at      = $now,
    d.stale             = false
RETURN d.observed_count AS observed_count
"""

CYPHER_MARK_STALE = """
// Run daily. Absence of observation is not evidence of absence — flag, never delete.
MATCH ()-[d:DEPENDS_ON]->()
WHERE d.last_seen_at < $cutoff AND d.stale = false
SET d.stale = true
RETURN count(d) AS marked_stale
"""

CYPHER_COMPLETENESS = """
// Phase-1 promotion gate: fraction of services corroborated by 2+ sources.
MATCH (s:Service)
WITH count(s) AS total,
     sum(CASE WHEN SIZE(apoc.coll.toSet(s.sources)) >= 2 THEN 1 ELSE 0 END) AS corroborated
RETURN total, corroborated,
       CASE WHEN total = 0 THEN 0.0 ELSE toFloat(corroborated) / total END AS completeness
"""
