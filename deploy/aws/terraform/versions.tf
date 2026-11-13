terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.70" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }

  # Keep state in S3 for anything beyond a trial; it lists every resource.
  # backend "s3" {
  #   bucket = "your-terraform-state-bucket"
  #   key    = "repodigger/terraform.tfstate"
  #   region = "us-east-1"
  # }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Application = "RepoDigger", Stack = var.name }
  }
}

data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" { state = "available" }
