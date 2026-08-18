"""
Example: Testing normalizer.py with AWS Config sample events
"""
import json

# Sample 1: Simple ECS Service Event
ecs_event = {
    "source": "aws.config",
    "detail": {
        "configurationItem": {
            "arn": "arn:aws:ecs:us-east-1:123456789012:service/prod-cluster/order-api",
            "resourceType": "AWS::ECS::Service",
            "resourceName": "order-api",
            "awsAccountId": "123456789012",
            "awsRegion": "us-east-1",
            "tags": {
                "Environment": "prod",
                "Tier": "tier1",
                "Owner": "Platform Team"
            },
            "relationships": [
                {
                    "resourceId": "arn:aws:rds:us-east-1:123456789012:db:order-db",
                    "relationshipName": "depends_on"
                }
            ]
        }
    }
}

# Sample 2: RDS Database Event
rds_event = {
    "source": "aws.config",
    "detail": {
        "configurationItem": {
            "arn": "arn:aws:rds:us-east-1:123456789012:db:order-db-prod",
            "resourceType": "AWS::RDS::DBInstance",
            "resourceName": "order-db-prod",
            "awsAccountId": "123456789012",
            "awsRegion": "us-east-1",
            "tags": {
                "Environment": "prod",
                "Tier": "tier1",
                "Owner": "Data Team",
                "Criticality": "critical"
            },
            "relationships": [
                {
                    "resourceId": "sg-0123456",
                    "relationshipName": "attached_to_security_group"
                }
            ]
        }
    }
}

# Sample 3: Lambda Function Event
lambda_event = {
    "source": "aws.config",
    "detail": {
        "configurationItem": {
            "arn": "arn:aws:lambda:us-east-1:123456789012:function:payment-processor",
            "resourceType": "AWS::Lambda::Function",
            "resourceName": "payment-processor",
            "awsAccountId": "123456789012",
            "awsRegion": "us-east-1",
            "tags": {
                "Environment": "prod",
                "Tier": "tier2",
                "Owner": "Payments Team"
            },
            "relationships": [
                {
                    "resourceId": "arn:aws:dynamodb:us-east-1:123456789012:table/payments",
                    "relationshipName": "writes_to"
                }
            ]
        }
    }
}

# Sample 4: DynamoDB Table Event
dynamodb_event = {
    "source": "aws.config",
    "detail": {
        "configurationItem": {
            "arn": "arn:aws:dynamodb:us-east-1:123456789012:table/payment-transactions",
            "resourceType": "AWS::DynamoDB::Table",
            "resourceName": "payment-transactions",
            "awsAccountId": "123456789012",
            "awsRegion": "us-east-1",
            "tags": {
                "Environment": "prod",
                "Tier": "tier1",
                "Owner": "Payments Team"
            },
            "relationships": []
        }
    }
}

# Sample 5: EKS Cluster Event
eks_event = {
    "source": "aws.config",
    "detail": {
        "configurationItem": {
            "arn": "arn:aws:eks:us-east-1:123456789012:cluster/analytics-prod",
            "resourceType": "AWS::EKS::Cluster",
            "resourceName": "analytics-prod",
            "awsAccountId": "123456789012",
            "awsRegion": "us-east-1",
            "tags": {
                "Environment": "prod",
                "Tier": "tier1",
                "Owner": "Analytics Team"
            },
            "relationships": [
                {
                    "resourceId": "arn:aws:rds:us-east-1:123456789012:db:analytics-db",
                    "relationshipName": "depends_on"
                }
            ]
        }
    }
}

# Sample 6: ALB Event
alb_event = {
    "source": "aws.config",
    "detail": {
        "configurationItem": {
            "arn": "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/api-alb/123abc",
            "resourceType": "AWS::ElasticLoadBalancingV2::LoadBalancer",
            "resourceName": "api-alb",
            "awsAccountId": "123456789012",
            "awsRegion": "us-east-1",
            "tags": {
                "Environment": "prod",
                "Tier": "tier2",
                "Owner": "Platform Team"
            },
            "relationships": [
                {
                    "resourceId": "arn:aws:ecs:us-east-1:123456789012:service/prod-cluster/order-api",
                    "relationshipName": "routes_to"
                }
            ]
        }
    }
}

# Sample 7: API Gateway Event
apigw_event = {
    "source": "aws.config",
    "detail": {
        "configurationItem": {
            "arn": "arn:aws:apigateway:us-east-1::/restapis/abc123def456",
            "resourceType": "AWS::ApiGateway::RestApi",
            "resourceName": "orders-api",
            "awsAccountId": "123456789012",
            "awsRegion": "us-east-1",
            "tags": {
                "Environment": "prod",
                "Tier": "tier2",
                "Owner": "API Team"
            },
            "relationships": [
                {
                    "resourceId": "arn:aws:lambda:us-east-1:123456789012:function:order-handler",
                    "relationshipName": "invokes"
                }
            ]
        }
    }
}

# Sample 8: Ambiguous Resource Event (will trigger identity review queue)
ambiguous_event = {
    "source": "aws.config",
    "detail": {
        "configurationItem": {
            "arn": "arn:aws:ecs:us-east-1:123456789012:service/prod-cluster/api-service",
            "resourceType": "AWS::ECS::Service",
            "resourceName": "api-service",  # Ambiguous: could match multiple services
            "awsAccountId": "123456789012",
            "awsRegion": "us-east-1",
            "tags": {
                "Environment": "prod",
                "Tier": "tier1"
                # Missing Owner tag - will trigger identity review
            },
            "relationships": []
        }
    }
}


if __name__ == "__main__":
    # All sample events
    events = {
        "ECS Service": ecs_event,
        "RDS Database": rds_event,
        "Lambda Function": lambda_event,
        "DynamoDB Table": dynamodb_event,
        "EKS Cluster": eks_event,
        "Application Load Balancer": alb_event,
        "API Gateway": apigw_event,
        "Ambiguous Resource": ambiguous_event,
    }

    print("=" * 80)
    print("AWS CONFIG SAMPLE EVENTS FOR NORMALIZER.PY")
    print("=" * 80)
    print("\nThese events would be received by normalizer.py Lambda handler")
    print("Pass any of these to: handler(event, context)")
    print("\n")

    for name, event in events.items():
        print(f"\n{name}:")
        print("-" * 80)
        print(json.dumps(event, indent=2))
        print()

    # How to invoke normalizer.py with one of these events:
    print("\n" + "=" * 80)
    print("USAGE IN NORMALIZER.PY")
    print("=" * 80)
    print("""
# In normalizer.py:
from handlers.normalizer import handler

event = ecs_event  # Pick any sample from above
result = handler(event, None)

# Expected output:
# {
#     "status": "ok",
#     "summary": "service=order-api account=123456789012 region=us-east-1 deps=1",
#     "needs_review": 0
# }

# The handler will:
# 1. Archive the raw event to S3
# 2. Extract metadata from configurationItem
# 3. Resolve service identity
# 4. Discover dependencies
# 5. Build DiscoveryEvent envelope
# 6. Publish to genome bus
    """)

    # Show what gets published
    print("\n" + "=" * 80)
    print("WHAT GETS PUBLISHED TO EVENT BUS")
    print("=" * 80)
    print("""
After processing, normalizer.py publishes an event like:

{
  "EventBusName": "genome-bus",
  "Source": "cre.genome",
  "DetailType": "DiscoveryEvent",
  "Detail": {
    "source": "aws.config",
    "observed_at": "2026-08-17T15:30:45Z",
    "account": "123456789012",
    "region": "us-east-1",
    "services": [
      {
        "id": "svc-order-api-prod",
        "name": "order-api",
        "environment": "prod",
        "account": "123456789012",
        "region": "us-east-1",
        "type": "ecs",
        "tier": "tier1",
        "owner_team": "Platform Team",
        "sources": ["aws.config"]
      }
    ],
    "resources": [
      {
        "arn": "arn:aws:ecs:us-east-1:123456789012:service/prod-cluster/order-api",
        "type": "AWS::ECS::Service",
        "account": "123456789012",
        "region": "us-east-1",
        "service_id": "svc-order-api-prod"
      }
    ],
    "dependencies": [
      {
        "from_service": "svc-order-api-prod",
        "to_service": "svc-order-db-prod",
        "kind": "sync",
        "confidence": 0.85,
        "propagation_score": 0.90,
        "discovered_by": "aws.config"
      }
    ],
    "identity_review_queue": []
  }
}

This event is then consumed by graph_writer.py to update Neptune and DynamoDB.
    """)
