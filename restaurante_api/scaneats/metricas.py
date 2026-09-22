# restaurante_api/scaneats/metricas.py
"""
Cálculo de las 3 métricas obligatorias (RF-10, RF-11, RF-12).

Los cálculos se hacen sobre los eventos ya registrados; los volúmenes de un restaurante
(cientos de eventos por día) permiten agregar en Python con aritmética exacta y el
mismo resultado en SQLite y PostgreSQL.
"""
import re
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta

from django.db.models import Count, Min
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.exceptions import ValidationError

from .models import EventoEntrega, EventoOcupacion, Mesa, Personal

SOLO_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------- #
# Filtros comunes
# --------------------------------------------------------------------------- #
def _parsear_instante(texto, campo, es_hasta):
    """Acepta 'YYYY-MM-DD' (día completo) o un datetime ISO 8601. Devuelve un instante aware."""
    zona = timezone.get_current_timezone()
    texto = texto.strip()
    # Ojo: desde Python 3.11 parse_datetime() también acepta "YYYY-MM-DD" (medianoche); la solo-fecha
    # se detecta primero para que 'hasta' abarque el día completo.
    if SOLO_FECHA.match(texto):
        d = parse_date(texto)
        if d is None:
            raise ValidationError({campo: "Fecha inválida."})
        inicio = timezone.make_aware(datetime.combine(d, time.min), zona)
        return inicio + timedelta(days=1) if es_hasta else inicio
    try:
        dt = parse_datetime(texto)
    except ValueError:
        dt = None
    if dt is None:
        raise ValidationError({campo: "Formato inválido; use YYYY-MM-DD o un datetime ISO 8601."})
    return dt if timezone.is_aware(dt) else timezone.make_aware(dt, zona)


def _entero(params, campo):
    valor = params.get(campo)
    if valor in (None, ""):
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        raise ValidationError({campo: "Debe ser un número entero."})


def leer_filtros(params):
    """Devuelve dict con desde, hasta (EXCLUSIVO), mesa, zona. Valida coherencia del rango."""
    desde = params.get("desde")
    hasta = params.get("hasta")
    filtros = {
        "desde": _parsear_instante(desde, "desde", False) if desde else None,
        "hasta": _parsear_instante(hasta, "hasta", True) if hasta else None,
        "mesa": _entero(params, "mesa"),
        "zona": _entero(params, "zona"),
    }
    if filtros["desde"] and filtros["hasta"] and filtros["desde"] >= filtros["hasta"]:
        raise ValidationError({"desde": "'desde' debe ser anterior a 'hasta'."})
    return filtros


def _iso(valor):
    return timezone.localtime(valor).isoformat() if valor else None


def _descripcion_filtros(f):
    return {"mesa": f["mesa"], "zona": f["zona"], "desde": _iso(f["desde"]), "hasta_exclusivo": _iso(f["hasta"])}


def _aplicar_rango(qs, campo, f):
    if f["desde"]:
        qs = qs.filter(**{f"{campo}__gte": f["desde"]})
    if f["hasta"]:
        qs = qs.filter(**{f"{campo}__lt": f["hasta"]})
    return qs


def _redondear(valor, cifras=3):
    return None if valor is None else round(valor, cifras)


# --------------------------------------------------------------------------- #
# RF-10 — tiempo de espera promedio (incluye RF-09)
# --------------------------------------------------------------------------- #
def tiempo_espera(params):
    f = leer_filtros(params)
    ocupaciones = _aplicar_rango(EventoOcupacion.objects.all(), "hora_inicio", f)
    if f["mesa"] is not None:
        ocupaciones = ocupaciones.filter(mesa_id=f["mesa"])
    if f["zona"] is not None:
        ocupaciones = ocupaciones.filter(zona_id=f["zona"])

    total = ocupaciones.count()
    filas = (
        ocupaciones.annotate(primera_entrega=Min("entregas__hora"))
        .filter(primera_entrega__isnull=False)
        .values_list("mesa_id", "mesa__numero", "hora_inicio", "primera_entrega")
    )
    esperas = []
    por_mesa = defaultdict(list)
    numero_de = {}
    for mesa_id, numero, inicio, primera in filas:
        segundos = (primera - inicio).total_seconds()  # RF-09
        esperas.append(segundos)
        por_mesa[mesa_id].append(segundos)
        numero_de[mesa_id] = numero

    promedio = sum(esperas) / len(esperas) if esperas else None
    return {
        "filtros": _descripcion_filtros(f),
        "total_ocupaciones": total,
        "ocupaciones_con_entrega": len(esperas),
        "ocupaciones_sin_entrega": total - len(esperas),
        "promedio_segundos": _redondear(promedio),
        "promedio_minutos": _redondear(promedio / 60 if promedio is not None else None),
        "minimo_segundos": _redondear(min(esperas)) if esperas else None,
        "maximo_segundos": _redondear(max(esperas)) if esperas else None,
        "por_mesa": [
            {
                "mesa_id": mesa_id,
                "mesa_numero": numero_de[mesa_id],
                "muestras": len(v),
                "promedio_segundos": _redondear(sum(v) / len(v)),
                "promedio_minutos": _redondear(sum(v) / len(v) / 60),
            }
            for mesa_id, v in sorted(por_mesa.items(), key=lambda kv: numero_de[kv[0]])
        ],
    }


# --------------------------------------------------------------------------- #
# RF-11 — empleado con más mesas atendidas (con empates)
# --------------------------------------------------------------------------- #
def empleados(params):
    """
    "Mesa atendida" = ocupación distinta en la que el empleado hizo al menos una entrega
    dentro del periodo (varias entregas a la misma mesa cuentan una sola vez).
    Devuelve a TODOS los empleados empatados en primer lugar.
    """
    f = leer_filtros(params)
    entregas = _aplicar_rango(EventoEntrega.objects.all(), "hora", f)
    if f["mesa"] is not None:
        entregas = entregas.filter(ocupacion__mesa_id=f["mesa"])
    if f["zona"] is not None:
        entregas = entregas.filter(ocupacion__zona_id=f["zona"])

    conteos = {
        fila["personal_id"]: fila
        for fila in entregas.values("personal_id").annotate(
            mesas=Count("ocupacion_id", distinct=True), entregas=Count("id")
        )
    }
    personal = Personal.objects.filter(pk__in=conteos.keys()) | Personal.objects.filter(activo=True)
    ranking = [
        {
            "personal_id": p.pk,
            "identificador": p.identificador,
            "nombre": p.nombre,
            "mesas_atendidas": conteos.get(p.pk, {}).get("mesas", 0),
            "entregas": conteos.get(p.pk, {}).get("entregas", 0),
        }
        for p in personal.distinct()
    ]
    ranking.sort(key=lambda r: (-r["mesas_atendidas"], r["nombre"].lower(), r["personal_id"]))
    maximo = ranking[0]["mesas_atendidas"] if ranking else 0
    top = [r for r in ranking if r["mesas_atendidas"] == maximo] if maximo > 0 else []
    return {
        "filtros": _descripcion_filtros(f),
        "maximo_mesas_atendidas": maximo,
        "empate": len(top) > 1,
        "empleados_top": top,
        "ranking": ranking,
    }


# --------------------------------------------------------------------------- #
# RF-12 — uso de mesas (más/menos usadas, horas pico)
# --------------------------------------------------------------------------- #
def uso_mesas(params):
    """
    Frecuencia = nº de ocupaciones iniciadas en el periodo. Se incluyen las mesas con 0 usos
    (para poder decir cuáles se usan MENOS). Las zonas de exclusión (caja) no cuentan como mesas.
    Hora pico = hora del día con más ocupaciones iniciadas (se devuelven todas las empatadas).
    """
    f = leer_filtros(params)
    mesas = Mesa.objects.exclude(zona__es_exclusion=True).select_related("zona")
    if f["mesa"] is not None:
        mesas = mesas.filter(pk=f["mesa"])
    if f["zona"] is not None:
        mesas = mesas.filter(zona_id=f["zona"])
    mesas = list(mesas)

    ocupaciones = _aplicar_rango(EventoOcupacion.objects.filter(mesa__in=mesas), "hora_inicio", f)
    ahora = timezone.now()
    por_mesa = defaultdict(lambda: {"frecuencia": 0, "segundos": 0.0, "cerradas": 0, "segundos_cerradas": 0.0})
    por_hora = Counter()
    for mesa_id, inicio, fin in ocupaciones.values_list("mesa_id", "hora_inicio", "hora_fin"):
        acc = por_mesa[mesa_id]
        acc["frecuencia"] += 1
        acc["segundos"] += ((fin or ahora) - inicio).total_seconds()
        if fin:
            acc["cerradas"] += 1
            acc["segundos_cerradas"] += (fin - inicio).total_seconds()
        por_hora[timezone.localtime(inicio).hour] += 1

    filas_mesa = []
    for m in mesas:
        acc = por_mesa[m.pk]
        filas_mesa.append({
            "mesa_id": m.pk,
            "mesa_numero": m.numero,
            "zona_id": m.zona_id,
            "zona": m.zona.nombre if m.zona else None,
            "frecuencia": acc["frecuencia"],
            "tiempo_ocupada_segundos": _redondear(acc["segundos"]),
            "duracion_promedio_segundos": _redondear(acc["segundos_cerradas"] / acc["cerradas"]) if acc["cerradas"] else None,
        })
    filas_mesa.sort(key=lambda r: (-r["frecuencia"], r["mesa_numero"]))

    def extremos(filas):
        if not filas:
            return [], []
        mayor = max(r["frecuencia"] for r in filas)
        menor = min(r["frecuencia"] for r in filas)
        return (
            [r for r in filas if r["frecuencia"] == mayor],
            [r for r in filas if r["frecuencia"] == menor],
        )

    mas, menos = extremos(filas_mesa)
    # Con datos, "más usadas" no debe listar mesas con 0 usos.
    if mas and mas[0]["frecuencia"] == 0:
        mas = []

    zonas = defaultdict(list)
    for r in filas_mesa:
        zonas[(r["zona_id"], r["zona"])].append(r)
    filas_zona = [
        {
            "zona_id": zid,
            "zona": nombre,
            "mesas": len(filas),
            "frecuencia": sum(r["frecuencia"] for r in filas),
            "tiempo_ocupada_segundos": _redondear(sum(r["tiempo_ocupada_segundos"] for r in filas)),
        }
        for (zid, nombre), filas in zonas.items()
    ]
    filas_zona.sort(key=lambda r: (-r["frecuencia"], r["zona"] or ""))

    pico = max(por_hora.values()) if por_hora else 0
    return {
        "filtros": _descripcion_filtros(f),
        "total_ocupaciones": sum(r["frecuencia"] for r in filas_mesa),
        "por_mesa": filas_mesa,
        "por_zona": filas_zona,
        "mas_usadas": mas,
        "menos_usadas": menos,
        "por_hora": [{"hora": h, "ocupaciones": por_hora.get(h, 0)} for h in range(24)],
        "horas_pico": [h for h in sorted(por_hora) if por_hora[h] == pico] if pico else [],
    }
