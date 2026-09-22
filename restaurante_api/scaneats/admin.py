# restaurante_api/scaneats/admin.py
"""
Panel de administración (Django admin): alta de mesas, zonas y personal sin escribir código (RNF-04).
Las mesas se crean primero (sin zona) y luego se agrupan en la zona desde la API o editando cada mesa.
"""
from django.contrib import admin

from .models import (
    ClasificacionPersonal, EventoEntrega, EventoOcupacion, EvidenciaVideo, FusionMesas, Mesa, Personal, Zona,
)


@admin.register(Zona)
class ZonaAdmin(admin.ModelAdmin):
    list_display = ("nombre", "es_exclusion")
    list_filter = ("es_exclusion",)
    search_fields = ("nombre",)


@admin.register(Mesa)
class MesaAdmin(admin.ModelAdmin):
    list_display = ("numero", "capacidad", "zona")
    list_filter = ("zona",)
    search_fields = ("numero",)


@admin.register(Personal)
class PersonalAdmin(admin.ModelAdmin):
    list_display = ("identificador", "nombre", "turno", "prenda_tipo", "prenda_color", "activo")
    list_filter = ("activo", "turno", "prenda_color")
    search_fields = ("identificador", "nombre")


@admin.register(EventoOcupacion)
class EventoOcupacionAdmin(admin.ModelAdmin):
    list_display = ("id", "mesa", "estado", "hora_inicio", "hora_fin")
    list_filter = ("estado", "mesa")
    date_hierarchy = "hora_inicio"


@admin.register(EventoEntrega)
class EventoEntregaAdmin(admin.ModelAdmin):
    list_display = ("id", "ocupacion", "personal", "hora", "metodo_identificacion", "confianza")
    list_filter = ("personal", "metodo_identificacion")


@admin.register(FusionMesas)
class FusionMesasAdmin(admin.ModelAdmin):
    list_display = ("id", "hora_evento", "hora_fin")
    filter_horizontal = ("mesas", "ocupaciones")


@admin.register(EvidenciaVideo)
class EvidenciaVideoAdmin(admin.ModelAdmin):
    list_display = ("id", "s3_key", "timestamp", "expira_en")


admin.site.register(ClasificacionPersonal)
