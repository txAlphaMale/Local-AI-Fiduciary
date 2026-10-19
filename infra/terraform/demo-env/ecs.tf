# Guide §8 (cluster + task def; replaces portfolio-api-demo-task.json), §10 (service),
# §14a (scale schedules), §14b (backup/restore task defs), §14c (daily backup schedule).

resource "aws_cloudwatch_log_group" "ecs" {
  name              = local.log_group
  retention_in_days = var.log_retention_days # CLI build relied on awslogs-create-group => never expires
}

resource "aws_ecs_cluster" "main" {
  name = local.name

  # CKV_AWS_65. Adds CloudWatch metric/log charges -- see README "Container Insights cost".
  setting {
    name  = "containerInsights"
    value = var.container_insights_enabled ? "enabled" : "disabled"
  }
}

locals {
  awslogs = { for p in ["portfolio-api", "mcpo", "backup", "restore"] : p => {
    logDriver = "awslogs"
    options = {
      awslogs-group         = aws_cloudwatch_log_group.ecs.name
      awslogs-region        = var.region
      awslogs-stream-prefix = p
    }
  } }

  app_env = {
    MCPO_BASE_URL              = "http://localhost:8000"
    MONARCH_ROUTE              = "monarch"
    CACHE_TTL                  = "600"
    TZ                         = "America/Chicago"
    INTERNAL_SCHEDULER         = "0"
    ANALYSIS_FILE              = "/data/analysis.json"
    SESSION_MAX_AGE_DAYS       = "1"
    SESSION_HTTPS_ONLY         = "1"
    TRUST_PROXY_HEADERS        = "1" # safe ONLY because of the fargate_from_alb SG rule (network.tf)
    PORTFOLIO_PROVIDER         = "csv"
    LLM_API_DIALECT            = "anthropic"
    ANTHROPIC_MODEL            = var.anthropic_model
    ANTHROPIC_STRATEGIST_MODEL = var.anthropic_strategist_model
    LLM_TIMEOUT                = "1200"
    SEC_USER_AGENT             = var.sec_user_agent
    DEMO_MODE                  = "1"
    DEMO_ADMIN_USERNAME        = "demo"
    DEMO_RESET_INTERVAL_S      = tostring(var.demo_reset_interval_s)
    COMPLIANCE_DIR             = "/compliance-baked"
  }

  # env-var name => SSM parameter name. Values are set OUT OF BAND (scripts/aws-demo/set-secrets.sh)
  # and never enter Terraform state -- see infra/terraform/README.md "Secrets".
  app_secrets = {
    MCPO_API_KEY        = "MCPO_API_KEY"
    SESSION_SECRET      = "SESSION_SECRET"
    LLM_API_KEY         = "ANTHROPIC_API_KEY"
    DEMO_ADMIN_PASSWORD = "DEMO_ADMIN_PASSWORD"
    FINNHUB_API_KEY     = "FINNHUB_API_KEY"
  }
  mcpo_secrets = {
    TAVILY_API_KEY = "TAVILY_API_KEY"
    MCPO_API_KEY   = "MCPO_API_KEY"
  }
}

resource "aws_ecs_task_definition" "app" {
  family                   = "${local.name}-portfolio-api"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.exec.arn
  task_role_arn            = aws_iam_role.task.arn

  volume {
    name = "data"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.data.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.data.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([
    {
      name         = "portfolio-api"
      image        = "${data.aws_ecr_repository.portfolio_api.repository_url}:${var.image_tag}"
      essential    = true
      portMappings = [{ containerPort = 8097, protocol = "tcp" }]
      mountPoints  = [{ sourceVolume = "data", containerPath = "/data" }]
      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:8097/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 30
      }
      environment      = [for k, v in local.app_env : { name = k, value = v }]
      secrets          = [for k, p in local.app_secrets : { name = k, valueFrom = "${local.ssm_prefix}/${p}" }]
      logConfiguration = local.awslogs["portfolio-api"]
    },
    {
      name             = "mcpo"
      image            = "${data.aws_ecr_repository.mcpo.repository_url}:${var.mcpo_image_tag}"
      essential        = true
      portMappings     = [{ containerPort = 8000, protocol = "tcp" }]
      secrets          = [for k, p in local.mcpo_secrets : { name = k, valueFrom = "${local.ssm_prefix}/${p}" }]
      logConfiguration = local.awslogs["mcpo"]
    }
  ])
}

# backup / restore one-off task defs (same shape backup.sh / restore.sh register; those scripts
# still re-register their own revision on each run, which is harmless).
resource "aws_ecs_task_definition" "data_job" {
  for_each                 = { backup = true, restore = false } # value = EFS mount read-only?
  family                   = "${local.name}-${each.key}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.exec.arn
  task_role_arn            = data.aws_iam_role.backup.arn

  volume {
    name = "data"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.data.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.data.id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name             = each.key
    image            = "public.ecr.aws/aws-cli/aws-cli:latest"
    essential        = true
    mountPoints      = [{ sourceVolume = "data", containerPath = "/data", readOnly = each.value }]
    entryPoint       = ["sh", "-c"]
    logConfiguration = local.awslogs[each.key]
  }])
}

resource "aws_ecs_service" "app" {
  name            = "portfolio-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.initial_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = [for s in aws_subnet.private : s.id]
    security_groups  = [aws_security_group.fargate.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "portfolio-api"
    container_port   = 8097
  }

  depends_on = [aws_lb_listener.https, aws_efs_mount_target.data, aws_iam_role_policy.exec_ssm_read]

  # The scale schedules and manual `update-service --desired-count` own the count after creation.
  lifecycle { ignore_changes = [desired_count] }
}

# ------------------------------------------------------------------- EventBridge Scheduler

locals {
  scale_schedules = {
    "scale-up"   = { cron = "cron(0 8 ? * MON-FRI *)", count = 1 }
    "scale-down" = { cron = "cron(0 17 ? * MON-FRI *)", count = 0 }
  }
}

resource "aws_scheduler_schedule" "scale" {
  for_each                     = local.scale_schedules
  name                         = "${local.name}-${each.key}"
  schedule_expression          = each.value.cron
  schedule_expression_timezone = "America/New_York" # handles EST/EDT automatically
  state                        = var.business_hours_schedules_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window { mode = "OFF" }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ecs:updateService"
    role_arn = aws_iam_role.scheduler.arn
    input = jsonencode({
      Cluster      = aws_ecs_cluster.main.name
      Service      = aws_ecs_service.app.name
      DesiredCount = each.value.count
    })
  }
}

# FIXED vs schedule-backup.sh: that script overrides Command with ["sh","-c",<cmd>], but the task
# def's entryPoint is already ["sh","-c"]. ECS appends Command to entryPoint, so the container runs
# `sh -c sh -c "<cmd>"` -- the outer shell executes the bare word "sh" (which exits immediately on
# a closed stdin) and the sync never runs, while the task still exits 0. backup.sh passes a single
# string, which is correct; this now does the same. Check the snapshot's s3-backup-prefixes.json for
# any "scheduled-*" prefixes -- if there are none, the daily backup has never actually worked.
resource "aws_scheduler_schedule" "backup_daily" {
  name                         = "${local.name}-backup-daily"
  schedule_expression          = "cron(0 8 * * ? *)" # 08:00 UTC, outside the 8-5 Eastern window
  schedule_expression_timezone = "UTC"
  state                        = var.daily_backup_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window { mode = "OFF" }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ecs:runTask"
    role_arn = aws_iam_role.scheduler.arn
    input = jsonencode({
      Cluster        = aws_ecs_cluster.main.name
      TaskDefinition = aws_ecs_task_definition.data_job["backup"].family
      LaunchType     = "FARGATE"
      NetworkConfiguration = {
        AwsvpcConfiguration = {
          Subnets        = [for s in aws_subnet.private : s.id]
          SecurityGroups = [aws_security_group.fargate.id]
          AssignPublicIp = "DISABLED"
        }
      }
      Overrides = {
        ContainerOverrides = [{
          Name    = "backup"
          Command = ["aws s3 sync /data s3://${local.backup_bucket}/scheduled-$(date -u +%Y%m%dT%H%M%SZ)/ && echo BACKUP_OK"]
        }]
      }
    })
  }

  depends_on = [aws_iam_role_policy.scheduler_backup, aws_iam_role_policy.backup_access]
}
