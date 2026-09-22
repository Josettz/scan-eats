# restaurante_api/scaneats/models.py
from datetime import timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from .validators import validar_region


# --------------------------------------------------------------------------- #
# Configuración (RF-04, RF-05, RF-06, RF-15)
# --------------------------------------------------------------------------- #
class Zona(models.Model):
    """
    Área del salón que agrupa mesas. Si `es_exclusion` es True (p. ej. la caja) el
    sistema no genera eventos de ocupación/entrega/fusión sobre sus mesas (RF-15).

    La regla "al menos una mesa" (RF-04) se valida en el serializador, porque la
    relación vive en `Mesa.zona` y una zona vacía no puede existir en la API.
    """

    nombre = models.CharField(max_length=80, unique=True)
    es_exclusion = models.BooleanField(
        default=False, help_text="Zona de exclusión (caja): no se monitorea por privacidad (RF-15)."
    )
    region = models.JSONField(
        null=True, blank=True, validators=[validar_region],
        help_text="Polígono normalizado [[x, y], ...] usado por el microservicio de visión.",
    )
    creada_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre + (" (exclusión)" if self.es_exclusion else "")


class Mesa(models.Model):
    numero = models.PositiveIntegerField(unique=True, validators=[MinValueValidator(1)])
    capacidad = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(50)])
    zona = models.ForeignKey(Zona, null=True, blank=True, on_delete=models.SET_NULL, related_name="mesas")
    region = models.JSONField(null=True, blank=True, validators=[validar_region])

    class Meta:
        ordering = ["numero"]

    def __str__(self):
        return f"Mesa {self.numero}"


class Personal(models.Model):
    class Turno(models.TextChoices):
        MANANA = "MANANA", "Mañana"
        TARDE = "TARDE", "Tarde"
        NOCHE = "NOCHE", "Noche"

    class PrendaTipo(models.TextChoices):
        DELANTAL = "DELANTAL", "Delantal"
        CAMISETA = "CAMISETA", "Camiseta"
        CAMISA = "CAMISA", "Camisa"
        CHALECO = "CHALECO", "Chaleco"
        GORRA = "GORRA", "Gorra"
        OTRO = "OTRO", "Otro"

    class PrendaColor(models.TextChoices):
        ROJO = "ROJO", "Rojo"
        NARANJA = "NARANJA", "Naranja"
        AMARILLO = "AMARILLO", "Amarillo"
        VERDE = "VERDE", "Verde"
        AZUL = "AZUL", "Azul"
        MORADO = "MORADO", "Morado"
        ROSADO = "ROSADO", "Rosado"
        NEGRO = "NEGRO", "Negro"
        BLANCO = "BLANCO", "Blanco"
        GRIS = "GRIS", "Gris"

    identificador = models.CharField(max_length=30, unique=True, help_text="Identificación única (RF-06).")
    nombre = models.CharField(max_length=100, help_text="Nombre o alias.")
    zona = models.ForeignKey(Zona, null=True, blank=True, on_delete=models.SET_NULL, related_name="personal")
    turno = models.CharField(max_length=10, choices=Turno.choices, blank=True)
    # Método PRINCIPAL de reconocimiento (RF-08): prenda distintiva. Si queda en blanco, esa
    # persona solo podrá reconocerse por el respaldo de comportamiento.
    prenda_tipo = models.CharField(max_length=10, choices=PrendaTipo.choices, blank=True)
    prenda_color = models.CharField(max_length=10, choices=PrendaColor.choices, blank=True)
    activo = models.BooleanField(default=True, help_text="False = dado de baja (conserva su historial).")

    class Meta:
        ordering = ["nombre"]
        verbose_name_plural = "personal"

    def save(self, *args, **kwargs):
        # Normaliza para que "a01" y "A01" no puedan coexistir (RF-06).
        self.identificador = (self.identificador or "").strip().upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.nombre} ({self.identificador})"


# --------------------------------------------------------------------------- #
# Eventos generados por el microservicio de visión (RF-01, RF-02, RF-03)
# --------------------------------------------------------------------------- #
class EventoBase(models.Model):
    # `uid` lo genera el microservicio: si reintenta una petición cuya respuesta se perdió,
    # el backend devuelve el evento ya guardado en vez de duplicarlo.
    uid = models.UUIDField(null=True, blank=True, unique=True, default=None, editable=False)
    registrado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class EventoOcupacion(EventoBase):
    class Estado(models.TextChoices):
        OCUPADA = "OCUPADA", "Ocupada"
        POSIBLEMENTE_LIBRE = "POSIBLEMENTE_LIBRE", "Posiblemente libre"
        LIBRE = "LIBRE", "Libre"

    # Transiciones permitidas de la máquina de estados (LIBRE es terminal: una nueva ocupación = nuevo evento).
    TRANSICIONES = {
        Estado.OCUPADA: {Estado.POSIBLEMENTE_LIBRE, Estado.LIBRE},
        Estado.POSIBLEMENTE_LIBRE: {Estado.OCUPADA, Estado.LIBRE},
        Estado.LIBRE: set(),
    }

    mesa = models.ForeignKey(Mesa, on_delete=models.PROTECT, related_name="ocupaciones")
    zona = models.ForeignKey(
        Zona, null=True, blank=True, on_delete=models.SET_NULL, related_name="ocupaciones",
        help_text="Zona de la mesa al momento de la ocupación (se congela para el histórico).",
    )
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.OCUPADA)
    hora_inicio = models.DateTimeField(default=timezone.now, db_index=True)
    hora_fin = models.DateTimeField(null=True, blank=True)
    # Momento en que la visión CONFIRMÓ la ocupación. RF-01 (≤ 3 s) mide registrado_en - detectado_en.
    detectado_en = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-hora_inicio"]
        indexes = [models.Index(fields=["mesa", "hora_inicio"])]
        constraints = [
            # Una sola ocupación abierta por mesa (también protege ante reintentos concurrentes).
            models.UniqueConstraint(
                fields=["mesa"], condition=Q(hora_fin__isnull=True), name="una_ocupacion_abierta_por_mesa"
            ),
            models.CheckConstraint(
                condition=Q(hora_fin__isnull=True) | Q(hora_fin__gte=F("hora_inicio")),
                name="ocupacion_fin_posterior_a_inicio",
            ),
        ]

    @property
    def abierta(self):
        return self.hora_fin is None

    @property
    def latencia_registro(self):
        return self.registrado_en - self.detectado_en

    def __str__(self):
        return f"Ocupación mesa {self.mesa.numero} {self.hora_inicio:%Y-%m-%d %H:%M:%S} [{self.estado}]"


class EventoEntrega(EventoBase):
    class Metodo(models.TextChoices):
        VESTIMENTA = "VESTIMENTA", "Vestimenta (principal)"
        COMPORTAMIENTO = "COMPORTAMIENTO", "Comportamiento (respaldo)"

    # Una ocupación puede tener VARIAS entregas (aperitivo, plato fuerte, bebidas...).
    ocupacion = models.ForeignKey(EventoOcupacion, on_delete=models.CASCADE, related_name="entregas")
    personal = models.ForeignKey(Personal, on_delete=models.PROTECT, related_name="entregas")
    hora = models.DateTimeField(default=timezone.now, db_index=True)
    metodo_identificacion = models.CharField(max_length=15, choices=Metodo.choices, blank=True)
    confianza = models.FloatField(null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(1)])

    class Meta:
        ordering = ["hora"]

    def __str__(self):
        return f"Entrega mesa {self.ocupacion.mesa.numero} por {self.personal.identificador} {self.hora:%H:%M:%S}"


class FusionMesas(EventoBase):
    """Un único registro por fusión (no uno por mesa) — RF-03."""

    mesas = models.ManyToManyField(Mesa, related_name="fusiones")
    ocupaciones = models.ManyToManyField(EventoOcupacion, related_name="fusiones", blank=True)
    hora_evento = models.DateTimeField(default=timezone.now, db_index=True)
    hora_fin = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-hora_evento"]
        verbose_name_plural = "fusiones de mesas"

    @property
    def activa(self):
        return self.hora_fin is None

    def __str__(self):
        return f"Fusión #{self.pk} {self.hora_evento:%Y-%m-%d %H:%M:%S}"


class ClasificacionPersonal(models.Model):
    """Auditoría de cada identificación de personal reportada por visión (base para medir RF-08 ≥ 85 %)."""

    personal = models.ForeignKey(Personal, null=True, blank=True, on_delete=models.SET_NULL, related_name="clasificaciones")
    metodo = models.CharField(max_length=15, choices=EventoEntrega.Metodo.choices)
    confianza = models.FloatField(validators=[MinValueValidator(0), MaxValueValidator(1)])
    track_id = models.IntegerField(null=True, blank=True)
    mesa = models.ForeignKey(Mesa, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    timestamp = models.DateTimeField(default=timezone.now)
    registrado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]
        verbose_name_plural = "clasificaciones de personal"


# --------------------------------------------------------------------------- #
# Evidencia (RF-14, RNF-03)
# --------------------------------------------------------------------------- #
class EvidenciaVideo(models.Model):
    ocupacion = models.ForeignKey(EventoOcupacion, null=True, blank=True, on_delete=models.CASCADE, related_name="evidencias")
    entrega = models.ForeignKey(EventoEntrega, null=True, blank=True, on_delete=models.CASCADE, related_name="evidencias")
    fusion = models.ForeignKey(FusionMesas, null=True, blank=True, on_delete=models.CASCADE, related_name="evidencias")
    s3_key = models.CharField(max_length=500, help_text="Clave del objeto en S3 (o ruta relativa en almacenamiento local).")
    timestamp = models.DateTimeField(default=timezone.now, help_text="Instante del evento que muestra el clip.")
    duracion_s = models.FloatField(null=True, blank=True)
    tamano_bytes = models.BigIntegerField(null=True, blank=True)
    content_type = models.CharField(max_length=100, default="video/mp4")
    creado_en = models.DateTimeField(auto_now_add=True)
    expira_en = models.DateTimeField(db_index=True, help_text="Vence según la política de retención (RNF-03).")

    class Meta:
        ordering = ["-timestamp"]
        verbose_name_plural = "evidencias de video"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(ocupacion__isnull=False, entrega__isnull=True, fusion__isnull=True)
                    | Q(ocupacion__isnull=True, entrega__isnull=False, fusion__isnull=True)
                    | Q(ocupacion__isnull=True, entrega__isnull=True, fusion__isnull=False)
                ),
                name="evidencia_referencia_a_un_solo_evento",
            )
        ]

    def save(self, *args, **kwargs):
        if self.expira_en is None:
            self.expira_en = (self.creado_en or timezone.now()) + timedelta(days=settings.EVIDENCIA_RETENCION_DIAS)
        super().save(*args, **kwargs)

    @property
    def vencida(self):
        return self.expira_en <= timezone.now()

    def __str__(self):
        return f"Evidencia {self.pk} ({self.s3_key})"
