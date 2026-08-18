"""
Canonical genome types.

Every discovery source normalises into these before anything touches a store.
Keeping the canonical form in one module is what stops each new source adding
its own subtly different notion of "a service".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class DependencyKind(str, Enum):
    """How failure propagates across an edge — drives blast-radius weighting."""
    SYNC = "sync"      # caller fails when callee fails
    ASYNC = "async"    # caller degrades, queue absorbs
    DATA = "data"      # shared datastore; may not fail the caller at all
    INFRA = "infra"    # shared substrate (AZ, cluster, VPC endpoint)

    @property
    def propagation_weight(self) -> float:
        return {"sync": 1.0, "async": 0.4, "data": 0.6, "infra": 0.8}[self.value]


class Tier(str, Enum):
    """Business criticality. Drives both scoring and automation policy."""
    TIER0 = "tier0"    # revenue-critical, customer-facing
    TIER1 = "tier1"    # customer-facing, degradable
    TIER2 = "tier2"    # internal, business hours
    TIER3 = "tier3"    # best effort

    @property
    def automation_allowed(self) -> bool:
        """Tier 0 never auto-executes without a human, regardless of confidence."""
        return self.value != "tier0"


class Source(str, Enum):
    CONFIG = "aws-config"
    CLOUDTRAIL = "cloudtrail"
    XRAY = "xray"
    APP_SIGNALS = "application-signals"
    CMDB = "cmdb"
    TAGS = "resource-tags"
    MANUAL = "manual"

    @property
    def edge_confidence(self) -> float:
        """
        Observed traffic is evidence. Structural possibility is not.
        Do not let an inferred edge reach a causal agent as though observed.
        """
        return {
            "xray": 0.95,              # observed call
            "application-signals": 0.90,
            "cloudtrail": 0.75,        # observed API action
            "cmdb": 0.70,              # human-asserted
            "manual": 0.85,
            "aws-config": 0.45,        # structural: SG allows it, may never happen
            "resource-tags": 0.40,
        }[self.value]


_SLUG = re.compile(r"[^a-z0-9]+")


def slug(value: str) -> str:
    return _SLUG.sub("-", value.lower()).strip("-")


@dataclass
class Service:
    """The logical unit an operator reasons about."""
    id: str                       # canonical, stable: "prod/payments-api"
    name: str
    environment: str              # prod | staging | dev
    account: str
    region: str
    type: str = "unknown"         # ecs | eks | lambda | rds | mainframe | saas
    tier: Tier = Tier.TIER2
    owner_team: str | None = None
    revenue_per_minute: float | None = None   # USD; None = not yet assessed
    rto_minutes: int | None = None
    rpo_minutes: int | None = None
    sources: list[str] = field(default_factory=list)
    discovered_at: str = field(default_factory=utcnow)
    last_seen_at: str = field(default_factory=utcnow)

    @staticmethod
    def make_id(environment: str, name: str) -> str:
        return f"{slug(environment)}/{slug(name)}"

    @property
    def is_multi_sourced(self) -> bool:
        """Completeness signal: corroborated by two or more independent sources."""
        return len(set(self.sources)) >= 2

    def to_item(self) -> dict[str, Any]:
        d = asdict(self)
        d["tier"] = self.tier.value
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class Resource:
    """A physical AWS object. Many per Service; replaced on re-platforming."""
    arn: str
    type: str
    account: str
    region: str
    service_id: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    last_seen_at: str = field(default_factory=utcnow)

    @property
    def id(self) -> str:
        return self.arn


@dataclass
class Dependency:
    """A directed edge: `from_service` needs `to_service`."""
    from_service: str
    to_service: str
    kind: DependencyKind
    discovered_by: Source
    confidence: float | None = None       # defaults to the source's confidence
    observed_count: int = 1
    last_seen_at: str = field(default_factory=utcnow)
    stale: bool = False

    def __post_init__(self):
        if self.confidence is None:
            self.confidence = self.discovered_by.edge_confidence
        if self.from_service == self.to_service:
            raise ValueError(f"self-dependency rejected: {self.from_service}")

    @property
    def edge_key(self) -> str:
        return f"{self.from_service}|{self.kind.value}|{self.to_service}"

    @property
    def propagation_score(self) -> float:
        """How strongly failure travels this edge. Used by blast radius."""
        return self.kind.propagation_weight * float(self.confidence)


@dataclass
class DiscoveryEvent:
    """
    The canonical envelope every source normalises into.

    One schema on the bus is what lets a new source be added without touching
    any consumer — see ADR-002.
    """
    source: Source
    observed_at: str
    services: list[Service] = field(default_factory=list)
    resources: list[Resource] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    account: str | None = None
    region: str | None = None

    def summary(self) -> str:
        return (f"{self.source.value}: {len(self.services)} services, "
                f"{len(self.resources)} resources, {len(self.dependencies)} deps")
