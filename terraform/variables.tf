variable "region" {
  type        = string
  default     = "us-east-1"
  description = "Deployment region. Neptune is regional; run one genome per region and federate at query time if you are multi-region."
}

variable "environment" {
  type        = string
  description = "prod | staging | dev — also namespaces every resource"
  validation {
    condition     = contains(["prod", "staging", "dev"], var.environment)
    error_message = "environment must be prod, staging or dev."
  }
}

variable "owner_team" {
  type        = string
  default     = "platform-reliability"
  description = "Tagged on everything; also the escalation target for genome alarms."
}

# --------------------------------------------------------------- networking
variable "vpc_id" {
  type        = string
  description = "Existing VPC. Neptune has no public endpoint by design."
}

variable "subnet_ids" {
  type        = list(string)
  description = "At least two PRIVATE subnets in different AZs."
  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "Neptune requires subnets in at least two availability zones."
  }
}

# ---------------------------------------------------------------- encryption
variable "kms_key_arn" {
  type        = string
  default     = null
  description = "Customer-managed key. Leave null in dev to use AWS-managed keys; set it for prod."
}

# ------------------------------------------------------------------ Neptune
variable "neptune_min_ncu" {
  type        = number
  default     = 1.0
  description = "Serverless floor. 1.0 is enough for Phase 1 discovery volumes."
}

variable "neptune_max_ncu" {
  type        = number
  default     = 8.0
  description = "Serverless ceiling. Raise only after measuring — bursty ingestion is normal and brief."
}

# ---------------------------------------------------------------- discovery
variable "discovery_schedule" {
  type        = string
  default     = "rate(6 hours)"
  description = <<-EOT
    Full estate sweep cadence. Event-driven ingestion handles changes in near
    real time; this sweep exists to catch what the event stream missed
    (dropped events, resources created before onboarding, drift in the
    identity mapping). Six hours is a reasonable Phase 1 default — tighten
    only if the reconciliation delta proves consistently non-trivial.
  EOT
}

variable "stale_after_days" {
  type        = number
  default     = 14
  description = <<-EOT
    An edge unobserved for this long is FLAGGED stale, never deleted. Failover
    paths are exercised rarely; deleting them destroys the evidence needed to
    explain the incident where they finally matter.
  EOT
}

variable "member_account_ids" {
  type        = list(string)
  default     = []
  description = "Member accounts to discover cross-account. Requires the read role below to exist in each."
}

variable "member_discovery_role_name" {
  type        = string
  default     = "CREGenomeDiscoveryRead"
  description = "Same-named read-only role deployed to every member account via stackset."
}

variable "consumer_principal_arns" {
  type        = list(string)
  default     = []
  description = "Principals allowed to assume the read-only consumer role (Causal AI agent, Intent Compiler, dashboards)."
}

# ------------------------------------------------------------- observability
variable "alarm_sns_topic_arns" {
  type        = list(string)
  default     = []
  description = "Where genome health alarms go. An empty list means nobody learns the genome went stale — set this."
}

variable "log_retention_days" {
  type        = number
  default     = 30
}

variable "log_level" {
  type    = string
  default = "INFO"
}

variable "raw_retention_days" {
  type        = number
  default     = 365
  description = "Raw discovery events. One year supports replay and year-over-year topology comparison."
}
