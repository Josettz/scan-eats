# restaurante_api/scaneats/services.py
"""
Reglas de negocio de los eventos que reporta el microservicio de visión.

Diseño: las vistas solo validan formato; aquí viven las reglas (RF-01, RF-02, RF-03,
RF-09, RF-15) para poder probarlas sin HTTP. Las peticiones son IDEMPOTENTES: si el
microservicio reintenta una petición cuya respuesta se perdió, se devuelve el evento
existente en vez de duplicarlo.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import APIException

from .models import EventoEntrega, EventoOcupacion, FusionMesas
from .permissions import GRUPO_SERVICIO

Estado = EventoOcupacion.Estado


class ConflictoNegocio(APIException):
    status_code = 409
    default_code = "conflicto"
    default_detail = "La operación entra en conflicto con el estado actual."


class ZonaExcluida(APIException):
    status_code = 422
    default_code = "zona_excluida"
    default_detail = "La mesa pertenece a una zona de exclusión: no se generan eventos sobre ella (RF-15)."


# --------------------------------------------------------------------------- #
# RF-09 — tiempo de espera
# --------------------------------------------------------------------------- #
def tiempo_espera_segundos(ocupacion):
    """
    RF-09: diferencia entre la ocupación y la PRIMERA entrega de esa ocupación.
    Si aún no hay entregas devuelve None. Es aritmética exacta sobre las marcas de tiempo
    (sin redondeos), por lo que el margen contra el cálculo manual es 0 s (≤ 1 s exigido).
    """
    primera = ocupacion.entregas.order_by("hora").values_list("hora", flat=True).first()
    if primera is None:
        return None
    return (primera - ocupacion.hora_inicio).total_seconds()


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def _validar_no_excluida(mesa):
    if mesa.zona_id and mesa.zona.es_exclusion:
        raise ZonaExcluida()


# --------------------------------------------------------------------------- #
# RF-01 — ocupación
# --------------------------------------------------------------------------- #
def registrar_ocupacion(mesa, hora_inicio=None, detectado_en=None, uid=None):
    """Devuelve (evento, creado). Idempotente por `uid` y por "una ocupación abierta por mesa"."""
    ahora = timezone.now()
    _validar_no_excluida(mesa)
    if uid:
        existente = EventoOcupacion.objects.filter(uid=uid).first()
        if existente:
            return existente, False
    with transaction.atomic():
        abierta = EventoOcupacion.objects.select_for_update().filter(mesa=mesa, hora_fin__isnull=True).first()
        if abierta:
            return abierta, False
        try:
            with transaction.atomic():  # savepoint: un choque de unicidad no invalida la transacción externa
                evento = EventoOcupacion.objects.create(
                    mesa=mesa,
                    zona=mesa.zona,
                    estado=Estado.OCUPADA,
                    hora_inicio=hora_inicio or ahora,
                    detectado_en=detectado_en or ahora,
                    uid=uid,
                )
        except IntegrityError:  # carrera con otra petición idéntica
            return EventoOcupacion.objects.get(mesa=mesa, hora_fin__isnull=True), False
    return evento, True


def cambiar_estado_ocupacion(mesa, estado, hora_fin=None):
    """Aplica una transición de la máquina de estados a la ocupación abierta de la mesa."""
    estado = Estado(estado)
    with transaction.atomic():
        ocupacion = EventoOcupacion.objects.select_for_update().filter(mesa=mesa, hora_fin__isnull=True).first()
        if ocupacion is None:
            ultima = mesa.ocupaciones.order_by("-hora_inicio").first()
            if estado == Estado.LIBRE and ultima is not None and ultima.estado == Estado.LIBRE:
                return ultima  # reintento de un cierre ya aplicado
            raise ConflictoNegocio(f"La mesa {mesa.numero} no tiene una ocupación abierta.")

        if estado == ocupacion.estado:
            return ocupacion
        if estado not in EventoOcupacion.TRANSICIONES[Estado(ocupacion.estado)]:
            raise ConflictoNegocio(f"Transición no permitida: {ocupacion.estado} -> {estado}.")

        ocupacion.estado = estado
        if estado == Estado.LIBRE:
            fin = hora_fin or timezone.now()
            if fin < ocupacion.hora_inicio:
                raise ConflictoNegocio("hora_fin no puede ser anterior a hora_inicio.")
            ocupacion.hora_fin = fin
        ocupacion.save()
        if estado == Estado.LIBRE:
            _cerrar_fusiones_completas(ocupacion)
    return ocupacion


# --------------------------------------------------------------------------- #
# RF-02 — entrega
# --------------------------------------------------------------------------- #
def registrar_entrega(mesa, personal, hora=None, ocupacion_id=None, metodo="", confianza=None, uid=None):
    """Devuelve (entrega, creada). Una ocupación admite varias entregas."""
    hora = hora or timezone.now()
    _validar_no_excluida(mesa)
    if uid:
        existente = EventoEntrega.objects.filter(uid=uid).first()
        if existente:
            return existente, False

    if ocupacion_id is not None:
        ocupacion = mesa.ocupaciones.filter(pk=ocupacion_id).first()
        if ocupacion is None:
            raise ConflictoNegocio(f"La ocupación {ocupacion_id} no pertenece a la mesa {mesa.numero}.")
    else:
        # La ocupación que contiene la hora de la entrega (la abierta, o una ya cerrada si el reporte llegó tarde).
        ocupacion = mesa.ocupaciones.filter(hora_inicio__lte=hora).order_by("-hora_inicio").first()
    if ocupacion is None or hora < ocupacion.hora_inicio or (ocupacion.hora_fin and hora > ocupacion.hora_fin):
        raise ConflictoNegocio(f"La mesa {mesa.numero} no tenía una ocupación activa en {hora.isoformat()}.")

    entrega = EventoEntrega.objects.create(
        ocupacion=ocupacion, personal=personal, hora=hora,
        metodo_identificacion=metodo or "", confianza=confianza, uid=uid,
    )
    return entrega, True


# --------------------------------------------------------------------------- #
# RF-03 — fusión (UN registro por fusión)
# --------------------------------------------------------------------------- #
def registrar_fusion(mesas, hora_evento=None, uid=None):
    """
    Devuelve (fusion, creada).

    * Mismo conjunto de mesas (o subconjunto) con una fusión abierta -> se devuelve esa fusión.
    * Conjunto que AMPLÍA una fusión abierta ({4,5} -> {4,5,6}) -> se actualiza el mismo registro.
    * En cualquier otro caso se crea un único registro para todas las mesas.
    """
    hora_evento = hora_evento or timezone.now()
    for mesa in mesas:
        _validar_no_excluida(mesa)
    if uid:
        existente = FusionMesas.objects.filter(uid=uid).first()
        if existente:
            return existente, False

    ocupaciones = []
    sin_ocupacion = []
    for mesa in mesas:
        abierta = mesa.ocupaciones.filter(hora_fin__isnull=True).first()
        (ocupaciones if abierta else sin_ocupacion).append(abierta or mesa)
    if sin_ocupacion:
        numeros = ", ".join(str(m.numero) for m in sin_ocupacion)
        raise ConflictoNegocio(f"Solo se pueden fusionar mesas ocupadas; sin ocupación abierta: {numeros}.")

    with transaction.atomic():
        abiertas = list(
            FusionMesas.objects.select_for_update()
            .filter(hora_fin__isnull=True, mesas__in=mesas)
            .distinct()
            .order_by("hora_evento", "pk")
        )
        if not abiertas:
            fusion = FusionMesas.objects.create(hora_evento=hora_evento, uid=uid)
            fusion.mesas.set(mesas)
            fusion.ocupaciones.set(ocupaciones)
            return fusion, True

        base, resto = abiertas[0], abiertas[1:]
        base.mesas.add(*mesas)
        base.ocupaciones.add(*ocupaciones)
        for otra in resto:  # fusiones que quedaron unidas por este evento
            base.mesas.add(*otra.mesas.all())
            base.ocupaciones.add(*otra.ocupaciones.all())
            otra.hora_fin = max(hora_evento, otra.hora_evento)
            otra.save(update_fields=["hora_fin"])
        return base, False


def finalizar_fusion(mesas, hora_fin=None):
    """Cierra las fusiones abiertas que involucren alguna de las mesas. Devuelve la lista cerrada."""
    hora_fin = hora_fin or timezone.now()
    cerradas = []
    with transaction.atomic():
        for fusion in FusionMesas.objects.select_for_update().filter(hora_fin__isnull=True, mesas__in=mesas).distinct():
            fusion.hora_fin = max(hora_fin, fusion.hora_evento)
            fusion.save(update_fields=["hora_fin"])
            cerradas.append(fusion)
    return cerradas


def _cerrar_fusiones_completas(ocupacion):
    """Si todas las ocupaciones de una fusión ya se liberaron, la fusión termina."""
    for fusion in ocupacion.fusiones.filter(hora_fin__isnull=True):
        if not fusion.ocupaciones.filter(hora_fin__isnull=True).exists():
            fin = max(fusion.ocupaciones.values_list("hora_fin", flat=True))
            fusion.hora_fin = max(fin, fusion.hora_evento)
            fusion.save(update_fields=["hora_fin"])


# --------------------------------------------------------------------------- #
# RF-13 — token de servicio
# --------------------------------------------------------------------------- #
def asegurar_usuario_servicio():
    """Crea (si falta) el usuario y el grupo del Microservicio de Visión. Sin contraseña utilizable."""
    User = get_user_model()
    usuario, creado = User.objects.get_or_create(
        username=settings.SERVICIO_VISION_USERNAME,
        defaults={"is_staff": False, "is_superuser": False, "first_name": "Microservicio de Visión"},
    )
    if creado:
        usuario.set_unusable_password()
        usuario.save(update_fields=["password"])
    grupo, _ = Group.objects.get_or_create(name=GRUPO_SERVICIO)
    usuario.groups.add(grupo)
    return usuario


def emitir_token_servicio():
    """Emite un token nuevo e invalida el anterior (rotación). Devuelve la clave en claro una sola vez."""
    usuario = asegurar_usuario_servicio()
    Token.objects.filter(user=usuario).delete()
    return usuario, Token.objects.create(user=usuario)
