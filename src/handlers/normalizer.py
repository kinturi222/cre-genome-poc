"""
Normalizer Lambda — every discovery source enters the genome through here.

Input : native event (AWS Config CI change today; X-Ray, CMDB next)
Output: one canonical DiscoveryEvent envelope on the genome bus
        + the raw event archived to S3 for replay and lineage

Adding a source means adding an adapter function below and an EventBridge
rule. No consumer changes. That is the whole point of the envelope (ADR-002).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import boto3

from common.identity import IdentityResolver
from common.models import (DiscoveryEvent, Dependency, DependencyKind,
                           Resource, Service, Source, Tier, utcnow)

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

events = boto3.client("events")
s3 = boto3.client("s3")

BUS = os.environ["EVENT_BUS"]
RAW_BUCKET = os.environ["RAW_BUCKET"]

# Config resourceType -> (our service type, default tier)
TYPE_MAP = {
    "AWS::ECS::Service":       ("ecs", Tier.TIER2),
    "AWS::EKS::Cluster":       ("eks", Tier.TIER1),
    "AWS::Lambda::Function":   ("lambda", Tier.TIER2),
    "AWS::RDS::DBInstance":    ("rds", Tier.TIER1),
    "AWS::RDS::DBCluster":     ("rds", Tier.TIER1),
    "AWS::DynamoDB::Table":    ("dynamodb", Tier.TIER1),
    "AWS::ElasticLoadBalancingV2::LoadBalancer": ("alb", Tier.TIER2),
    "AWS::ApiGateway::RestApi": ("apigw", Tier.TIER2),
}


def _archive(raw: dict, source: str) -> None:
    """Write the untouched source event before we transform anything."""
    now = datetime.now(timezone.utc)
    key = (f"raw/source={source}/dt={now:%Y-%m-%d}/hour={now:%H}/"
           f"{now:%Y%m%dT%H%M%S%f}.json")
    s3.put_object(Bucket=RAW_BUCKET, Key=key,
                  Body=json.dumps(raw).encode(), ContentType="application/json")


def _environment_from_tags(tags: dict[str, str], default: str = "prod") -> str:
    # Multiple tag key variants account for inconsistent tagging practices across teams
    for k in ("Environment", "environment", "env", "Stage"):
        if v := tags.get(k):
            return v.lower()
    return default


def _tier_from_tags(tags: dict[str, str], fallback: Tier) -> Tier:
    # Supports numeric (0-3), text (tier0-tier3), and semantic labels (critical/high/medium/low)
    # to normalize organizational tagging variance. Falls back to resource-type default if untagged.
    raw = (tags.get("Tier") or tags.get("criticality") or "").lower().strip()
    mapping = {
        "0": Tier.TIER0, "tier0": Tier.TIER0, "critical": Tier.TIER0,
        "1": Tier.TIER1, "tier1": Tier.TIER1, "high": Tier.TIER1,
        "2": Tier.TIER2, "tier2": Tier.TIER2, "medium": Tier.TIER2,
        "3": Tier.TIER3, "tier3": Tier.TIER3, "low": Tier.TIER3,
    }
    return mapping.get(raw, fallback)


def adapt_config_event(detail: dict, resolver: IdentityResolver) -> DiscoveryEvent:
    """AWS Config configuration-item change -> canonical envelope."""
    ci = detail.get("configurationItem") or detail.get("configurationItemSummary") or {}
    arn = ci.get("ARN") or ci.get("arn", "")
    rtype = ci.get("resourceType", "")
    tags = ci.get("tags") or {}
    account = ci.get("awsAccountId", "")
    region = ci.get("awsRegion", "")
    name = ci.get("resourceName") or arn.split("/")[-1] or "unknown"

    svc_type, default_tier = TYPE_MAP.get(rtype, ("unknown", Tier.TIER2))
    environment = _environment_from_tags(tags)

    res = resolver.resolve(name=name, environment=environment, arn=arn, tags=tags)
    log.info("identity: %s -> %s (%s, conf=%.2f)",
             name, res.service_id, res.method, res.confidence)

    service = Service(
        id=res.service_id, name=name, environment=environment,
        account=account, region=region, type=svc_type,
        tier=_tier_from_tags(tags, default_tier),
        owner_team=tags.get("Owner") or tags.get("team"),
        sources=[Source.CONFIG.value],
    )
    resource = Resource(arn=arn, type=rtype, account=account, region=region,
                        service_id=res.service_id, tags=tags)

    # Structural relationships Config can see. LOW confidence by design: a
    # security-group rule proves a path is *possible*, not that it is *used*.
    deps: list[Dependency] = []
    for rel in ci.get("relationships", []) or []:
        target = rel.get("resourceId") or ""
        rel_name = (rel.get("relationshipName") or "").lower()
        # Skip self-references and containment relationships (parent-child hierarchy, not dependencies)
        if not target or "contains" in rel_name:
            continue
        t = resolver.resolve(name=target, environment=environment)
        if t.service_id == res.service_id:
            continue
        # 'attached' relationships (e.g., security groups) are infrastructure dependencies;
        # others (e.g., DynamoDB streams, cross-service references) are synchronous calls
        kind = DependencyKind.INFRA if "attached" in rel_name else DependencyKind.SYNC
        try:
            deps.append(Dependency(
                from_service=res.service_id, to_service=t.service_id,
                kind=kind, discovered_by=Source.CONFIG))
        except ValueError:
            continue    # self-dependency

    return DiscoveryEvent(
        source=Source.CONFIG, observed_at=utcnow(), services=[service],
        resources=[resource], dependencies=deps,
        account=account, region=region,
    )


ADAPTERS = {"aws.config": adapt_config_event}


def handler(event, _context):
    src = event.get("source", "")
    detail = event.get("detail", {})
    log.info("normalizer received source=%s", src)

    try:
        _archive(event, src)
    except Exception:                       # archiving must never block ingestion
        log.exception("raw archive failed; continuing")

    adapter = ADAPTERS.get(src)
    if not adapter:
        log.warning("no adapter for source=%s — dropping", src)
        return {"status": "ignored", "source": src}

    resolver = IdentityResolver()
    # Call the adapter function with IdentityResolver to transform source event to canonical envelope
    envelope = adapter(detail, resolver)

    payload = {
        "source": envelope.source.value,
        "observed_at": envelope.observed_at,
        "account": envelope.account,
        "region": envelope.region,
        "services": [s.to_item() for s in envelope.services],
        "resources": [r.__dict__ for r in envelope.resources],
        "dependencies": [{
            "from_service": d.from_service, "to_service": d.to_service,
            "kind": d.kind.value, "confidence": d.confidence,
            "propagation_score": d.propagation_score,
            "discovered_by": d.discovered_by.value, "last_seen_at": d.last_seen_at,
        } for d in envelope.dependencies],
        # Ambiguous identity matches (confidence < 1.0) never merge silently — they surface
        # here for manual review to prevent silent data corruption or namespace collisions.
        "identity_review_queue": [
            {"service_id": r.service_id, "confidence": r.confidence,
             "matched_against": r.matched_against}
            for r in resolver.review_queue
        ],
    }

    events.put_events(Entries=[{
        "EventBusName": BUS,
        "Source": "cre.genome",
        "DetailType": "DiscoveryEvent",
        "Detail": json.dumps(payload),
    }])

    log.info("normalised -> %s", envelope.summary())
    return {"status": "ok", "summary": envelope.summary(),
            "needs_review": len(resolver.review_queue)}
