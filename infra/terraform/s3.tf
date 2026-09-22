# infra/terraform/s3.tf — bucket de evidencia (RF-14, RNF-03)

resource "aws_s3_bucket" "evidencia" {
  bucket = var.s3_bucket_name
  tags   = { Name = "${var.project_name}-evidencia" }
}

# Bloqueo de acceso público: el bucket es privado, siempre. Los clips se sirven con URLs firmadas
# temporales que genera el backend (ver storage.py), no con un enlace público al bucket.
resource "aws_s3_bucket_public_access_block" "evidencia" {
  bucket                  = aws_s3_bucket.evidencia.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evidencia" {
  bucket = aws_s3_bucket.evidencia.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# RNF-03: borrado automático como red de seguridad, además del comando `purgar_evidencia` del backend.
# Si el cron de purga fallara algún día, S3 igual limpia los clips vencidos.
resource "aws_s3_bucket_lifecycle_configuration" "evidencia" {
  bucket = aws_s3_bucket.evidencia.id
  rule {
    id     = "expirar-evidencia-vencida"
    status = "Enabled"
    filter { prefix = "evidencia/" }
    expiration {
      days = var.evidencia_retencion_dias
    }
  }
}
