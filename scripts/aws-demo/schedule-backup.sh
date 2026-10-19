#!/usr/bin/env bash
# LEGACY (2026-10-16): superseded by infra/terraform/demo-env for Terraform-managed environments.
# Kept for reference only -- running it against a Terraform-built stack creates drift.
# schedule-backup.sh -- SEC-7a (Backup & Disaster Recovery), AWS demo deployment.
#
# WHAT THIS FIXES: backup.sh (this directory) is fully built and idempotent -- it creates the S3
# bucket/IAM role/task-definition on first run and syncs /data to S3 -- but the hosting guide only
# ever told you to run it MANUALLY, "before every teardown." There was no recurring schedule, so a
# real disaster (host issue, bad deploy, accidental EFS mistake) between teardowns had no backup
# newer than whenever you last remembered to run backup.sh by hand. This adds an EventBridge
# Scheduler rule that runs the SAME backup task on a daily cron, no memory required.
#
# PREREQUISITE: run ./backup.sh at least once first (manually) -- this script does NOT create the
# S3 bucket, backup IAM role, or task definition; it only verifies they exist and adds a recurring
# trigger for them. This keeps the already-evidenced backup.sh untouched rather than duplicating
# its setup logic a second time.
#
# Usage:
#   export AWS_PROFILE=fiduciary-demo
#   ./scripts/aws-demo/schedule-backup.sh
#
# Safe to re-run (idempotent: re-creates the schedule/policy with the same content if already present).

cd "$(dirname "${BASH_SOURCE[0]}")"
source ./common.sh

log "Account: $ACCOUNT_ID | Region: $REGION | Bucket: $BACKUP_BUCKET"

# --- Verify the one-time backup infra already exists (from a manual ./backup.sh run) ---

if ! aws s3api head-bucket --bucket "$BACKUP_BUCKET" 2>/dev/null; then
  die "Backup bucket $BACKUP_BUCKET doesn't exist yet. Run ./backup.sh once manually first."
fi
BROLE="$(backup_role_arn)"
[ -z "$BROLE" ] && die "fiduciary-demo-backup-role doesn't exist yet. Run ./backup.sh once manually first."
if ! aws ecs describe-task-definition --task-definition fiduciary-demo-backup >/dev/null 2>&1; then
  die "fiduciary-demo-backup task definition doesn't exist yet. Run ./backup.sh once manually first."
fi
EXEC_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/fiduciary-demo-exec-role"
log "Backup infra confirmed present (bucket, role, task definition)."

# --- Scheduler IAM role: reuse fiduciary-demo-scheduler (created in §14a for scale-up/down) if it
# exists, adding a SECOND, narrowly-scoped inline policy for this one new action -- one role,
# multiple named single-purpose policies, rather than a role per schedule. RunTask additionally
# needs iam:PassRole for the two roles named IN the task definition (the backup task's own task
# role + the shared exec role), which UpdateService never needed -- that's the real difference
# from the scale-up/down role's existing policy. ---

SCHED_ROLE_ARN="$(sched_role_arn)"
if [ -z "$SCHED_ROLE_ARN" ]; then
  log "Creating fiduciary-demo-scheduler role (scale-to-zero automation, §14a, hasn't been set up yet)..."
  cat > /tmp/scheduler-trust-policy.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
  "Principal":{"Service":"scheduler.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
  aws iam create-role --role-name fiduciary-demo-scheduler \
    --assume-role-policy-document file:///tmp/scheduler-trust-policy.json >/dev/null
  sleep 10
  SCHED_ROLE_ARN="$(sched_role_arn)"
else
  log "Re-using existing fiduciary-demo-scheduler role."
fi

log "Attaching/refreshing the run-backup-task policy on fiduciary-demo-scheduler..."
cat > /tmp/scheduler-backup-permissions.json <<JSON
{"Version":"2012-10-17","Statement":[
  {"Effect":"Allow","Action":"ecs:RunTask",
   "Resource":"arn:aws:ecs:${REGION}:${ACCOUNT_ID}:task-definition/fiduciary-demo-backup:*",
   "Condition":{"ArnLike":{"ecs:cluster":"arn:aws:ecs:${REGION}:${ACCOUNT_ID}:cluster/${CLUSTER}"}}},
  {"Effect":"Allow","Action":"iam:PassRole",
   "Resource":["${BROLE}","${EXEC_ROLE_ARN}"]}
]}
JSON
aws iam put-role-policy --role-name fiduciary-demo-scheduler \
  --policy-name run-backup-task --policy-document file:///tmp/scheduler-backup-permissions.json

# --- The recurring schedule itself: EventBridge Scheduler -> ECS RunTask directly, no Lambda ---
# (same "native SDK target" pattern as the scale-up/down schedules in §14a). The command below
# generates ITS OWN timestamp at execution time (the shell runs inside the container when the task
# actually starts, not when this schedule is created), so every scheduled run gets a fresh,
# distinct S3 prefix -- exactly like backup.sh's manual run does.
#
# TIME CHOSEN: 08:00 UTC daily -- outside the demo's 8am-5pm Eastern (12:00-21:00 UTC) business
# window from §14a, so it never competes with the scale-up transition, and EFS is reachable
# regardless of whether portfolio-api itself is scaled to 0 or 1 (EFS persists independent of the
# service's desired count -- this is a separate one-off task, not the portfolio-api service).

# Built via temp JSON files (--target file://...), not an inline escaped string -- the scale-up/
# down schedules in §14a could get away with an inline string because their Input has no nested
# arrays; RunTask's NetworkConfiguration.Subnets/SecurityGroups DOES, and hand-escaping a JSON array
# inside a JSON string inside a shell string is exactly the kind of thing that silently produces
# invalid JSON (a bare unquoted subnet-id) if done by hand. A real JSON file, built with each field
# interpolated once, is both correct and far easier to read/debug than the equivalent inline string.

PSA="$(subnet_id fiduciary-demo-private-a)"
PSB="$(subnet_id fiduciary-demo-private-b)"
FSG="$(sg_id fiduciary-demo-fargate)"
if [ -z "$PSA" ] || [ -z "$PSB" ] || [ -z "$FSG" ]; then
  die "Could not find private subnets/Fargate SG -- has the environment been provisioned?"
fi

# The RunTask Input itself is JSON, but the Scheduler API takes it as a JSON STRING VALUE inside
# the outer target document -- so it's written here as a compact one-line string via python3's
# json.dumps (guaranteed correct escaping, no hand-counted backslashes) rather than typed by hand.
RUNTASK_INPUT="$(python3 - "$CLUSTER" "$PSA" "$PSB" "$FSG" "$BACKUP_BUCKET" <<'PYEOF'
import json, sys
cluster, psa, psb, fsg, bucket = sys.argv[1:6]
cmd = f"aws s3 sync /data s3://{bucket}/scheduled-$(date -u +%Y%m%dT%H%M%SZ)/ && echo BACKUP_OK"
print(json.dumps({
    "Cluster": cluster,
    "TaskDefinition": "fiduciary-demo-backup",
    "LaunchType": "FARGATE",
    "NetworkConfiguration": {"AwsvpcConfiguration": {
        "Subnets": [psa, psb], "SecurityGroups": [fsg], "AssignPublicIp": "DISABLED"}},
    "Overrides": {"ContainerOverrides": [{"Name": "backup", "Command": ["sh", "-c", cmd]}]},
}))
PYEOF
)"

python3 - "$SCHED_ROLE_ARN" "$RUNTASK_INPUT" > /tmp/schedule-target.json <<'PYEOF'
import json, sys
role_arn, runtask_input = sys.argv[1:3]
print(json.dumps({
    "Arn": "arn:aws:scheduler:::aws-sdk:ecs:runTask",
    "RoleArn": role_arn,
    "Input": runtask_input,
}))
PYEOF

if aws scheduler get-schedule --name fiduciary-demo-backup-daily >/dev/null 2>&1; then
  log "Schedule already exists -- updating it in place with the same definition."
  aws scheduler update-schedule --name fiduciary-demo-backup-daily \
    --schedule-expression "cron(0 8 * * ? *)" \
    --schedule-expression-timezone "UTC" \
    --flexible-time-window '{"Mode":"OFF"}' \
    --target file:///tmp/schedule-target.json
else
  aws scheduler create-schedule --name fiduciary-demo-backup-daily \
    --schedule-expression "cron(0 8 * * ? *)" \
    --schedule-expression-timezone "UTC" \
    --flexible-time-window '{"Mode":"OFF"}' \
    --target file:///tmp/schedule-target.json
fi

log "Schedule 'fiduciary-demo-backup-daily' active: runs 08:00 UTC every day."

# --- S3 lifecycle: bound cost. Versioning was already turned on by backup.sh's one-time setup
# (protects against an accidental overwrite), but WITHOUT a lifecycle rule, every daily sync's
# objects accumulate forever. 30 days is a reasonable default for a demo instance -- adjust if you
# want a longer history.
log "Setting a 30-day expiration lifecycle rule on $BACKUP_BUCKET (adjust if you want longer retention)..."
cat > /tmp/backup-lifecycle.json <<'JSON'
{"Rules":[{"ID":"expire-old-backups","Status":"Enabled","Filter":{},"Expiration":{"Days":30},
  "NoncurrentVersionExpiration":{"NoncurrentDays":30}}]}
JSON
aws s3api put-bucket-lifecycle-configuration --bucket "$BACKUP_BUCKET" \
  --lifecycle-configuration file:///tmp/backup-lifecycle.json

echo
echo "Done. Verify with:"
echo "  aws scheduler get-schedule --name fiduciary-demo-backup-daily"
echo "  aws s3api get-bucket-lifecycle-configuration --bucket $BACKUP_BUCKET"
echo
echo "Cost: one Fargate task (256 CPU / 512MB) running for well under a minute, once a day --"
echo "negligible (a small fraction of a cent per run). The 30-day S3 lifecycle rule keeps storage"
echo "cost bounded regardless of how long this schedule runs unattended."
echo
echo "To delete this schedule later (e.g. before a full §15 teardown, alongside the scale-up/down ones):"
echo "  aws scheduler delete-schedule --name fiduciary-demo-backup-daily"
