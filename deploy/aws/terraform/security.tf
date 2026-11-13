resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "HTTPS from allowed networks"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = distinct(concat(var.allowed_cidrs, var.github_webhook_cidrs))
  }
  ingress {
    description = "HTTP, redirected to HTTPS"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }
  egress {
    description = "To the instance"
    from_port   = 3000
    to_port     = 3000
    protocol    = "tcp"
    cidr_blocks = [aws_subnet.private.cidr_block]
  }
  egress {
    description = "Cognito token and user info endpoints"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "app" {
  name        = "${var.name}-app"
  description = "Only the load balancer may reach the web container"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Web, from the load balancer only"
    from_port       = 3000
    to_port         = 3000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  # No SSH: use Session Manager. Outbound HTTPS only: the LLM provider, git hosts,
  # GitHub, the ALB public-key endpoint and AWS APIs. A security group cannot filter
  # by domain name; add AWS Network Firewall if you must allowlist hosts.
  egress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
