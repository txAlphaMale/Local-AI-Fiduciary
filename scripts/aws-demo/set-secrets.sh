#!/usr/bin/env bash
# set-secrets.sh -- create/update the /fiduciary-demo/* SSM SecureString parameters OUTSIDE
# Terraform, so secret values never land in Terraform state (refreshing a Terraform-managed
# aws_ssm_parameter reads the decrypted value back into state). See infra/terraform/README.md.
#
# Values are read from a hidden prompt (never argv, never shell history, no $/!/` expansion --
# avoids the DEMO_ADMIN_PASSWORD mangling hit live on 2026-07-26, guide §6).
#
# These parameters are NOT deleted by `terraform destroy` of demo-env -- they persist (free,
# Standard tier) so a resume doesn't require re-entering every key.
#
# Usage:
#   export AWS_PROFILE=fiduciary-demo
#   ./scripts/aws-demo/set-secrets.sh                 # status: which exist, last-modified (no values)
#   ./scripts/aws-demo/set-secrets.sh FINNHUB_API_KEY # prompt for and set one
#   ./scripts/aws-demo/set-secrets.sh --missing       # prompt only for the ones not yet set
#   ./scripts/aws-demo/set-secrets.sh --generate      # (re)generate MCPO_API_KEY + SESSION_SECRET if missing
#   ./scripts/aws-demo/set-secrets.sh --delete-all    # clean-account teardown (asks to confirm)

cd "$(dirname "${BASH_SOURCE[0]}")"
source ./common.sh

ALL="MCPO_API_KEY SESSION_SECRET ANTHROPIC_API_KEY DEMO_ADMIN_PASSWORD FINNHUB_API_KEY TAVILY_API_KEY"
exists() { aws ssm describe-parameters --parameter-filters "Key=Name,Values=/fiduciary-demo/$1" \
             --query 'length(Parameters)' --output text 2>/dev/null | grep -q '^1$'; }
put()    { printf '%s' "$2" | aws ssm put-parameter --name "/fiduciary-demo/$1" --type SecureString \
             --overwrite --value "file:///dev/stdin" >/dev/null && log "set /fiduciary-demo/$1"; }
prompt() { local v; read -r -s -p "Value for $1: " v; echo; [ -z "$v" ] && die "empty value, not set"; put "$1" "$v"; }

case "${1:-}" in
  "")
    aws ssm describe-parameters --parameter-filters Key=Name,Option=BeginsWith,Values=/fiduciary-demo/ \
      --query 'Parameters[].[Name,Type,Version,LastModifiedDate]' --output table
    for p in $ALL; do exists "$p" || warn "missing: /fiduciary-demo/$p"; done ;;
  --missing)
    for p in $ALL; do
      exists "$p" && continue
      case "$p" in
        MCPO_API_KEY)   put "$p" "$(openssl rand -hex 24)" ;;
        SESSION_SECRET) put "$p" "$(openssl rand -hex 32)" ;;
        *)              prompt "$p" ;;
      esac
    done ;;
  --generate)
    exists MCPO_API_KEY   || put MCPO_API_KEY   "$(openssl rand -hex 24)"
    exists SESSION_SECRET || put SESSION_SECRET "$(openssl rand -hex 32)" ;;
  --delete-all)
    read -r -p "Delete ALL /fiduciary-demo/* parameters? Type 'delete': " c
    [ "$c" = "delete" ] || { log "Aborted."; exit 1; }
    for p in $ALL; do aws ssm delete-parameter --name "/fiduciary-demo/$p" 2>/dev/null && log "deleted $p"; done ;;
  *)
    echo " $ALL " | grep -q " $1 " || die "unknown parameter '$1' (expected one of: $ALL)"
    prompt "$1" ;;
esac
