###############################################################################
# CRE Reliability Genome — Phase 1 foundation
#
# Deploys: Neptune Serverless (graph) · DynamoDB (service DNA) · EventBridge
#          (discovery bus) · Lambda (normalizer, graph writer, discovery scan)
#          · S3 (raw event lake) · least-privilege IAM
#
# Deliberately NOT included — these belong to your platform team's existing
# modules and vary per organisation:
#   · VPC / subnets / NAT           (supply via var.vpc_id, var.subnet_ids)
#   · KMS CMK                       (supply via var.kms_key_arn)
#   · AWS Config aggregator          (usually already exists org-wide)
#   · Cross-account discovery roles  (see docs/ADR-003 for the trust pattern)
###############################################################################

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.40" }
  }
  # backend "s3" { ... }   # supply your own state backend
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = "cre-reliability-genome"
      Environment = var.environment
      ManagedBy   = "terraform"
      Owner       = var.owner_team
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  name   = "cre-genome-${var.environment}"
  prefix = substr(local.name, 0, 32)

  # Lambdas share one build artifact; the handler differs per function.
  lambda_handlers = {
    normalizer   = "handlers.normalizer.handler"
    graph_writer = "handlers.graph_writer.handler"
    discovery    = "handlers.discovery.handler"
  }
}

###############################################################################
# S3 — raw discovery events (replay + lineage)
###############################################################################
resource "aws_s3_bucket" "raw" {
  bucket = "${local.name}-raw-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.kms_key_arn == null ? "AES256" : "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
  }
}

resource "aws_s3_bucket_public_access_block" "raw" {
  bucket                  = aws_s3_bucket.raw.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "raw" {
  bucket = aws_s3_bucket.raw.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    id     = "tier-and-expire"
    status = "Enabled"
    filter {}
    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }
    expiration { days = var.raw_retention_days }
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
}

###############################################################################
# DynamoDB — service DNA
#
# Single-table: PK = service id, SK = record type. Keeps the incident-time
# read to one query regardless of how many record types a service accumulates.
###############################################################################
resource "aws_dynamodb_table" "service_dna" {
  name         = "${local.name}-service-dna"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "service_id"
  range_key    = "record_type"

  attribute {
    name = "service_id"
    type = "S"
  }
  attribute {
    name = "record_type"
    type = "S"
  }
  attribute {
    name = "tier"
    type = "S"
  }
  attribute {
    name = "last_seen_at"
    type = "S"
  }

  # "show me every tier-0 service and how fresh its data is" — the query the
  # completeness gate and the exec dashboard both need.
  global_secondary_index {
    name            = "tier-freshness-index"
    hash_key        = "tier"
    range_key       = "last_seen_at"
    projection_type = "ALL"
  }

  point_in_time_recovery { enabled = true }

  server_side_encryption {
    enabled     = var.kms_key_arn != null
    kms_key_arn = var.kms_key_arn
  }

  # Stream lets the graph writer react to DNA changes without polling.
  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  lifecycle { prevent_destroy = true }
}

###############################################################################
# Neptune Serverless — the dependency graph
###############################################################################
resource "aws_neptune_subnet_group" "genome" {
  name       = "${local.prefix}-subnets"
  subnet_ids = var.subnet_ids
}

resource "aws_security_group" "neptune" {
  name        = "${local.prefix}-neptune"
  description = "Neptune access from CRE genome Lambdas only"
  vpc_id      = var.vpc_id

  ingress {
    description     = "openCypher/Bolt from genome lambdas"
    from_port       = 8182
    to_port         = 8182
    protocol        = "tcp"
    security_groups = [aws_security_group.lambda.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "lambda" {
  name        = "${local.prefix}-lambda"
  description = "CRE genome ingestion lambdas"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_neptune_cluster_parameter_group" "genome" {
  family      = "neptune1.3"
  name        = "${local.prefix}-cluster-params"
  description = "CRE genome — audit logging on"

  parameter {
    name  = "neptune_enable_audit_log"
    value = "1"
  }
}

resource "aws_neptune_cluster" "genome" {
  cluster_identifier                  = local.name
  engine                              = "neptune"
  engine_version                      = "1.3.2.0"
  neptune_subnet_group_name           = aws_neptune_subnet_group.genome.name
  neptune_cluster_parameter_group_name = aws_neptune_cluster_parameter_group.genome.name
  vpc_security_group_ids              = [aws_security_group.neptune.id]

  # Serverless: scales to near-zero between discovery runs, which matters
  # because ingestion is bursty and query load is incident-driven.
  serverless_v2_scaling_configuration {
    min_capacity = var.neptune_min_ncu
    max_capacity = var.neptune_max_ncu
  }

  iam_database_authentication_enabled = true
  storage_encrypted                   = true
  kms_key_arn                         = var.kms_key_arn
  backup_retention_period             = 7
  preferred_backup_window             = "03:00-04:00"
  enable_cloudwatch_logs_exports      = ["audit"]
  apply_immediately                   = var.environment != "prod"
  skip_final_snapshot                 = var.environment != "prod"
  final_snapshot_identifier           = var.environment == "prod" ? "${local.name}-final" : null

  lifecycle { prevent_destroy = false } # set true once past Phase 1
}

resource "aws_neptune_cluster_instance" "writer" {
  identifier                   = "${local.name}-writer"
  cluster_identifier           = aws_neptune_cluster.genome.id
  instance_class               = "db.serverless"
  engine                       = "neptune"
  neptune_parameter_group_name = aws_neptune_parameter_group.genome.name
}

resource "aws_neptune_parameter_group" "genome" {
  family = "neptune1.3"
  name   = "${local.prefix}-instance-params"
}

###############################################################################
# EventBridge — the discovery bus
#
# One bus, one schema. Adding a source means adding a rule, never touching a
# consumer. See ADR-002.
###############################################################################
resource "aws_cloudwatch_event_bus" "genome" {
  name = "${local.name}-discovery"
}

resource "aws_cloudwatch_event_archive" "genome" {
  name             = "${local.name}-archive"
  event_source_arn = aws_cloudwatch_event_bus.genome.arn
  retention_days   = 90
  description      = "Replay discovery events after a bad ingestion"
}

# AWS Config changes -> normalizer (on the DEFAULT bus, where Config publishes)
resource "aws_cloudwatch_event_rule" "config_changes" {
  name        = "${local.name}-config-changes"
  description = "Resource configuration changes feed genome discovery"
  event_pattern = jsonencode({
    source        = ["aws.config"]
    "detail-type" = ["Config Configuration Item Change"]
  })
}

resource "aws_cloudwatch_event_target" "config_to_normalizer" {
  rule      = aws_cloudwatch_event_rule.config_changes.name
  target_id = "normalizer"
  arn       = aws_lambda_function.fn["normalizer"].arn
  dead_letter_config { arn = aws_sqs_queue.dlq.arn }
  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 3
  }
}

# Normalised events -> graph writer (on our own bus)
resource "aws_cloudwatch_event_rule" "normalised" {
  name           = "${local.name}-normalised"
  event_bus_name = aws_cloudwatch_event_bus.genome.name
  event_pattern = jsonencode({
    source        = ["cre.genome"]
    "detail-type" = ["DiscoveryEvent"]
  })
}

resource "aws_cloudwatch_event_target" "normalised_to_writer" {
  rule           = aws_cloudwatch_event_rule.normalised.name
  event_bus_name = aws_cloudwatch_event_bus.genome.name
  target_id      = "graph-writer"
  arn            = aws_lambda_function.fn["graph_writer"].arn
  dead_letter_config { arn = aws_sqs_queue.dlq.arn }
  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 3
  }
}

# Scheduled full discovery sweep — catches anything the event stream missed.
resource "aws_cloudwatch_event_rule" "discovery_schedule" {
  name                = "${local.name}-discovery-schedule"
  description         = "Full estate sweep; reconciles drift from event-driven ingestion"
  schedule_expression = var.discovery_schedule
}

resource "aws_cloudwatch_event_target" "scheduled_discovery" {
  rule      = aws_cloudwatch_event_rule.discovery_schedule.name
  target_id = "discovery"
  arn       = aws_lambda_function.fn["discovery"].arn
}

resource "aws_sqs_queue" "dlq" {
  name                      = "${local.name}-dlq"
  message_retention_seconds = 1209600 # 14 days
  kms_master_key_id         = var.kms_key_arn
}

resource "aws_lambda_permission" "eventbridge" {
  for_each      = local.lambda_handlers
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn[each.key].function_name
  principal     = "events.amazonaws.com"
}

###############################################################################
# Lambda
###############################################################################
data "archive_file" "src" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/.build/genome-src.zip"
  excludes    = ["__pycache__", "*.pyc"]
}

resource "aws_lambda_function" "fn" {
  for_each = local.lambda_handlers

  function_name    = "${local.name}-${each.key}"
  role             = aws_iam_role.lambda.arn
  handler          = each.value
  runtime          = "python3.12"
  filename         = data.archive_file.src.output_path
  source_code_hash = data.archive_file.src.output_base64sha256
  timeout          = each.key == "discovery" ? 900 : 60
  memory_size      = each.key == "discovery" ? 1024 : 512

  environment {
    variables = {
      NEPTUNE_ENDPOINT = aws_neptune_cluster.genome.endpoint
      NEPTUNE_PORT     = "8182"
      DNA_TABLE        = aws_dynamodb_table.service_dna.name
      RAW_BUCKET       = aws_s3_bucket.raw.id
      EVENT_BUS        = aws_cloudwatch_event_bus.genome.name
      STALE_AFTER_DAYS = tostring(var.stale_after_days)
      LOG_LEVEL        = var.log_level
    }
  }

  vpc_config {
    subnet_ids         = var.subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  tracing_config { mode = "Active" }

  dead_letter_config { target_arn = aws_sqs_queue.dlq.arn }
}

resource "aws_cloudwatch_log_group" "fn" {
  for_each          = local.lambda_handlers
  name              = "/aws/lambda/${local.name}-${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

###############################################################################
# Observability — the Phase 1 promotion gate is itself measured
###############################################################################
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${local.name}-dlq-not-empty"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "Discovery events are failing to process — genome is going stale"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_sns_topic_arns
}

resource "aws_cloudwatch_metric_alarm" "ingestion_stalled" {
  alarm_name          = "${local.name}-ingestion-stalled"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Invocations"
  namespace           = "AWS/Lambda"
  period              = 3600
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "No graph writes in 2h — discovery pipeline may be broken"
  dimensions          = { FunctionName = aws_lambda_function.fn["graph_writer"].function_name }
  treat_missing_data  = "breaching"
  alarm_actions       = var.alarm_sns_topic_arns
}
