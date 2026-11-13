variable "name" {
  description = "Stack name: prefixes every resource. One stack per customer."
  type        = string
  default     = "repodigger"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,24}$", var.name))
    error_message = "Lowercase letters, digits and hyphens, 3 to 25 characters."
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "domain_name" {
  description = "Where users reach RepoDigger, e.g. repodigger.example.com"
  type        = string
}

variable "route53_zone_id" {
  description = "Route 53 hosted zone for domain_name. Set this, or certificate_arn."
  type        = string
  default     = ""
}

variable "certificate_arn" {
  description = "An existing, validated ACM certificate for domain_name, if you do not use Route 53."
  type        = string
  default     = ""
}

variable "allowed_cidrs" {
  description = "Who may reach the site. Narrow this to your offices or VPN if you can."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "github_webhook_cidrs" {
  description = "GitHub's hook source ranges (api.github.com/meta, \"hooks\"), if PR comments are on and allowed_cidrs is narrowed."
  type        = list(string)
  default     = []
}

variable "instance_type" {
  description = "16 GB is the minimum: Neo4j takes about 6 GB, the guardrail model and parsers the rest."
  type        = string
  default     = "m6i.xlarge"
}

variable "data_volume_gb" {
  description = "Docker's data (graph, clones, images). Kept when the instance is replaced."
  type        = number
  default     = 100
}

variable "git_repo_url" {
  description = "Where the instance clones RepoDigger from (https)."
  type        = string
}

variable "git_ref" {
  type    = string
  default = "main"
}

variable "initial_user_emails" {
  description = "Cognito users to create; each is emailed a temporary password. There is no self sign-up."
  type        = list(string)
  default     = []
}

variable "mfa" {
  description = "Cognito MFA: OFF, OPTIONAL or ON (authenticator app)."
  type        = string
  default     = "OPTIONAL"
}

variable "backup_retention_days" {
  type    = number
  default = 30
}

variable "snapshot_retention_count" {
  description = "Daily EBS snapshots of the data volume to keep."
  type        = number
  default     = 7
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}
