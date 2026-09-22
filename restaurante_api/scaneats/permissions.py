# restaurante_api/scaneats/permissions.py
from rest_framework.permissions import BasePermission

from django.conf import settings

GRUPO_SERVICIO = "servicio_vision"


class EsAdministrador(BasePermission):
    """Administrador/Gerente: usuario Django activo con is_staff (sesión con usuario y contraseña)."""

    message = "Se requiere una sesión de Administrador/Gerente."

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.is_active and u.is_staff)


class EsServicioVision(BasePermission):
    """Microservicio de Visión: usuario de servicio (grupo `servicio_vision`) autenticado por token."""

    message = "Se requiere el token de servicio del Microservicio de Visión."

    def has_permission(self, request, view):
        u = request.user
        if not (u and u.is_authenticated and u.is_active):
            return False
        if not hasattr(u, "_es_servicio_vision"):
            u._es_servicio_vision = (
                u.username == settings.SERVICIO_VISION_USERNAME
                or u.groups.filter(name=GRUPO_SERVICIO).exists()
            )
        return u._es_servicio_vision
