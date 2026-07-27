#!/usr/bin/env bash
# common.sh -- shared config + resource discovery for the fiduciary-demo AWS scripts
# (backup.sh / teardown.sh / restore.sh, this directory). Source this, don't run it directly.
#
# WHY DISCOVERY-BY-TAG INSTEAD OF A SOURCED VARS FILE: during the original live buildout
# (2026-07-26, AWS-DEMO-HOSTING-GUIDE.md), shell variables like $VPC_ID/$TG_ARN were repeatedly
# lost across fresh terminal sessions, requiring manual re-lookup each time. These scripts avoid
# that entirely by looking up every resource by its Name tag / well-known name at run time --
# they work correctly no matter what terminal you run them from, as long as AWS_PROFILE is set
# and the resources actually exist under the names this guide's provisioning steps create them with.
#
# Requires: aws CLI v2, jq is NOT required (uses --query/--output text throughout).

set -euo pipefail

: "${AWS_PROFILE:?Set AWS_PROFILE (e.g. 'export AWS_PROFILE=fiduciary-demo') before running this script.}"
export AWS_PROFILE
REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$REGION"

ACCOUNT_ID="$(aws sts get-caller-identity --query 'Account' --output text)"
CLUSTER="fiduciary-demo"
SERVICE="portfolio-api"
BACKUP_BUCKET="fiduciary-demo-backups-${ACCOUNT_ID}"

log()  { echo "[$(date -u +%H:%M:%S)] $*"; }
warn() { echo "[$(date -u +%H:%M:%S)] WARNING: $*" >&2; }
die()  { echo "[$(date -u +%H:%M:%S)] ERROR: $*" >&2; exit 1; }

# --- Resource discovery (each prints empty string, not an error, if not found) ---

vpc_id()          { aws ec2 describe-vpcs --filters Name=tag:Name,Values=fiduciary-demo \
                       --query 'Vpcs[0].VpcId' --output text 2>/dev/null | sed 's/None//'; }
subnet_id()       { aws ec2 describe-subnets --filters "Name=tag:Name,Values=$1" \
                       --query 'Subnets[0].SubnetId' --output text 2>/dev/null | sed 's/None//'; }
sg_id()           { aws ec2 describe-security-groups --filters "Name=group-name,Values=$1" \
                       --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null | sed 's/None//'; }
igw_id()          { aws ec2 describe-internet-gateways --filters Name=tag:Name,Values=fiduciary-demo-igw \
                       --query 'InternetGateways[0].InternetGatewayId' --output text 2>/dev/null | sed 's/None//'; }
rt_id()           { aws ec2 describe-route-tables --filters "Name=tag:Name,Values=$1" \
                       --query 'RouteTables[0].RouteTableId' --output text 2>/dev/null | sed 's/None//'; }
nat_gw_id()       { aws ec2 describe-nat-gateways --filter "Name=tag:Name,Values=fiduciary-demo-nat" "Name=state,Values=available,pending" \
                       --query 'NatGateways[0].NatGatewayId' --output text 2>/dev/null | sed 's/None//'; }
nat_eip_alloc_id(){ local nat; nat="$(nat_gw_id)"; [ -z "$nat" ] && return 0
                     aws ec2 describe-nat-gateways --nat-gateway-ids "$nat" \
                       --query 'NatGateways[0].NatGatewayAddresses[0].AllocationId' --output text 2>/dev/null | sed 's/None//'; }
fs_id()           { aws efs describe-file-systems \
                       --query "FileSystems[?Name=='fiduciary-demo-data'].FileSystemId | [0]" --output text 2>/dev/null | sed 's/None//'; }
ap_id()           { local fs; fs="$(fs_id)"; [ -z "$fs" ] && return 0
                     aws efs describe-access-points --file-system-id "$fs" \
                       --query 'AccessPoints[0].AccessPointId' --output text 2>/dev/null | sed 's/None//'; }
alb_arn()         { (aws elbv2 describe-load-balancers --names fiduciary-demo-alb \
                       --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true) | sed 's/None//'; }
tg_arn()          { (aws elbv2 describe-target-groups --names fiduciary-demo-tg \
                       --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true) | sed 's/None//'; }
cert_arn()        { aws acm list-certificates --query \
                       "CertificateSummaryList[?DomainName=='demo.bentleybuilds.tech'].CertificateArn | [0]" \
                       --output text 2>/dev/null | sed 's/None//'; }
zone_id()         { aws route53 list-hosted-zones-by-name --dns-name demo.bentleybuilds.tech \
                       --query 'HostedZones[0].Id' --output text 2>/dev/null | sed 's/None//' | sed 's#/hostedzone/##'; }
backup_role_arn() { (aws iam get-role --role-name fiduciary-demo-backup-role \
                       --query 'Role.Arn' --output text 2>/dev/null || true) | sed 's/None//'; }
sched_role_arn()  { (aws iam get-role --role-name fiduciary-demo-scheduler \
                       --query 'Role.Arn' --output text 2>/dev/null || true) | sed 's/None//'; }
# WAF (2026-07-27, setup-waf.sh) -- same discovery-by-name pattern as everything else above.
waf_web_acl_id()  { aws wafv2 list-web-acls --scope REGIONAL --region "$REGION" \
                       --query "WebACLs[?Name=='fiduciary-demo-acl'].Id | [0]" --output text 2>/dev/null | sed 's/None//'; }
waf_ip_set_id()   { aws wafv2 list-ip-sets --scope REGIONAL --region "$REGION" \
                       --query "IPSets[?Name=='fiduciary-demo-blocklist'].Id | [0]" --output text 2>/dev/null | sed 's/None//'; }

# Convenience: private subnets + Fargate SG, the trio every one-off run-task call needs.
network_config() {
  local psa psb fsg
  psa="$(subnet_id fiduciary-demo-private-a)"
  psb="$(subnet_id fiduciary-demo-private-b)"
  fsg="$(sg_id fiduciary-demo-fargate)"
  if [ -z "$psa" ] || [ -z "$psb" ] || [ -z "$fsg" ]; then
    die "Could not find private subnets/Fargate SG -- has the environment been provisioned?"
  fi
  echo "awsvpcConfiguration={subnets=[$psa,$psb],securityGroups=[$fsg],assignPublicIp=DISABLED}"
}
