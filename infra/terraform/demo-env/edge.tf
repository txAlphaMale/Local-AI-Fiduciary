# Guide §4 (ACM + DNS validation), §9 (TG/ALB/listener), §11 (A-alias), §14d (WAF).

# ------------------------------------------------------------------------------------- ACM

resource "aws_acm_certificate" "demo" {
  domain_name       = var.domain_name
  validation_method = "DNS"
  lifecycle { create_before_destroy = true }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for o in aws_acm_certificate.demo.domain_validation_options : o.domain_name => {
      name   = o.resource_record_name
      type   = o.resource_record_type
      record = o.resource_record_value
    }
  }
  zone_id = data.aws_route53_zone.demo.zone_id
  name    = each.value.name
  type    = each.value.type
  ttl     = 300
  records = [each.value.record]
  # teardown.sh only removed the A-record, so the validation CNAME from the CLI build is likely
  # still in the zone (ACM reuses the same token per account+domain). Overwrite, don't fail.
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "demo" {
  certificate_arn         = aws_acm_certificate.demo.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# ------------------------------------------------------------------------------- ALB + TG

resource "aws_lb_target_group" "app" {
  name        = "${local.name}-tg"
  port        = 8097
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  # Explicit values = the ELBv2 API defaults the CLI build got (Terraform's own defaults differ).
  health_check {
    path                = "/health"
    protocol            = "HTTP"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 5
    unhealthy_threshold = 2
    matcher             = "200"
  }
}

resource "aws_lb" "main" {
  name               = "${local.name}-alb"
  internal           = false
  load_balancer_type = "application"
  subnets            = [for s in aws_subnet.public : s.id]
  security_groups    = [aws_security_group.alb.id]
  idle_timeout       = var.alb_idle_timeout

  # ADDED: drop headers with invalid field names before they reach the app -- a cheap hardening
  # step that matters more here than usual, since the app trusts X-Forwarded-For (TRUST_PROXY_HEADERS=1).
  drop_invalid_header_fields = true
  enable_deletion_protection = false # torn down on purpose every pause cycle

  # CKV_AWS_91. Lands in s3://fiduciary-demo-logs-<account>/alb/AWSLogs/<account>/elasticloadbalancing/...
  # ORDERING: the log bucket's policy (persistent/) must already grant
  # logdelivery.elasticloadbalancing.amazonaws.com, because enabling logs makes ELB write an
  # ELBAccessLogTestFile and the create/modify call fails with Access Denied if it can't.
  # persistent/ is always applied before demo-env, so this holds; the data source above only proves
  # the bucket exists, not the policy.
  access_logs {
    bucket  = data.aws_s3_bucket.logs.id
    prefix  = "alb"
    enabled = true
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = var.alb_ssl_policy
  certificate_arn   = aws_acm_certificate_validation.demo.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

resource "aws_route53_record" "alias" {
  zone_id         = data.aws_route53_zone.demo.zone_id
  name            = var.domain_name
  type            = "A"
  allow_overwrite = true
  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}

# ------------------------------------------------------------------------------------- WAF

resource "aws_wafv2_ip_set" "blocklist" {
  name               = "${local.name}-blocklist"
  description        = "Manually-curated CIDR blocklist, e.g. observed scanner ranges like Shodan"
  scope              = "REGIONAL"
  ip_address_version = "IPV4"
  addresses          = var.waf_blocklist_cidrs
}

resource "aws_wafv2_web_acl" "main" {
  name  = "${local.name}-acl"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  # 1. AWS-maintained IP reputation list -- block
  rule {
    name     = "AWS-AmazonIpReputationList"
    priority = 1
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesAmazonIpReputationList"
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "IpReputationList"
    }
  }

  # 2. Custom CIDR blocklist -- block
  rule {
    name     = "CustomIpBlocklist"
    priority = 2
    action {
      block {}
    }
    statement {
      ip_set_reference_statement {
        arn = aws_wafv2_ip_set.blocklist.arn
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "CustomIpBlocklist"
    }
  }

  # 3. Geo allow-list -- block everything NOT from an allowed country
  rule {
    name     = "GeoAllowListOnly"
    priority = 3
    action {
      block {}
    }
    statement {
      not_statement {
        statement {
          geo_match_statement {
            country_codes = var.waf_geo_allowed_countries
          }
        }
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "GeoAllowListOnly"
    }
  }

  # 4. AWS-managed known-bad-inputs (CKV_AWS_192): Log4j/Log4Shell (CVE-2021-44228) JNDI lookups,
  #    exploitable paths, localhost Host headers, etc. -- block. Runs last, so non-US and
  #    blocklisted sources are already rejected by the cheaper rules above.
  #
  #    WCU: IpReputationList 25 + IP-set reference 1 + geo match (inside NOT) 1 + KnownBadInputs 200
  #    = 227 of the 1500 default capacity (1273 headroom). Rule-group WCUs are AWS-published and
  #    fixed per group; if AWS revises one, create/update fails loudly rather than silently.
  #    Cost: +$1/month rule fee; the AWS baseline managed groups carry no subscription fee.
  rule {
    name     = "AWS-KnownBadInputsRuleSet"
    priority = 4
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "KnownBadInputsRuleSet"
    }
  }

  visibility_config {
    sampled_requests_enabled   = true
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name}-acl"
  }
}

# A freshly created Web ACL is not always immediately associable (WAFUnavailableEntityException,
# hit live 2026-07-27; setup-waf.sh retried for up to 3 min). A fixed pause avoids a failed first
# apply; if it still races, simply re-run `terraform apply`.
resource "time_sleep" "waf_propagation" {
  create_duration = "45s"
  triggers = {
    web_acl_arn = aws_wafv2_web_acl.main.arn
    alb_arn     = aws_lb.main.arn
  }
}

resource "aws_wafv2_web_acl_association" "alb" {
  resource_arn = time_sleep.waf_propagation.triggers["alb_arn"]
  web_acl_arn  = time_sleep.waf_propagation.triggers["web_acl_arn"]
}
