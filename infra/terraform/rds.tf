# infra/terraform/rds.tf

resource "aws_db_subnet_group" "this" {
  name       = "${var.project_name}-db-subnets"
  subnet_ids = data.aws_subnets.default.ids
  tags       = { Name = "${var.project_name}-db-subnets" }
}

resource "aws_db_instance" "this" {
  identifier     = "${var.project_name}-db"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage     = 20
  storage_type          = "gp3"
  max_allocated_storage  = 50  # autoescala el disco hasta 50 GB si se llena; evita quedarse sin espacio

  db_name  = var.db_name
  username = var.db_username
  password = var.db_password

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false  # solo alcanzable desde dentro de la VPC (la EC2 del backend)

  backup_retention_period = 3     # copias de seguridad automáticas diarias, 3 días (mínimo razonable de clase)
  skip_final_snapshot     = true  # simplifica `terraform destroy` para un proyecto de clase; en producción real: false
  multi_az                = false # una sola zona de disponibilidad (más barato); producción real: true
  deletion_protection     = false

  tags = { Name = "${var.project_name}-db" }
}
