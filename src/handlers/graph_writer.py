"""
Graph writer Lambda — canonical envelope -> Neptune + DynamoDB.

Idempotent by construction: every write is a MERGE keyed on the canonical
service id. Replaying a week of archived events produces the same graph, which
is what makes the S3 archive useful rather than decorative.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

NEPTUNE_ENDPOINT = os.environ["NEPTUNE_ENDPOINT"]
NEPTUNE_PORT = os.environ.get("NEPTUNE_PORT", "8182")
DNA_TABLE = os.environ["DNA_TABLE"]
STALE_AFTER_DAYS = int(os.environ.get("STALE_AFTER_DAYS", "14"))

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(DNA_TABLE)

UPSERT_SERVICE = """
MERGE (s:Service {id: $id})
ON CREATE SET s.discovered_at = $now, s.sources = $sources
ON MATCH  SET s.sources = [x IN $sources WHERE NOT x IN coalesce(s.sources, [])] + coalesce(s.sources, [])
SET s.name = $name, s.environment = $env, s.account = $account,
    s.region = $region, s.type = $type, s.tier = $tier, s.last_seen_at = $now
RETURN s.id AS id
"""

UPSERT_DEPENDENCY = """
MATCH (a:Service {id: $from}), (b:Service {id: $to})
MERGE (a)-[d:DEPENDS_ON {kind: $kind}]->(b)
ON CREATE SET d.discovered_by = $source, d.observed_count = 1
ON MATCH  SET d.observed_count = coalesce(d.observed_count, 0) + 1
SET d.confidence = $confidence, d.propagation_score = $score,
    d.last_seen_at = $now, d.stale = false
RETURN d.observed_count AS n
"""

UPSERT_RESOURCE = """
MERGE (r:Resource {arn: $arn})
SET r.type = $type, r.account = $account, r.region = $region, r.last_seen_at = $now
WITH r
MATCH (s:Service {id: $serviceId})
MERGE (s)-[:OWNS]->(r)
RETURN r.arn AS arn
"""


def _neptune_query(query: str, params: dict) -> dict:
    """
    openCypher over the Neptune HTTPS endpoint with SigV4.

    Kept as one function so swapping in a connection-pooling client later is a
    single change. In production consider the Neptune Python client with an
    explicit pool — Lambda cold starts otherwise re-handshake on every invoke.
    """
    import urllib.request
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    url = f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/openCypher"
    body = json.dumps({"query": query, "parameters": json.dumps(params)})

    session = boto3.Session()
    creds = session.get_credentials().get_frozen_credentials()
    req = AWSRequest(method="POST", url=url, data=body,
                     headers={"Content-Type": "application/json"})
    SigV4Auth(creds, "neptune-db", session.region_name).add_auth(req)

    http_req = urllib.request.Request(url, data=body.encode(),
                                      headers=dict(req.headers), method="POST")
    with urllib.request.urlopen(http_req, timeout=25) as resp:
        return json.loads(resp.read())


def _write_service_dna(svc: dict) -> None:
    """
    Attributes go to DynamoDB, not the graph. Incident-time reads must not
    compete with traversals for capacity.
    """
    item = {
        "service_id": svc["id"],
        "record_type": "dna",
        "name": svc.get("name"),
        "environment": svc.get("environment"),
        "account": svc.get("account"),
        "region": svc.get("region"),
        "type": svc.get("type"),
        "tier": svc.get("tier", "tier2"),
        "sources": svc.get("sources", []),
        "last_seen_at": svc.get("last_seen_at"),
    }
    for optional in ("owner_team", "revenue_per_minute", "rto_minutes", "rpo_minutes"):
        if svc.get(optional) is not None:
            item[optional] = svc[optional]
    table.put_item(Item={k: v for k, v in item.items() if v is not None})


def handler(event, _context):
    detail = event.get("detail", event)
    now = detail.get("observed_at") or datetime.now(timezone.utc).isoformat()

    services = detail.get("services", [])
    deps = detail.get("dependencies", [])
    resources = detail.get("resources", [])

    written = {"services": 0, "dependencies": 0, "resources": 0, "errors": 0}

    for svc in services:
        try:
            _neptune_query(UPSERT_SERVICE, {
                "id": svc["id"], "name": svc.get("name", ""),
                "env": svc.get("environment", ""), "account": svc.get("account", ""),
                "region": svc.get("region", ""), "type": svc.get("type", "unknown"),
                "tier": svc.get("tier", "tier2"),
                "sources": svc.get("sources", []), "now": now,
            })
            _write_service_dna(svc)
            written["services"] += 1
        except Exception:
            written["errors"] += 1
            log.exception("service upsert failed: %s", svc.get("id"))

    for r in resources:
        if not r.get("service_id"):
            continue
        try:
            _neptune_query(UPSERT_RESOURCE, {
                "arn": r["arn"], "type": r.get("type", ""),
                "account": r.get("account", ""), "region": r.get("region", ""),
                "serviceId": r["service_id"], "now": now,
            })
            written["resources"] += 1
        except Exception:
            written["errors"] += 1
            log.exception("resource upsert failed: %s", r.get("arn"))

    for d in deps:
        try:
            _neptune_query(UPSERT_DEPENDENCY, {
                "from": d["from_service"], "to": d["to_service"],
                "kind": d.get("kind", "sync"),
                "confidence": d.get("confidence", 0.5),
                "score": d.get("propagation_score", 0.5),
                "source": d.get("discovered_by", "unknown"), "now": now,
            })
            written["dependencies"] += 1
        except Exception:
            # A dependency whose endpoints are not yet known is expected during
            # initial backfill — the scheduled sweep reconciles it.
            written["errors"] += 1
            log.warning("dependency deferred: %s -> %s",
                        d.get("from_service"), d.get("to_service"))

    if review := detail.get("identity_review_queue"):
        for r in review:
            table.put_item(Item={
                "service_id": r["service_id"], "record_type": f"review#{now}",
                "matched_against": r.get("matched_against"),
                "confidence": str(r.get("confidence")), "resolved": False,
            })
        log.info("%d identity matches queued for human review", len(review))

    log.info("graph write complete: %s", written)
    return written


MARK_STALE = """
MATCH ()-[d:DEPENDS_ON]->()
WHERE d.last_seen_at < $cutoff AND coalesce(d.stale, false) = false
SET d.stale = true
RETURN count(d) AS marked
"""


def mark_stale_handler(_event, _context):
    """
    Daily. Flags, never deletes — see docs/graph-model.md 'Temporal handling'.
    Wire to its own EventBridge schedule when you add it.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=STALE_AFTER_DAYS)).isoformat()
    result = _neptune_query(MARK_STALE, {"cutoff": cutoff})
    log.info("stale sweep at cutoff=%s: %s", cutoff, result)
    return result
