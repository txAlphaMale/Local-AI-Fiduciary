#!/usr/bin/env bash
# export-live-config.sh -- READ-ONLY snapshot of the running fiduciary-demo environment, taken
# before migrating it to Terraform (infra/terraform/). Makes ZERO changes to AWS: every call below
# is a describe/get/list. Nothing here reads secret VALUES -- SSM parameters are listed by
# name/type/version only (describe-parameters, never get-parameter --with-decryption).
#
# Why: hand-edited state (e.g. CIDRs added to the WAF blocklist via update-ip-set after
# setup-waf.sh ran) exists only in AWS, not in this repo. This captures it so the Terraform
# config is built from what is actually live, not from what the scripts originally created.
#
# Outputs (both gitignored except the tfvars file -- see infra/terraform/README.md):
#   infra/terraform/.live-snapshot/<UTC stamp>/*.json   raw describe output, one file per resource
#   infra/terraform/demo-env/waf.auto.tfvars.json       live WAF blocklist + geo-allow list, in the
#                                                       exact shape demo-env/variables.tf expects
#
# Usage:
#   export AWS_PROFILE=fiduciary-demo
#   ./scripts/aws-demo/export-live-config.sh

cd "$(dirname "${BASH_SOURCE[0]}")"
source ./common.sh
set +e   # a missing optional resource must not abort the whole snapshot

REPO_ROOT="$(cd ../.. && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$REPO_ROOT/infra/terraform/.live-snapshot/$STAMP"
mkdir -p "$OUT"
log "Account: $ACCOUNT_ID | Region: $REGION | Writing snapshot to $OUT"

# snap <file> <aws args...> -- run one read-only call, save JSON, never abort on failure
snap() {
  local f="$1"; shift
  if aws "$@" --output json > "$OUT/$f.json" 2> "$OUT/$f.err"; then
    rm -f "$OUT/$f.err"; log "  ok   $f"
  else
    log "  MISS $f ($(head -c 160 "$OUT/$f.err" | tr '\n' ' '))"
  fi
}

log "--- WAF (the main reason this script exists) ---"
WAF_ID="$(waf_web_acl_id)"; IPSET_ID="$(waf_ip_set_id)"
[ -n "$IPSET_ID" ] && snap waf-ip-set wafv2 get-ip-set --name fiduciary-demo-blocklist --scope REGIONAL --id "$IPSET_ID"
[ -n "$WAF_ID" ]   && snap waf-web-acl wafv2 get-web-acl --name fiduciary-demo-acl --scope REGIONAL --id "$WAF_ID"
snap waf-ip-sets-all  wafv2 list-ip-sets  --scope REGIONAL
snap waf-web-acls-all wafv2 list-web-acls --scope REGIONAL
ALB="$(alb_arn)"
[ -n "$ALB" ] && snap waf-association wafv2 get-web-acl-for-resource --resource-arn "$ALB"
[ -n "$WAF_ID" ] && snap waf-logging wafv2 get-logging-configuration \
  --resource-arn "$(aws wafv2 get-web-acl --name fiduciary-demo-acl --scope REGIONAL --id "$WAF_ID" --query WebACL.ARN --output text)"

log "--- Network ---"
VPC="$(vpc_id)"
snap vpc            ec2 describe-vpcs --filters Name=tag:Name,Values=fiduciary-demo
if [ -n "$VPC" ]; then
  snap subnets        ec2 describe-subnets          --filters "Name=vpc-id,Values=$VPC"
  snap route-tables   ec2 describe-route-tables     --filters "Name=vpc-id,Values=$VPC"
  snap igw            ec2 describe-internet-gateways --filters "Name=attachment.vpc-id,Values=$VPC"
  snap nat-gateways   ec2 describe-nat-gateways     --filter "Name=vpc-id,Values=$VPC"
  snap security-groups ec2 describe-security-groups --filters "Name=vpc-id,Values=$VPC"
  snap vpc-endpoints  ec2 describe-vpc-endpoints    --filters "Name=vpc-id,Values=$VPC"
  snap flow-logs      ec2 describe-flow-logs        --filter "Name=resource-id,Values=$VPC"
fi
snap eips ec2 describe-addresses

log "--- ALB / TLS / DNS ---"
TG="$(tg_arn)"
if [ -n "$ALB" ]; then
  snap alb             elbv2 describe-load-balancers --load-balancer-arns "$ALB"
  snap alb-attributes  elbv2 describe-load-balancer-attributes --load-balancer-arn "$ALB"
  snap alb-listeners   elbv2 describe-listeners --load-balancer-arn "$ALB"
  snap alb-tags        elbv2 describe-tags --resource-arns "$ALB"
fi
if [ -n "$TG" ]; then
  snap target-group            elbv2 describe-target-groups --target-group-arns "$TG"
  snap target-group-attributes elbv2 describe-target-group-attributes --target-group-arn "$TG"
fi
CERT="$(cert_arn)"
[ -n "$CERT" ] && snap acm-cert acm describe-certificate --certificate-arn "$CERT"
ZONE="$(zone_id)"
if [ -n "$ZONE" ]; then
  snap route53-zone    route53 get-hosted-zone --id "$ZONE"
  snap route53-records route53 list-resource-record-sets --hosted-zone-id "$ZONE"
fi

log "--- ECS ---"
snap ecs-cluster ecs describe-clusters --clusters "$CLUSTER" --include SETTINGS CONFIGURATIONS TAGS
snap ecs-service ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --include TAGS
for fam in fiduciary-demo-portfolio-api fiduciary-demo-backup fiduciary-demo-restore; do
  snap "taskdef-$fam" ecs describe-task-definition --task-definition "$fam" --include TAGS
done

log "--- EFS ---"
FS="$(fs_id)"
if [ -n "$FS" ]; then
  snap efs                efs describe-file-systems --file-system-id "$FS"
  snap efs-mount-targets  efs describe-mount-targets --file-system-id "$FS"
  snap efs-access-points  efs describe-access-points --file-system-id "$FS"
  snap efs-lifecycle      efs describe-lifecycle-configuration --file-system-id "$FS"
  snap efs-backup-policy  efs describe-backup-policy --file-system-id "$FS"
  snap efs-fs-policy      efs describe-file-system-policy --file-system-id "$FS"
fi

log "--- IAM (roles + inline/attached policies; includes the GitHub OIDC push role if present) ---"
snap iam-roles-fiduciary iam list-roles --query "Roles[?contains(RoleName, 'fiduciary') || contains(RoleName, 'github')]"
for role in $(aws iam list-roles --query "Roles[?contains(RoleName, 'fiduciary') || contains(RoleName, 'github')].RoleName" --output text); do
  snap "iam-role-$role"          iam get-role --role-name "$role"
  snap "iam-role-$role-attached" iam list-attached-role-policies --role-name "$role"
  for pol in $(aws iam list-role-policies --role-name "$role" --query 'PolicyNames' --output text); do
    snap "iam-role-$role-inline-$pol" iam get-role-policy --role-name "$role" --policy-name "$pol"
  done
done
snap iam-oidc-providers iam list-open-id-connect-providers

log "--- Scheduler, logs, ECR, S3, SSM (names only), budgets ---"
snap scheduler-schedules scheduler list-schedules --name-prefix fiduciary-demo
for s in fiduciary-demo-scale-up fiduciary-demo-scale-down fiduciary-demo-backup-daily; do
  snap "schedule-$s" scheduler get-schedule --name "$s"
done
snap log-groups logs describe-log-groups --log-group-name-prefix /ecs/fiduciary-demo
snap ecr-repos  ecr describe-repositories --repository-names fiduciary/portfolio-api-demo fiduciary/mcpo-demo
for r in fiduciary/portfolio-api-demo fiduciary/mcpo-demo; do
  f="ecr-images-${r//\//_}"
  snap "$f" ecr describe-images --repository-name "$r" --query 'sort_by(imageDetails,&imagePushedAt)[-5:]'
  snap "ecr-lifecycle-${r//\//_}" ecr get-lifecycle-policy --repository-name "$r"
done
snap s3-backup-versioning s3api get-bucket-versioning           --bucket "$BACKUP_BUCKET"
snap s3-backup-lifecycle  s3api get-bucket-lifecycle-configuration --bucket "$BACKUP_BUCKET"
snap s3-backup-encryption s3api get-bucket-encryption           --bucket "$BACKUP_BUCKET"
snap s3-backup-pab        s3api get-public-access-block         --bucket "$BACKUP_BUCKET"
snap s3-backup-policy     s3api get-bucket-policy               --bucket "$BACKUP_BUCKET"
snap s3-backup-prefixes   s3api list-objects-v2 --bucket "$BACKUP_BUCKET" --delimiter / --query 'CommonPrefixes[].Prefix'
# Names/types/versions ONLY -- describe-parameters never returns values.
snap ssm-parameters ssm describe-parameters --parameter-filters Key=Name,Option=BeginsWith,Values=/fiduciary-demo/
snap budgets budgets describe-budgets --account-id "$ACCOUNT_ID"

# --- Derive the Terraform WAF input from the live IP set + Web ACL (not from setup-waf.sh's seed) ---
TFVARS="$REPO_ROOT/infra/terraform/demo-env/waf.auto.tfvars.json"
if [ -s "$OUT/waf-ip-set.json" ] && [ -s "$OUT/waf-web-acl.json" ]; then
  python3 - "$OUT/waf-ip-set.json" "$OUT/waf-web-acl.json" "$STAMP" > "$TFVARS" <<'PYEOF'
import json, sys
ipset = json.load(open(sys.argv[1]))["IPSet"]
acl   = json.load(open(sys.argv[2]))["WebACL"]
countries = []
for r in acl.get("Rules", []):
    st = r.get("Statement", {})
    geo = st.get("NotStatement", {}).get("Statement", {}).get("GeoMatchStatement") or st.get("GeoMatchStatement")
    if geo:
        countries = geo.get("CountryCodes", [])
# (No "_source" key: Terraform warns on undeclared variables in tfvars files. Provenance is the
# snapshot directory name; regenerate this file rather than hand-editing if the live set changes.)
print(json.dumps({
    "waf_blocklist_cidrs": sorted(ipset.get("Addresses", [])),
    "waf_geo_allowed_countries": sorted(countries),
}, indent=2))
PYEOF
  log "WAF inputs written to $TFVARS:"
  cat "$TFVARS"
else
  warn "WAF IP set / Web ACL not found -- $TFVARS NOT written. Check $OUT/*.err."
fi

echo
log "Snapshot complete: $OUT"
log "MISS lines above are expected for anything never configured (bucket policy, EFS backup policy, flow logs, etc.)."
log "Nothing in this snapshot contains a secret value. It does contain account IDs, ARNs, and IPs --"
log "it is gitignored; share it with the Fiduciary session for reconciliation, don't commit it."
