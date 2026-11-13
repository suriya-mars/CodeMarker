# Login. The load balancer sends every request through Cognito and forwards a signed
# token, which the api verifies (api/auth.py, AUTH_MODE=alb). Users are created by an
# administrator only; there is no self sign-up.
resource "aws_cognito_user_pool" "main" {
  name                     = var.name
  deletion_protection      = "ACTIVE"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = var.mfa

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  dynamic "software_token_mfa_configuration" {
    for_each = var.mfa == "OFF" ? [] : [1]
    content {
      enabled = true
    }
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${var.name}-${random_id.suffix.hex}"
  user_pool_id = aws_cognito_user_pool.main.id
}

resource "aws_cognito_user_pool_client" "alb" {
  name                                 = "${var.name}-alb"
  user_pool_id                         = aws_cognito_user_pool.main.id
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["https://${var.domain_name}/oauth2/idpresponse"]
  logout_urls                          = ["https://${var.domain_name}/"]
  prevent_user_existence_errors        = "ENABLED"
}

resource "aws_cognito_user" "initial" {
  for_each     = toset(var.initial_user_emails)
  user_pool_id = aws_cognito_user_pool.main.id
  username     = each.value
  attributes = {
    email          = each.value
    email_verified = "true"
  }
  desired_delivery_mediums = ["EMAIL"]
}
