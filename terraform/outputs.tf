output "neptune_endpoint" {
  value       = aws_neptune_cluster.genome.endpoint
  description = "openCypher endpoint (port 8182). Reachable only from inside the VPC."
}

output "neptune_reader_endpoint" {
  value       = aws_neptune_cluster.genome.reader_endpoint
  description = "Point read-heavy consumers (blast radius, dashboards) here."
}

output "service_dna_table" {
  value = aws_dynamodb_table.service_dna.name
}

output "discovery_bus_name" {
  value       = aws_cloudwatch_event_bus.genome.name
  description = "Publish DiscoveryEvent envelopes here to add a new source."
}

output "raw_bucket" {
  value = aws_s3_bucket.raw.id
}

output "consumer_role_arn" {
  value       = aws_iam_role.consumer.arn
  description = "Read-only role for CRE innovations 2-6. They cannot write the graph."
}

output "dlq_url" {
  value       = aws_sqs_queue.dlq.url
  description = "Failed discovery events land here. A non-empty DLQ means the genome is going stale."
}

output "phase1_gate_query" {
  value       = "MATCH (s:Service) WITH count(s) AS total, sum(CASE WHEN SIZE(s.sources) >= 2 THEN 1 ELSE 0 END) AS corroborated RETURN toFloat(corroborated)/total AS completeness"
  description = "Run against the reader endpoint. This is the Phase 1 promotion gate — target >= 0.80 before advancing to Phase 2."
}
