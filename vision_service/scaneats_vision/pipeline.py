# vision_service/scaneats_vision/pipeline.py
"""
Orquestador end-to-end del microservicio de visión y punto de entrada CLI.

Flujo por fotograma (`ScanEatsPipeline.process`):

    detecciones (YOLOv8) ─▶ tracking (SORT/DeepSORT) ─▶ asignación a zonas por overlap+centroide (geometry)
        ├─ personas en la zona de EXCLUSIÓN (caja): se descartan, ni siquiera se rastrean        (RF-15)
        ├─ clasificación personal/cliente en cascada vestimenta → comportamiento              (RF-08)
        ├─ clientes + objetos por mesa ─▶ máquina de estados de ocupación                      (RF-07, RNF-01)
        │       └─ transiciones ─▶ API: ocupación / cambio de estado                           (RF-01)
        ├─ mesas ocupadas adyacentes ─▶ fusión con histéresis ─▶ API                           (RF-03)
        └─ sustracción de fondo + mesero presente ─▶ entrega ─▶ atribución al mesero ─▶ API   (RF-02, RF-08)

`process()` recibe detecciones YA calculadas: toda la lógica es testeable sin cámara, GPU ni pesos de modelo.
Solo `run_video()` (OpenCV/YOLO) es "pesado" y se importa de forma perezosa.

Uso:
    python -m scaneats_vision.pipeline --source video.mp4 --config config/zonas_ejemplo.json \\
        --api-url http://localhost:8000 --token <VISION_API_TOKEN>
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

from .api_client import ApiClient, nuevo_uid
from .config_loader import VisionConfig, load_config
from .delivery_detector import DeliveryConfig, DeliveryDetector
from .dispatcher import EventDispatcher
from .geometry import BBox, assign_to_zone
from .occupancy_engine import OccupancyConfig, OccupancyManager, OccupancyTransition, TableState
from .settings import ServiceSettings
from .staff_classifier import (
    BehaviorAnalyzer, BehaviorConfig, CascadeConfig, Classification, ClothingClassifier, Method, Role, StaffClassifier,
)
from .table_fusion import FusionConfig, FusionEventKind, TableFusionDetector, TableObservation

log = logging.getLogger("scaneats_vision")


@dataclass(frozen=True)
class TrackedPerson:
    track_id: int
    bbox: BBox  # coordenadas normalizadas


@dataclass
class PipelineStats:
    frames: int = 0
    ocupaciones: int = 0
    entregas: int = 0
    entregas_sin_atribuir: int = 0
    fusiones: int = 0
    clasificaciones: int = 0


@dataclass
class _PendingDelivery:
    table: int
    at: float
    created: float
    wall_at: datetime
    uid: str


class ScanEatsPipeline:
    def __init__(
        self,
        config: VisionConfig,
        publisher,
        *,
        occupancy_config: Optional[OccupancyConfig] = None,
        fusion_config: Optional[FusionConfig] = None,
        delivery_config: Optional[DeliveryConfig] = None,
        behavior_config: Optional[BehaviorConfig] = None,
        cascade_config: Optional[CascadeConfig] = None,
        color_ratio_fn: Optional[Callable] = None,
        clip_recorder=None,
        min_zone_overlap: float = 0.25,
        attribution_window_s: float = 15.0,
        attribution_timeout_s: float = 20.0,
        classification_report_interval_s: float = 30.0,
    ):
        self.config = config
        self.publisher = publisher
        self.min_zone_overlap = min_zone_overlap
        self.attribution_window_s = attribution_window_s
        self.attribution_timeout_s = attribution_timeout_s
        self.classification_report_interval_s = classification_report_interval_s

        self.occupancy = OccupancyManager([t.numero for t in config.tables], occupancy_config)
        self.fusion = TableFusionDetector(config.adjacency, fusion_config)
        self.delivery = DeliveryDetector(delivery_config)
        if color_ratio_fn is None:
            from .color_utils import torso_color_ratios  # perezoso: numpy/OpenCV solo con video real

            color_ratio_fn = torso_color_ratios
        self.classifier = StaffClassifier(
            ClothingClassifier(config.staff, color_ratio_fn), BehaviorAnalyzer(behavior_config), cascade_config
        )
        self.clip_recorder = clip_recorder
        if clip_recorder is not None:
            clip_recorder.on_ready = self._subir_clip

        self.stats = PipelineStats()
        self._recent_staff: dict[int, dict[int, float]] = defaultdict(dict)  # mesa -> {track: última vez}
        self._last_class: dict[int, Classification] = {}
        self._last_seen: dict[int, float] = {}
        self._reported: dict[int, tuple] = {}
        self._reported_at: dict[int, float] = {}
        self._pending: list[_PendingDelivery] = []
        self._table_customers: dict[int, tuple] = {}

    # ------------------------------------------------------------------ #
    # Utilidades
    # ------------------------------------------------------------------ #
    @staticmethod
    def _wall_at(now: float, wall: datetime, t: float) -> datetime:
        """Convierte un instante del reloj del pipeline `t` a hora de pared (wall corresponde a `now`)."""
        return wall - timedelta(seconds=now - t)

    def _subir_clip(self, tipo, uid, path, wall, duracion_s) -> None:
        self.publisher.submit("subir_clip", tipo=tipo, evento_uid=uid, ruta_clip=path, timestamp=wall, duracion_s=duracion_s)

    def _pedir_clip(self, tipo: str, uid: str, now: float, wall: datetime) -> None:
        if self.clip_recorder is not None:
            self.clip_recorder.request(tipo, uid, now, wall)

    def on_frame(self, now: float, frame) -> None:
        """Alimenta el búfer de clips (llamar con cada fotograma procesado si hay grabación activa)."""
        if self.clip_recorder is not None:
            self.clip_recorder.add_frame(now, frame)

    # ------------------------------------------------------------------ #
    # Procesamiento de un fotograma
    # ------------------------------------------------------------------ #
    def process(
        self,
        now: float,
        wall: datetime,
        persons: Sequence[TrackedPerson],
        objects: Sequence[BBox] = (),
        zone_change: Optional[dict] = None,
        crops: Optional[dict] = None,
    ) -> None:
        """
        now: reloj del pipeline en segundos (monótono; en archivos, la posición del video).
        wall: hora de pared correspondiente a `now` (para timestamps hacia el backend).
        persons/objects: cajas en coordenadas NORMALIZADAS. zone_change: {mesa: razón de primer plano | None}.
        crops: {track_id: recorte BGR de la persona} (solo los que se quieran clasificar por vestimenta).
        """
        crops = crops or {}
        self.stats.frames += 1
        zonas = self.config.all_zones

        # 1) Personas -> zona. La caja queda fuera del monitoreo por privacidad (RF-15).
        en_mesa: dict[int, list] = defaultdict(list)
        for p in persons:
            match = assign_to_zone(p.bbox, zonas, self.min_zone_overlap)
            if match is not None and match.zone.is_exclusion:
                continue
            mesa = match.zone.id if match else None
            self.classifier.behavior.observe(p.track_id, now, mesa)
            clasif = self.classifier.classify(p.track_id, now, crops.get(p.track_id))
            self._last_class[p.track_id] = clasif
            self._last_seen[p.track_id] = now
            self._reportar_clasificacion(p.track_id, clasif, mesa, now, wall)
            if mesa is not None:
                en_mesa[mesa].append((p, clasif))

        # 2) Objetos sobre las mesas.
        objetos_mesa: Counter = Counter()
        for caja in objects:
            match = assign_to_zone(caja, zonas, self.min_zone_overlap)
            if match is not None and not match.zone.is_exclusion:
                objetos_mesa[match.zone.id] += 1

        # 3) Ocupación: solo los NO-personal cuentan como clientes.
        observaciones = {}
        for t in self.config.tables:
            gente = en_mesa.get(t.numero, [])
            clientes = [p for p, c in gente if c.role != Role.STAFF]
            for p, c in gente:
                if c.role == Role.STAFF:
                    self.delivery.note_staff_presence(t.numero, now)
                    self._recent_staff[t.numero][p.track_id] = now
            observaciones[t.numero] = (len(clientes), objetos_mesa[t.numero])
            self._table_customers[t.numero] = (len(clientes), tuple(p.bbox.centroid for p in clientes))
        for tr in self.occupancy.update(now, observaciones):
            self._on_transition(tr, now, wall)

        # 4) Fusión de mesas.
        self._procesar_fusion(now, wall)

        # 5) Entregas.
        for t in self.config.tables:
            ocupada = self.occupancy.state(t.numero) != TableState.LIBRE
            cambio = (zone_change or {}).get(t.numero)
            det = self.delivery.update(t.numero, now, cambio, ocupada)
            if det is not None:
                self._nueva_entrega(t.numero, det.at, now, wall)
        self._resolver_pendientes(now, wall)
        self._podar(now)

    # ------------------------------------------------------------------ #
    # Ocupación (RF-01, RF-07)
    # ------------------------------------------------------------------ #
    def _on_transition(self, tr: OccupancyTransition, now: float, wall: datetime) -> None:
        mesa = tr.table_id
        log.info("Mesa %s: %s -> %s (%s)", mesa, tr.previous.value, tr.current.value, tr.reason)
        if tr.current == TableState.OCUPADA and tr.previous == TableState.LIBRE:
            uid = nuevo_uid()
            self.publisher.submit(
                "reportar_ocupacion", mesa_numero=mesa,
                hora_inicio=self._wall_at(now, wall, tr.since), detectado_en=self._wall_at(now, wall, tr.at), uid=uid,
            )
            self.stats.ocupaciones += 1
            self._pedir_clip("ocupacion", uid, now, wall)
        elif tr.current == TableState.LIBRE:
            self.publisher.submit(
                "cambiar_estado_ocupacion", mesa_numero=mesa, estado="LIBRE", hora_fin=self._wall_at(now, wall, tr.since)
            )
            self._limpiar_mesa(mesa)
        else:  # OCUPADA (regreso) o POSIBLEMENTE_LIBRE
            self.publisher.submit("cambiar_estado_ocupacion", mesa_numero=mesa, estado=tr.current.value)

    def _limpiar_mesa(self, mesa: int) -> None:
        self.delivery.reset(mesa)
        self._recent_staff.pop(mesa, None)
        self._pending = [p for p in self._pending if p.table != mesa]

    # ------------------------------------------------------------------ #
    # Fusión (RF-03)
    # ------------------------------------------------------------------ #
    def _procesar_fusion(self, now: float, wall: datetime) -> None:
        obs = {}
        for t in self.config.tables:
            engine = self.occupancy.engines[t.numero]
            clientes, puntos = self._table_customers.get(t.numero, (0, ()))
            obs[t.numero] = TableObservation(
                occupied=engine.state == TableState.OCUPADA, customers=clientes, capacity=t.capacidad,
                occupied_since=engine.occupied_since, customer_points=puntos,
            )
        for ev in self.fusion.update(now, obs):
            mesas = sorted(ev.tables)
            if ev.kind == FusionEventKind.CONFIRMED:
                uid = nuevo_uid()
                log.info("Fusión de mesas %s confirmada", mesas)
                self.publisher.submit("reportar_fusion", mesas=mesas, hora_evento=self._wall_at(now, wall, ev.at), uid=uid)
                self.stats.fusiones += 1
                self._pedir_clip("fusion", uid, now, wall)
            else:
                log.info("Fusión de mesas %s terminada (%s)", mesas, ev.reason)
                self.publisher.submit("finalizar_fusion", mesas=mesas, hora_fin=self._wall_at(now, wall, ev.at))

    # ------------------------------------------------------------------ #
    # Personal (RF-08)
    # ------------------------------------------------------------------ #
    def _reportar_clasificacion(self, track_id: int, c: Classification, mesa, now: float, wall: datetime) -> None:
        if c.role != Role.STAFF:
            return
        clave = (c.identificador, c.method)
        if self._reported.get(track_id) == clave:
            return
        if now - self._reported_at.get(track_id, -1e9) < self.classification_report_interval_s and track_id in self._reported:
            return  # evita ráfagas si la confianza oscila alrededor del umbral
        self._reported[track_id], self._reported_at[track_id] = clave, now
        self.stats.clasificaciones += 1
        self.publisher.submit(
            "reportar_personal_clasificado", metodo=c.method.value, confianza=c.confidence,
            personal_identificador=c.identificador, track_id=track_id, mesa_numero=mesa, timestamp=wall,
        )

    # ------------------------------------------------------------------ #
    # Entregas (RF-02) y atribución al mesero (RF-08)
    # ------------------------------------------------------------------ #
    def _candidatos(self, mesa: int, now: float) -> list[tuple[int, Classification]]:
        """Personal visto en la mesa dentro de la ventana de atribución, del más reciente al más antiguo."""
        vistos = self._recent_staff.get(mesa, {})
        recientes = sorted(
            ((t, seen) for t, seen in vistos.items() if now - seen <= self.attribution_window_s),
            key=lambda ts: ts[1], reverse=True,
        )
        return [(t, self._last_class[t]) for t, _ in recientes if t in self._last_class]

    def _identidad(self, track_id: int, c: Classification):
        ident = c.identificador or self.classifier.remembered(track_id)
        return ident, (c.method if c.method != Method.NINGUNO else Method.COMPORTAMIENTO)

    def _atribuir(self, mesa: int, now: float):
        """Mejor (identificador, método, confianza) entre el personal cercano, o None si nadie está identificado."""
        mejores = []
        for track_id, c in self._candidatos(mesa, now):
            ident, metodo = self._identidad(track_id, c)
            if ident:
                # Vestimenta pesa más que comportamiento; luego, la confianza.
                mejores.append(((metodo == Method.VESTIMENTA, c.confidence), ident, metodo, c.confidence))
        if not mejores:
            return None
        _, ident, metodo, conf = max(mejores, key=lambda m: m[0])
        return ident, metodo, conf

    def _nueva_entrega(self, mesa: int, at: float, now: float, wall: datetime) -> None:
        pend = _PendingDelivery(mesa, at, now, self._wall_at(now, wall, at), nuevo_uid())
        if not self._intentar_reportar(pend, now):
            self._pending.append(pend)  # el mesero se ve pero aún no se sabe QUIÉN es: se espera un poco

    def _intentar_reportar(self, pend: _PendingDelivery, now: float) -> bool:
        res = self._atribuir(pend.table, now)
        if res is None:
            return False
        ident, metodo, conf = res
        log.info("Entrega en mesa %s por %s (%s, %.2f)", pend.table, ident, metodo.value, conf)
        self.publisher.submit(
            "reportar_entrega", mesa_numero=pend.table, personal_identificador=ident, hora=pend.wall_at,
            metodo=metodo.value, confianza=conf, uid=pend.uid,
        )
        self.stats.entregas += 1
        self._pedir_clip("entrega", pend.uid, pend.at, pend.wall_at)
        return True

    def _resolver_pendientes(self, now: float, wall: datetime) -> None:
        restantes = []
        for pend in self._pending:
            if self._intentar_reportar(pend, now):
                continue
            if now - pend.created >= self.attribution_timeout_s:
                # Nunca se inventa un mesero: se registra el hueco para poder medirlo y calibrar la vestimenta.
                self.stats.entregas_sin_atribuir += 1
                log.warning("Entrega en mesa %s SIN atribuir: no se identificó al mesero en %.0f s", pend.table,
                            self.attribution_timeout_s)
                continue
            restantes.append(pend)
        self._pending = restantes

    def _podar(self, now: float, ttl_s: float = 600.0) -> None:
        """Libera memoria de personas que no se ven hace tiempo (el tracker les asignaría otro id)."""
        if self.stats.frames % 200:
            return
        for tid in [t for t, seen in self._last_seen.items() if now - seen > ttl_s]:
            for d in (self._last_seen, self._last_class, self._reported, self._reported_at):
                d.pop(tid, None)
            self.classifier.forget(tid)
            for vistos in self._recent_staff.values():
                vistos.pop(tid, None)

    def close(self) -> None:
        if self.clip_recorder is not None:
            self.clip_recorder.flush()
        if hasattr(self.publisher, "close"):
            self.publisher.close()


# --------------------------------------------------------------------------- #
# Ejecución sobre video real (OpenCV + YOLOv8): importaciones perezosas
# --------------------------------------------------------------------------- #
def _recorte(frame, bbox: BBox):
    alto, ancho = frame.shape[:2]
    x1, y1, x2, y2 = int(bbox.x1 * ancho), int(bbox.y1 * alto), int(bbox.x2 * ancho), int(bbox.y2 * alto)
    return frame[max(y1, 0):max(y2, 0), max(x1, 0):max(x2, 0)]


def run_video(pipeline: ScanEatsPipeline, settings: ServiceSettings, source: str, *, show: bool = False,
              start_wall: Optional[datetime] = None, max_frames: Optional[int] = None, detector=None, tracker=None,
              annotator=None, clips_anotados: bool = False, analisis=None) -> None:
    import cv2  # perezoso

    from .delivery_detector import ForegroundExtractor
    from .detector import YoloDetector
    from .tracking import build_tracker

    detector = detector or YoloDetector(settings.yolo_model, settings.detection_confidence, imgsz=settings.yolo_imgsz)
    tracker = tracker or build_tracker(settings.tracker, **pipeline.config.tracker)
    fg = ForegroundExtractor()

    es_archivo = os.path.isfile(source)
    cap = cv2.VideoCapture(int(source) if str(source).isdigit() else source)
    if not cap.isOpened():
        raise RuntimeError(f"No se pudo abrir la fuente de video: {source}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    if analisis is not None:
        analisis.resolucion = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        analisis.duracion_video = cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps if es_archivo else 0.0
    inicio_proceso = time.perf_counter()
    salto = max(1, round(fps / settings.process_fps)) if es_archivo else 1
    inicio_wall = start_wall or datetime.now(timezone.utc)
    ultimo = -1e9
    idx = procesados = fallos = 0
    try:
        while max_frames is None or procesados < max_frames:
            if es_archivo and (idx + 1) % salto:
                idx += 1
                if not cap.grab():  # fotograma que no se procesa: se salta sin decodificarlo (mucho más rápido)
                    break
                continue
            ok, frame = cap.read()
            if not ok:
                if es_archivo:
                    break
                fallos += 1  # RTSP: reconexión con espera creciente
                if fallos > 10:
                    raise RuntimeError("Se perdió la conexión con la cámara.")
                log.warning("Fotograma perdido; reintentando conexión (%d/10)...", fallos)
                cap.release()
                time.sleep(min(2 ** fallos, 30))
                cap = cv2.VideoCapture(source)
                continue
            fallos = 0
            idx += 1
            if es_archivo:
                now = idx / fps
                wall = inicio_wall + timedelta(seconds=now)
            else:
                now = time.monotonic()
                if now - ultimo < 1.0 / settings.process_fps:
                    continue
                ultimo = now
                wall = datetime.now(timezone.utc)

            dets = detector.detect(frame)
            pistas = tracker.update([d.bbox for d in dets if d.is_person], frame)
            cambio = fg.change_ratios(frame, pipeline.config.surface_zones, [t.bbox for t in pistas])
            recortes = {t.track_id: _recorte(frame, t.bbox) for t in pistas}
            if annotator is not None:
                annotator.begin(now)
            pipeline.process(
                now, wall, [TrackedPerson(t.track_id, t.bbox) for t in pistas],
                [d.bbox for d in dets if not d.is_person], cambio, recortes,
            )
            anotada = annotator.draw(frame, now, dets, pistas, pipeline, cambio) if annotator is not None else None
            # Los clips de evidencia llevan dibujadas las detecciones (cajas de YOLO, roles, estado de las mesas).
            pipeline.on_frame(now, anotada if (clips_anotados and anotada is not None) else frame)
            if analisis is not None:
                analisis.observar(now, dets, pistas, pipeline, anotada)
            procesados += 1
            if show:
                cv2.imshow("ScanEats", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    finally:
        cap.release()
        pipeline.close()
        if annotator is not None:
            annotator.close()
        if analisis is not None:
            analisis.tiempo_proceso_s = time.perf_counter() - inicio_proceso
        if show:
            cv2.destroyAllWindows()


class _LogApi:
    """API "de mentira" para --dry-run: imprime lo que se enviaría."""

    def __getattr__(self, nombre):
        def llamada(**kwargs):
            log.info("[dry-run] %s %s", nombre, {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in kwargs.items()})
        return llamada


def main(argv=None) -> int:
    base = ServiceSettings.from_env()
    ap = argparse.ArgumentParser(description="ScanEats — microservicio de visión")
    ap.add_argument("--source", default=base.video_source, help="archivo de video, URL RTSP o índice de webcam")
    ap.add_argument("--config", default=base.config_path, help="JSON de zonas/mesas (ver config/zonas_ejemplo.json)")
    ap.add_argument("--api-url", default=base.api_base_url)
    ap.add_argument("--token", default=base.api_token, help="token de servicio (o VISION_API_TOKEN)")
    ap.add_argument("--model", default=base.yolo_model)
    ap.add_argument("--fps", type=float, default=base.process_fps, help="fotogramas por segundo a procesar")
    ap.add_argument("--tracker", choices=["sort", "deepsort"], default=base.tracker)
    ap.add_argument("--start-time", help="hora de pared del inicio de un archivo grabado (ISO 8601); por defecto, ahora")
    ap.add_argument("--clips", action="store_true", default=base.clips_enabled, help="recortar y subir clips de evidencia")
    ap.add_argument("--clips-crudos", action="store_true", help="los clips llevan el video original, sin dibujar detecciones")
    ap.add_argument("--show", action="store_true", help="ventana con el video")
    ap.add_argument("--informe", metavar="CARPETA", help="analiza TODO el video y escribe informe.html/json, video anotado y fotogramas clave")
    ap.add_argument("--annotate", metavar="SALIDA.mp4", help="guarda un video con las detecciones de YOLO, roles y estados dibujados")
    ap.add_argument("--dry-run", action="store_true", help="no llamar al backend; solo registrar lo que se enviaría")
    ap.add_argument("--max-frames", type=int)
    ap.add_argument("--log-level", default=base.log_level)
    args = ap.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.source:
        ap.error("Indique --source (o VIDEO_SOURCE).")

    # TODO: reemplazar por la API de Django -> VisionConfig.from_api_payload(ApiClient(...).obtener_configuracion())
    config = load_config(args.config)

    if args.dry_run:
        publisher = EventDispatcher(_LogApi(), sync=True)
    else:
        api = ApiClient(args.api_url, args.token)
        api.validar_token()  # falla rápido si el token es inválido (RF-13)
        publisher = EventDispatcher(api)

    annotator = analisis = None
    clips_anotados = bool(args.clips and not args.clips_crudos)
    if args.informe:
        os.makedirs(args.informe, exist_ok=True)
        args.annotate = args.annotate or os.path.join(args.informe, "deteccion_yolo.mp4")
    if args.annotate or clips_anotados or args.informe:
        from .annotate import Annotator, EventTap

        publisher = EventTap(publisher)
        annotator = Annotator(args.annotate, args.fps, publisher)
        if args.informe:
            from .analysis import AnalisisVideo

            analisis = AnalisisVideo(config, publisher, args.fps, args.informe)
    settings = ServiceSettings(**{**base.__dict__, "yolo_model": args.model, "process_fps": args.fps,
                                  "tracker": args.tracker, "clips_enabled": args.clips})
    grabador = None
    if args.clips:
        from .clip_recorder import ClipRecorder

        grabador = ClipRecorder(on_ready=lambda **kw: None, pre_s=settings.clip_pre_s, post_s=settings.clip_post_s,
                                fps=args.fps, out_dir=settings.clip_dir)
    hueco = max(1.0, 1.5 / args.fps)  # peor hueco esperado entre fotogramas procesados
    occ = OccupancyConfig(max_frame_gap_s=hueco, presence_gap_tolerance_s=max(2.5, hueco * 1.5))
    pipeline = ScanEatsPipeline(config, publisher, occupancy_config=occ, clip_recorder=grabador,
                                delivery_config=DeliveryConfig(**config.delivery),
                                behavior_config=BehaviorConfig(**config.behavior),
                                cascade_config=CascadeConfig(**config.cascade))
    inicio = datetime.fromisoformat(args.start_time) if args.start_time else None
    run_video(pipeline, settings, args.source, show=args.show, start_wall=inicio, max_frames=args.max_frames,
              annotator=annotator, clips_anotados=clips_anotados, analisis=analisis)
    if analisis is not None:
        resumen = analisis.escribir(video_rel=os.path.basename(args.annotate))
        d = resumen["demanda"]
        log.info("Informe: %s (demanda %s: máx. %d de %d mesas a la vez)", os.path.join(args.informe, "informe.html"),
                 d["nivel"].upper(), d["maximo_mesas_ocupadas_a_la_vez"], d["mesas_monitoreadas"])
    log.info("Fin. %s", pipeline.stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
