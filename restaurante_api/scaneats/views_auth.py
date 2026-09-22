# restaurante_api/scaneats/views_auth.py
"""Autenticación (RF-13, RNF-05): login/logout del Administrador y token de servicio de visión."""
from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.db import connection
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import services
from .permissions import EsAdministrador, EsServicioVision

# Mensaje ÚNICO para cualquier fallo de credenciales: no revela si falló el usuario o la contraseña.
CREDENCIALES_INVALIDAS = "Credenciales inválidas."


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(max_length=256, trim_whitespace=False, write_only=True)


class LoginView(APIView):
    """POST /api/auth/login/ — usuario y contraseña del Administrador/Gerente. Abre sesión (cookie)."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        s = LoginSerializer(data=request.data)
        if not s.is_valid():
            return Response({"detail": CREDENCIALES_INVALIDAS}, status=status.HTTP_401_UNAUTHORIZED)
        usuario = authenticate(request, username=s.validated_data["username"], password=s.validated_data["password"])
        # Solo cuentas de Administrador/Gerente inician sesión aquí (el servicio de visión usa token).
        if usuario is None or not usuario.is_staff:
            return Response({"detail": CREDENCIALES_INVALIDAS}, status=status.HTTP_401_UNAUTHORIZED)
        login(request, usuario)
        return Response({
            "usuario": usuario.get_username(),
            "sesion_expira_por_inactividad_segundos": settings.SESSION_COOKIE_AGE,
        })


class LogoutView(APIView):
    """POST /api/auth/logout/"""

    permission_classes = [EsAdministrador]

    def post(self, request):
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    """GET /api/auth/me/ — comprueba la sesión vigente."""

    permission_classes = [EsAdministrador]

    def get(self, request):
        return Response({"usuario": request.user.get_username(), "es_administrador": True})


class TokenServicioView(APIView):
    """
    POST /api/auth/servicio/token/ — el Administrador emite (o ROTA) el token del microservicio de visión.
    El token anterior deja de valer de inmediato. La clave se muestra una sola vez.
    """

    permission_classes = [EsAdministrador]

    def post(self, request):
        usuario, token = services.emitir_token_servicio()
        return Response(
            {"usuario": usuario.username, "token": token.key, "uso": "Authorization: Token <token>"},
            status=status.HTTP_201_CREATED,
        )


class ValidarTokenServicioView(APIView):
    """GET /api/auth/servicio/validar/ — el microservicio comprueba que su token sigue vigente."""

    permission_classes = [EsServicioVision]

    def get(self, request):
        return Response({"valido": True, "servicio": request.user.username})


class HealthView(APIView):
    """GET /health/ — sonda de disponibilidad (RNF-02). Abierta a propósito y sin datos sensibles."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            connection.ensure_connection()
        except Exception:  # noqa: BLE001
            return Response({"status": "db_error"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"status": "ok"})
