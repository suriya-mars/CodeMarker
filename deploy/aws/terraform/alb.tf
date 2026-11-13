# --- certificate ----------------------------------------------------------------------
locals {
  issue_cert      = var.certificate_arn == ""
  certificate_arn = local.issue_cert ? aws_acm_certificate_validation.main[0].certificate_arn : var.certificate_arn
}

resource "aws_acm_certificate" "main" {
  count             = local.issue_cert ? 1 : 0
  domain_name       = var.domain_name
  validation_method = "DNS"
  lifecycle {
    create_before_destroy = true
    precondition {
      condition     = var.route53_zone_id != ""
      error_message = "Set route53_zone_id so the certificate can be validated, or pass certificate_arn."
    }
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = local.issue_cert ? {
    for o in aws_acm_certificate.main[0].domain_validation_options : o.domain_name => o
  } : {}
  zone_id = var.route53_zone_id
  name    = each.value.resource_record_name
  type    = each.value.resource_record_type
  records = [each.value.resource_record_value]
  ttl     = 300
}

resource "aws_acm_certificate_validation" "main" {
  count                   = local.issue_cert ? 1 : 0
  certificate_arn         = aws_acm_certificate.main[0].arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# --- load balancer ----------------------------------------------------------------------
resource "aws_lb" "main" {
  name                       = var.name
  load_balancer_type         = "application"
  internal                   = false
  security_groups            = [aws_security_group.alb.id]
  subnets                    = aws_subnet.public[*].id
  idle_timeout               = 300 # a question may take up to AGENT_TIMEOUT_S (120 s)
  drop_invalid_header_fields = true
  enable_deletion_protection = true
}

resource "aws_lb_target_group" "web" {
  name     = "${var.name}-web"
  port     = 3000
  protocol = "HTTP"
  vpc_id   = aws_vpc.main.id

  health_check {
    path                = "/api/health"
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }
}

resource "aws_lb_target_group_attachment" "web" {
  target_group_arn = aws_lb_target_group.web.arn
  target_id        = aws_instance.app.id
  port             = 3000
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# Everything goes through the Cognito login first...
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = local.certificate_arn

  default_action {
    type  = "authenticate-cognito"
    order = 1
    authenticate_cognito {
      user_pool_arn       = aws_cognito_user_pool.main.arn
      user_pool_client_id = aws_cognito_user_pool_client.alb.id
      user_pool_domain    = aws_cognito_user_pool_domain.main.domain
      session_timeout     = 28800 # 8 hours
      scope               = "openid email"
    }
  }
  default_action {
    type             = "forward"
    order            = 2
    target_group_arn = aws_lb_target_group.web.arn
  }
}

# ...except the health check and GitHub's webhook. GitHub cannot log in; the api
# checks its HMAC signature instead, and answers 404 unless a webhook secret is set.
resource "aws_lb_listener_rule" "unauthenticated" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 10
  condition {
    path_pattern {
      values = ["/api/health", "/api/webhooks/github"]
    }
  }
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

resource "aws_route53_record" "site" {
  count   = var.route53_zone_id == "" ? 0 : 1
  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = "A"
  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
