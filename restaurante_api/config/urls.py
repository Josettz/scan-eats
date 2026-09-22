# restaurante_api/config/urls.py
from django.contrib import admin
from django.urls import include, path

from scaneats.views_auth import HealthView
from scaneats.views_panel import panel

urlpatterns = [
    path("", panel, name="panel"),  # panel del administrador: exige sesión de personal (redirige al login)
    path("admin/", admin.site.urls),
    path("api/", include("scaneats.urls")),
    # Única ruta abierta además del login: sonda de disponibilidad (RNF-02). No expone datos.
    path("health/", HealthView.as_view(), name="health"),
]
