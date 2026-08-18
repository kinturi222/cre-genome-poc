"""
Service identity resolution.

THE PROBLEM
-----------
The same logical service appears under different names in every source:

    AWS Config    arn:aws:ecs:us-east-1:123:service/prod-cluster/payments-api-svc
    X-Ray         payments-api
    CloudWatch    payments_api
    CMDB          PAY-API-PROD          (CI number CI0004421)
    Tags          Service=payments  Component=api

If these become five nodes, the graph is worse than useless: blast radius
under-reports because the edges are spread across duplicates, and the causal
agent reasons over a fragmented topology.

THE APPROACH
------------
Three tiers, tried in order. Later tiers never override an earlier match.

  1. EXPLICIT   an authoritative tag or CMDB mapping. Trusted outright.
  2. STRUCTURAL parsed from the ARN. Deterministic, no guessing.
  3. FUZZY      normalised-name similarity, and ONLY above a high threshold,
                and only ever recorded as a *candidate* for human confirmation.

Tier 3 never silently merges. An unconfirmed fuzzy match is written to a review
queue, because a wrong merge is far more damaging than a missing one: it invents
dependencies that do not exist and hides ones that do.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .models import Service, Source, slug

# Tags an organisation designates as authoritative, in priority order.
AUTHORITATIVE_TAGS = ("cre:service-id", "ServiceId", "Service", "app", "Application")

# Suffixes/prefixes that carry no identity — stripped before comparison.
NOISE = re.compile(
    r"^(prod|production|staging|stage|dev|test|qa|uat)[-_]|"
    r"[-_](prod|production|staging|stage|dev|test|qa|uat|svc|service|"
    r"app|api|cluster|tg|alb|nlb|fn|lambda)$",
    re.IGNORECASE,
)

FUZZY_ACCEPT = 0.92     # auto-link at/above this
FUZZY_REVIEW = 0.80     # queue for human review between REVIEW and ACCEPT


# camelCase / PascalCase carry word boundaries that slug() would otherwise lose:
# "PaymentsAPI" must reduce to the same token as "payments-api".
_CAMEL_1 = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_2 = re.compile(r"([A-Z]+)([A-Z][a-z])")


def decamel(name: str) -> str:
    return _CAMEL_2.sub(r"\1-\2", _CAMEL_1.sub(r"\1-\2", name))


def normalise(name: str) -> str:
    """Strip environment and platform noise so names become comparable."""
    n = slug(decamel(name))
    prev = None
    while prev != n:                       # repeatedly strip stacked suffixes
        prev = n
        n = NOISE.sub("", n).strip("-_")
    return n


def parse_arn(arn: str) -> dict[str, str]:
    """
    arn:partition:service:region:account:resource-type/resource-id
    Returns {} for anything that is not a well-formed ARN.
    """
    parts = arn.split(":", 5)
    if len(parts) < 6 or parts[0] != "arn":
        return {}
    tail = parts[5]
    if "/" in tail:
        rtype, _, rid = tail.partition("/")
    else:
        rtype, rid = tail.split(":", 1) if ":" in tail else ("", tail)
    return {
        "aws_service": parts[2], "region": parts[3],
        "account": parts[4], "resource_type": rtype, "resource_id": rid,
    }


def name_from_arn(arn: str) -> str | None:
    """
    Extract the *logical* name. ECS services carry cluster/service — the
    service segment is the identity, the cluster is placement.
    """
    p = parse_arn(arn)
    if not p:
        return None
    rid = p["resource_id"]
    if p["aws_service"] == "ecs" and "/" in rid:
        rid = rid.split("/")[-1]           # cluster/service -> service
    if p["aws_service"] == "rds" and rid.startswith("cluster-"):
        rid = rid[len("cluster-"):]
    return rid or None


@dataclass
class Resolution:
    service_id: str
    method: str                 # explicit | structural | fuzzy | new
    confidence: float
    needs_review: bool = False
    matched_against: str | None = None


class IdentityResolver:
    """
    Stateful across a batch: earlier resolutions inform later ones, so a
    CloudTrail event arriving after a Config scan links to the same node.
    """

    def __init__(self, known: dict[str, Service] | None = None):
        # canonical id -> Service
        self._known: dict[str, Service] = dict(known or {})
        # normalised name -> canonical id, for fast structural/fuzzy lookup
        self._by_norm: dict[str, str] = {}
        for sid, svc in self._known.items():
            self._by_norm.setdefault(normalise(svc.name), sid)
        self.review_queue: list[Resolution] = []

    # ---------------------------------------------------------------- tier 1
    def _explicit(self, tags: dict[str, str], environment: str) -> Resolution | None:
        for key in AUTHORITATIVE_TAGS:
            for tag_key, tag_val in tags.items():
                if tag_key.lower() == key.lower() and tag_val.strip():
                    sid = Service.make_id(environment, tag_val.strip())
                    return Resolution(sid, "explicit", 1.0)
        return None

    # ---------------------------------------------------------------- tier 2
    def _structural(self, arn: str | None, environment: str) -> Resolution | None:
        if not arn:
            return None
        logical = name_from_arn(arn)
        if not logical:
            return None
        norm = normalise(logical)
        if norm in self._by_norm:
            return Resolution(self._by_norm[norm], "structural", 0.9,
                              matched_against=norm)
        # Build the id from the NORMALISED name so every source converges on
        # the same canonical node even when no authoritative tag exists.
        return Resolution(Service.make_id(environment, norm), "structural", 0.85)

    # ---------------------------------------------------------------- tier 3
    def _fuzzy(self, name: str, environment: str) -> Resolution | None:
        norm = normalise(name)
        if not norm:
            return None
        best_id, best_score, best_norm = None, 0.0, None
        for known_norm, sid in self._by_norm.items():
            score = SequenceMatcher(None, norm, known_norm).ratio()
            if score > best_score:
                best_id, best_score, best_norm = sid, score, known_norm
        if best_id is None or best_score < FUZZY_REVIEW:
            return None
        r = Resolution(best_id, "fuzzy", best_score,
                       needs_review=best_score < FUZZY_ACCEPT,
                       matched_against=best_norm)
        if r.needs_review:
            # Do NOT merge. Record the candidate and let a human decide.
            self.review_queue.append(r)
            return None
        return r

    # ------------------------------------------------------------------ main
    def resolve(
        self,
        *,
        name: str,
        environment: str,
        arn: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> Resolution:
        tags = tags or {}
        for attempt in (
            lambda: self._explicit(tags, environment),
            lambda: self._structural(arn, environment),
            lambda: self._fuzzy(name, environment),
        ):
            r = attempt()
            if r:
                self._remember(r.service_id, name)
                return r
        r = Resolution(Service.make_id(environment, normalise(name)), "new", 0.8)
        self._remember(r.service_id, name)
        return r

    def _remember(self, service_id: str, name: str) -> None:
        """
        Register BOTH the incoming name and the canonical id's own tail.
        The second is what lets an explicit-tag id ("prod/payments-api") absorb
        later structural matches that normalise to "payments".
        """
        self._by_norm.setdefault(normalise(name), service_id)
        tail = service_id.split("/", 1)[-1]
        self._by_norm.setdefault(normalise(tail), service_id)

    def merge_sources(self, existing: Service, incoming: Service) -> Service:
        """
        Union the sources; prefer the more specific value for every field.
        Never let a low-confidence source blank a field an authoritative one set.
        """
        existing.sources = sorted(set(existing.sources) | set(incoming.sources))
        existing.last_seen_at = max(existing.last_seen_at, incoming.last_seen_at)
        for f in ("owner_team", "revenue_per_minute", "rto_minutes",
                  "rpo_minutes", "type"):
            incoming_val = getattr(incoming, f)
            if incoming_val not in (None, "unknown") and getattr(existing, f) in (None, "unknown"):
                setattr(existing, f, incoming_val)
        # tier: keep the most severe assessment seen
        order = ["tier3", "tier2", "tier1", "tier0"]
        if order.index(incoming.tier.value) > order.index(existing.tier.value):
            existing.tier = incoming.tier
        return existing
