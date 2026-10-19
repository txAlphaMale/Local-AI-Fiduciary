variable "region" {
  type    = string
  default = "us-east-1"
}

variable "domain_name" {
  type    = string
  default = "demo.bentleybuilds.tech"
}

variable "azs" {
  description = "Exactly two AZs (ALB minimum). Index 0 = '-a' subnets, index 1 = '-b' subnets."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

# Image tags. No defaults on purpose: the ECR repos are IMMUTABLE, so every deploy names an exact,
# unique tag (git short SHA or timestamp). A deploy = build, push a unique tag, then
#   terraform apply -var image_tag=<tag> -var mcpo_image_tag=<tag>
# which registers a new task-definition revision and rolls the service onto it. up.sh resolves
# both to the most recently pushed non-"latest" tag in each repo unless told otherwise.
# Current values: terraform output -raw image_tag / mcpo_image_tag.
variable "image_tag" {
  description = "portfolio-api-demo image tag (unique; never \"latest\")."
  type        = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$", var.image_tag)) && var.image_tag != "latest"
    error_message = "image_tag must be a valid, unique Docker tag (e.g. a git short SHA) -- not \"latest\"."
  }
}

variable "mcpo_image_tag" {
  description = "mcpo-demo image tag (unique; never \"latest\"). Separate from image_tag because mcpo-demo is rebuilt far less often."
  type        = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$", var.mcpo_image_tag)) && var.mcpo_image_tag != "latest"
    error_message = "mcpo_image_tag must be a valid, unique Docker tag -- not \"latest\"."
  }
}

variable "initial_desired_count" {
  description = <<-EOT
    Task count used ONLY when the ECS service is first created. Defaults to 0 so a fresh stand-up
    never boots the app against an empty EFS before restore.sh has put /data back (up.sh restores,
    then scales to 1). After creation, desired_count is ignored by Terraform -- the EventBridge
    schedules and manual update-service calls own it.
  EOT
  type        = number
  default     = 0
}

# ---------------------------------------------------------------------------------------- WAF
# Populated from the LIVE IP set by scripts/aws-demo/export-live-config.sh into
# waf.auto.tfvars.json. Terraform now owns this list: a CIDR added by hand with
# `aws wafv2 update-ip-set` will be REVERTED by the next apply. Add CIDRs here instead.

variable "waf_blocklist_cidrs" {
  type = list(string)
  validation {
    condition     = length(var.waf_blocklist_cidrs) > 0 && alltrue([for c in var.waf_blocklist_cidrs : can(cidrhost(c, 0))])
    error_message = "waf_blocklist_cidrs must be a non-empty list of valid IPv4 CIDRs (run export-live-config.sh to generate it)."
  }
}

variable "waf_geo_allowed_countries" {
  description = "ISO 3166-1 alpha-2 codes allowed through; every other country is blocked."
  type        = list(string)
  default     = ["US"]
}

# -------------------------------------------------------------------------------------- ALB/TLS

variable "alb_ssl_policy" {
  description = "TLS 1.2+/1.3 policy. The CLI-built listener used whatever the API default was at creation -- check the snapshot's alb-listeners.json."
  type        = string
  default     = "ELBSecurityPolicy-TLS13-1-2-2021-06"
}

variable "alb_idle_timeout" {
  description = "Seconds. 60 = AWS default and what the live ALB was built with (verify in alb-attributes.json)."
  type        = number
  default     = 60
}

# ------------------------------------------------------------------------------------ schedules

variable "business_hours_schedules_enabled" {
  description = "Scale-up 08:00 / scale-down 17:00 America/New_York, Mon-Fri (§14a). false = pause the automation without deleting it."
  type        = bool
  default     = true
}

variable "daily_backup_enabled" {
  type    = bool
  default = true
}

variable "container_insights_enabled" {
  description = "ECS Container Insights on the cluster. Set false to drop its CloudWatch cost."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  type    = number
  default = 30
}

# ---------------------------------------------------------------------- app config (task def)

variable "anthropic_model" {
  type    = string
  default = "claude-sonnet-5"
}

variable "anthropic_strategist_model" {
  type    = string
  default = "claude-opus-5"
}

variable "demo_reset_interval_s" {
  type    = number
  default = 21600
}

variable "sec_user_agent" {
  type    = string
  default = "Fiduciary Demo 16510154+txAlphaMale@users.noreply.github.com"
}
