#!/usr/bin/env bash
# down.sh -- pause the demo: verified backup of /data -> terraform destroy of demo-env.
# Replaces teardown.sh for Terraform-managed environments. The persistent stack (backup bucket,
# backup role, Route 53 zone, ECR repos) and the SSM secrets are untouched.
#
# Usage:
#   export AWS_PROFILE=fiduciary-demo
#   ./scripts/aws-demo/down.sh        # backup, then destroy plan + interactive confirm
#   ./scripts/aws-demo/down.sh -y     # no confirmation (backup must still verify)

cd "$(dirname "${BASH_SOURCE[0]}")"
source ./common.sh
TF_DIR="$(cd ../../infra/terraform/demo-env && pwd)"
AUTO=""; [ "${1:-}" = "-y" ] && AUTO="-auto-approve"

command -v terraform >/dev/null || die "terraform not installed"

log "=== 1/2 backup /data (refuses to continue unless verified) ==="
./backup.sh || die "Backup failed -- NOT destroying anything. EFS is the only copy of /data."

log "=== 2/2 terraform destroy demo-env ==="
terraform -chdir="$TF_DIR" init -input=false -backend-config=../backend.hcl >/dev/null
terraform -chdir="$TF_DIR" destroy $AUTO

log "Down. Last backup: $(cat /tmp/fiduciary-demo-last-backup-stamp.txt 2>/dev/null || echo unknown)"
log "Still billed (cents/mo): Route 53 zone, ECR images, S3 backups. SSM secrets are free."
