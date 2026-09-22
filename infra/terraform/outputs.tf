# infra/terraform/outputs.tf

output "ec2_public_ip" {
  description = "IP pública de la EC2 — panel en http://<esto>:8000/  ·  SSH: ssh -i tu-llave.pem ec2-user@<esto>"
  value       = aws_instance.backend.public_ip
}

output "rds_endpoint" {
  description = "Host de la base de datos, para DB_HOST en el .env del backend (sin el puerto)."
  value       = aws_db_instance.this.address
}

output "s3_bucket" {
  description = "Bucket de evidencia, para AWS_STORAGE_BUCKET_NAME en el .env del backend."
  value       = aws_s3_bucket.evidencia.bucket
}

output "region" {
  value = var.aws_region
}
