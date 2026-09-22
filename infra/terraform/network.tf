# infra/terraform/network.tf
# Usa la VPC y las subredes POR DEFECTO de la cuenta (existen automáticamente en cada región nueva).
# Suficiente y más simple para un proyecto de clase; una VPC dedicada con subredes privadas para RDS
# es la mejora natural antes de un uso en producción real.

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}
