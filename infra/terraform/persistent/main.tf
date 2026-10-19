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

# ------------------------------------------------------------------------------------- outputs

output "backup_bucket" { value = aws_s3_bucket.backups.id }
output "backup_role_arn" { value = aws_iam_role.backup.arn }
output "zone_id" { value = aws_route53_zone.demo.zone_id }
output "zone_name_servers" {
  description = "Must match the 4 NS records for 'demo' at DreamHost."
  value       = aws_route53_zone.demo.name_servers
}
output "ecr_repository_urls" { value = { for k, r in aws_ecr_repository.repo : k => r.repository_url } }
