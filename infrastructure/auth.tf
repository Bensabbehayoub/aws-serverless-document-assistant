# ---------- Cognito: sign-in ----------

resource "aws_cognito_user_pool" "users" {
  name = "${local.name}-users"

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  admin_create_user_config {
    # false: only an administrator can create accounts (private demo)
    allow_admin_create_user_only = !var.allow_self_signup
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  deletion_protection = var.environment == "prod" ? "ACTIVE" : "INACTIVE"
}

# Hosted login page at https://<prefix>.auth.<region>.amazoncognito.com
resource "aws_cognito_user_pool_domain" "login" {
  domain       = "${local.name}-${random_string.suffix.result}"
  user_pool_id = aws_cognito_user_pool.users.id
}

# Public browser client: Authorization Code + PKCE, no client secret.
resource "aws_cognito_user_pool_client" "web" {
  name         = "${local.name}-web"
  user_pool_id = aws_cognito_user_pool.users.id

  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]

  callback_urls = ["${var.frontend_origin}/callback"]
  logout_urls   = ["${var.frontend_origin}/"]

  explicit_auth_flows           = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  access_token_validity  = 60 # minutes
  id_token_validity      = 60 # minutes
  refresh_token_validity = 7  # days

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}
