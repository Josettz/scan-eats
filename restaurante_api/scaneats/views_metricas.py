# restaurante_api/scaneats/views_metricas.py
"""Las 3 métricas obligatorias. Solo Administrador/Gerente (RF-13)."""
from rest_framework.response import Response
from rest_framework.views import APIView

from . import metricas
from .permissions import EsAdministrador


class TiempoEsperaView(APIView):
    """
    GET /api/metricas/tiempo-espera/  (RF-10, incluye RF-09)
    Query: mesa=<id> zona=<id> desde=<YYYY-MM-DD|ISO> hasta=<YYYY-MM-DD|ISO>
    """

    permission_classes = [EsAdministrador]

    def get(self, request):
        return Response(metricas.tiempo_espera(request.query_params))


class EmpleadosView(APIView):
    """
    GET /api/metricas/empleados/  (RF-11)
    Query: desde, hasta, zona. Devuelve a todos los empleados empatados en primer lugar.
    """

    permission_classes = [EsAdministrador]

    def get(self, request):
        return Response(metricas.empleados(request.query_params))


class UsoMesasView(APIView):
    """
    GET /api/metricas/uso-mesas/  (RF-12)
    Query: mesa, zona, desde, hasta. Frecuencia, más/menos usadas, horas pico.
    """

    permission_classes = [EsAdministrador]

    def get(self, request):
        return Response(metricas.uso_mesas(request.query_params))
