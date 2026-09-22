# restaurante_api/scaneats/views_panel.py
"""
Panel del Administrador/Gerente: las 3 métricas obligatorias, historial y evidencia en una sola página.

Es una vista de solo lectura sobre los mismos cálculos de `metricas.py` (misma fuente que los endpoints /api/metricas/).
Exige sesión de personal (`is_staff`): sin ella redirige al login; la sesión caduca a los 30 min de inactividad (RNF-05).
"""
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from . import metricas
from .models import EventoEntrega, EventoOcupacion, EvidenciaVideo
from .services import tiempo_espera_segundos


def _mmss(segundos):
    if segundos is None:
        return "—"
    segundos = int(round(segundos))
    return f"{segundos // 60}:{segundos % 60:02d}"


def _mensaje(exc):
    detalle = exc.detail
    if isinstance(detalle, dict):
        return "; ".join(
            f"{campo}: {' '.join(str(m) for m in (v if isinstance(v, list) else [v]))}" for campo, v in detalle.items()
        )
    return str(detalle)


def _clips(campo, ids):
    """{id_evento: [(url, duración), ...]} de los clips vigentes (los vencidos no se muestran: RNF-03)."""
    salida = {}
    for ev in EvidenciaVideo.objects.filter(**{f"{campo}__in": ids}, expira_en__gt=timezone.now()):
        clave = getattr(ev, f"{campo}_id")
        salida.setdefault(clave, []).append((reverse("evidencia-descargar", args=[ev.pk]), ev.duracion_s))
    return salida


@staff_member_required
def panel(request):
    filtros = {k: request.GET.get(k, "").strip() for k in ("desde", "hasta")}
    params = {k: v for k, v in filtros.items() if v}
    contexto = {"filtros": filtros, "error": None, "espera": None}
    try:
        f = metricas.leer_filtros(params)
        espera, empleados, uso = metricas.tiempo_espera(params), metricas.empleados(params), metricas.uso_mesas(params)
    except ValidationError as exc:
        contexto["error"] = _mensaje(exc)
        return render(request, "scaneats/panel.html", contexto)

    for fila in espera["por_mesa"]:
        fila["mmss"] = _mmss(fila["promedio_segundos"])
    espera["promedio_mmss"] = _mmss(espera["promedio_segundos"])
    maximo = max((h["ocupaciones"] for h in uso["por_hora"]), default=0)
    horas = [h for h in uso["por_hora"] if h["ocupaciones"]]
    for h in horas:
        h["ancho"] = round(100 * h["ocupaciones"] / maximo) if maximo else 0
        h["pico"] = h["hora"] in uso["horas_pico"]
    for fila in uso["por_mesa"]:
        fila["ocupada_mmss"] = _mmss(fila["tiempo_ocupada_segundos"])

    ocupaciones = list(metricas._aplicar_rango(EventoOcupacion.objects.select_related("mesa"), "hora_inicio", f)[:15])
    entregas = list(
        metricas._aplicar_rango(EventoEntrega.objects.select_related("ocupacion__mesa", "personal"), "hora", f)
        .order_by("-hora")[:15]
    )
    clips_oc = _clips("ocupacion", [o.pk for o in ocupaciones])
    clips_en = _clips("entrega", [e.pk for e in entregas])
    contexto.update({
        "espera": espera, "empleados": empleados, "uso": uso, "horas": horas,
        "ocupaciones": [
            {"o": o, "espera": _mmss(tiempo_espera_segundos(o)), "clips": clips_oc.get(o.pk, [])} for o in ocupaciones
        ],
        "entregas": [{"e": e, "clips": clips_en.get(e.pk, [])} for e in entregas],
    })
    return render(request, "scaneats/panel.html", contexto)
