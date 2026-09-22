# Terraform — ScanEats en AWS (EC2 + RDS + S3)

⚠ **Nunca se ejecutó.** Esta máquina no tiene AWS CLI ni credenciales configuradas (se verificó). Escrito y
revisado a mano, pero `terraform plan`/`apply` deben correrse por primera vez con cuidado, revisando el plan
antes de confirmar. No es apto para producción real de una empresa (le falta ALB+TLS, multi-AZ, backups
automáticos probados, WAF); es exactamente el alcance de un proyecto de clase: **EC2 + RDS + S3**, económico
y defendible.

## Qué crea

| Recurso | Para qué | Tamaño (capa gratuita si la cuenta es nueva) |
|---|---|---|
| 1 instancia RDS PostgreSQL | Base de datos del backend | `db.t3.micro`, 20 GB |
| 1 bucket S3 privado | Clips de evidencia (RF-14), con regla de expiración (RNF-03) | uso mínimo |
| 1 instancia EC2 | Corre el backend Django (gunicorn) | `t3.micro` |
| 1 rol IAM + política | La EC2 sube/lee/borra clips en S3 sin guardar claves | — |
| 2 grupos de seguridad | EC2 (80/8000 público, 22 solo desde tu IP) · RDS (5432 solo desde la EC2) | — |

Usa la **VPC por defecto** de la cuenta (más simple para un proyecto de clase; una VPC dedicada con subredes
privadas es el paso siguiente para producción real, no aquí).

## Antes de `apply`

1. Instala Terraform y el AWS CLI, y configura credenciales: `aws configure` (pide el Access Key ID y el
   Secret Access Key del usuario IAM que creaste).
2. Crea (o reutiliza) un **key pair de EC2** en la consola (EC2 → Key Pairs) para poder entrar por SSH.
3. Copia `terraform.tfvars.example` a `terraform.tfvars` y complétalo: tu IP pública (`curl ifconfig.me`),
   el nombre del key pair, una contraseña fuerte para la base de datos, y el nombre del bucket S3 (debe ser
   único **en todo AWS**, no solo en tu cuenta).

```bash
cd infra/terraform
terraform init
terraform plan      # REVISA la lista de recursos antes de continuar — no la aceptes a ciegas
terraform apply      # pide confirmación escribiendo "yes"
```

Al terminar, `terraform output` imprime la IP pública de la EC2, el endpoint de RDS y el nombre del bucket.

## Después de `apply` — desplegar el código

Terraform deja el sistema operativo listo (Python, venv, gunicorn como servicio) pero **no copia el código**
automáticamente (evita depender de que el repo ya esté en GitHub). Con SSH a la IP que imprimió Terraform:

```bash
ssh -i tu-llave.pem ec2-user@<ip_publica>
sudo su - scaneats
git clone <URL_DE_TU_REPO> app && cd app/restaurante_api      # o: scp -r restaurante_api ec2-user@<ip>:~
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cat > .env <<EOF
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(50))")
DJANGO_ALLOWED_HOSTS=<ip_publica>
DB_HOST=<endpoint_rds_del_output>
DB_NAME=scaneats
DB_USER=scaneats
DB_PASSWORD=<la_que_pusiste_en_terraform.tfvars>
CLIP_STORAGE_BACKEND=s3
AWS_STORAGE_BUCKET_NAME=<nombre_del_bucket_del_output>
AWS_S3_REGION_NAME=<tu_region>
CACHE_BACKEND=db
EOF
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createcachetable
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py cargar_demo --admin gerente --password 'Cambiar-Esta-123'
.venv/bin/python manage.py crear_servicio_vision
sudo systemctl restart scaneats-api   # el servicio systemd ya lo dejó listo el user_data de Terraform
curl http://localhost:8000/health/
```

Verifica desde tu navegador: `http://<ip_publica>:8000/`. **Cambia la contraseña del demo** antes de mostrarlo
a nadie fuera del equipo.

## Purga de evidencia (RNF-03) en producción

El `user_data` deja instalado un cron diario que corre `manage.py purgar_evidencia`; confirma que quedó activo:
`sudo crontab -l -u scaneats`.

## Apagar todo (evitar cobros)

```bash
terraform destroy    # pide confirmación; borra EC2, RDS y el bucket S3 (si no está vacío, bórralo a mano antes)
```

**No dejes esto corriendo después de la defensa** si no lo vas a seguir usando: RDS cobra por hora aunque
nadie lo use.
