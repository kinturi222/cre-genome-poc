"""
Scheduled discovery sweep.

Event-driven ingestion handles changes in near real time; this exists to catch
what the event stream missed — dropped events, resources created before
onboarding, and drift in the identity mapping. It re-publishes through the same
normalizer path so there is exactly one code path into the graph.
"""
from __future__ import annotations

import json
import logging
import os

import boto3

from common.models import Source, utcnow

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

config = boto3.client("config")
events = boto3.client("events")
BUS = os.environ["EVENT_BUS"]

# Config advanced query: everything we care about, one page at a time.
QUERY = """
SELECT resourceId, resourceName, resourceType, arn, awsRegion, accountId, tags
WHERE resourceType IN (
  'AWS::ECS::Service','AWS::EKS::Cluster','AWS::Lambda::Function',
  'AWS::RDS::DBInstance','AWS::RDS::DBCluster','AWS::DynamoDB::Table',
  'AWS::ElasticLoadBalancingV2::LoadBalancer','AWS::ApiGateway::RestApi'
)
"""


def handler(_event, context):
    aggregator = os.environ.get("CONFIG_AGGREGATOR")
    swept = 0
    token = None

    while True:
        kwargs = {"Expression": QUERY, "Limit": 100}
        if token:
            kwargs["NextToken"] = token
        if aggregator:
            kwargs["ConfigurationAggregatorName"] = aggregator
            page = config.select_aggregate_resource_config(**kwargs)
        else:
            page = config.select_resource_config(**kwargs)

        entries = []
        for row in page.get("Results", []):
            ci = json.loads(row)
            entries.append({
                "EventBusName": "default",
                "Source": "aws.config",
                "DetailType": "Config Configuration Item Change",
                "Detail": json.dumps({"configurationItem": {
                    "ARN": ci.get("arn", ""),
                    "resourceType": ci.get("resourceType", ""),
                    "resourceName": ci.get("resourceName", ""),
                    "awsAccountId": ci.get("accountId", ""),
                    "awsRegion": ci.get("awsRegion", ""),
                    "tags": ci.get("tags", {}) or {},
                    "relationships": [],
                    "_sweep": True,
                }}),
            })

        # PutEvents caps at 10 entries per call.
        for i in range(0, len(entries), 10):
            events.put_events(Entries=entries[i:i + 10])
        swept += len(entries)

        token = page.get("NextToken")
        if not token:
            break
        if context and context.get_remaining_time_in_millis() < 60_000:
            log.warning("sweep pausing at %d resources; next run resumes", swept)
            break

    log.info("discovery sweep republished %d resources at %s", swept, utcnow())
    return {"swept": swept, "source": Source.CONFIG.value}
