resource "random_id" "suffix" {
  byte_length = 3
}

# --- secrets ----------------------------------------------------------------------
# Terraform creates the secret but never its value, so no key or password ever lands
# in Terraform state. Set the value once with the command in the outputs.
resource "aws_secretsmanager_secret" "app" {
  name                    = "${var.name}/app"
  description             = "RepoDigger .env values (JSON object). Read by deploy/render-env.sh."
  recovery_window_in_days = 7
}

# --- backups ------------------------------------------------------------------------
resource "aws_s3_bucket" "backups" {
  bucket = "${var.name}-backups-${data.aws_caller_identity.current.account_id}-${random_id.suffix.hex}"
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    expiration {
      days = var.backup_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
  }
}

# --- the data volume ------------------------------------------------------------------
# Docker's whole data directory: the graph, cloned repos, images. Separate from the
# instance, so replacing the instance keeps the data, and protected from `destroy`.
resource "aws_ebs_volume" "data" {
  availability_zone = aws_subnet.private.availability_zone
  size              = var.data_volume_gb
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "${var.name}-data", Snapshot = var.name }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_dlm_lifecycle_policy" "snapshots" {
  description        = "${var.name} daily data-volume snapshots"
  execution_role_arn = aws_iam_role.dlm.arn
  state              = "ENABLED"

  policy_details {
    resource_types = ["VOLUME"]
    target_tags    = { Snapshot = var.name }

    schedule {
      name      = "daily"
      copy_tags = true
      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["02:00"]
      }
      retain_rule {
        count = var.snapshot_retention_count
      }
    }
  }
}

# --- logs -------------------------------------------------------------------------------
# Container logs carry counts and events, never prompts or source (api/telemetry.py).
resource "aws_cloudwatch_log_group" "app" {
  name              = "/repodigger/${var.name}"
  retention_in_days = var.log_retention_days
}
