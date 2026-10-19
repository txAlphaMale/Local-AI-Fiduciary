# Guide §1 (VPC/subnets/IGW/NAT) + §2 (security groups). CIDRs identical to the live build.

resource "aws_vpc" "main" {
  cidr_block           = "10.21.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

# CKV2_AWS_11. VPC flow logs -> persistent log bucket under vpc-flow/ (survives pause/resume).
# Path: vpc-flow/AWSLogs/<account>/vpcflowlogs/<region>/YYYY/MM/DD/. The delivery permission is the
# AllowVPCFlowLogDelivery* statements in persistent/'s log-bucket policy.
resource "aws_flow_log" "vpc" {
  vpc_id                   = aws_vpc.main.id
  traffic_type             = var.flow_log_traffic_type
  log_destination_type     = "s3"
  log_destination          = "${data.aws_s3_bucket.logs.arn}/vpc-flow/"
  max_aggregation_interval = 600 # 10 min (default); 60 s costs the same per GB but writes more records

  destination_options {
    file_format                = "plain-text"
    hive_compatible_partitions = false
    per_hour_partition         = false
  }

  tags = { Name = "${local.name}-flow-log" }
}

# Strip the default SG's allow-all rules (CIS AWS 5.4). Nothing in this design uses it.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-default-unused" }
}

locals {
  public_subnets = {
    "public-a" = { cidr = "10.21.0.0/24", az = var.azs[0] }
    "public-b" = { cidr = "10.21.3.0/24", az = var.azs[1] }
  }
  private_subnets = {
    "private-a" = { cidr = "10.21.1.0/24", az = var.azs[0] }
    "private-b" = { cidr = "10.21.2.0/24", az = var.azs[1] }
  }
}

resource "aws_subnet" "public" {
  for_each          = local.public_subnets
  vpc_id            = aws_vpc.main.id
  cidr_block        = each.value.cidr
  availability_zone = each.value.az
  # CHANGED vs §1 (was --map-public-ip-on-launch): only the ALB and NAT live here and neither uses
  # this setting (both get their public addresses explicitly), so auto-assign is pure exposure.
  map_public_ip_on_launch = false
  tags                    = { Name = "${local.name}-${each.key}" }
}

resource "aws_subnet" "private" {
  for_each          = local.private_subnets
  vpc_id            = aws_vpc.main.id
  cidr_block        = each.value.cidr
  availability_zone = each.value.az
  tags              = { Name = "${local.name}-${each.key}" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-igw" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.name}-public-rt" }
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${local.name}-nat-eip" }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public["public-a"].id
  tags          = { Name = "${local.name}-nat" }
  depends_on    = [aws_internet_gateway.main]
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
  tags = { Name = "${local.name}-private-rt" }
}

resource "aws_route_table_association" "private" {
  for_each       = aws_subnet.private
  subnet_id      = each.value.id
  route_table_id = aws_route_table.private.id
}

# ------------------------------------------------------------------------------ security groups
# Rules are separate resources (not inline) so ALB-egress -> Fargate and Fargate-ingress <- ALB
# can reference each other without a dependency cycle.
#
# NOTE ON EGRESS: the CLI-created groups got AWS's implicit allow-all egress. Terraform removes
# that implicit rule, so egress is now explicit -- and tightened where the design allows:
#   ALB     -> only the Fargate SG on 8097 (all it ever talks to)
#   Fargate -> anywhere (needs NAT egress for ECR, SSM, CloudWatch, Anthropic/Finnhub/Tavily/SEC, EFS)
#   EFS     -> none (mount targets never initiate connections)

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public-facing ALB -- 443 only"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-alb" }
}

resource "aws_security_group" "fargate" {
  name        = "${local.name}-fargate"
  description = "portfolio-api + mcpo tasks -- ONLY the ALB can reach them"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-fargate" }
}

resource "aws_security_group" "efs" {
  name        = "${local.name}-efs"
  description = "EFS mount targets"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-efs" }
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from the internet (WAF filters before the ALB forwards)"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_fargate" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward + health checks to the app port only"
  ip_protocol                  = "tcp"
  from_port                    = 8097
  to_port                      = 8097
  referenced_security_group_id = aws_security_group.fargate.id
}

# This rule is what makes TRUST_PROXY_HEADERS=1 safe (guide §8): nothing but the ALB can reach the
# app, so nothing else can hand it a forged X-Forwarded-For.
resource "aws_vpc_security_group_ingress_rule" "fargate_from_alb" {
  security_group_id            = aws_security_group.fargate.id
  description                  = "App port from the ALB only"
  ip_protocol                  = "tcp"
  from_port                    = 8097
  to_port                      = 8097
  referenced_security_group_id = aws_security_group.alb.id
}

resource "aws_vpc_security_group_egress_rule" "fargate_all" {
  security_group_id = aws_security_group.fargate.id
  description       = "Outbound via NAT (AWS APIs, LLM/market-data APIs) and to EFS"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "efs_from_fargate" {
  security_group_id            = aws_security_group.efs.id
  description                  = "NFS from app/backup/restore tasks"
  ip_protocol                  = "tcp"
  from_port                    = 2049
  to_port                      = 2049
  referenced_security_group_id = aws_security_group.fargate.id
}
