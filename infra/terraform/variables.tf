# infra/terraform/variables.tf
variable "aws_region" {
  description = "Región de AWS. us-east-1 suele ser la más barata; sa-east-1 (São Paulo) es la más cercana a Ecuador."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Prefijo para nombrar los recursos (se ve en la consola de AWS)."
  type        = string
  default     = "scaneats"
}

variable "my_ip_cidr" {
  description = "Tu IP pública con /32, para permitir SSH solo desde tu equipo. Averíguala con: curl ifconfig.me"
  type        = string
  # Sin valor por defecto a propósito: dejar esto abierto a 0.0.0.0/0 expondría SSH a todo internet.
}

variable "ec2_key_pair_name" {
  description = "Nombre de un key pair de EC2 ya creado en la consola (EC2 → Key Pairs), para poder entrar por SSH."
  type        = string
}

variable "instance_type" {
  description = "Tamaño de la EC2. t3.micro entra en la capa gratuita de cuentas nuevas."
  type        = string
  default     = "t3.micro"
}

variable "db_instance_class" {
  description = "Tamaño de RDS. db.t3.micro entra en la capa gratuita de cuentas nuevas."
  type        = string
  default     = "db.t3.micro"
}

variable "db_name" {
  type    = string
  default = "scaneats"
}

variable "db_username" {
  type    = string
  default = "scaneats"
}

variable "db_password" {
  description = "Contraseña de la base de datos. NO la deje en el .tf: póngala en terraform.tfvars (gitignored) o en $TF_VAR_db_password."
  type        = string
  sensitive   = true
}

variable "s3_bucket_name" {
  description = "Nombre del bucket para los clips de evidencia. Debe ser único en TODO AWS (no solo su cuenta)."
  type        = string
}

variable "evidencia_retencion_dias" {
  description = "RNF-03: días mínimos que se conserva un clip antes de poder borrarse (regla de ciclo de vida de S3)."
  type        = number
  default     = 31  # 30 días de retención (RNF-03) + 1 día de margen
}
