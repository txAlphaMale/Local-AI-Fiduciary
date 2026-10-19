# persistent -- resources that SURVIVE a demo pause (terraform destroy of demo-env/).
# Mirrors exactly what teardown.sh already deliberately kept (backup bucket, backup role, Route 53
# zone), plus the two ECR repos (MOVED here from the delete list: keeping ~2 images costs cents/mo
# and removes the "rebuild + push both images before the task can start" step from every resume).
#
# Init:  terraform init -backend-config=../backend.hcl

terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 5.80, < 7.0" }
  }
  backend "s3" {
    key = "persistent/terraform.tfstate"
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = "fiduciary-demo", ManagedBy = "terraform", Stack = "persistent" } }
}

data "aws_caller_identity" "current" {}

locals {
  account_id    = data.aws_caller_identity.current.account_id
  backup_bucket = "fiduciary-demo-backups-${local.account_id}" # same name common.sh derives
}

# ---------------------------------------------------------------- backup bucket (§14b / §14c)

resource "aws_s3_bucket" "backups" {
  bucket = local.backup_bucket
  lifecycle { prevent_destroy = true } # the whole point is that it outlives the environment
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
    bucket_key_enabled = true
  }
}

# CHANGED vs schedule-backup.sh: that script expires EVERY object after 30 days. That includes the
# manual pre-teardown backup (prefix = bare timestamp), so a demo paused for 30+ days would lose
# its only restore point (delete marker at day 30, gone entirely at day 60). Here the 30-day
# expiry applies only to the daily "scheduled-*" prefixes; manual/pre-teardown backups are kept
# until deleted on purpose. Noncurrent-version cleanup still applies bucket-wide.
resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    id     = "expire-scheduled-daily-backups"
    status = "Enabled"
    filter { prefix = "scheduled-" }
    expiration { days = var.scheduled_backup_retention_days }
  }

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 30 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

resource "aws_s3_bucket_policy" "backups" {
  bucket = aws_s3_bucket.backups.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.backups.arn, "${aws_s3_bucket.backups.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# ------------------------------------------------------------------ backup IAM role (trust only)
# The role persists; its EFS/S3 permission policy lives in demo-env/ because it must reference the
# CURRENT environment's EFS filesystem ARN, which changes on every stand-up.

resource "aws_iam_role" "backup" {
  name = "fiduciary-demo-backup-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# --------------------------------------------------------------------- Route 53 zone (§0 / §4)
# Kept so the DreamHost NS delegation never has to be redone. Its NS set is an output -- if the
# zone is ever recreated, those 4 values must be re-entered at DreamHost.

resource "aws_route53_zone" "demo" {
  name    = var.domain_name
  comment = "fiduciary-demo -- delegated from DreamHost (bentleybuilds.tech)"
  lifecycle { prevent_destroy = true }
}

# ------------------------------------------------------------------------------ ECR repos (§7)

# image_tag_mutability = IMMUTABLE (CKV_AWS_51): a pushed tag can never be overwritten, so a tag
# always names exactly one image. Every push uses a unique tag (git short SHA / timestamp); nothing
# pushes :latest any more. Changing this on an existing repo is an in-place update
# (PutImageTagMutability), not a replacement. See README "Image tags".
resource "aws_ecr_repository" "repo" {
  for_each             = toset(["fiduciary/portfolio-api-demo", "fiduciary/mcpo-demo"])
  name                 = each.key
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_lifecycle_policy" "repo" {
  for_each   = aws_ecr_repository.repo
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the ${var.ecr_keep_images} most recent images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = var.ecr_keep_images
      }
      action = { type = "expire" }
    }]
  })
}

# ---------------------------------------------------------------------- access-log bucket
# Destination for ALB access logs and VPC flow logs (demo-env) and S3 server access logs (tfstate +
# backups buckets). WAF logs can't land here: their destination name must start with aws-waf-logs-.
# Lives in persistent/ so logs survive every pause/resume (demo-env is destroyed on each pause).
#
# Constraints taken from the AWS docs (checked 2026-10-19):
#  - ALB access logs: bucket in the SAME region as the ALB; SSE-S3 is the only supported encryption
#    (no SSE-KMS); grant the service principal logdelivery.elasticloadbalancing.amazonaws.com, with
#    the account ID in the resource path ("<prefix>/AWSLogs/<account>/*"), optionally narrowed with
#    aws:SourceArn. https://docs.aws.amazon.com/elasticloadbalancing/latest/application/enable-access-logging.html
#  - S3 server access logs: destination in the same region AND account, SSE-S3 (not SSE-KMS), no
#    Object Lock, no Requester Pays; with BucketOwnerEnforced, permission must come from a bucket
#    policy for logging.s3.amazonaws.com (not ACLs).
#    https://docs.aws.amazon.com/AmazonS3/latest/userguide/enable-server-access-logging.html

locals {
  log_bucket = "fiduciary-demo-logs-${local.account_id}"
}

resource "aws_s3_bucket" "logs" {
  # DELIBERATE: no server access logging ON this bucket. Logging the log bucket would either loop
  # (logging to itself) or need yet another bucket; AWS itself says a destination bucket should not
  # have server access logging enabled.
  #checkov:skip=CKV_AWS_18:Log destination bucket; logging it to itself loops, and AWS advises destination buckets not have access logging enabled.
  bucket = local.log_bucket
  lifecycle { prevent_destroy = true } # logs must outlive the environments they describe
}

resource "aws_s3_bucket_ownership_controls" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule { object_ownership = "BucketOwnerEnforced" } # ACLs off; delivery is authorized by bucket policy
}

resource "aws_s3_bucket_versioning" "logs" {
  bucket = aws_s3_bucket.logs.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# SSE-S3 only: neither ALB access logs nor S3 server access logs support an SSE-KMS destination.
resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    id     = "expire-logs"
    status = "Enabled"
    filter {}
    expiration { days = var.log_retention_days }
    # Versioning keeps a noncurrent copy when the expiration above deletes an object; drop it soon
    # after, so retention really is ~log_retention_days and storage doesn't double.
    noncurrent_version_expiration { noncurrent_days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }

  rule {
    id     = "remove-expired-delete-markers"
    status = "Enabled"
    filter {}
    expiration { expired_object_delete_marker = true }
  }
}

locals {
  tfstate_bucket = "fiduciary-demo-tfstate-${local.account_id}" # created by bootstrap/
  # source bucket name => target prefix in the log bucket
  s3_access_log_sources = {
    (local.tfstate_bucket) = "s3/tfstate/"
    (local.backup_bucket)  = "s3/backups/"
  }
}

resource "aws_s3_bucket_policy" "logs" {
  bucket = aws_s3_bucket.logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          # Current (post-Aug-2022) ALB log-delivery model: service principal, not the legacy
          # per-region ELB account (127311923021 in us-east-1), which AWS still accepts but no
          # longer recommends. Account ID in the path + SourceArn = only THIS account's load
          # balancers in THIS region can write, and only under alb/.
          Sid       = "AllowALBAccessLogDelivery"
          Effect    = "Allow"
          Principal = { Service = "logdelivery.elasticloadbalancing.amazonaws.com" }
          Action    = "s3:PutObject"
          Resource  = "${aws_s3_bucket.logs.arn}/alb/AWSLogs/${local.account_id}/*"
          Condition = {
            ArnLike = { "aws:SourceArn" = "arn:aws:elasticloadbalancing:${var.region}:${local.account_id}:loadbalancer/*" }
          }
        }
      ],
      [
        # One statement per source bucket, so each can only write under its own prefix.
        for src, prefix in local.s3_access_log_sources : {
          Sid       = "AllowS3ServerAccessLogs${replace(title(replace(prefix, "/", " ")), " ", "")}"
          Effect    = "Allow"
          Principal = { Service = "logging.s3.amazonaws.com" }
          Action    = "s3:PutObject"
          Resource  = "${aws_s3_bucket.logs.arn}/${prefix}*"
          Condition = {
            ArnLike      = { "aws:SourceArn" = "arn:aws:s3:::${src}" }
            StringEquals = { "aws:SourceAccount" = local.account_id }
          }
        }
      ],
      [
        # VPC flow logs (demo-env aws_flow_log -> s3://<log bucket>/vpc-flow/). Same two statements
        # AWS documents for flow-log delivery to S3, narrowed from "bucket/*" to this account's path
        # under vpc-flow/. delivery.logs.amazonaws.com is the CloudWatch Logs vended-log delivery
        # service; SourceAccount + SourceArn (logs:<region>:<account>:*) are the documented
        # confused-deputy guards. The x-amz-acl condition is AWS's own requirement; it is compatible
        # with BucketOwnerEnforced (bucket-owner-full-control is the one ACL that setting accepts).
        # https://docs.aws.amazon.com/vpc/latest/userguide/flow-logs-s3-permissions.html
        {
          Sid       = "AllowVPCFlowLogDeliveryWrite"
          Effect    = "Allow"
          Principal = { Service = "delivery.logs.amazonaws.com" }
          Action    = "s3:PutObject"
          Resource  = "${aws_s3_bucket.logs.arn}/vpc-flow/AWSLogs/${local.account_id}/*"
          Condition = {
            StringEquals = {
              "aws:SourceAccount" = local.account_id
              "s3:x-amz-acl"      = "bucket-owner-full-control"
            }
            ArnLike = { "aws:SourceArn" = "arn:aws:logs:${var.region}:${local.account_id}:*" }
          }
        },
        {
          Sid       = "AllowVPCFlowLogDeliveryAclCheck"
          Effect    = "Allow"
          Principal = { Service = "delivery.logs.amazonaws.com" }
          Action    = "s3:GetBucketAcl"
          Resource  = aws_s3_bucket.logs.arn
          Condition = {
            StringEquals = { "aws:SourceAccount" = local.account_id }
            ArnLike      = { "aws:SourceArn" = "arn:aws:logs:${var.region}:${local.account_id}:*" }
          }
        }
      ],
      [
        {
          Sid       = "DenyInsecureTransport"
          Effect    = "Deny"
          Principal = "*"
          Action    = "s3:*"
          Resource  = [aws_s3_bucket.logs.arn, "${aws_s3_bucket.logs.arn}/*"]
          Condition = { Bool = { "aws:SecureTransport" = "false" } }
        }
      ]
    )
  })

  # Ownership controls first: a policy-only delivery model needs ACLs disabled.
  depends_on = [aws_s3_bucket_ownership_controls.logs, aws_s3_bucket_public_access_block.logs]
}

# ------------------------------------------------------------------- WAF log destination
# CloudWatch Logs, not S3 (rationale and cost in README "Logging"). AWS WAF requires the name to
# start with "aws-waf-logs-" and the group to be in the same account and region as the web ACL:
# https://docs.aws.amazon.com/waf/latest/developerguide/logging-cw-logs.html
# Lives in persistent/ so WAF history survives pause/resume; demo-env attaches to it by name.

locals {
  waf_log_group = "aws-waf-logs-fiduciary-demo"
}

resource "aws_cloudwatch_log_group" "waf" {
  # KMS: AWS-owned-key encryption at rest (CloudWatch Logs default). A customer-managed key would add
  # a key, a key policy that admits logs.<region>.amazonaws.com, and ~$1/month, for a demo whose WAF
  # logs carry no credentials (Authorization/Cookie are redacted at source in demo-env).
  #checkov:skip=CKV_AWS_158:Encrypted at rest with the CloudWatch Logs default key; CMK cost/complexity not justified for redacted demo WAF logs.
  #checkov:skip=CKV_AWS_338:Retention is a deliberate variable (waf_log_retention_days, default 90) matching the access-log bucket, not 1 year.
  name              = local.waf_log_group
  retention_in_days = var.waf_log_retention_days
  lifecycle { prevent_destroy = true } # logs must outlive the web ACLs they describe
}

# Account-level CloudWatch Logs resource policy letting the vended-log delivery service write WAF
# logs into THIS log group only. Mirrors the policy AWS auto-creates for vended delivery
# (AWSLogDeliveryWrite20150319), managed here so it is explicit and scoped, and so the deploying
# role doesn't need logs:PutResourcePolicy at apply time of demo-env:
# https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/AWS-logs-infrastructure-CWL.html
# Note: account-level resource policies are a small per-region quota (10) and size-capped.
resource "aws_cloudwatch_log_resource_policy" "waf" {
  policy_name = "fiduciary-demo-waf-log-delivery"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AWSLogDeliveryWriteWAF"
      Effect    = "Allow"
      Principal = { Service = "delivery.logs.amazonaws.com" }
      Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource  = "${aws_cloudwatch_log_group.waf.arn}:*"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:logs:${var.region}:${local.account_id}:*" }
      }
    }]
  })
}

# --------------------------------------------------- S3 server access logging (sources)
# CROSS-STACK NOTE: the tfstate bucket is created by bootstrap/ (local state), but its logging
# configuration is managed HERE, by name. Ordering is therefore the natural one -- bootstrap, then
# persistent -- and bootstrap needs no new variable or second apply. Accesses to the state bucket
# before persistent is first applied (i.e. bootstrap's own run and persistent's first init) are not
# logged. Do not also add logging to bootstrap/, or the two stacks will fight over the setting.

resource "aws_s3_bucket_logging" "sources" {
  for_each      = local.s3_access_log_sources
  bucket        = each.key
  target_bucket = aws_s3_bucket.logs.id
  target_prefix = each.value

  # Date-partitioned keys (s3/<src>/<account>/<region>/<bucket>/YYYY/MM/DD/...) -- easier to query
  # and to prune than the flat SimplePrefix default.
  target_object_key_format {
    partitioned_prefix { partition_date_source = "EventTime" }
  }

  depends_on = [aws_s3_bucket_policy.logs, aws_s3_bucket.backups]
}

# ------------------------------------------------------------------------------------- outputs

output "backup_bucket" { value = aws_s3_bucket.backups.id }
output "log_bucket" { value = aws_s3_bucket.logs.id }
output "waf_log_group" { value = aws_cloudwatch_log_group.waf.name }
output "backup_role_arn" { value = aws_iam_role.backup.arn }
output "zone_id" { value = aws_route53_zone.demo.zone_id }
output "zone_name_servers" {
  description = "Must match the 4 NS records for 'demo' at DreamHost."
  value       = aws_route53_zone.demo.name_servers
}
output "ecr_repository_urls" { value = { for k, r in aws_ecr_repository.repo : k => r.repository_url } }
