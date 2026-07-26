#!/usr/bin/env bash
# restore.sh -- syncs a previous S3 backup back onto the CURRENT EFS filesystem's /data.
#
# IMPORTANT SCOPE: this script only handles the "put the data back" step. It assumes you've
# already re-run AWS-DEMO-HOSTING-GUIDE.md §1-§3 (VPC/subnets/NAT + security groups + a FRESH EFS
# filesystem+access point) after a teardown, and that you have NOT yet started the ECS service
# (§10) -- restore before anything is reading/writing /data, same as the guide's own note. Full
# re-provisioning of the network/compute stack is deliberately NOT automated here; that part
# still benefits from watching each step succeed live rather than a blind script (see this
# session's own troubleshooting history in RESUME-TRACKER.md for why -- several real bugs only
# showed up by watching intermediate output).
#
# Usage:
#   export AWS_PROFILE=fiduciary-demo
#   ./scripts/aws-demo/restore.sh              # lists available backups, does nothing else
#   ./scripts/aws-demo/restore.sh 20260726T220000Z   # restores that specific backup

cd "$(dirname "${BASH_SOURCE[0]}")"
source ./common.sh

STAMP="${1:-}"

if [ -z "$STAMP" ]; then
  log "No backup timestamp given. Available backups in s3://$BACKUP_BUCKET/:"
  aws s3 ls "s3://$BACKUP_BUCKET/" | awk '{print "  "$2}' | sed 's#/$##'
  echo
  echo "Re-run as: $0 <timestamp>"
  exit 0
fi

if ! aws s3 ls "s3://$BACKUP_BUCKET/$STAMP/" >/dev/null 2>&1; then
  die "No backup found at s3://$BACKUP_BUCKET/$STAMP/ -- run with no argument to list what's available."
fi

FS="$(fs_id)"; AP="$(ap_id)"
[ -z "$FS" ] && die "No EFS filesystem found -- run AWS-DEMO-HOSTING-GUIDE.md §1-§3 to provision a fresh one before restoring."
[ -z "$AP" ] && die "No access point found on filesystem $FS."

BROLE="$(backup_role_arn)"
[ -z "$BROLE" ] && die "fiduciary-demo-backup-role not found -- run backup.sh once first (even on a fresh account) to create it, or re-create it manually per §14b."

log "Registering fiduciary-demo-restore task definition (writable EFS mount, FS=$FS)..."
cat > /tmp/restore-task.json <<JSON
{
  "family": "fiduciary-demo-restore",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "256", "memory": "512",
  "executionRoleArn": "arn:aws:iam::${ACCOUNT_ID}:role/fiduciary-demo-exec-role",
  "taskRoleArn": "${BROLE}",
  "volumes": [{"name": "data", "efsVolumeConfiguration": {"fileSystemId": "${FS}",
    "transitEncryption": "ENABLED", "authorizationConfig": {"accessPointId": "${AP}", "iam": "ENABLED"}}}],
  "containerDefinitions": [{
    "name": "restore", "image": "public.ecr.aws/aws-cli/aws-cli:latest",
    "mountPoints": [{"sourceVolume": "data", "containerPath": "/data", "readOnly": false}],
    "entryPoint": ["sh", "-c"],
    "logConfiguration": {"logDriver": "awslogs", "options": {
      "awslogs-group": "/ecs/fiduciary-demo", "awslogs-region": "${REGION}",
      "awslogs-stream-prefix": "restore", "awslogs-create-group": "true"}}
  }]
}
JSON
aws ecs register-task-definition --cli-input-json file:///tmp/restore-task.json >/dev/null

NETCFG="$(network_config)"
log "Restoring s3://$BACKUP_BUCKET/$STAMP/ -> /data ..."
TASK_ARN="$(aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition fiduciary-demo-restore \
  --launch-type FARGATE \
  --network-configuration "$NETCFG" \
  --overrides "{\"containerOverrides\":[{\"name\":\"restore\",\"command\":[\"aws s3 sync s3://$BACKUP_BUCKET/$STAMP/ /data && echo RESTORE_OK && ls -la /data\"]}]}" \
  --query 'tasks[0].taskArn' --output text)"
[ -z "$TASK_ARN" ] || [ "$TASK_ARN" = "None" ] && die "run-task did not return a task ARN -- check for an error above."
log "Task: $TASK_ARN -- waiting for it to finish..."
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"

TASK_ID="$(echo "$TASK_ARN" | awk -F/ '{print $NF}')"
LOG_OUTPUT="$(aws logs get-log-events --log-group-name /ecs/fiduciary-demo \
  --log-stream-name "restore/restore/$TASK_ID" --query 'events[].message' --output text 2>/dev/null || true)"
echo "$LOG_OUTPUT"

if echo "$LOG_OUTPUT" | grep -q "RESTORE_OK"; then
  log "Restore verified. You can now proceed with §5 onward (IAM roles already deleted on teardown"
  log "and need re-creating) through §10 (start the ECS service) in AWS-DEMO-HOSTING-GUIDE.md."
else
  die "Restore did NOT report RESTORE_OK -- check the log output above before starting the service."
fi
