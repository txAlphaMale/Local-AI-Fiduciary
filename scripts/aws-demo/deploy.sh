#!/usr/bin/env bash
# Deploy a new portfolio-api image to the public AWS demo, whatever state the demo is in.
#
#   source ~/fiduciary-demo-vars.sh     # sets ACCOUNT_ID and REGION (and AWS credentials/profile)
#   ./scripts/aws-demo/deploy.sh --check    # read-only: say what state the demo is in and what a deploy would do
#   ./scripts/aws-demo/deploy.sh            # preflight, then build, push and roll, asking before every change
#
# Written 2026-11-08 after the old copy-paste runbook carried on past a failed `terraform init`. This script runs in its OWN
# process with `set -euo pipefail`: the first failing command stops it (never your terminal), and nothing that changes AWS runs
# until a preflight has printed a verdict and you have typed y.
#
# Two possible states (decided by the preflight, not by you):
#   LEGACY    the demo was built by the original CLI guide: ECR repo is MUTABLE, the task definition points at ":latest",
#             no Terraform state bucket. Deploy = push a short-SHA tag AND :latest, then force a new ECS deployment.
#   MIGRATED  infra/terraform/README.md "One-time migration" was completed: ECR is IMMUTABLE, state lives in S3. Deploy = push
#             a unique tag, then `terraform apply -var image_tag=<tag>`; a paused demo is resumed with up.sh.
# Anything in between is reported as PARTIAL and the script stops without touching AWS.
set -euo pipefail

CHECK_ONLY=0
case "${1:-}" in
  "") ;;
  --check) CHECK_ONLY=1 ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

die()     { printf 'STOP: %s\n' "$*" >&2; exit 1; }
say()     { printf '%s\n' "$*"; }
confirm() { local a; read -r -p "$1 [y/N] " a; [ "$a" = y ]; }

: "${ACCOUNT_ID:?ACCOUNT_ID is not set: run  source ~/fiduciary-demo-vars.sh  first}"
: "${REGION:?REGION is not set: run  source ~/fiduciary-demo-vars.sh  first}"
CLUSTER=fiduciary-demo
SERVICE=portfolio-api
ECR_NAME=fiduciary/portfolio-api-demo
STATE_BUCKET="fiduciary-demo-tfstate-$ACCOUNT_ID"
REGISTRY="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"
REPO="$REGISTRY/$ECR_NAME"
TFDIR=infra/terraform/demo-env
BACKEND=infra/terraform/backend.hcl

cd "$(git rev-parse --show-toplevel)"

# ---- 1. the code to deploy: committed, clean, on main, identical to origin/main ----
branch=$(git rev-parse --abbrev-ref HEAD)
[ "$branch" = main ] || die "you are on '$branch', not main"
[ -z "$(git status --porcelain)" ] || die "the working tree has uncommitted changes"
git fetch -q origin main || die "could not reach origin"
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || die "local main differs from origin/main (run: git pull --ff-only origin main)"
TAG=$(git rev-parse --short HEAD)
say "code: main @ $TAG"

# ---- 2. preflight: what state is the demo in? (read-only) ----
say; say "== preflight (read-only) =="
mut=$(aws ecr describe-repositories --repository-names "$ECR_NAME" --region "$REGION" \
        --query 'repositories[0].imageTagMutability' --output text) \
  || die "cannot read the ECR repository $ECR_NAME (credentials? profile? region?)"
have_backend=no; [ -f "$BACKEND" ] && have_backend=yes
bucket=no; aws s3api head-bucket --bucket "$STATE_BUCKET" --region "$REGION" >/dev/null 2>&1 && bucket=yes
state_objs=no
if [ "$bucket" = yes ]; then
  # the ENVIRONMENT stack's state is what a deploy needs; persistent/ alone (migration half-way) is not enough
  # capture first, then match: `aws ... | grep -q` makes aws die with SIGPIPE when grep stops at the first hit, and under
  # `set -o pipefail` that read as "no match" (2026-11-09: reported demo-env state missing on a migrated demo)
  state_listing=$(aws s3 ls "s3://$STATE_BUCKET/" --recursive --region "$REGION" 2>/dev/null || true)
  case "$state_listing" in *demo-env/*) state_objs=yes ;; esac
fi
tfver=$(terraform version 2>/dev/null | head -1 || true)
svc_active=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --region "$REGION" \
        --query 'length(services[?status==`ACTIVE`])' --output text 2>/dev/null || echo 0)
svc_counts=none
if [ "$svc_active" = 1 ]; then
  svc_counts=$(aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" --region "$REGION" \
        --query 'services[0].[desiredCount,runningCount]' --output text | tr '\t' '/')
fi
say "ECR tag mutability     : $mut"
say "state bucket exists    : $bucket"
say "demo-env state present  : $state_objs"
say "backend.hcl present    : $have_backend"
say "terraform              : ${tfver:-not found}"
say "ECS service ACTIVE     : $svc_active (desired/running: $svc_counts)"

if [ "$mut" = MUTABLE ] && [ "$bucket" = no ] && [ "$have_backend" = no ]; then
  VERDICT=LEGACY
elif [ "$mut" = IMMUTABLE ] && [ "$bucket" = yes ] && [ "$state_objs" = yes ] && [ "$have_backend" = yes ]; then
  VERDICT=MIGRATED
else
  VERDICT=PARTIAL
fi
say; say "VERDICT: $VERDICT"
case "$VERDICT" in
  LEGACY)   say "  The demo is the CLI-built environment (nothing was migrated to Terraform). The Terraform stacks in the repo describe the TARGET state, not what is deployed today."
            say "  A deploy pushes :$TAG and :latest, then forces a new ECS deployment." ;;
  MIGRATED) say "  The demo is Terraform-managed. A deploy pushes the unique tag :$TAG, then applies it through Terraform." ;;
  PARTIAL)  say "  Mixed signals; this script will not guess. Compare the facts above with infra/terraform/README.md 'One-time migration' (steps 2 to 5)."
            say "  Notes: backend.hcl can be rebuilt from the bucket name (bootstrap state is LOCAL: do not re-run 'bootstrap apply' blindly)."
            say "         regenerate:  terraform -chdir=infra/terraform/bootstrap output -raw backend_hcl > $BACKEND"
            say "         or copy infra/terraform/backend.hcl.example and set bucket = \"$STATE_BUCKET\"."
            die "state is PARTIAL; nothing was changed" ;;
esac
if [ "$VERDICT" = MIGRATED ]; then
  command -v terraform >/dev/null || die "terraform is not installed"
  case "$tfver" in *"v1."[1-9][0-9]*|*"v1.1"[0-9]*) : ;; *) say "  (could not confirm Terraform >= 1.10 from: $tfver)";; esac
fi
if [ "$CHECK_ONLY" = 1 ]; then say; say "--check: nothing was changed."; exit 0; fi

# ---- 3. build (and optional scan) ----
say; say "== build =="
confirm "Build $REPO:$TAG (docker build --pull -f Dockerfile.demo) and push it?" || die "cancelled; nothing was changed"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null \
  || die "ECR login failed"
tags=(-t "$REPO:$TAG")
[ "$VERDICT" = LEGACY ] && tags+=(-t "$REPO:latest")     # the CLI-built task definition points at :latest
docker build --pull -f Dockerfile.demo "${tags[@]}" .
if [ "${SKIP_SCAN:-0}" != 1 ]; then
  say "scanning for fixable CRITICAL/HIGH findings (same gate as CI); set SKIP_SCAN=1 to skip"
  if ! docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:0.69.3 image \
        --severity CRITICAL,HIGH --ignore-unfixed --exit-code 1 "$REPO:$TAG"; then
    confirm "The scan found fixable CRITICAL/HIGH issues. Push anyway?" || die "stopped after the scan; nothing was pushed"
  fi
fi

# ---- 4. push ----
if [ "$VERDICT" = MIGRATED ] && aws ecr describe-images --repository-name "$ECR_NAME" --image-ids imageTag="$TAG" --region "$REGION" >/dev/null 2>&1; then
  say "tag $TAG is already in ECR (immutable): not pushing again"
else
  docker push "$REPO:$TAG"
fi
[ "$VERDICT" = LEGACY ] && docker push "$REPO:latest"

# ---- 5. roll the service ----
say; say "== deploy ($VERDICT) =="
if [ "$VERDICT" = LEGACY ]; then
  if [ "$svc_active" != 1 ]; then
    say "There is no ACTIVE ECS service (the demo was torn down). The image is pushed (:$TAG and :latest)."
    say "To bring the demo back, re-provision it from AWS-DEMO-HOSTING-GUIDE.md (or run the Terraform migration). Nothing else was changed."
    exit 0
  fi
  desired=${svc_counts%%/*}
  if [ "$desired" = 0 ]; then
    say "The service is scaled to zero. The image is pushed; the next scale-up (schedule, or: aws ecs update-service --cluster $CLUSTER --service $SERVICE --desired-count 1) will start it."
    exit 0
  fi
  confirm "Force a new ECS deployment of $SERVICE (restarts the task onto the new :latest)?" || { say "not rolled; the image is pushed"; exit 0; }
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" --force-new-deployment --region "$REGION" >/dev/null
  say "waiting for the service to settle..."
  aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE" --region "$REGION"
  say "done: the demo runs the new image (code $TAG). Open the demo URL and check it."
else
  if [ "$svc_active" != 1 ]; then
    confirm "The demo is paused (no ACTIVE service). Resume it with ./scripts/aws-demo/up.sh --image-tag $TAG ?" || { say "not resumed; the image is pushed"; exit 0; }
    ./scripts/aws-demo/up.sh --image-tag "$TAG"
    exit 0
  fi
  terraform -chdir="$TFDIR" init -input=false -backend-config="../backend.hcl" >/dev/null || die "terraform init failed"
  PREV=$(terraform -chdir="$TFDIR" output -raw image_tag) || die "cannot read the current image_tag from Terraform state"
  MCPO=$(terraform -chdir="$TFDIR" output -raw mcpo_image_tag) || die "cannot read the current mcpo_image_tag"
  say "current image tag (for rollback): $PREV"
  set +e
  terraform -chdir="$TFDIR" plan -input=false -out=tfplan -detailed-exitcode -var "image_tag=$TAG" -var "mcpo_image_tag=$MCPO"
  rc=$?
  set -e
  [ "$rc" = 1 ] && die "terraform plan failed; nothing was changed"
  [ "$rc" = 0 ] && { say "terraform reports no changes (tag $TAG is already deployed)."; exit 0; }
  confirm "Apply exactly this plan?" || die "not applied; nothing was changed"
  terraform -chdir="$TFDIR" apply -input=false tfplan
  aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE" --region "$REGION"
  say "done: the demo runs code $TAG. Roll back by re-running with the previous code, or: terraform apply -var image_tag=$PREV"
fi
