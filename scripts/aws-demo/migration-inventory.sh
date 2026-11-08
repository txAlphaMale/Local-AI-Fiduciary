#!/usr/bin/env bash
# READ-ONLY inventory to run BEFORE the one-time Terraform migration (infra/terraform/README.md "One-time migration").
# It changes nothing. It reports what already exists in AWS under the names the `persistent/` stack manages, flags the
# things that would go wrong, and prints the exact `terraform import` commands to adopt the existing resources.
#
#   source ~/fiduciary-demo-vars.sh          # ACCOUNT_ID, REGION, credentials/profile
#   ./scripts/aws-demo/migration-inventory.sh
#
# Why it exists: the repo has NO import blocks. Running `terraform apply` in persistent/ against an account that already
# has the CLI-built resources would create a SECOND Route 53 hosted zone with the same name (Route 53 allows that), with
# different name servers: the DreamHost delegation and ACM validation would then point at the old zone while Terraform
# writes records into the new one. Adopt first (import), apply second.
set -euo pipefail
: "${ACCOUNT_ID:?run: source ~/fiduciary-demo-vars.sh}"
: "${REGION:?run: source ~/fiduciary-demo-vars.sh}"
cd "$(git rev-parse --show-toplevel)"

DOMAIN="${DOMAIN_NAME:-$(sed -n '/variable "domain_name"/,/}/s/.*default *= *"\(.*\)".*/\1/p' infra/terraform/persistent/variables.tf | head -1)}"
[ -n "$DOMAIN" ] || { echo "STOP: cannot determine the demo domain; set DOMAIN_NAME" >&2; exit 1; }
BACKUPS="fiduciary-demo-backups-$ACCOUNT_ID"
LOGS="fiduciary-demo-logs-$ACCOUNT_ID"
BLOCKERS=(); IMPORTS=()
say()  { printf '%s\n' "$*"; }
note() { BLOCKERS+=("$*"); say "  !! $*"; }
A()    { aws --region "$REGION" "$@"; }

say "== AWS inventory for the Terraform migration (read-only) =="
A sts get-caller-identity --query Arn --output text >/dev/null || { echo "STOP: AWS credentials do not work" >&2; exit 1; }

say; say "-- Route 53 zone for $DOMAIN"
mapfile -t ZONES < <(A route53 list-hosted-zones-by-name --dns-name "$DOMAIN" --max-items 5 \
        --query "HostedZones[?Name=='${DOMAIN}.'].Id" --output text | tr '\t' '\n' | sed 's#/hostedzone/##' | sed '/^$/d')
case "${#ZONES[@]}" in
  0) note "no hosted zone named $DOMAIN: the migration would create a NEW one and the registrar delegation would have to be redone" ;;
  1) say "  zone: ${ZONES[0]}"
     A route53 get-hosted-zone --id "${ZONES[0]}" --query 'DelegationSet.NameServers' --output text | tr '\t' '\n' | sed 's/^/    NS: /'
     say "  -> confirm these 4 name servers are what the registrar (DreamHost) delegates to."
     IMPORTS+=("terraform -chdir=infra/terraform/persistent import aws_route53_zone.demo ${ZONES[0]}") ;;
  *) note "MORE THAN ONE hosted zone named $DOMAIN (${ZONES[*]}): decide which one the registrar uses before importing" ;;
esac

say; say "-- S3 buckets"
for b in "$BACKUPS" "$LOGS"; do
  if A s3api head-bucket --bucket "$b" >/dev/null 2>&1; then say "  exists: $b"
    if [ "$b" = "$BACKUPS" ]; then
      newest=$(A s3 ls "s3://$b/" 2>/dev/null | tail -1 || true); say "    newest entry: ${newest:-none}"
      [ -n "$newest" ] || note "backup bucket exists but holds no backup: take one before tearing anything down"
      IMPORTS+=("terraform -chdir=infra/terraform/persistent import aws_s3_bucket.backups $b")
    else IMPORTS+=("terraform -chdir=infra/terraform/persistent import aws_s3_bucket.logs $b"); fi
  else say "  missing: $b (Terraform will create it)"; fi
done

say; say "-- ECR repositories"
for r in fiduciary/portfolio-api-demo fiduciary/mcpo-demo; do
  if mut=$(A ecr describe-repositories --repository-names "$r" --query 'repositories[0].imageTagMutability' --output text 2>/dev/null); then
    tags=$(A ecr describe-images --repository-name "$r" --query 'imageDetails[].imageTags[]' --output text 2>/dev/null | tr '\t' ' ')
    say "  $r: $mut; tags: ${tags:-none}"
    IMPORTS+=("terraform -chdir=infra/terraform/persistent import 'aws_ecr_repository.repo[\"$r\"]' $r")
    A ecr get-lifecycle-policy --repository-name "$r" >/dev/null 2>&1 && IMPORTS+=("terraform -chdir=infra/terraform/persistent import 'aws_ecr_lifecycle_policy.repo[\"$r\"]' $r")
    only_latest=1; for t in $tags; do [ "$t" = latest ] || only_latest=0; done
    if [ "$only_latest" = 1 ]; then note "$r holds only ':latest': add a unique tag BEFORE up.sh (README 'Image tags' has the put-image commands)"; fi
  else say "  missing: $r"; note "$r does not exist: the demo has no image to run until one is pushed"; fi
done

say; say "-- IAM / logs / secrets"
A iam get-role --role-name fiduciary-demo-backup-role >/dev/null 2>&1 \
  && { say "  exists: role fiduciary-demo-backup-role"; IMPORTS+=("terraform -chdir=infra/terraform/persistent import aws_iam_role.backup fiduciary-demo-backup-role"); } \
  || say "  missing: role fiduciary-demo-backup-role (Terraform will create it)"
A logs describe-log-groups --log-group-name-prefix aws-waf-logs-fiduciary-demo --query 'logGroups[].logGroupName' --output text 2>/dev/null | grep -q . \
  && { say "  exists: log group aws-waf-logs-fiduciary-demo"; IMPORTS+=("terraform -chdir=infra/terraform/persistent import aws_cloudwatch_log_group.waf aws-waf-logs-fiduciary-demo"); } \
  || say "  missing: log group aws-waf-logs-fiduciary-demo"
n=$(A ssm describe-parameters --parameter-filters Key=Name,Option=BeginsWith,Values=/fiduciary-demo/ --query 'length(Parameters)' --output text 2>/dev/null || echo 0)
say "  SSM parameters under /fiduciary-demo/: $n (the stack expects 6; ./scripts/aws-demo/set-secrets.sh manages them)"
[ "$n" -ge 6 ] 2>/dev/null || note "only $n SSM parameters found under /fiduciary-demo/ (expected 6): run set-secrets.sh before up.sh"

say; say "-- the running (CLI-built) environment"
A ecs describe-services --cluster fiduciary-demo --services portfolio-api --query 'services[0].[status,desiredCount,runningCount]' --output text 2>/dev/null | sed 's/^/  ECS service (status desired running): /' || say "  no ECS service"
for s in fiduciary-demo-scale-up fiduciary-demo-scale-down fiduciary-demo-backup-daily; do
  A scheduler get-schedule --name "$s" >/dev/null 2>&1 && say "  schedule: $s exists" || say "  schedule: $s missing"
done
A wafv2 list-web-acls --scope REGIONAL --query "WebACLs[?Name=='fiduciary-demo-acl'].Name" --output text 2>/dev/null | grep -q . \
  && say "  WAF Web ACL fiduciary-demo-acl exists (export-live-config.sh must snapshot its blocklist BEFORE teardown)" || say "  no WAF Web ACL named fiduciary-demo-acl"

say; say "-- local files"
[ -f infra/terraform/backend.hcl ] && say "  backend.hcl present" || say "  backend.hcl absent (expected before migration)"
[ -f infra/terraform/bootstrap/terraform.tfstate ] && say "  bootstrap local state present: BACK IT UP somewhere safe after step 2" || say "  bootstrap local state absent (expected before migration)"
if command -v terraform >/dev/null; then tfv=$(terraform version 2>/dev/null | head -1 || true); say "  ${tfv:-terraform present}"; else note "terraform is not installed"; fi

say; say "== import commands to run after the state bucket exists (README step 3), BEFORE the first 'terraform apply' in persistent/ =="
if [ "${#IMPORTS[@]}" -eq 0 ]; then say "  (nothing to import)"; else printf '  %s\n' "${IMPORTS[@]}"; fi
say; say "== things that need a decision or a fix first =="
if [ "${#BLOCKERS[@]}" -eq 0 ]; then say "  none found"; else printf '  - %s\n' "${BLOCKERS[@]}"; fi
say; say "Read-only run: nothing was changed."
