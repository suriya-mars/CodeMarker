data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.private.id
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

  # IMDSv2 only, one network hop: the host can read its role credentials, but the
  # containers (one hop further away) cannot. A parser exploit in the api container
  # must not be able to borrow the instance's AWS permissions.
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    http_endpoint               = "enabled"
  }

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region         = var.region
    secret_id      = aws_secretsmanager_secret.app.arn
    alb_arn        = aws_lb.main.arn
    public_url     = "https://${var.domain_name}"
    backup_bucket  = aws_s3_bucket.backups.bucket
    log_group      = aws_cloudwatch_log_group.app.name
    data_volume_id = aws_ebs_volume.data.id
    git_repo_url   = var.git_repo_url
    git_ref        = var.git_ref
  })

  tags = { Name = var.name }

  lifecycle {
    ignore_changes = [ami] # a newer AMI must not replace a running server on every plan
  }
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.app.id
}

resource "aws_cloudwatch_metric_alarm" "unhealthy" {
  alarm_name          = "${var.name}-unhealthy"
  alarm_description   = "RepoDigger is failing its health check behind the load balancer"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  period              = 60
  evaluation_periods  = 5
  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.web.arn_suffix
  }
}
