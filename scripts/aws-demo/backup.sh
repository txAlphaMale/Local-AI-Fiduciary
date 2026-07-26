#!/usr/bin/env bash
# backup.sh -- one-time setup (idempotent) + a fresh timestamped backup of the fiduciary-demo
# EFS /data volume to S3. See AWS-DEMO-HOSTING-GUIDE.md §14b for the full explanation.
#
# Usage:
#   export AWS_PROFILE=fiduciary-demo
#   ./scripts/aws-demo/backup.sh
#
# Safe to run repeatedly -- each run creates a new timestamped prefix in S3 and never touches a
# previous backup. The one-time setup (bucket/role/task-definition) is skipped if already present.

cd "$(dirname "${BASH_SOURCE[0]}")"
source ./common.sh

log "Account: $ACCOUNT_ID | Region: $REGION | Bucket: $BACKUP_BUCKET"

# --- One-time setup (idempotent: each step checks first) ---

if ! aws s3api head-bucket --bucket "$BACKUP_BUCKET" 2>/dev/null; then
  log "Creating backup bucket $BACKUP_BUCKET..."
  aws s3api create-bucket --bucket "$BACKUP_BUCKET" --region "$REGION"
  aws s3api put-bucket-versioning --bucket "$BACKUP_BUCKET" --versioning-configuration Status=Enabled
  aws s3api put-public-access-block --bucket "$BACKUP_BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
else
  log "Backup bucket already exists, skipping creation."
fi

FS="$(fs_id)"; AP="$(ap_id)"
[ -z "$FS" ] && die "No fiduciary-demo EFS filesystem found -- has the environment been provisioned?"
[ -z "$AP" ] && die "No access point found on filesystem $FS."

BROLE="$(backup_role_arn)"
if [ -z "$BROLE" ]; then
  log "Creating fiduciary-demo-backup-role..."
  cat > /tmp/backup-trust-policy.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
  aws iam create-role --role-name fiduciary-demo-backup-role \
    --assume-role-policy-document file:///tmp/backup-trust-policy.json >/dev/null
  cat > /tmp/backup-permissions.json <<JSON
{"Version":"2012-10-17","Statement":[
  {"Effect":"Allow","Action":["elasticfilesystem:ClientMount","elasticfilesystem:ClientRead","elasticfilesystem:ClientWrite"],
   "Resource":"arn:aws:elasticfilesystem:${REGION}:${ACCOUNT_ID}:file-system/${FS}"},
  {"Effect":"Allow","Action":["s3:PutObject","s3:GetObject","s3:ListBucket"],
   "Resource":["arn:aws:s3:::${BACKUP_BUCKET}","arn:aws:s3:::${BACKUP_BUCKET}/*"]}]}
JSON
  aws iam put-role-policy --role-name fiduciary-demo-backup-role --policy-name backup-access \
    --policy-document file:///tmp/backup-permissions.json
  log "Waiting a moment for IAM propagation..."
  sleep 10
  BROLE="$(backup_role_arn)"
else
  log "Backup role already exists, skipping creation."
fi

# Task definitions are free to leave registered/re-register -- always (re-)register so this script
# self-heals if the definition is ever missing or the FS/AP IDs changed (e.g. after a restore-from-
# scratch on a new filesystem).
log "Registering fiduciary-demo-backup task definition (readOnly EFS mount)..."
cat > /tmp/backup-task.json <<JSON
{
  "family": "fiduciary-demo-backup",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "256", "memory": "512",
  "executionRoleArn": "arn:aws:iam::${ACCOUNT_ID}:role/fiduciary-demo-exec-role",
  "taskRoleArn": "${BROLE}",
  "volumes": [{"name": "data", "efsVolumeConfiguration": {"fileSystemId": "${FS}",
    "transitEncryption": "ENABLED", "authorizationConfig": {"accessPointId": "${AP}", "iam": "ENABLED"}}}],
  "containerDefinitions": [{
    "name": "backup", "image": "public.ecr.aws/aws-cli/aws-cli:latest",
    "mountPoints": [{"sourceVolume": "data", "containerPath": "/data", "readOnly": true}],
    "entryPoint": ["sh", "-c"],
    "logConfiguration": {"logDriver": "awslogs", "options": {
      "awslogs-group": "/ecs/fiduciary-demo", "awslogs-region": "${REGION}",
      "awslogs-stream-prefix": "backup", "awslogs-create-group": "true"}}
  }]
}
JSON
aws ecs register-task-definition --cli-input-json file:///tmp/backup-task.json >/dev/null

# --- The actual backup ---

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NETCFG="$(network_config)"
log "Starting backup task (stamp: $STAMP)..."
TASK_ARN="$(aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition fiduciary-demo-backup \
  --launch-type FARGATE \
  --network-configuration "$NETCFG" \
  --overrides "{\"containerOverrides\":[{\"name\":\"backup\",\"command\":[\"aws s3 sync /data s3://$BACKUP_BUCKET/$STAMP/ && echo BACKUP_OK\"]}]}" \
  --query 'tasks[0].taskArn' --output text)"
[ -z "$TASK_ARN" ] || [ "$TASK_ARN" = "None" ] && die "run-task did not return a task ARN -- check for an error above."
log "Task: $TASK_ARN -- waiting for it to finish..."
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"

TASK_ID="$(echo "$TASK_ARN" | awk -F/ '{print $NF}')"
LOG_OUTPUT="$(aws logs get-log-events --log-group-name /ecs/fiduciary-demo \
  --log-stream-name "backup/backup/$TASK_ID" --query 'events[].message' --output text 2>/dev/null || true)"
echo "$LOG_OUTPUT"

FILE_COUNT="$(aws s3 ls "s3://$BACKUP_BUCKET/$STAMP/" --recursive | wc -l)"

if echo "$LOG_OUTPUT" | grep -q "BACKUP_OK" && [ "$FILE_COUNT" -gt 0 ]; then
  log "Backup verified: $FILE_COUNT file(s) at s3://$BACKUP_BUCKET/$STAMP/"
  echo "$STAMP" > /tmp/fiduciary-demo-last-backup-stamp.txt
  exit 0
else
  die "Backup did NOT verify cleanly (BACKUP_OK marker present: $(echo "$LOG_OUTPUT" | grep -c BACKUP_OK || true), files found: $FILE_COUNT). Check the log output above before proceeding with any teardown."
fi
