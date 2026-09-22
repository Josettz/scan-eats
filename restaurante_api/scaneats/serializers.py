# restaurante_api/scaneats/serializers.py
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from . import services
from .models import (
    ClasificacionPersonal, EventoEntrega, EventoOcupacion, EvidenciaVideo, FusionMesas, Mesa, Personal, Zona,
)


# --------------------------------------------------------------------------- #
# Configuración: Zona / Mesa / Personal (RF-04, RF-05, RF-06, RF-15)
# --------------------------------------------------------------------------- #
class ZonaSerializer(serializers.ModelSerializer):
    mesas = serializers.PrimaryKeyRelatedField(
        queryset=Mesa.objects.all(),
        many=True,
        allow_empty=False,
        error_messages={
            "required": "Debe asignar al menos una mesa a la zona.",
            "empty": "Debe asignar al menos una mesa a la zona.",
            "null": "Debe asignar al menos una mesa a la zona.",
        },
    )

    class Meta:
        model = Zona
        fields = ["id", "nombre", "es_exclusion", "region", "mesas"]

    def validate_nombre(self, valor):
        valor = valor.strip()
        qs = Zona.objects.filter(nombre__iexact=valor)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("Ya existe una zona con ese nombre.")
        return valor

    def _asignar_mesas(self, zona, mesas):
        # Una mesa que se traslada desde otra zona no puede dejarla vacía (RF-04).
        for mesa in mesas:
            origen = mesa.zona
            if origen and origen.pk != zona.pk and not origen.mesas.exclude(pk__in=[m.pk for m in mesas]).exists():
                raise serializers.ValidationError(
                    {"mesas": f"La mesa {mesa.numero} es la única de la zona '{origen.nombre}'; esa zona quedaría sin mesas."}
                )
        # Las mesas que ya no están en la lista quedan sin zona; las nuevas se asignan.
        zona.mesas.exclude(pk__in=[m.pk for m in mesas]).update(zona=None)
        Mesa.objects.filter(pk__in=[m.pk for m in mesas]).update(zona=zona)

    @transaction.atomic
    def create(self, validated_data):
        mesas = validated_data.pop("mesas")
        zona = Zona.objects.create(**validated_data)
        self._asignar_mesas(zona, mesas)
        return zona

    @transaction.atomic
    def update(self, instance, validated_data):
        mesas = validated_data.pop("mesas", None)
        for campo, valor in validated_data.items():
            setattr(instance, campo, valor)
        instance.save()
        if mesas is not None:
            self._asignar_mesas(instance, mesas)
        return instance


class MesaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Mesa
        fields = ["id", "numero", "capacidad", "zona", "region"]
        extra_kwargs = {
            "numero": {"min_value": 1, "error_messages": {"unique": "Ya existe una mesa con ese número."}},
            "capacidad": {"min_value": 1, "max_value": 50},
        }

    def validate(self, attrs):
        # Mover la única mesa de una zona la dejaría vacía (RF-04).
        if self.instance and "zona" in attrs:
            actual = self.instance.zona
            if actual and attrs["zona"] != actual and actual.mesas.count() == 1:
                raise serializers.ValidationError(
                    {"zona": f"Es la única mesa de la zona '{actual.nombre}'; esa zona quedaría sin mesas."}
                )
        return attrs


class PersonalSerializer(serializers.ModelSerializer):
    identificador = serializers.CharField(max_length=30)
    nombre = serializers.CharField(max_length=100)

    class Meta:
        model = Personal
        fields = ["id", "identificador", "nombre", "zona", "turno", "prenda_tipo", "prenda_color", "activo"]

    def validate_identificador(self, valor):
        valor = valor.strip().upper()
        if not valor:
            raise serializers.ValidationError("La identificación es obligatoria.")
        qs = Personal.objects.filter(identificador=valor)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("Ya existe un integrante del personal con esta identificación.")
        return valor

    def validate_nombre(self, valor):
        valor = valor.strip()
        if not valor:
            raise serializers.ValidationError("El nombre es obligatorio.")
        return valor


# --------------------------------------------------------------------------- #
# Eventos: lectura
# --------------------------------------------------------------------------- #
class OcupacionSerializer(serializers.ModelSerializer):
    mesa_numero = serializers.IntegerField(source="mesa.numero", read_only=True)
    latencia_registro_ms = serializers.SerializerMethodField()
    espera_segundos = serializers.SerializerMethodField()  # RF-09

    class Meta:
        model = EventoOcupacion
        fields = [
            "id", "mesa", "mesa_numero", "zona", "estado", "hora_inicio", "hora_fin",
            "detectado_en", "registrado_en", "latencia_registro_ms", "espera_segundos", "uid",
        ]

    def get_latencia_registro_ms(self, obj):
        return round(obj.latencia_registro.total_seconds() * 1000, 1)

    def get_espera_segundos(self, obj):
        return services.tiempo_espera_segundos(obj)


class EntregaSerializer(serializers.ModelSerializer):
    mesa_numero = serializers.IntegerField(source="ocupacion.mesa.numero", read_only=True)
    personal_identificador = serializers.CharField(source="personal.identificador", read_only=True)
    personal_nombre = serializers.CharField(source="personal.nombre", read_only=True)

    class Meta:
        model = EventoEntrega
        fields = [
            "id", "ocupacion", "mesa_numero", "personal", "personal_identificador", "personal_nombre",
            "hora", "metodo_identificacion", "confianza", "registrado_en", "uid",
        ]


class FusionSerializer(serializers.ModelSerializer):
    mesas_numeros = serializers.SerializerMethodField()
    ocupaciones = serializers.PrimaryKeyRelatedField(many=True, read_only=True)

    class Meta:
        model = FusionMesas
        fields = ["id", "mesas", "mesas_numeros", "ocupaciones", "hora_evento", "hora_fin", "registrado_en", "uid"]

    def get_mesas_numeros(self, obj):
        return sorted(m.numero for m in obj.mesas.all())


class ClasificacionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClasificacionPersonal
        fields = ["id", "personal", "metodo", "confianza", "track_id", "mesa", "timestamp", "registrado_en"]


class EvidenciaSerializer(serializers.ModelSerializer):
    class Meta:
        model = EvidenciaVideo
        fields = ["id", "timestamp", "duracion_s", "tamano_bytes", "content_type", "creado_en", "expira_en"]


# --------------------------------------------------------------------------- #
# Eventos: escritura (los reporta el microservicio de visión)
# --------------------------------------------------------------------------- #
class _ReporteMesaMixin:
    """Resuelve `mesa_numero` -> `mesa` (clave natural que conoce la visión)."""

    def _resolver_mesa(self, numero):
        mesa = Mesa.objects.select_related("zona").filter(numero=numero).first()
        if mesa is None:
            raise serializers.ValidationError({"mesa_numero": f"No existe la mesa {numero}."})
        return mesa


class OcupacionReportarSerializer(_ReporteMesaMixin, serializers.Serializer):
    mesa_numero = serializers.IntegerField(min_value=1)
    hora_inicio = serializers.DateTimeField(required=False)
    detectado_en = serializers.DateTimeField(required=False)
    uid = serializers.UUIDField(required=False)

    def validate(self, attrs):
        attrs["mesa"] = self._resolver_mesa(attrs.pop("mesa_numero"))
        return attrs


class CambioEstadoSerializer(_ReporteMesaMixin, serializers.Serializer):
    mesa_numero = serializers.IntegerField(min_value=1)
    estado = serializers.ChoiceField(choices=EventoOcupacion.Estado.choices)
    hora_fin = serializers.DateTimeField(required=False)

    def validate(self, attrs):
        attrs["mesa"] = self._resolver_mesa(attrs.pop("mesa_numero"))
        return attrs


class EntregaReportarSerializer(_ReporteMesaMixin, serializers.Serializer):
    mesa_numero = serializers.IntegerField(min_value=1)
    personal_identificador = serializers.CharField(max_length=30)  # RF-02: sin mesero no hay entrega
    hora = serializers.DateTimeField(required=False)
    ocupacion = serializers.IntegerField(required=False, min_value=1)
    metodo_identificacion = serializers.ChoiceField(choices=EventoEntrega.Metodo.choices, required=False)
    confianza = serializers.FloatField(required=False, min_value=0, max_value=1)
    uid = serializers.UUIDField(required=False)

    def validate(self, attrs):
        attrs["mesa"] = self._resolver_mesa(attrs.pop("mesa_numero"))
        ident = attrs.pop("personal_identificador").strip().upper()
        personal = Personal.objects.filter(identificador=ident).first()
        if personal is None:
            raise serializers.ValidationError({"personal_identificador": f"No existe personal con identificación {ident}."})
        if not personal.activo:
            raise serializers.ValidationError({"personal_identificador": f"{ident} está dado de baja."})
        attrs["personal"] = personal
        attrs["ocupacion_id"] = attrs.pop("ocupacion", None)
        attrs["metodo"] = attrs.pop("metodo_identificacion", "")
        return attrs


class FusionReportarSerializer(serializers.Serializer):
    mesas = serializers.ListField(child=serializers.IntegerField(min_value=1), min_length=2, allow_empty=False)
    hora_evento = serializers.DateTimeField(required=False)
    uid = serializers.UUIDField(required=False)

    def validate_mesas(self, numeros):
        if len(set(numeros)) != len(numeros):
            raise serializers.ValidationError("Las mesas de una fusión no pueden repetirse.")
        mesas = list(Mesa.objects.select_related("zona").filter(numero__in=numeros))
        faltantes = sorted(set(numeros) - {m.numero for m in mesas})
        if faltantes:
            raise serializers.ValidationError(f"No existen las mesas: {', '.join(map(str, faltantes))}.")
        return mesas


class FusionFinalizarSerializer(serializers.Serializer):
    mesas = serializers.ListField(child=serializers.IntegerField(min_value=1), min_length=1)
    hora_fin = serializers.DateTimeField(required=False)

    def validate_mesas(self, numeros):
        return list(Mesa.objects.filter(numero__in=numeros))


class ClasificacionReportarSerializer(serializers.Serializer):
    personal_identificador = serializers.CharField(max_length=30, required=False, allow_blank=True)
    metodo = serializers.ChoiceField(choices=EventoEntrega.Metodo.choices)
    confianza = serializers.FloatField(min_value=0, max_value=1)
    track_id = serializers.IntegerField(required=False)
    mesa_numero = serializers.IntegerField(required=False, min_value=1)
    timestamp = serializers.DateTimeField(required=False)

    def validate(self, attrs):
        ident = (attrs.pop("personal_identificador", "") or "").strip().upper()
        attrs["personal"] = None
        if ident:
            attrs["personal"] = Personal.objects.filter(identificador=ident).first()
            if attrs["personal"] is None:
                raise serializers.ValidationError({"personal_identificador": f"No existe personal con identificación {ident}."})
        numero = attrs.pop("mesa_numero", None)
        attrs["mesa"] = Mesa.objects.filter(numero=numero).first() if numero else None
        attrs.setdefault("timestamp", timezone.now())
        return attrs


class EvidenciaSubirSerializer(serializers.Serializer):
    TIPOS = ("ocupacion", "entrega", "fusion")

    clip = serializers.FileField()
    tipo = serializers.ChoiceField(choices=TIPOS)
    # El evento se identifica por su id, o por el `uid` que la visión generó al reportarlo
    # (la visión no conoce los ids de la base de datos).
    evento_id = serializers.IntegerField(min_value=1, required=False)
    evento_uid = serializers.UUIDField(required=False)
    timestamp = serializers.DateTimeField(required=False)
    duracion_s = serializers.FloatField(required=False, min_value=0)

    def validate(self, attrs):
        if ("evento_id" in attrs) == ("evento_uid" in attrs):
            raise serializers.ValidationError("Indique exactamente uno de: evento_id, evento_uid.")
        return attrs
