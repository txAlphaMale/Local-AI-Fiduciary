# bootstrap -- the ONE stack with local state: creates the S3 bucket that holds remote state for
# persistent/ and demo-env/. Run once per account; never destroyed by the normal pause/resume cycle.
# (Chicken-and-egg: a backend bucket can't store its own creation's state. Its local
# terraform.tfstate is gitignored; losing it is harmless -- re-import the bucket or leave it.)

terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 5.80, < 7.0" }
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = "fiduciary-demo", ManagedBy = "terraform", Stack = "bootstrap" } }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "tfstate" {
  # Server access logging for this bucket IS enabled -- by persistent/ (aws_s3_bucket_logging.sources,
  # target fiduciary-demo-logs-<account>/s3/tfstate/), because the log bucket lives there and this
  # stack is applied first on local state. Checkov scans each stack's resources separately and
  # can't see that cross-stack link, hence the skip.
  #checkov:skip=CKV_AWS_18:Access logging is configured from persistent/ (aws_s3_bucket_logging.sources); cross-stack, not visible to Checkov.
  bucket = "fiduciary-demo-tfstate-${data.aws_caller_identity.current.account_id}"
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

# Deny any non-TLS access to state (state holds ARNs/IDs, and would hold anything sensitive that
# ever slipped into it -- see README "Secrets" for why SSM values are kept OUT of state entirely).
resource "aws_s3_bucket_policy" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.tfstate.arn, "${aws_s3_bucket.tfstate.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 90 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

output "state_bucket" {
  value = aws_s3_bucket.tfstate.id
}

output "backend_hcl" {
  description = "Paste into infra/terraform/backend.hcl (gitignored)."
  value       = <<-EOT
    bucket       = "${aws_s3_bucket.tfstate.id}"
    region       = "${var.region}"
    encrypt      = true
    use_lockfile = true
  EOT
}
