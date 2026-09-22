# restaurante_api/scaneats/views.py
"""CRUD de configuración (RF-04/05/06/15) y endpoints de eventos que reporta la visión (RF-01/02/03)."""
from django.db.models import ProtectedError
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .filters import EntregaFilter, FusionFilter, MesaFilter, OcupacionFilter, PersonalFilter, ZonaFilter
from .models import ClasificacionPersonal, EventoEntrega, EventoOcupacion, FusionMesas, Mesa, Personal, Zona
from .permissions import EsAdministrador, EsServicioVision
from .serializers import (
    CambioEstadoSerializer, ClasificacionReportarSerializer, ClasificacionSerializer, EntregaReportarSerializer,
    EntregaSerializer, FusionFinalizarSerializer, FusionReportarSerializer, FusionSerializer, MesaSerializer,
    OcupacionReportarSerializer, OcupacionSerializer, PersonalSerializer, ZonaSerializer,
)
from .services import ConflictoNegocio


# --------------------------------------------------------------------------- #
# Configuración (solo Administrador/Gerente)
# --------------------------------------------------------------------------- #
class _CrudAdminViewSet(viewsets.ModelViewSet):
    permission_classes = [EsAdministrador]
    mensaje_protegido = "El registro tiene historial asociado y no puede eliminarse."

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            raise ConflictoNegocio(self.mensaje_protegido)


class ZonaViewSet(_CrudAdminViewSet):
    """RF-04 / RF-15. `mesas` es obligatorio y no vacío; `es_exclusion=true` marca la zona de caja."""

    queryset = Zona.objects.prefetch_related("mesas").all()
    serializer_class = ZonaSerializer
    filterset_class = ZonaFilter
    ordering_fields = ["nombre", "id"]


class MesaViewSet(_CrudAdminViewSet):
    """RF-05."""

    queryset = Mesa.objects.select_related("zona").all()
    serializer_class = MesaSerializer
    filterset_class = MesaFilter
    ordering_fields = ["numero", "capacidad", "id"]
    mensaje_protegido = "La mesa tiene ocupaciones registradas y no puede eliminarse."

    def perform_destroy(self, instance):
        zona = instance.zona
        if zona and zona.mesas.count() == 1:
            raise ConflictoNegocio(
                f"Es la única mesa de la zona '{zona.nombre}'. Reasigne la mesa o elimine primero la zona."
            )
        super().perform_destroy(instance)


class PersonalViewSet(_CrudAdminViewSet):
    """RF-06. Para dar de baja conservando el historial use PATCH {"activo": false}."""

    queryset = Personal.objects.select_related("zona").all()
    serializer_class = PersonalSerializer
    filterset_class = PersonalFilter
    ordering_fields = ["nombre", "identificador", "id"]
    mensaje_protegido = (
        "El empleado tiene entregas registradas y no puede eliminarse; désele de baja con PATCH {\"activo\": false}."
    )


# --------------------------------------------------------------------------- #
# Eventos: el microservicio ESCRIBE, el Administrador LEE el historial
# --------------------------------------------------------------------------- #
class _EventoViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    acciones_servicio = ("create",)

    def get_permissions(self):
        if self.action in self.acciones_servicio:
            return [EsServicioVision()]
        return [EsAdministrador()]


class OcupacionViewSet(_EventoViewSet):
    """RF-01. POST registra la ocupación; POST cambiar-estado mueve la máquina de estados."""

    queryset = EventoOcupacion.objects.select_related("mesa").all()
    serializer_class = OcupacionSerializer
    filterset_class = OcupacionFilter
    ordering_fields = ["hora_inicio", "id"]
    acciones_servicio = ("create", "cambiar_estado")

    def create(self, request):
        s = OcupacionReportarSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        evento, creado = services.registrar_ocupacion(**s.validated_data)
        return Response(OcupacionSerializer(evento).data, status=status.HTTP_201_CREATED if creado else status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="cambiar-estado")
    def cambiar_estado(self, request):
        s = CambioEstadoSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        evento = services.cambiar_estado_ocupacion(**s.validated_data)
        return Response(OcupacionSerializer(evento).data)


class EntregaViewSet(_EventoViewSet):
    """RF-02 (incluye RF-08: método y confianza de la identificación del mesero)."""

    queryset = EventoEntrega.objects.select_related("ocupacion__mesa", "personal").all()
    serializer_class = EntregaSerializer
    filterset_class = EntregaFilter
    ordering_fields = ["hora", "id"]

    def create(self, request):
        s = EntregaReportarSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        entrega, creada = services.registrar_entrega(**s.validated_data)
        return Response(EntregaSerializer(entrega).data, status=status.HTTP_201_CREATED if creada else status.HTTP_200_OK)


class FusionViewSet(_EventoViewSet):
    """RF-03. Un único registro por fusión."""

    queryset = FusionMesas.objects.prefetch_related("mesas", "ocupaciones").all()
    serializer_class = FusionSerializer
    filterset_class = FusionFilter
    ordering_fields = ["hora_evento", "id"]
    acciones_servicio = ("create", "finalizar")

    def create(self, request):
        s = FusionReportarSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        fusion, creada = services.registrar_fusion(
            mesas=s.validated_data["mesas"], hora_evento=s.validated_data.get("hora_evento"), uid=s.validated_data.get("uid"),
        )
        return Response(FusionSerializer(fusion).data, status=status.HTTP_201_CREATED if creada else status.HTTP_200_OK)

    @action(detail=False, methods=["post"])
    def finalizar(self, request):
        s = FusionFinalizarSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        cerradas = services.finalizar_fusion(s.validated_data["mesas"], s.validated_data.get("hora_fin"))
        return Response({"finalizadas": FusionSerializer(cerradas, many=True).data})


class ClasificacionPersonalViewSet(_EventoViewSet):
    """Clasificación de personal reportada por visión (auditoría de RF-08)."""

    queryset = ClasificacionPersonal.objects.select_related("personal").all()
    serializer_class = ClasificacionSerializer
    filterset_fields = ["personal", "metodo"]
    ordering_fields = ["timestamp", "id"]

    def create(self, request):
        s = ClasificacionReportarSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        registro = ClasificacionPersonal.objects.create(**s.validated_data)
        return Response(ClasificacionSerializer(registro).data, status=status.HTTP_201_CREATED)


# --------------------------------------------------------------------------- #
# Configuración para el microservicio (sustituye al JSON local cuando se active)
# --------------------------------------------------------------------------- #
class ConfiguracionVisionView(APIView):
    """GET /api/vision/configuracion/ — zonas, mesas y personal en el formato que consume la visión."""

    permission_classes = [EsServicioVision | EsAdministrador]

    def get(self, request):
        zonas = Zona.objects.all()
        return Response({
            "zonas": [
                {"id": z.pk, "nombre": z.nombre, "es_exclusion": z.es_exclusion, "region": z.region} for z in zonas
            ],
            "mesas": [
                {"id": m.pk, "numero": m.numero, "capacidad": m.capacidad, "zona_id": m.zona_id, "region": m.region}
                for m in Mesa.objects.all()
            ],
            "personal": [
                {
                    "identificador": p.identificador, "nombre": p.nombre,
                    "prenda_tipo": p.prenda_tipo, "prenda_color": p.prenda_color,
                }
                for p in Personal.objects.filter(activo=True)
            ],
        })
