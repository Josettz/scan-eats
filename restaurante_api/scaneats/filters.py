# restaurante_api/scaneats/filters.py
import django_filters as df
from django.db.models import Q

from .models import EventoEntrega, EventoOcupacion, FusionMesas, Mesa, Personal, Zona


class ZonaFilter(df.FilterSet):
    search = df.CharFilter(method="buscar", label="Texto en el nombre")

    class Meta:
        model = Zona
        fields = ["es_exclusion"]

    def buscar(self, qs, nombre, valor):
        return qs.filter(nombre__icontains=valor)


class MesaFilter(df.FilterSet):
    search = df.CharFilter(method="buscar", label="Número de mesa (o parte)")
    capacidad_min = df.NumberFilter(field_name="capacidad", lookup_expr="gte")

    class Meta:
        model = Mesa
        fields = ["zona", "capacidad"]

    def buscar(self, qs, nombre, valor):
        return qs.filter(numero__icontains=valor)


class PersonalFilter(df.FilterSet):
    search = df.CharFilter(method="buscar", label="Texto en nombre o identificación")

    class Meta:
        model = Personal
        fields = ["zona", "turno", "activo", "prenda_color"]

    def buscar(self, qs, nombre, valor):
        return qs.filter(Q(nombre__icontains=valor) | Q(identificador__icontains=valor))


class OcupacionFilter(df.FilterSet):
    desde = df.IsoDateTimeFilter(field_name="hora_inicio", lookup_expr="gte")
    hasta = df.IsoDateTimeFilter(field_name="hora_inicio", lookup_expr="lte")
    abierta = df.BooleanFilter(field_name="hora_fin", lookup_expr="isnull")

    class Meta:
        model = EventoOcupacion
        fields = ["mesa", "zona", "estado"]


class EntregaFilter(df.FilterSet):
    desde = df.IsoDateTimeFilter(field_name="hora", lookup_expr="gte")
    hasta = df.IsoDateTimeFilter(field_name="hora", lookup_expr="lte")
    mesa = df.NumberFilter(field_name="ocupacion__mesa_id")

    class Meta:
        model = EventoEntrega
        fields = ["personal", "ocupacion"]


class FusionFilter(df.FilterSet):
    desde = df.IsoDateTimeFilter(field_name="hora_evento", lookup_expr="gte")
    hasta = df.IsoDateTimeFilter(field_name="hora_evento", lookup_expr="lte")
    activa = df.BooleanFilter(field_name="hora_fin", lookup_expr="isnull")
    mesa = df.NumberFilter(field_name="mesas")

    class Meta:
        model = FusionMesas
        fields = []
