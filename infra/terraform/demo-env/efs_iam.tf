# Guide §3 (EFS) + §5 (IAM) + §14a/§14b/§14c role policies.

resource "aws_efs_file_system" "data" {
  encrypted = true
  tags      = { Name = "${local.name}-data" } # common.sh fs_id() looks this up by Name
}

resource "aws_efs_mount_target" "data" {
  for_each        = aws_subnet.private
  file_system_id  = aws_efs_file_system.data.id
  subnet_id       = each.value.id
  security_groups = [aws_security_group.efs.id]
}

resource "aws_efs_access_point" "data" {
  file_system_id = aws_efs_file_system.data.id
  posix_user {
    uid = 0
    gid = 0
  }
  root_directory {
    path = "/data"
    creation_info {
      owner_uid   = 0
      owner_gid   = 0
      permissions = "755"
    }
  }
  tags = { Name = "${local.name}-data-ap" }
}

# ------------------------------------------------------------------------------------ IAM

locals {
  ecs_tasks_trust = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role" "exec" {
  name               = "${local.name}-exec-role"
  assume_role_policy = local.ecs_tasks_trust
}

resource "aws_iam_role_policy_attachment" "exec_managed" {
  role       = aws_iam_role.exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "exec_ssm_read" {
  name = "ssm-read"
  role = aws_iam_role.exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "ssm:GetParameters"
      Resource = "${local.ssm_prefix}/*"
    }]
  })
}

# Kept even though Terraform now pre-creates the log group: restore.sh / backup.sh still register
# their own task-definition revisions with awslogs-create-group=true.
resource "aws_iam_role_policy" "exec_logs" {
  name = "cloudwatch-logs"
  role = aws_iam_role.exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "arn:aws:logs:${var.region}:${local.account_id}:log-group:${local.log_group}:*"
    }]
  })
}

resource "aws_iam_role" "task" {
  name               = "${local.name}-task-role"
  assume_role_policy = local.ecs_tasks_trust
}

resource "aws_iam_role_policy" "task_efs" {
  name = "efs-access"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
      Resource = aws_efs_file_system.data.arn
    }]
  })
}

# Policy on the PERSISTENT backup role, scoped to THIS environment's filesystem.
resource "aws_iam_role_policy" "backup_access" {
  name = "backup-access"
  role = data.aws_iam_role.backup.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientRead", "elasticfilesystem:ClientWrite"]
        Resource = aws_efs_file_system.data.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
        Resource = ["arn:aws:s3:::${local.backup_bucket}", "arn:aws:s3:::${local.backup_bucket}/*"]
      }
    ]
  })
}

resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      # Confused-deputy guard (not present in the CLI build): only schedules in this account.
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_scale" {
  name = "scale-portfolio-api"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "ecs:UpdateService"
      Resource = "arn:aws:ecs:${var.region}:${local.account_id}:service/${aws_ecs_cluster.main.name}/portfolio-api"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler_backup" {
  name = "run-backup-task"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = "ecs:RunTask"
        Resource  = "arn:aws:ecs:${var.region}:${local.account_id}:task-definition/fiduciary-demo-backup:*"
        Condition = { ArnLike = { "ecs:cluster" = aws_ecs_cluster.main.arn } }
      },
      {
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = [data.aws_iam_role.backup.arn, aws_iam_role.exec.arn]
      }
    ]
  })
}
