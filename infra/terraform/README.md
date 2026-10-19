# Fiduciary demo: Terraform

This replaces the copy-and-paste CLI runbook in `AWS-DEMO-HOSTING-GUIDE.md` §1–§11, §14a, §14c and
§14d, and the delete sequence in `teardown.sh`. The guide is still the reference for why each piece
exists. Terraform now owns how each piece is built.

## Layout

| Stack | State | Lifecycle | Contents |
|---|---|---|---|
| `bootstrap/` | local | once per account | S3 bucket for remote state (versioned, encrypted, TLS-only) |
| `persistent/` | S3 `persistent/` | never destroyed in normal use | access-log bucket, backup bucket, backup IAM role (trust only), Route 53 zone, both ECR repos |
| `demo-env/` | S3 `demo-env/` | `apply` on resume, `destroy` on pause | VPC/NAT/SGs, EFS, IAM, ACM, ALB, DNS record, WAF, ECS cluster/task defs/service, all 3 schedules |

**Wrappers.** These handle the steps Terraform is not suited to. They are in `scripts/aws-demo/`:

- `up.sh`: preflight, then `apply`, then restore the newest `/data` backup, then scale to 1.
- `down.sh`: run a verified backup, then `destroy`.
- `set-secrets.sh`: manage the SSM secret values.
- `export-live-config.sh`: take a read-only snapshot of the live environment.

`backup.sh` and `restore.sh` are unchanged. They find resources by name, and Terraform keeps every
name, so they keep working.

## Secrets: deliberately not in Terraform

Terraform state stores every attribute in plaintext. A managed `aws_ssm_parameter` reads its
decrypted value back into state on every refresh. To avoid that, the six `/fiduciary-demo/*`
SecureStrings are kept out of Terraform:

- `set-secrets.sh` writes them. Values are entered at a hidden prompt.
- The task definition references them only by ARN.
- They persist across pause and resume. Standard-tier parameters are free.
- For a clean account, run `set-secrets.sh --delete-all`.

## Deliberate changes vs. the CLI build

| Change | Why |
|---|---|
| ECR repos moved to `persistent/` | Resuming no longer requires rebuilding and pushing both images |
| SSM secrets kept across pauses | No re-entering keys on every resume, and values stay out of state |
| Backup lifecycle: 30-day expiry applies to `scheduled-*` only | The old rule expired **all** objects, including the pre-teardown backup. A pause longer than 30 days would have deleted the only restore point |
| Daily backup schedule `Command` fixed | `["sh","-c",cmd]` on top of `entryPoint ["sh","-c"]` ran `sh -c sh …`, which is a silent no-op. Check `s3-backup-prefixes.json` in the snapshot: if it has no `scheduled-*` prefixes, the daily backup never ran |
| Explicit SG egress; ALB egress limited to Fargate:8097 | Terraform drops AWS's implicit allow-all egress, so egress has to be stated. It was tightened where possible |
| Default SG emptied; public subnets don't auto-assign public IPs | CIS hygiene. Nothing used either |
| ALB `drop_invalid_header_fields = true` | The app trusts `X-Forwarded-For` |
| Listener TLS policy `ELBSecurityPolicy-TLS13-1-2-2021-06` | Explicit TLS 1.2+ only. Check the snapshot for what the CLI build actually got |
| Log group created by Terraform with 30-day retention | Was auto-created with no expiry |
| Scheduler role trust has an `aws:SourceAccount` condition | Guards against the confused-deputy problem |
| ECR repos are IMMUTABLE; deploys use unique tags (CKV_AWS_51) | A tag always names exactly one image, so deploys are traceable and rollbacks are exact. See "Image tags" |
| WAF rule 4: `AWSManagedRulesKnownBadInputsRuleSet` (CKV_AWS_192) | Log4Shell and other known-bad request patterns. 227/1500 WCU, +$1/mo |
| ECS Container Insights on, behind a variable (CKV_AWS_65) | Task/service CPU, memory and network metrics. See "Container Insights cost" |
| WAF IP set is owned by Terraform | **A CIDR added by hand with `update-ip-set` is reverted by the next apply.** Add CIDRs to `demo-env/waf.auto.tfvars.json` instead |
| `teardown.sh`: deletes the backup-daily schedule and the `run-backup-task` policy; adds `KEEP_ECR` / `KEEP_SSM` | Previously the schedule kept firing after teardown, and the scheduler role silently failed to delete. Both would have blocked `terraform apply` |

## One-time migration (environment currently running)

Run these with `export AWS_PROFILE=fiduciary-demo`. Terraform must be version 1.10 or later.

1. **Snapshot the live config (read-only):** `./scripts/aws-demo/export-live-config.sh`
   - Writes `.live-snapshot/<stamp>/` (gitignored) and `demo-env/waf.auto.tfvars.json`, which holds the
     live blocklist and geo-allow list.
   - Reconcile anything unexpected before going further. Things worth checking: `alb-listeners.json`
     (TLS policy), `alb-attributes.json` (idle timeout), and any hand-made changes.
2. **Bootstrap the state bucket:**
   - `terraform -chdir=infra/terraform/bootstrap init && terraform -chdir=infra/terraform/bootstrap apply`
   - Then: `terraform -chdir=infra/terraform/bootstrap output -raw backend_hcl > infra/terraform/backend.hcl`
3. **Adopt the persistent resources while the environment is still up:**
   - `cd infra/terraform/persistent && terraform init -backend-config=../backend.hcl`
   - `terraform plan -var import_zone_id=<ZONE_ID from route53-zone.json>`
   - Expected plan: imports, plus a few adds/changes as described in `imports.tf`. **Zero destroys.**
   - `terraform apply -var import_zone_id=...`, then delete `imports.tf`.
4. **Tear down the CLI-built environment:** `KEEP_ECR=1 KEEP_SSM=1 ./scripts/aws-demo/teardown.sh`
   (it backs up first).
5. **Rebuild from Terraform:** `./scripts/aws-demo/up.sh`. It restores the backup step 4 just took.
   Both repos must first hold an image with a unique (non-`latest`) tag. See "Image tags" below.
6. **Verify:**
   - The site loads, demo data is intact, and the WAF has 4 rules with the full blocklist.
   - `aws scheduler get-schedule --name fiduciary-demo-backup-daily`.
   - After 08:00 UTC, a `scheduled-*` prefix appears in the backup bucket.

Expect roughly 15–25 minutes of downtime, mostly NAT gateway, ACM and EFS creation.

## Day-to-day

```bash
./scripts/aws-demo/down.sh          # pause between interview cycles
./scripts/aws-demo/up.sh            # resume (newest backup restored)
# drift check (should be "No changes"); image tags are required variables, so pass the running ones:
D=infra/terraform/demo-env
terraform -chdir=$D plan -var image_tag=$(terraform -chdir=$D output -raw image_tag) \
                         -var mcpo_image_tag=$(terraform -chdir=$D output -raw mcpo_image_tag)
```

Deploy a new image: build, push a **unique** tag (guide §7 or `build-push-ecr-demo.yml`), then:

```bash
terraform -chdir=$D apply -var image_tag=<new-tag> -var mcpo_image_tag=$(terraform -chdir=$D output -raw mcpo_image_tag)
```

This registers a new task-definition revision and ECS rolls the service onto it. Roll back the
same way with the previous tag. `aws ecs update-service --force-new-deployment` still works, but it
is **no longer a deploy**: the task definition pins an immutable tag, so it just restarts the same
image. Use it only to restart (for example, to pick up a rotated SSM secret).

## Image tags (ECR is IMMUTABLE)

- Both repos use `image_tag_mutability = "IMMUTABLE"`. A tag can never be re-pushed, so nothing
  pushes `:latest` any more. Use a git short SHA (CI default) or a UTC timestamp.
- `image_tag` (portfolio-api-demo) and `mcpo_image_tag` (mcpo-demo) are separate, required
  variables with no default, and the value `latest` is rejected. `up.sh` resolves each one to the
  newest pushed image that carries a non-`latest` tag. Pin them with `--image-tag` / `--mcpo-tag`.
- **Existing `latest` images.** Switching an existing repo to IMMUTABLE is an in-place update, and
  existing images are kept. The current `:latest` simply becomes frozen: it still pulls, but can't
  be pushed again, and it is not deployable through Terraform (validation rejects it). An image that
  is tagged **only** `latest` must be given a unique tag before `up.sh` can use it. Adding a new tag
  to an existing image is allowed under IMMUTABLE. This is likely the case for `mcpo-demo`, which
  was only ever pushed manually as `:latest`:

  ```bash
  R=fiduciary/mcpo-demo; NEW=mcpo-20260726-legacy
  # two calls on purpose: the manifest is multi-line JSON, so it can't share a `read` line
  MT=$(aws ecr batch-get-image --repository-name $R --image-ids imageTag=latest \
      --query 'images[0].imageManifestMediaType' --output text)
  MANIFEST=$(aws ecr batch-get-image --repository-name $R --image-ids imageTag=latest \
      --query 'images[0].imageManifest' --output text)
  aws ecr put-image --repository-name $R --image-tag $NEW \
      --image-manifest "$MANIFEST" --image-manifest-media-type "$MT"
  ```

  `portfolio-api-demo` images pushed by CI already carry a short-SHA tag alongside `latest`, so
  nothing is needed there unless it was only pushed manually.
- The ECR lifecycle policy (keep the 10 most recent) still applies, so the frozen `latest` image
  expires naturally once 10 newer images exist.

## Container Insights cost

`container_insights_enabled` (default `true`) turns on ECS Container Insights for the cluster
(CKV_AWS_65). It bills as CloudWatch custom metrics plus performance-log ingestion, only while the
cluster exists. For this one-service, one-task cluster I'd estimate a few dollars a month. That is
an estimate, not a quote: confirm against CloudWatch pricing. Set it to `false` to remove the cost;
the cluster setting changes in place.


## Logging

All access logs go to one bucket in `persistent/`, `fiduciary-demo-logs-<account>`, so they survive
`down.sh`. It is versioned, SSE-S3 (the only encryption both log types support), blocks all public
access, denies non-TLS requests, and accepts writes only from the two AWS log-delivery services, each
restricted to its own prefix and to this account.

| Log | Source | Lands under | Delay |
|---|---|---|---|
| ALB access logs | `aws_lb.main` (demo-env) | `alb/AWSLogs/<account>/elasticloadbalancing/us-east-1/YYYY/MM/DD/` | Every 5 minutes, while the ALB exists |
| S3 server access logs | tfstate bucket | `s3/tfstate/<account>/us-east-1/<bucket>/YYYY/MM/DD/` | Best effort, usually within a few hours |
| S3 server access logs | backups bucket | `s3/backups/<account>/us-east-1/<bucket>/YYYY/MM/DD/` | Best effort, usually within a few hours |

- **Retention:** `log_retention_days` in `persistent/`, default 90. The noncurrent copy that
  versioning keeps is removed 7 days later. Incomplete uploads are aborted after 7 days.
- **Not logged:** the log bucket itself. This is deliberate, to avoid a delivery loop
  (CKV_AWS_18 is skipped for it, with a justification). WAF logs and VPC flow logs are a later step.
- **Ordering:** `bootstrap`, then `persistent`, then `demo-env`.
  - The tfstate bucket's logging is configured from `persistent/`, so `bootstrap` is unchanged.
    Accesses to the tfstate bucket before the first `persistent` apply are not logged.
  - `demo-env` reads the log bucket with a data source, so its plan fails early if `persistent`
    hasn't been applied.
  - The ALB writes a test file at creation and fails with Access Denied if the bucket policy is missing.
- **Check that logs are arriving:**

  ```bash
  B=fiduciary-demo-logs-$(aws sts get-caller-identity --query Account --output text)
  aws s3 ls s3://$B/alb/ --recursive | tail -5   # ELBAccessLogTestFile right after apply; .log.gz within ~5 min of traffic
  aws s3 ls s3://$B/s3/ --recursive | tail -5    # S3 server access logs: allow a few hours
  aws elbv2 describe-load-balancer-attributes --load-balancer-arn \
    $(aws elbv2 describe-load-balancers --names fiduciary-demo-alb --query 'LoadBalancers[0].LoadBalancerArn' --output text) \
    --query "Attributes[?starts_with(Key,'access_logs')]"
  ```

- **Cost:** neither the ALB nor S3 charges for delivering these logs. You pay only S3 storage for
  the log files (S3 Standard, about $0.023/GB-month in us-east-1), plus your own reads. For a
  low-traffic demo that is typically cents a month. The 90-day expiry caps the growth.

## Not managed (yet)

- AWS Organizations member account and CLI profile (guide §0).
- The budget alarm.
- The GitHub OIDC push role. Its shape is captured in the snapshot (`iam-role-github*`) and can be
  imported into `persistent/` later.
- The `mcpo-demo` image build. Still manual (guide §7).
- WAF logging and VPC flow logs. Neither was configured in the CLI build; both are candidates for
  the security-portfolio track.

## Suggested CI follow-up

Add a workflow on PRs that touch `infra/**`: `terraform fmt -check`, `terraform validate`, Checkov
or Trivy IaC scan, and `terraform plan` through the existing GitHub OIDC role. No auto-apply.
