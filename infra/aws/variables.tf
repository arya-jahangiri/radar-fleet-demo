variable "aws_region" {
  description = "AWS region for the hosted demo slice."
  type        = string
  default     = "eu-west-2"
}

variable "aws_profile" {
  description = "Optional AWS shared-config profile. Leave blank to use env/instance credentials."
  type        = string
  default     = ""
}

variable "project_prefix" {
  description = "Name prefix for AWS resources. Hyphens are converted where services require underscores."
  type        = string
  default     = "imperial-radar-demo"
}

variable "hosted_home_count" {
  description = "Number of AWS IoT registry marker homes to create. Direct Pi-node publishers are represented by topic/policy contract, not thousands of Thing resources."
  type        = number
  default     = 1

  validation {
    condition     = var.hosted_home_count >= 1 && var.hosted_home_count <= 1000
    error_message = "hosted_home_count must stay between 1 and 1000 for this demo."
  }
}

variable "nodes_per_home" {
  description = "Radar/Pi node count recorded as metadata for each home marker."
  type        = number
  default     = 5
}

variable "iot_rule_enabled" {
  description = "Set false to pause cloud ingestion while keeping provisioned resources."
  type        = bool
  default     = true
}

variable "s3_retention_days" {
  description = "Cold-copy S3 retention in days. Keep short to control free-credit spend."
  type        = number
  default     = 1
}

variable "s3_cold_copy_enabled" {
  description = "Write each IoT summary to S3 as a cold audit copy. Disable for longer low-cost demo runs."
  type        = bool
  default     = false
}

variable "latest_state_ttl_seconds" {
  description = "DynamoDB TTL for latest-state items."
  type        = number
  default     = 86400
}

variable "dashboard_snapshot_max_items" {
  description = "Maximum latest-state items scanned by the public hosted dashboard snapshot Lambda."
  type        = number
  default     = 10000
}

variable "dashboard_cors_allow_origin" {
  description = "CORS origin allowed to read the synthetic dashboard snapshot endpoint."
  type        = string
  default     = "*"
}

variable "force_destroy_bucket" {
  description = "Allow terraform destroy to delete the demo bucket and its objects."
  type        = bool
  default     = true
}

variable "enable_rule_cloudwatch_logs" {
  description = "Route IoT rule errors to CloudWatch Logs. Keep true for demo observability."
  type        = bool
  default     = true
}

variable "cloud_simulator_enabled" {
  description = "Run the optional synthetic direct-node load generator in AWS Lambda via EventBridge."
  type        = bool
  default     = false
}

variable "cloud_simulator_home_count" {
  description = "Number of homes published by the optional cloud-side direct-node load generator."
  type        = number
  default     = 100

  validation {
    condition     = var.cloud_simulator_home_count >= 1 && var.cloud_simulator_home_count <= 1000
    error_message = "cloud_simulator_home_count must stay between 1 and 1000 for this demo."
  }
}

variable "cloud_simulator_period_seconds" {
  description = "Sequence/idempotency period used inside cloud simulator payloads. EventBridge still invokes the simulator once per minute."
  type        = number
  default     = 60

  validation {
    condition     = var.cloud_simulator_period_seconds >= 1 && var.cloud_simulator_period_seconds <= 300
    error_message = "cloud_simulator_period_seconds must stay between 1 and 300 seconds."
  }
}
