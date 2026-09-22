# infra/terraform/providers.tf
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
  # Credenciales: NO se ponen aquí. Vienen de `aws configure` (~/.aws/credentials) o de variables de
  # entorno AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY. Nunca se comitean claves a este repositorio.
}
