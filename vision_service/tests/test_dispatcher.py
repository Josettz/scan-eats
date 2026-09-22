# vision_service/tests/test_dispatcher.py
import threading
import time

from scaneats_vision.api_client import ApiAuthError, ApiError, ApiUnavailable
from scaneats_vision.dispatcher import EventDispatcher


class ApiFalsa:
    def __init__(self, fallos=()):
        self.llamadas = []
        self.fallos = list(fallos)  # excepciones a lanzar, en orden, antes de tener éxito

    def evento(self, **kw):
        if self.fallos:
            raise self.fallos.pop(0)
        self.llamadas.append(kw)


def esperar(cond, timeout=3.0):
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_modo_sincrono_ejecuta_en_linea():
    api = ApiFalsa()
    d = EventDispatcher(api, sync=True)
    d.submit("evento", n=1)
    assert api.llamadas == [{"n": 1}] and d.enviados == 1


def test_modo_asincrono_conserva_el_orden():
    api = ApiFalsa()
    d = EventDispatcher(api)
    for i in range(50):
        d.submit("evento", n=i)
    d.close()
    assert [c["n"] for c in api.llamadas] == list(range(50))  # la ocupación siempre antes que sus entregas


def test_si_el_backend_cae_el_evento_se_reintenta_hasta_lograrlo():
    api = ApiFalsa(fallos=[ApiUnavailable("caído"), ApiUnavailable("caído")])
    d = EventDispatcher(api, retry_wait_s=0.0, sleep=lambda s: None)
    d.submit("evento", n=1)
    d.submit("evento", n=2)
    assert esperar(lambda: len(api.llamadas) == 2)
    assert [c["n"] for c in api.llamadas] == [1, 2]  # no se perdió ni se desordenó nada
    d.close()


def test_los_errores_del_negocio_se_descartan_sin_bloquear_la_cola():
    api = ApiFalsa(fallos=[ApiError("conflicto", 409)])
    d = EventDispatcher(api)
    d.submit("evento", n=1)  # rechazado por el backend
    d.submit("evento", n=2)
    d.close()
    assert [c["n"] for c in api.llamadas] == [2] and d.descartados == 1


def test_token_rechazado_se_descarta_y_se_registra():
    api = ApiFalsa(fallos=[ApiAuthError("403", 403)])
    d = EventDispatcher(api, sync=True)
    d.submit("evento", n=1)
    assert d.descartados == 1 and api.llamadas == []


def test_un_error_inesperado_no_tumba_el_hilo():
    api = ApiFalsa(fallos=[RuntimeError("bug")])
    d = EventDispatcher(api)
    d.submit("evento", n=1)
    d.submit("evento", n=2)
    d.close()
    assert [c["n"] for c in api.llamadas] == [2]


def test_cola_llena_descarta_el_evento_mas_antiguo():
    liberar = threading.Event()

    class Lenta:
        llamadas = []

        def evento(self, **kw):
            liberar.wait(2)
            self.llamadas.append(kw["n"])

    api = Lenta()
    d = EventDispatcher(api, max_queue=3)
    for i in range(8):
        d.submit("evento", n=i)
    liberar.set()
    d.close()
    assert d.descartados >= 1
    assert api.llamadas[-1] == 7  # lo más reciente se conserva


def test_sin_reintento_en_modo_sincrono_si_el_backend_esta_caido():
    d = EventDispatcher(ApiFalsa(fallos=[ApiUnavailable("caído")]), sync=True)
    d.submit("evento", n=1)  # no debe quedarse en bucle
    assert d.descartados == 1
