#!/usr/bin/env bash
# teardown.sh -- backs up /data (via backup.sh), then tears down every fiduciary-demo AWS resource
# EXCEPT the things worth keeping across a pause/resume cycle: the S3 backup bucket + its contents,
# the backup IAM role/task-definition, and the Route 53 HOSTED ZONE itself (only the DNS record
# inside it is removed -- keeping the zone means the DreamHost NS delegation from the original
# buildout never has to be redone; the zone alone costs ~$0.50/mo, far cheaper than that manual
# re-delegation dance). See AWS-DEMO-HOSTING-GUIDE.md §14b/§15 for the full narrative.
#
# Usage:
#   export AWS_PROFILE=fiduciary-demo
#   ./scripts/aws-demo/teardown.sh          # prompts for confirmation
#   ./scripts/aws-demo/teardown.sh -y        # skips confirmation (for scripting/automation)
#
# Safe-ish to re-run: most deletions are guarded with existence checks first, so a second run
# after a partial failure will just skip whatever's already gone rather than erroring out.

cd "$(dirname "${BASH_SOURCE[0]}")"
source ./common.sh

SKIP_CONFIRM=0
[ "${1:-}" = "-y" ] && SKIP_CONFIRM=1

log "=== Step 1/3: backing up /data before touching anything ==="
if ! ./backup.sh; then
  die "Backup failed -- refusing to tear down. Fix the backup first (see output above), or if you genuinely don't care about /data's contents, delete backup.sh's safety check yourself -- this script won't do it for you."
fi

if [ "$SKIP_CONFIRM" -ne 1 ]; then
  echo
  echo "About to DELETE the following, if present: EventBridge schedules, ECS service+cluster,"
  echo "ALB+target group, the DNS A-record (zone itself is KEPT), ACM cert, EFS (filesystem +"
  echo "data -- already backed up above), NAT Gateway + EIP, route tables/subnets/IGW/VPC,"
  echo "security groups, CloudWatch log group, both ECR repos, all 6 SSM parameters, and the"
  echo "exec/task/scheduler IAM roles. The S3 backup bucket, backup IAM role, backup task"
  echo "definition, and Route 53 hosted zone are deliberately KEPT."
  echo
  read -r -p "Type 'teardown' to proceed: " CONFIRM
  [ "$CONFIRM" != "teardown" ] && { log "Aborted, nothing deleted."; exit 1; }
fi

log "=== Step 2/3: deleting EventBridge schedules ==="
aws scheduler delete-schedule --name fiduciary-demo-scale-up 2>/dev/null || log "  (scale-up schedule already gone)"
aws scheduler delete-schedule --name fiduciary-demo-scale-down 2>/dev/null || log "  (scale-down schedule already gone)"

log "=== Step 3/3: full resource teardown ==="

ALB="$(alb_arn)"; TG="$(tg_arn)"; ZONE="$(zone_id)"

if [ -n "$ALB" ] && [ -n "$ZONE" ]; then
  log "Deleting DNS A-record for demo.bentleybuilds.tech (keeping the hosted zone itself)..."
  EXISTING="$(aws route53 list-resource-record-sets --hosted-zone-id "$ZONE" \
    --query "ResourceRecordSets[?Name=='demo.bentleybuilds.tech.' && Type=='A']" --output json)"
  if [ "$EXISTING" != "[]" ]; then
    cat > /tmp/dns-delete.json <<JSON
{"Changes":[{"Action":"DELETE","ResourceRecordSet":$(echo "$EXISTING" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)[0]))')}]}
JSON
    aws route53 change-resource-record-sets --hosted-zone-id "$ZONE" --change-batch file:///tmp/dns-delete.json >/dev/null
  else
    log "  (no A-record found, skipping)"
  fi
fi

log "Scaling service to 0 and deleting service + cluster..."
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --desired-count 0 2>/dev/null || true
aws ecs delete-service --cluster "$CLUSTER" --service "$SERVICE" --force 2>/dev/null || log "  (service already gone)"
aws ecs delete-cluster --cluster "$CLUSTER" 2>/dev/null || log "  (cluster already gone)"

if [ -n "$ALB" ]; then
  log "Deleting ALB..."
  aws elbv2 delete-load-balancer --load-balancer-arn "$ALB"
  log "  waiting for ALB deletion to finish before removing its target group/security group..."
  sleep 20
else
  log "  (ALB already gone)"
fi
[ -n "$TG" ] && aws elbv2 delete-target-group --target-group-arn "$TG" 2>/dev/null || true

CERT="$(cert_arn)"
if [ -n "$CERT" ]; then
  log "Deleting ACM certificate..."
  aws acm delete-certificate --certificate-arn "$CERT" 2>/dev/null || log "  (cert delete failed -- may still be referenced, check manually)"
fi

FS="$(fs_id)"
if [ -n "$FS" ]; then
  log "Deleting EFS mount targets, access point, filesystem (already backed up above)..."
  for mt in $(aws efs describe-mount-targets --file-system-id "$FS" --query 'MountTargets[].MountTargetId' --output text); do
    aws efs delete-mount-target --mount-target-id "$mt"
  done
  sleep 5
  AP="$(ap_id)"; [ -n "$AP" ] && aws efs delete-access-point --access-point-id "$AP" 2>/dev/null || true
  aws efs delete-file-system --file-system-id "$FS" 2>/dev/null || log "  (retry in a moment if mount targets haven't finished deleting)"
else
  log "  (EFS already gone)"
fi

NAT="$(nat_gw_id)"
if [ -n "$NAT" ]; then
  EIP="$(nat_eip_alloc_id)"
  log "Deleting NAT Gateway (this takes a minute or two)..."
  aws ec2 delete-nat-gateway --nat-gateway-id "$NAT"
  aws ec2 wait nat-gateway-deleted --nat-gateway-ids "$NAT"
  [ -n "$EIP" ] && aws ec2 release-address --allocation-id "$EIP" 2>/dev/null || true
else
  log "  (NAT Gateway already gone)"
fi

VPC="$(vpc_id)"
if [ -n "$VPC" ]; then
  log "Deleting route tables, subnets, IGW, VPC..."
  PRT="$(rt_id fiduciary-demo-private-rt)"; PUBRT="$(rt_id fiduciary-demo-public-rt)"
  [ -n "$PRT" ] && aws ec2 delete-route-table --route-table-id "$PRT" 2>/dev/null || true
  [ -n "$PUBRT" ] && aws ec2 delete-route-table --route-table-id "$PUBRT" 2>/dev/null || true
  for name in fiduciary-demo-private-a fiduciary-demo-private-b fiduciary-demo-public-a fiduciary-demo-public-b; do
    SN="$(subnet_id "$name")"; [ -n "$SN" ] && aws ec2 delete-subnet --subnet-id "$SN" 2>/dev/null || true
  done
  IGW="$(igw_id)"
  if [ -n "$IGW" ]; then
    aws ec2 detach-internet-gateway --internet-gateway-id "$IGW" --vpc-id "$VPC" 2>/dev/null || true
    aws ec2 delete-internet-gateway --internet-gateway-id "$IGW" 2>/dev/null || true
  fi
  log "Deleting security groups..."
  for name in fiduciary-demo-fargate fiduciary-demo-efs fiduciary-demo-alb; do
    SG="$(sg_id "$name")"; [ -n "$SG" ] && aws ec2 delete-security-group --group-id "$SG" 2>/dev/null || true
  done
  aws ec2 delete-vpc --vpc-id "$VPC" 2>/dev/null || warn "VPC deletion failed -- something's still attached, check manually (aws ec2 describe-vpc-attribute / check for lingering ENIs)."
else
  log "  (VPC already gone)"
fi

log "Deleting CloudWatch log group + ECR repos..."
aws logs delete-log-group --log-group-name /ecs/fiduciary-demo 2>/dev/null || true
aws ecr delete-repository --repository-name fiduciary/portfolio-api-demo --force 2>/dev/null || true
aws ecr delete-repository --repository-name fiduciary/mcpo-demo --force 2>/dev/null || true

log "Deleting SSM parameters..."
for p in MCPO_API_KEY SESSION_SECRET ANTHROPIC_API_KEY DEMO_ADMIN_PASSWORD FINNHUB_API_KEY TAVILY_API_KEY; do
  aws ssm delete-parameter --name "/fiduciary-demo/$p" 2>/dev/null || true
done

log "Deleting IAM roles (exec/task/scheduler -- backup role is KEPT for next time)..."
aws iam delete-role-policy --role-name fiduciary-demo-exec-role --policy-name ssm-read 2>/dev/null || true
aws iam delete-role-policy --role-name fiduciary-demo-exec-role --policy-name cloudwatch-logs 2>/dev/null || true
aws iam detach-role-policy --role-name fiduciary-demo-exec-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy 2>/dev/null || true
aws iam delete-role --role-name fiduciary-demo-exec-role 2>/dev/null || true
aws iam delete-role-policy --role-name fiduciary-demo-task-role --policy-name efs-access 2>/dev/null || true
aws iam delete-role --role-name fiduciary-demo-task-role 2>/dev/null || true
aws iam delete-role-policy --role-name fiduciary-demo-scheduler --policy-name scale-portfolio-api 2>/dev/null || true
aws iam delete-role --role-name fiduciary-demo-scheduler 2>/dev/null || true

log "=== Teardown complete ==="
log "Kept: S3 bucket $BACKUP_BUCKET (your data), fiduciary-demo-backup-role, fiduciary-demo-backup"
log "task definition, and the Route 53 hosted zone for demo.bentleybuilds.tech."
log "Check the AWS Billing console's Bill Details in a day or two to confirm charges dropped."
