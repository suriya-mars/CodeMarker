output "url" {
  value = "https://${var.domain_name}"
}

output "alb_dns_name" {
  description = "Point domain_name here (CNAME or alias) if you do not use Route 53."
  value       = aws_lb.main.dns_name
}

output "alb_arn" {
  description = "The api verifies login tokens were signed for exactly this load balancer."
  value       = aws_lb.main.arn
}

output "secret_arn" {
  value = aws_secretsmanager_secret.app.arn
}

output "set_secret_command" {
  description = "Run once (values from a local JSON file, never on the command line)."
  value       = "aws secretsmanager put-secret-value --region ${var.region} --secret-id ${aws_secretsmanager_secret.app.arn} --secret-string file://repodigger-secret.json"
}

output "instance_id" {
  value = aws_instance.app.id
}

output "shell_command" {
  value = "aws ssm start-session --region ${var.region} --target ${aws_instance.app.id}"
}

output "backup_bucket" {
  value = aws_s3_bucket.backups.bucket
}

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "add_user_command" {
  value = "aws cognito-idp admin-create-user --region ${var.region} --user-pool-id ${aws_cognito_user_pool.main.id} --username someone@example.com --user-attributes Name=email,Value=someone@example.com Name=email_verified,Value=true"
}

output "data_volume_id" {
  value = aws_ebs_volume.data.id
}
