# infra/terraform/security_groups.tf

resource "aws_security_group" "ec2" {
  name        = "${var.project_name}-ec2"
  description = "ScanEats backend: HTTP publico (demo sin TLS), SSH solo desde mi IP"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "Panel/API (demo sin ALB/TLS; para producción real, poner esto detrás de un ALB con ACM)"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "SSH — SOLO desde la IP del desarrollador"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }

  egress {
    description = "Salida libre (para pip install, git, hablar con RDS y S3)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project_name}-ec2" }
}

resource "aws_security_group" "rds" {
  name        = "${var.project_name}-rds"
  description = "PostgreSQL accesible SOLO desde la instancia EC2 del backend"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description     = "Postgres solo desde la EC2 del backend"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ec2.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.project_name}-rds" }
}
