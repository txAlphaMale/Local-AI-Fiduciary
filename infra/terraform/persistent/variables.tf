variable "region" {
  type    = string
  default = "us-east-1"
}

variable "domain_name" {
  type    = string
  default = "demo.bentleybuilds.tech"
}

variable "scheduled_backup_retention_days" {
  description = "Days to keep each daily scheduled-* backup prefix. Manual backups are never auto-expired."
  type        = number
  default     = 30
}

variable "log_retention_days" {
  description = "Days to keep ALB access logs, S3 server access logs and VPC flow logs in the log bucket."
  type        = number
  default     = 90
  validation {
    condition     = var.log_retention_days >= 1
    error_message = "log_retention_days must be at least 1."
  }
}

variable "waf_log_retention_days" {
  description = "Days to keep WAF logs in CloudWatch Logs. Must be a CloudWatch-supported retention value."
  type        = number
  default     = 90
  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.waf_log_retention_days)
    error_message = "waf_log_retention_days must be a CloudWatch Logs retention value (1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, ...)."
  }
}

variable "ecr_keep_images" {
  type    = number
  default = 10
}

# Only used by imports.tf during the one-time adoption of the live resources. Take the value from
# .live-snapshot/<stamp>/route53-zone.json (HostedZone.Id, without the "/hostedzone/" prefix).
variable "import_zone_id" {
  type    = string
  default = ""
}
