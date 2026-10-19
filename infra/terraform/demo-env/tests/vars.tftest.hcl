# Offline unit tests for demo-env: mocked providers, NO AWS credentials, NO real resources.
# Run from demo-env/:  terraform init -backend=false && terraform test
#
# Covers the variable contract added with the Checkov CKV_AWS_51/65 fixes:
#   - each container gets its own image tag variable
#   - "latest" is rejected for both tags (ECR repos are IMMUTABLE)
#   - Container Insights follows container_insights_enabled
#
# Limits of mocking (deliberate, not gaps to "fix" here):
#   - command = plan only. Mock providers invent random strings for computed attributes, and
#     apply-mode fails provider-side ARN validation on them (e.g. the WAF IP-set reference).
#   - The WAF rule count can't be asserted at plan time: the rule set contains the IP set's
#     unknown ARN, so the whole set is unknown. Rule content is covered by validate + Checkov.

mock_provider "aws" {
  # Make mocked data sources/resources known during plan, not just after apply.
  override_during = plan

  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_iam_role" {
    defaults = {
      arn  = "arn:aws:iam::123456789012:role/fiduciary-demo-backup-role"
      name = "fiduciary-demo-backup-role"
    }
  }
  mock_data "aws_ecr_repository" {
    defaults = { repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/mock" }
  }
  mock_data "aws_route53_zone" {
    defaults = { zone_id = "Z0000000000MOCK" }
  }
  # cert_validation uses for_each over domain_validation_options; its keys must be known at plan.
  mock_resource "aws_acm_certificate" {
    defaults = {
      arn = "arn:aws:acm:us-east-1:123456789012:certificate/mock"
      domain_validation_options = [{
        domain_name           = "demo.bentleybuilds.tech"
        resource_record_name  = "_mock.demo.bentleybuilds.tech."
        resource_record_type  = "CNAME"
        resource_record_value = "_mock.acm-validations.aws."
      }]
    }
  }
}

mock_provider "time" {}

# Placeholder inputs only -- the real blocklist comes from waf.auto.tfvars.json.
variables {
  waf_blocklist_cidrs = ["192.0.2.0/24"] # RFC 5737 documentation range
  image_tag           = "abc1234"
  mcpo_image_tag      = "mcpo-20260726-legacy"
}

run "image_tags_wired_per_container" {
  command = plan

  assert {
    condition     = endswith(jsondecode(aws_ecs_task_definition.app.container_definitions)[0].image, ":abc1234")
    error_message = "portfolio-api container must use var.image_tag"
  }
  assert {
    condition     = endswith(jsondecode(aws_ecs_task_definition.app.container_definitions)[1].image, ":mcpo-20260726-legacy")
    error_message = "mcpo container must use var.mcpo_image_tag"
  }
  assert {
    condition     = one([for s in aws_ecs_cluster.main.setting : s.value if s.name == "containerInsights"]) == "enabled"
    error_message = "Container Insights must default to enabled"
  }
}

run "container_insights_can_be_disabled" {
  command = plan

  variables {
    container_insights_enabled = false
  }

  assert {
    condition     = one([for s in aws_ecs_cluster.main.setting : s.value if s.name == "containerInsights"]) == "disabled"
    error_message = "container_insights_enabled = false must disable Container Insights"
  }
}

run "latest_rejected_for_app" {
  command = plan

  variables {
    image_tag = "latest"
  }

  expect_failures = [var.image_tag]
}

run "latest_rejected_for_mcpo" {
  command = plan

  variables {
    mcpo_image_tag = "latest"
  }

  expect_failures = [var.mcpo_image_tag]
}

run "invalid_tag_rejected" {
  command = plan

  variables {
    image_tag = "bad tag!"
  }

  expect_failures = [var.image_tag]
}
