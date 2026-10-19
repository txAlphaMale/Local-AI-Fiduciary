#!/usr/bin/env bash
# up.sh -- resume the demo from Terraform: apply demo-env -> restore newest /data backup -> start app.
# Replaces hand-running AWS-DEMO-HOSTING-GUIDE.md §1-§11 + setup-waf.sh + schedule-backup.sh.
#
# Prereqs (one-time): infra/terraform/README.md steps 1-4 (bootstrap, backend.hcl, persistent
# stack, images in ECR, secrets set via set-secrets.sh).
#
# Usage:
#   export AWS_PROFILE=fiduciary-demo
#   ./scripts/aws-demo/up.sh                      # plan + confirm, restore newest backup, scale to 1
#   ./scripts/aws-demo/up.sh --stamp <STAMP>      # restore a specific backup prefix instead
#   ./scripts/aws-demo/up.sh --fresh              # skip restore; app seeds a brand-new demo dataset
#   ./scripts/aws-demo/up.sh -y                   # no interactive apply confirmation
#   ./scripts/aws-demo/up.sh --image-tag <t> --mcpo-tag <t>   # pin image tags
#
# Image tags: the ECR repos are IMMUTABLE and nothing pushes :latest any more. Unless pinned, each
# image resolves to the most recently pushed image in its repo that carries a non-"latest" tag.

cd "$(dirname "${BASH_SOURCE[0]}")"
source ./common.sh
TF_DIR="$(cd ../../infra/terraform/demo-env && pwd)"

STAMP=""; FRESH=0; AUTO=""; APP_TAG=""; MCPO_TAG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --stamp) STAMP="$2"; shift 2 ;;
    --fresh) FRESH=1; shift ;;
    -y)      AUTO="-auto-approve"; shift ;;
    --image-tag) APP_TAG="$2"; shift 2 ;;
    --mcpo-tag)  MCPO_TAG="$2"; shift 2 ;;
    *) die "unknown arg $1" ;;
  esac
done

command -v terraform >/dev/null || die "terraform not installed"

log "=== 1/4 preflight: secrets present (names only) ==="
for p in MCPO_API_KEY SESSION_SECRET ANTHROPIC_API_KEY DEMO_ADMIN_PASSWORD FINNHUB_API_KEY TAVILY_API_KEY; do
  n="$(aws ssm describe-parameters --parameter-filters "Key=Name,Values=/fiduciary-demo/$p" --query 'length(Parameters)' --output text)"
  [ "$n" = "1" ] || die "/fiduciary-demo/$p missing -- run ./set-secrets.sh --missing first (task would fail ResourceInitializationError)."
done

# newest_tag <repo>: a tag (not "latest") from the most recently pushed image that has one.
newest_tag() {
  aws ecr describe-images --repository-name "$1" --filter tagStatus=TAGGED \
      --query 'imageDetails[].[imagePushedAt,imageTags]' --output json \
    | python3 -c '
import json, sys
rows = sorted(json.load(sys.stdin), key=lambda r: r[0])
for pushed, tags in reversed(rows):
    good = sorted(t for t in (tags or []) if t != "latest")
    if good:
        print(good[0]); break'
}
[ -z "$APP_TAG" ]  && APP_TAG="$(newest_tag fiduciary/portfolio-api-demo)"
[ -z "$MCPO_TAG" ] && MCPO_TAG="$(newest_tag fiduciary/mcpo-demo)"
for pair in "fiduciary/portfolio-api-demo:$APP_TAG" "fiduciary/mcpo-demo:$MCPO_TAG"; do
  r="${pair%%:*}"; t="${pair#*:}"
  [ -n "$t" ] || die "$r has no image with a unique (non-latest) tag. Push one (guide §7), or retag the existing image: see infra/terraform/README.md \"Image tags\"."
  [ "$t" != "latest" ] || die "'latest' is not deployable -- the repos are immutable; use a unique tag."
  aws ecr describe-images --repository-name "$r" --image-ids imageTag="$t" >/dev/null 2>&1 \
    || die "$r:$t not found in ECR."
done
log "Images: portfolio-api-demo:$APP_TAG  mcpo-demo:$MCPO_TAG"

log "=== 2/4 terraform apply (service created at desired_count=0) ==="
terraform -chdir="$TF_DIR" init -input=false -backend-config=../backend.hcl >/dev/null
terraform -chdir="$TF_DIR" apply $AUTO -var "image_tag=$APP_TAG" -var "mcpo_image_tag=$MCPO_TAG"

log "=== 3/4 restore /data ==="
if [ "$FRESH" -eq 1 ]; then
  log "--fresh: skipping restore; demo_seed.py will seed a new dataset on first boot."
else
  if [ -z "$STAMP" ]; then
    # Newest prefix by embedded timestamp, whether manual (<STAMP>/) or daily (scheduled-<STAMP>/).
    STAMP="$(aws s3api list-objects-v2 --bucket "$BACKUP_BUCKET" --delimiter / \
              --query 'CommonPrefixes[].Prefix' --output text | tr '\t' '\n' | sed 's#/$##' \
              | awk '{k=$0; sub(/^scheduled-/,"",k); print k"\t"$0}' | sort | tail -1 | cut -f2)"
  fi
  if [ -z "$STAMP" ] || [ "$STAMP" = "None" ]; then
    warn "No backups found in s3://$BACKUP_BUCKET -- continuing with a fresh seed."
  else
    log "Restoring backup prefix: $STAMP"
    ./restore.sh "$STAMP"
  fi
fi

log "=== 4/4 start the app ==="
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --desired-count 1 >/dev/null
log "Waiting for the service to stabilize (a few minutes)..."
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"
log "Up: $(terraform -chdir="$TF_DIR" output -raw url)"
log "Note: outside 08:00-17:00 ET Mon-Fri the scale-down schedule will set it back to 0 at the next 17:00."
