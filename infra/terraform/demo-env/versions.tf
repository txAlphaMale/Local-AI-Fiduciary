# demo-env -- everything that is created on resume and destroyed on pause.
# Replaces AWS-DEMO-HOSTING-GUIDE.md §1-§11, §14a, §14c, §14d and teardown.sh's delete sequence.
#
# NAMES ARE A COMPATIBILITY CONTRACT: every resource keeps the exact Name tag / name the guide and
# scripts/aws-demo/common.sh use (fiduciary-demo, fiduciary-demo-private-a, fiduciary-demo-fargate,
# fiduciary-demo-alb, ...), so backup.sh / restore.sh keep working unchanged against a
# Terraform-built environment.
#
# Init:  terraform init -backend-config=../backend.hcl

terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws  = { source = "hashicorp/aws", version = ">= 5.80, < 7.0" }
    time = { source = "hashicorp/time", version = ">= 0.11" }
  }
  backend "s3" {
    key = "demo-env/terraform.tfstate"
  }
}

provider "aws" {
  region = var.region
  default_tags { tags = { Project = "fiduciary-demo", ManagedBy = "terraform", Stack = "demo-env" } }
}

data "aws_caller_identity" "current" {}

# Persistent-stack resources, looked up by name rather than terraform_remote_state, so this stack
# has no dependency on reading the other stack's state file.
data "aws_route53_zone" "demo" {
  name = var.domain_name
}

data "aws_iam_role" "backup" {
  name = "fiduciary-demo-backup-role"
}

# Log bucket from persistent/. Reading it here also fails the plan early, with a clear error, if
# persistent/ hasn't been applied yet (rather than failing mid-apply on the ALB).
data "aws_s3_bucket" "logs" {
  bucket = "fiduciary-demo-logs-${data.aws_caller_identity.current.account_id}"
}

data "aws_ecr_repository" "portfolio_api" {
  name = "fiduciary/portfolio-api-demo"
}

data "aws_ecr_repository" "mcpo" {
  name = "fiduciary/mcpo-demo"
}

locals {
  account_id    = data.aws_caller_identity.current.account_id
  name          = "fiduciary-demo"
  backup_bucket = "fiduciary-demo-backups-${local.account_id}"
  ssm_prefix    = "arn:aws:ssm:${var.region}:${local.account_id}:parameter/fiduciary-demo"
  log_group     = "/ecs/fiduciary-demo"
}
