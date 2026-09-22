# restaurante_api/scaneats/urls.py
from django.urls import path
from rest_framework.routers import SimpleRouter

from . import views, views_auth, views_evidencia, views_metricas

router = SimpleRouter()
router.register("zonas", views.ZonaViewSet, basename="zona")
router.register("mesas", views.MesaViewSet, basename="mesa")
router.register("personal", views.PersonalViewSet, basename="personal")
router.register("eventos/ocupacion", views.OcupacionViewSet, basename="ocupacion")
router.register("eventos/entrega", views.EntregaViewSet, basename="entrega")
router.register("eventos/fusion", views.FusionViewSet, basename="fusion")
router.register("eventos/personal-clasificado", views.ClasificacionPersonalViewSet, basename="clasificacion")

urlpatterns = [
    # Autenticación (RF-13)
    path("auth/login/", views_auth.LoginView.as_view(), name="auth-login"),
    path("auth/logout/", views_auth.LogoutView.as_view(), name="auth-logout"),
    path("auth/me/", views_auth.MeView.as_view(), name="auth-me"),
    path("auth/servicio/token/", views_auth.TokenServicioView.as_view(), name="auth-servicio-token"),
    path("auth/servicio/validar/", views_auth.ValidarTokenServicioView.as_view(), name="auth-servicio-validar"),
    # Métricas obligatorias
    path("metricas/tiempo-espera/", views_metricas.TiempoEsperaView.as_view(), name="metricas-tiempo-espera"),
    path("metricas/empleados/", views_metricas.EmpleadosView.as_view(), name="metricas-empleados"),
    path("metricas/uso-mesas/", views_metricas.UsoMesasView.as_view(), name="metricas-uso-mesas"),
    # Evidencia (RF-14)
    path("evidencia/", views_evidencia.EvidenciaSubirView.as_view(), name="evidencia-subir"),
    path("evidencia/clip/<int:pk>/descargar/", views_evidencia.EvidenciaDescargarView.as_view(), name="evidencia-descargar"),
    path("evidencia/<int:evento_id>/", views_evidencia.EvidenciaConsultarView.as_view(), name="evidencia-consultar"),
    # Configuración para el microservicio de visión
    path("vision/configuracion/", views.ConfiguracionVisionView.as_view(), name="vision-configuracion"),
] + router.urls
