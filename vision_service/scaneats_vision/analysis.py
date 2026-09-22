# vision_service/scaneats_vision/analysis.py
"""
Análisis de un video COMPLETO y su informe (HTML + JSON).

    python -m scaneats_vision.pipeline --source video.mp4 --config config/zonas_video.json --dry-run --informe informe_video/

Qué produce en la carpeta indicada:
    informe.html          informe legible: qué detectó YOLO, ocupación por mesa, entregas, personal y NIVEL DE DEMANDA
    informe.json          los mismos números, para procesarlos
    deteccion_yolo.mp4    el video con las detecciones dibujadas (H.264)
    fotogramas/*.jpg      fotogramas anotados en los momentos clave (ocupación, entrega, liberación)

El nivel de demanda (baja / normal / alta) sale de cuántas mesas estuvieron ocupadas A LA VEZ frente al total de mesas
monitoreadas y se compara con las fichas de observación del documento de requerimientos (Anexo A). Un video de
poca gente NO es representativo de la hora pico: el informe lo dice y advierte que los promedios tienen muestra pequeña.
⚠ Los umbrales de demanda son una primera regla, PENDIENTE DE CALIBRAR con más video del local.
"""
from __future__ import annotations

import html
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from statistics import mean
from typing import Optional

from .staff_classifier import Role

# --- Referencias de las fichas de observación del documento (Encebollados X, 12 mesas) -------------------------
FICHAS = {
    "baja": {"espera_min": 1.4, "descripcion": "nunca más de 3 mesas ocupadas a la vez", "ficha": "A.1 (11/09, 13:00-14:00)"},
    "normal": {"espera_min": 5.2, "descripcion": "ocupación fluida, pico de 3-4 mesas por mesero", "ficha": "A.2 (10/09, 09:30-10:30)"},
    "alta": {"espera_min": 11.9, "descripcion": "las 12 mesas ocupadas casi de forma continua", "ficha": "A.3 (12/09, 11:00-12:00)"},
}
# Regla (PENDIENTE DE CALIBRAR): fracción de mesas ocupadas simultáneamente.
UMBRAL_BAJA = 0.25   # <= 25 % de las mesas a la vez  (la ficha de baja demanda: 3 de 12 = 25 %)
UMBRAL_ALTA = 0.75   # >= 75 % de las mesas a la vez  (la ficha de alta demanda: 12 de 12)
MUESTRA_PEQUENA = 10  # menos ocupaciones que esto: los promedios no son representativos


@dataclass
class _Serie:
    t: list = field(default_factory=list)
    personas: list = field(default_factory=list)
    mesas: list = field(default_factory=list)


class AnalisisVideo:
    """Acumula estadísticas por fotograma procesado. No toca el backend ni la lógica del pipeline."""

    def __init__(self, config, tap, fps_proceso: float, carpeta: str, max_fotogramas: int = 14):
        self.config, self.tap, self.fps = config, tap, fps_proceso
        self.carpeta = carpeta
        self.max_fotogramas = max_fotogramas
        os.makedirs(os.path.join(carpeta, "fotogramas"), exist_ok=True)
        self.frames = 0
        self.t_ini: Optional[float] = None
        self.t_fin = 0.0
        self.detecciones_persona = 0
        self.confianzas: list = []
        self.detecciones_objeto = 0
        self.frames_con_persona = 0
        self.max_personas = 0
        self.serie = _Serie()
        self.mesas_simultaneas = Counter()  # nº de mesas ocupadas -> nº de fotogramas
        self.tracks: dict = {}  # id -> [primera, ultima]
        self.roles: dict = defaultdict(Counter)  # id -> Counter(Role)
        self._registro_visto = 0
        self.fotogramas: list = []  # (t, etiqueta, archivo)
        self.resolucion = (0, 0)
        self.duracion_video = 0.0
        self.tiempo_proceso_s = 0.0

    # ------------------------------------------------------------------ #
    def observar(self, now: float, dets, pistas, pipeline, imagen) -> None:
        import cv2

        if self.t_ini is None:
            self.t_ini = now
        self.t_fin = now
        self.frames += 1
        personas = [d for d in dets if d.is_person]
        self.detecciones_persona += len(personas)
        self.confianzas += [d.confidence for d in personas]
        self.detecciones_objeto += len(dets) - len(personas)
        self.frames_con_persona += 1 if personas else 0
        self.max_personas = max(self.max_personas, len(personas))
        ocupadas = sum(1 for t in self.config.tables if pipeline.occupancy.state(t.numero).value != "LIBRE")
        self.mesas_simultaneas[ocupadas] += 1
        if self.frames % max(1, int(self.fps * 2)) == 0:  # una muestra cada ~2 s para las gráficas
            self.serie.t.append(now)
            self.serie.personas.append(len(personas))
            self.serie.mesas.append(ocupadas)
        for p in pistas:
            a = self.tracks.setdefault(p.track_id, [now, now])
            a[1] = now
            c = pipeline._last_class.get(p.track_id)
            self.roles[p.track_id][c.role if c else Role.UNKNOWN] += 1

        # fotograma clave cada vez que aparece un evento importante nuevo
        nuevos = self.tap.registro[self._registro_visto:]
        self._registro_visto = len(self.tap.registro)
        for t, metodo, kw in nuevos:
            etiqueta = {
                "reportar_ocupacion": f"ocupación mesa {kw.get('mesa_numero')}",
                "reportar_entrega": f"entrega mesa {kw.get('mesa_numero')} · {kw.get('personal_identificador')}",
                "reportar_fusion": f"fusión {kw.get('mesas')}",
            }.get(metodo)
            if metodo == "cambiar_estado_ocupacion" and kw.get("estado") == "LIBRE":
                etiqueta = f"mesa {kw.get('mesa_numero')} libre"
            if etiqueta and len(self.fotogramas) < self.max_fotogramas:
                nombre = f"{len(self.fotogramas) + 1:02d}_{int(t)}s.jpg"
                cv2.imwrite(os.path.join(self.carpeta, "fotogramas", nombre), imagen, [cv2.IMWRITE_JPEG_QUALITY, 82])
                self.fotogramas.append((t, etiqueta, nombre))

    # ------------------------------------------------------------------ #
    def resumen(self) -> dict:
        registro = self.tap.registro
        n_mesas = len(self.config.tables)
        duracion = max(self.t_fin - (self.t_ini or 0.0), 1e-9)

        # Las horas de pared de los eventos se convierten a "segundos de video" con una referencia común.
        ref = None
        for t, metodo, kw in registro:
            if metodo == "reportar_ocupacion":
                ref = kw["detectado_en"] - timedelta(seconds=t)
                break

        def a_video(dt, respaldo):
            return (dt - ref).total_seconds() if (ref is not None and dt is not None) else respaldo

        # ocupaciones a partir de los eventos enviados al backend
        abiertas, ocupaciones = {}, []
        for t, metodo, kw in registro:
            mesa = kw.get("mesa_numero")
            if metodo == "reportar_ocupacion":
                abiertas[mesa] = {"mesa": mesa, "inicio": a_video(kw["hora_inicio"], t), "fin": None, "entregas": []}
            elif metodo == "cambiar_estado_ocupacion" and kw.get("estado") == "LIBRE" and mesa in abiertas:
                o = abiertas.pop(mesa)
                o["fin"] = a_video(kw.get("hora_fin"), t)
                ocupaciones.append(o)
            elif metodo == "reportar_entrega" and mesa in abiertas:
                abiertas[mesa]["entregas"].append({
                    "t": a_video(kw.get("hora"), t), "mesero": kw.get("personal_identificador"),
                    "metodo": kw.get("metodo"), "confianza": kw.get("confianza"),
                })
        for o in abiertas.values():  # seguían ocupadas al terminar el video
            o["fin"] = self.t_fin
            ocupaciones.append(o)
        ocupaciones.sort(key=lambda o: o["inicio"])

        for o in ocupaciones:
            o["duracion_s"] = o["fin"] - o["inicio"]
            o["espera_s"] = (o["entregas"][0]["t"] - o["inicio"]) if o["entregas"] else None
        esperas = [o["espera_s"] for o in ocupaciones if o["espera_s"] is not None]
        entregas = [{**e, "mesa": o["mesa"]} for o in ocupaciones for e in o["entregas"]]

        por_mesa = {}
        for t in self.config.tables:
            propias = [o for o in ocupaciones if o["mesa"] == t.numero]
            seg = sum(o["duracion_s"] for o in propias)
            por_mesa[t.numero] = {"ocupaciones": len(propias), "segundos_ocupada": seg, "porcentaje_del_video": 100 * seg / duracion}

        total_frames = max(sum(self.mesas_simultaneas.values()), 1)
        max_sim = max(self.mesas_simultaneas) if self.mesas_simultaneas else 0
        media_sim = sum(k * v for k, v in self.mesas_simultaneas.items()) / total_frames
        vacio = 100 * self.mesas_simultaneas.get(0, 0) / total_frames

        roles = Counter(self._rol_final(i) for i in self.tracks)
        metodos = Counter(kw.get("metodo") for t, m, kw in registro if m == "reportar_personal_clasificado")
        return {
            "video": {"duracion_s": self.duracion_video or duracion, "resolucion": list(self.resolucion), "fps_proceso": self.fps,
                      "fotogramas_procesados": self.frames, "tiempo_de_proceso_s": self.tiempo_proceso_s,
                      "velocidad_x_tiempo_real": (self.duracion_video or duracion) / self.tiempo_proceso_s if self.tiempo_proceso_s else None},
            "yolo": {
                "detecciones_de_persona": self.detecciones_persona, "detecciones_de_vajilla": self.detecciones_objeto,
                "porcentaje_fotogramas_con_personas": 100 * self.frames_con_persona / max(self.frames, 1),
                "personas_por_fotograma_promedio": self.detecciones_persona / max(self.frames, 1),
                "maximo_personas_a_la_vez": self.max_personas,
                "confianza_promedio": mean(self.confianzas) if self.confianzas else None,
                "personas_distintas_seguidas": len(self.tracks),
            },
            "ocupacion": {
                "mesas_monitoreadas": n_mesas, "ocupaciones": len(ocupaciones),
                "maximo_mesas_ocupadas_a_la_vez": max_sim, "promedio_mesas_ocupadas": media_sim,
                "porcentaje_del_video_sin_ninguna_mesa_ocupada": vacio,
                "por_mesa": por_mesa, "detalle": ocupaciones,
            },
            "entregas": {"total": len(entregas), "detalle": entregas, "espera_promedio_s": mean(esperas) if esperas else None,
                         "por_mesero": dict(Counter(e["mesero"] for e in entregas))},
            "personal": {"trayectorias_por_rol": {k.value: v for k, v in roles.items()},
                         "clasificaciones_por_metodo": {k: v for k, v in metodos.items() if k}},
            "demanda": self._demanda(n_mesas, max_sim, media_sim, len(ocupaciones), mean(esperas) / 60 if esperas else None),
            "fotogramas": [{"t": t, "etiqueta": e, "archivo": f} for t, e, f in self.fotogramas],
        }

    def _rol_final(self, track_id):
        r = self.roles[track_id]
        if r[Role.STAFF] >= max(1, 0.3 * sum(r.values())):
            return Role.STAFF
        return Role.CUSTOMER if r[Role.CUSTOMER] else Role.UNKNOWN

    @staticmethod
    def _demanda(n_mesas, max_sim, media_sim, n_ocupaciones, espera_min) -> dict:
        ratio = max_sim / n_mesas if n_mesas else 0.0
        nivel = "baja" if ratio <= UMBRAL_BAJA else "alta" if ratio >= UMBRAL_ALTA else "normal"
        avisos = []
        if n_ocupaciones < MUESTRA_PEQUENA:
            avisos.append(f"Muestra pequeña: solo {n_ocupaciones} ocupación(es). Los promedios (tiempo de espera, uso por mesa) "
                          "NO son representativos; sirven para verificar el funcionamiento, no para evaluar el servicio.")
        if nivel == "baja":
            avisos.append("No es hora pico: con tan poca gente no hay cuellos de botella que medir. Para validar el comportamiento "
                          "bajo carga (esperas largas, un mesero con varias mesas, fusiones) se necesita video de hora pico.")
        return {
            "nivel": nivel, "maximo_mesas_ocupadas_a_la_vez": max_sim, "mesas_monitoreadas": n_mesas,
            "fraccion_maxima_ocupada": ratio, "regla": f"baja ≤ {UMBRAL_BAJA:.0%} de las mesas a la vez · alta ≥ {UMBRAL_ALTA:.0%}",
            "espera_promedio_min": espera_min, "referencias_documento": FICHAS, "avisos": avisos,
        }

    # ------------------------------------------------------------------ #
    def escribir(self, video_rel: Optional[str] = None) -> dict:
        r = self.resumen()
        with open(os.path.join(self.carpeta, "informe.json"), "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2, default=str)
        with open(os.path.join(self.carpeta, "informe.html"), "w", encoding="utf-8") as f:
            f.write(_html(r, self.serie, self.t_ini or 0.0, self.t_fin, video_rel))
        return r


# ---------------------------------------------------------------------------- #
# HTML
# ---------------------------------------------------------------------------- #
def _mmss(s):
    if s is None:
        return "—"
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"


def _conf(valor) -> str:
    return "—" if valor is None else f"{valor:.2f}"


def _svg_gantt(r, t0, t1) -> str:
    """Línea de tiempo por mesa: barras = mesa ocupada, rombos = entregas."""
    ancho, izq, alto_fila = 900, 70, 34
    mesas = list(r["ocupacion"]["por_mesa"])
    alto = alto_fila * len(mesas) + 46
    esc = (ancho - izq - 10) / max(t1 - t0, 1e-9)
    x = lambda t: izq + (t - t0) * esc  # noqa: E731
    out = [f'<svg viewBox="0 0 {ancho} {alto}" width="100%" role="img" aria-label="Línea de tiempo de ocupación por mesa">']
    paso = 60 if (t1 - t0) <= 900 else 120
    marca = (int(t0) // paso + 1) * paso
    while marca < t1:
        out.append(f'<line x1="{x(marca):.1f}" y1="6" x2="{x(marca):.1f}" y2="{alto - 30}" stroke="var(--line)"/>'
                   f'<text x="{x(marca):.1f}" y="{alto - 12}" text-anchor="middle" font-size="11" fill="var(--mut)">{_mmss(marca)}</text>')
        marca += paso
    for i, m in enumerate(mesas):
        y = 8 + i * alto_fila
        out.append(f'<text x="{izq - 8}" y="{y + 19}" text-anchor="end" font-size="12" fill="var(--tx)">Mesa {m}</text>'
                   f'<rect x="{izq}" y="{y + 4}" width="{ancho - izq - 10}" height="22" rx="4" fill="var(--bg)"/>')
        for o in r["ocupacion"]["detalle"]:
            if o["mesa"] == m:
                out.append(f'<rect x="{x(o["inicio"]):.1f}" y="{y + 4}" width="{max((o["fin"] - o["inicio"]) * esc, 2):.1f}" height="22" rx="4" fill="var(--ac)" opacity=".85">'
                           f'<title>Mesa {m}: {_mmss(o["inicio"])} → {_mmss(o["fin"])}</title></rect>')
        for e in r["entregas"]["detalle"]:
            if e["mesa"] == m:
                cx = x(e["t"])
                out.append(f'<path d="M{cx:.1f} {y + 6} l7 9 l-7 9 l-7 -9 z" fill="#fff" stroke="var(--tx)" stroke-width="1.5">'
                           f'<title>Entrega {_mmss(e["t"])} · {html.escape(str(e["mesero"]))}</title></path>')
    out.append("</svg>")
    return "".join(out)


def _svg_lineas(serie: _Serie, t0, t1) -> str:
    ancho, alto, izq, base = 900, 150, 40, 120
    n = max(max(serie.personas, default=1), 1)
    esc = (ancho - izq - 10) / max(t1 - t0, 1e-9)
    ys = (base - 10) / n

    def linea(valores, color, dash=""):
        pts = " ".join(f"{izq + (t - t0) * esc:.1f},{base - v * ys:.1f}" for t, v in zip(serie.t, valores))
        return f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" {dash}/>'
    out = [f'<svg viewBox="0 0 {ancho} {alto}" width="100%" role="img" aria-label="Personas y mesas ocupadas en el tiempo">',
           f'<line x1="{izq}" y1="{base}" x2="{ancho - 10}" y2="{base}" stroke="var(--line)"/>']
    for v in range(0, n + 1):
        out.append(f'<text x="{izq - 6}" y="{base - v * ys + 4:.1f}" text-anchor="end" font-size="11" fill="var(--mut)">{v}</text>')
    out.append(linea(serie.personas, "var(--ok)") + linea(serie.mesas, "var(--ac)", 'stroke-dasharray="5 4"'))
    out.append(f'<text x="{izq + 4}" y="14" font-size="12" fill="var(--ok)">— personas detectadas por YOLO</text>'
               f'<text x="{izq + 230}" y="14" font-size="12" fill="var(--ac)">- - mesas ocupadas</text></svg>')
    return "".join(out)


def _html(r, serie, t0, t1, video_rel) -> str:
    d, y, o, e, p = r["demanda"], r["yolo"], r["ocupacion"], r["entregas"], r["personal"]
    colores = {"baja": "#15803d", "normal": "#b45309", "alta": "#b91c1c"}
    fichas = "".join(
        f'<tr class="{"sel" if k == d["nivel"] else ""}"><td>{k.capitalize()}</td><td>{html.escape(v["descripcion"])}</td>'
        f'<td class="n">{v["espera_min"]:.1f} min</td><td>{v["ficha"]}</td></tr>' for k, v in d["referencias_documento"].items())
    mesas = "".join(
        f'<tr><td>Mesa {m}</td><td class="n">{v["ocupaciones"]}</td><td class="n">{_mmss(v["segundos_ocupada"])}</td>'
        f'<td class="n">{v["porcentaje_del_video"]:.1f} %</td></tr>' for m, v in o["por_mesa"].items())
    ocup = "".join(
        f'<tr><td>Mesa {x["mesa"]}</td><td>{_mmss(x["inicio"])}</td><td>{_mmss(x["fin"])}</td><td class="n">{_mmss(x["duracion_s"])}</td>'
        f'<td class="n">{_mmss(x["espera_s"])}</td></tr>' for x in o["detalle"]) or '<tr><td colspan="5" class="mut">Ninguna.</td></tr>'
    entr = "".join(
        f'<tr><td>{_mmss(x["t"])}</td><td>Mesa {x["mesa"]}</td><td>{html.escape(str(x["mesero"]))}</td><td>{html.escape(str(x["metodo"] or "—"))}</td>'
        f'<td class="n">{_conf(x["confianza"])}</td></tr>' for x in e["detalle"]) or '<tr><td colspan="5" class="mut">Ninguna.</td></tr>'
    avisos = "".join(f"<li>{html.escape(a)}</li>" for a in d["avisos"])
    frames = "".join(
        f'<figure><a href="fotogramas/{f["archivo"]}" target="_blank"><img loading="lazy" src="fotogramas/{f["archivo"]}" alt="{html.escape(f["etiqueta"])}"></a>'
        f'<figcaption>{_mmss(f["t"])} · {html.escape(f["etiqueta"])}</figcaption></figure>' for f in r["fotogramas"])
    video = (f'<video controls preload="metadata" src="{video_rel}"></video>' if video_rel else "")
    v = r["video"]
    nota_vajilla = ('<div class="sub">Sin detecciones de vajilla: con esta cámara y resolución, YOLOv8n no distingue los platos '
                    '(son pequeños). Las entregas no dependen de YOLO: se detectan por sustracción de fondo sobre el mantel.</div>'
                    if y["detecciones_de_vajilla"] == 0 else "")
    vel = f' · procesado a {v["velocidad_x_tiempo_real"]:.1f}× el tiempo real' if v.get("velocidad_x_tiempo_real") else ""
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ScanEats · Informe del video</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--tx:#1c2430;--mut:#667085;--line:#e4e7ec;--ac:#c2410c;--ok:#15803d}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12161c;--card:#1a2028;--tx:#e8ecf1;--mut:#98a2b3;--line:#2b3441;--ac:#fb923c;--ok:#4ade80}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1050px;margin:0 auto;padding:22px 16px 60px}}h1{{font-size:22px;margin:0 0 4px}}h1 span{{color:var(--ac)}}h2{{font-size:16px;margin:28px 0 8px}}
.sub,.mut{{color:var(--mut);font-size:13px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:12px}}
.grid{{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}}.k{{font-size:26px;font-weight:650;line-height:1.15}}.k small{{font-size:13px;font-weight:400;color:var(--mut)}}
.badge{{display:inline-block;padding:3px 12px;border-radius:99px;color:#fff;font-weight:650;text-transform:uppercase;letter-spacing:.04em;background:{colores[d["nivel"]]}}}
table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}}th{{color:var(--mut);font-size:12px;text-transform:uppercase}}
td.n,th.n{{text-align:right;font-variant-numeric:tabular-nums}}tr.sel td{{background:rgba(194,65,12,.12);font-weight:600}}
ul.av{{margin:8px 0 0;padding-left:18px}}video{{width:100%;border-radius:10px;background:#000}}.gal{{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(230px,1fr))}}
figure{{margin:0}}figure img{{width:100%;border-radius:8px;border:1px solid var(--line);display:block}}figcaption{{font-size:12px;color:var(--mut);margin-top:3px}}.wrap{{overflow-x:auto}}
</style></head><body><main>
<h1>Scan<span>Eats</span> · Informe del video</h1>
<div class="sub">{int(v["duracion_s"] // 60)} min {int(v["duracion_s"] % 60)} s · {v["resolucion"][0]}×{v["resolucion"][1]} · {v["fotogramas_procesados"]} fotogramas analizados a {v["fps_proceso"]:g} FPS{vel}</div>

<div class="card" style="margin-top:14px"><div style="display:flex;flex-wrap:wrap;gap:14px;align-items:center;justify-content:space-between">
<div><div class="sub">NIVEL DE DEMANDA DEL VIDEO</div><span class="badge">{d["nivel"]}</span>
<span class="sub" style="margin-left:8px">máx. {d["maximo_mesas_ocupadas_a_la_vez"]} de {d["mesas_monitoreadas"]} mesas ocupadas a la vez ({d["fraccion_maxima_ocupada"]:.0%}) · regla: {html.escape(d["regla"])}</span></div>
<div class="k">{_mmss(e["espera_promedio_s"])} <small>espera promedio (min:seg)</small></div></div>
<ul class="av">{avisos}</ul></div>

<h2>Detección con YOLOv8</h2><div class="grid">
<div class="card"><div class="k">{y["detecciones_de_persona"]}</div><div class="sub">detecciones de persona</div></div>
<div class="card"><div class="k">{y["personas_por_fotograma_promedio"]:.2f}</div><div class="sub">personas por fotograma (máx. {y["maximo_personas_a_la_vez"]} a la vez)</div></div>
<div class="card"><div class="k">{y["porcentaje_fotogramas_con_personas"]:.0f} %</div><div class="sub">de los fotogramas hay alguien</div></div>
<div class="card"><div class="k">{_conf(y["confianza_promedio"])}</div><div class="sub">confianza promedio de YOLO</div></div>
<div class="card"><div class="k">{y["personas_distintas_seguidas"]}</div><div class="sub">trayectorias distintas (tracker)</div></div>
<div class="card"><div class="k">{y["detecciones_de_vajilla"]}</div><div class="sub">detecciones de vajilla</div></div></div>
{nota_vajilla}<div class="card" style="margin-top:12px">{video}<div class="sub" style="margin-top:6px">Video con las detecciones dibujadas: caja blanca = detección cruda de YOLO; caja gruesa = persona seguida (verde personal, naranja cliente, gris sin clasificar); el color de cada mesa indica su estado.</div></div>

<h2>Línea de tiempo</h2><div class="card">{_svg_gantt(r, t0, t1)}<div class="sub">Barras: mesa ocupada · rombos: entregas.</div></div>
<div class="card">{_svg_lineas(serie, t0, t1)}</div>

<h2>Ocupación por mesa</h2><div class="card wrap"><table><tr><th>Mesa</th><th class="n">Ocupaciones</th><th class="n">Tiempo ocupada</th><th class="n">% del video</th></tr>{mesas}</table>
<div class="sub" style="margin-top:6px">Sin ninguna mesa ocupada el {o["porcentaje_del_video_sin_ninguna_mesa_ocupada"]:.0f} % del tiempo · promedio de {o["promedio_mesas_ocupadas"]:.2f} mesas ocupadas.</div></div>
<div class="card wrap"><table><tr><th>Mesa</th><th>Inicio</th><th>Fin</th><th class="n">Duración</th><th class="n">Espera hasta la 1.ª entrega</th></tr>{ocup}</table></div>

<h2>Entregas y personal</h2><div class="card wrap"><table><tr><th>Momento</th><th>Mesa</th><th>Mesero</th><th>Identificado por</th><th class="n">Confianza</th></tr>{entr}</table>
<div class="sub" style="margin-top:6px">Trayectorias seguidas por rol (el tracker puede partir a una misma persona en varias): {html.escape(", ".join(f"{k}: {v}" for k, v in p["trayectorias_por_rol"].items()) or "—")} · clasificaciones por método: {html.escape(", ".join(f"{k}: {v}" for k, v in p["clasificaciones_por_metodo"].items()) or "—")}</div></div>

<h2>Comparación con las fichas de observación del documento</h2><div class="card wrap"><table><tr><th>Demanda</th><th>Descripción</th><th class="n">Espera 1.er contacto</th><th>Ficha</th></tr>{fichas}</table>
<div class="sub" style="margin-top:6px">La espera del documento es la del primer contacto del mesero; aquí se mide la ocupación → primera entrega del pedido.</div></div>

<h2>Momentos clave</h2><div class="sub" style="margin-bottom:8px">Fotogramas del instante en que el sistema confirmó cada evento (unos segundos después de que ocurre en la realidad).</div><div class="gal">{frames}</div>
</main></body></html>"""
