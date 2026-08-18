###############################################################################
# IAM — least privilege.
#
# The genome reads the estate and writes only its own stores. It has no
# permission to modify any discovered resource. That separation is what lets
# security sign off on running it estate-wide with an org-level read role.
###############################################################################

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "vpc" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy_attachment" "xray" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

# ---------------------------------------------------------------- write side
data "aws_iam_policy_document" "genome_write" {
  statement {
    sid    = "NeptuneDataAccess"
    effect = "Allow"
    actions = [
      "neptune-db:connect",
      "neptune-db:ReadDataViaQuery",
      "neptune-db:WriteDataViaQuery",
      "neptune-db:DeleteDataViaQuery",
    ]
    resources = [
      "arn:aws:neptune-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:${aws_neptune_cluster.genome.cluster_resource_id}/*"
    ]
  }

  statement {
    sid    = "ServiceDnaTable"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query",
      "dynamodb:PutItem", "dynamodb:BatchWriteItem", "dynamodb:UpdateItem",
    ]
    resources = [
      aws_dynamodb_table.service_dna.arn,
      "${aws_dynamodb_table.service_dna.arn}/index/*",
    ]
  }

  statement {
    sid       = "RawEventLake"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.raw.arn}/*"]
  }

  statement {
    sid       = "PublishNormalisedEvents"
    effect    = "Allow"
    actions   = ["events:PutEvents"]
    resources = [aws_cloudwatch_event_bus.genome.arn]
  }

  statement {
    sid       = "DeadLetter"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.dlq.arn]
  }

  dynamic "statement" {
    for_each = var.kms_key_arn == null ? [] : [1]
    content {
      sid       = "KmsForOwnStores"
      effect    = "Allow"
      actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
      resources = [var.kms_key_arn]
    }
  }
}

resource "aws_iam_role_policy" "genome_write" {
  name   = "genome-write"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.genome_write.json
}

# ----------------------------------------------------------------- read side
# Discovery is READ ONLY across the estate. Note there is no Put/Update/Delete
# on any discovered service anywhere in this document — by design.
data "aws_iam_policy_document" "estate_read" {
  statement {
    sid    = "DiscoveryReadOnly"
    effect = "Allow"
    actions = [
      "config:SelectResourceConfig",
      "config:SelectAggregateResourceConfig",
      "config:BatchGetResourceConfig",
      "config:DescribeConfigurationAggregators",
      "cloudtrail:LookupEvents",
      "tag:GetResources",
      "tag:GetTagKeys",
      "xray:BatchGetTraces",
      "xray:GetServiceGraph",
      "xray:GetTraceSummaries",
      "application-signals:ListServices",
      "application-signals:GetService",
      "application-signals:ListServiceLevelObjectives",
      "cloudwatch:GetMetricData",
      "cloudwatch:ListMetrics",
      "ce:GetCostAndUsage",
    ]
    resources = ["*"] # these APIs do not support resource-level scoping
    # Blast radius requires estate-wide visibility; the mitigation is that the
    # role is read-only and its use is logged in CloudTrail Lake.
  }

  # Cross-account discovery: assume a same-named read role in every member
  # account. Deploy that role via your org's baseline stackset. See ADR-003.
  dynamic "statement" {
    for_each = length(var.member_account_ids) == 0 ? [] : [1]
    content {
      sid       = "AssumeMemberDiscoveryRole"
      effect    = "Allow"
      actions   = ["sts:AssumeRole"]
      resources = [for a in var.member_account_ids : "arn:aws:iam::${a}:role/${var.member_discovery_role_name}"]
    }
  }
}

resource "aws_iam_role_policy" "estate_read" {
  name   = "estate-read"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.estate_read.json
}

# -------------------------------------------------------- consumer read role
# What the Causal AI agent, Intent Compiler and dashboards assume. Read-only
# on the genome; cannot write, so a downstream bug cannot corrupt the graph.
data "aws_iam_policy_document" "consumer_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = length(var.consumer_principal_arns) > 0 ? var.consumer_principal_arns : [data.aws_caller_identity.current.arn]
    }
  }
}

resource "aws_iam_role" "consumer" {
  name               = "${local.name}-consumer"
  description        = "Read-only genome access for CRE innovations 2-6"
  assume_role_policy = data.aws_iam_policy_document.consumer_assume.json
}

data "aws_iam_policy_document" "consumer_read" {
  statement {
    effect  = "Allow"
    actions = ["neptune-db:connect", "neptune-db:ReadDataViaQuery"]
    resources = [
      "arn:aws:neptune-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:${aws_neptune_cluster.genome.cluster_resource_id}/*"
    ]
  }
  statement {
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.service_dna.arn, "${aws_dynamodb_table.service_dna.arn}/index/*"]
  }
}

resource "aws_iam_role_policy" "consumer_read" {
  name   = "genome-read"
  role   = aws_iam_role.consumer.id
  policy = data.aws_iam_policy_document.consumer_read.json
}
