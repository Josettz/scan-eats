# vision_service/tests/test_clip_recorder.py
from types import SimpleNamespace

from scaneats_vision.clip_recorder import ClipRecorder


class Frame:
    """Fotograma falso: solo necesita .shape (alto, ancho, canales)."""

    def __init__(self, t):
        self.t = t
        self.shape = (360, 640, 3)


class EscritorFalso:
    escritos = []

    def __init__(self, path, fps, size):
        self.path, self.fps, self.size, self.frames = path, fps, size, []
        EscritorFalso.escritos.append(self)

    def write(self, f):
        self.frames.append(f.t)

    def release(self):
        pass


def grabador(tmp_path, **kw):
    EscritorFalso.escritos = []
    listos = []
    rec = ClipRecorder(
        on_ready=lambda **k: listos.append(SimpleNamespace(**k)), pre_s=4.0, post_s=4.0, fps=2.0, out_dir=str(tmp_path),
        writer_factory=EscritorFalso, resize=lambda f, w: f, transcode=None, **kw,
    )
    return rec, listos


def test_el_clip_abarca_pre_y_post_del_evento(tmp_path):
    rec, listos = grabador(tmp_path)
    for i in range(40):  # 0.0 .. 19.5 s a 2 FPS
        t = i * 0.5
        rec.add_frame(t, Frame(t))
        if t == 10.0:
            rec.request("ocupacion", "uid-1", 10.0, "wall-1")
    assert len(listos) == 1
    clip = listos[0]
    assert (clip.tipo, clip.uid, clip.wall) == ("ocupacion", "uid-1", "wall-1")
    assert clip.path.endswith("ocupacion_uid-1.mp4")
    assert EscritorFalso.escritos[0].frames[0] == 6.0 and EscritorFalso.escritos[0].frames[-1] == 14.0  # [t-4, t+4]
    assert clip.duracion_s == len(EscritorFalso.escritos[0].frames) / 2.0


def test_no_se_finaliza_antes_de_tener_el_post_roll(tmp_path):
    rec, listos = grabador(tmp_path)
    for i in range(24):
        rec.add_frame(i * 0.5, Frame(i * 0.5))
        if i == 20:
            rec.request("entrega", "u", 10.0, None)
    assert listos == []  # a t=11.5 aún faltan 2.5 s de post-roll


def test_flush_cierra_los_pendientes_con_lo_disponible(tmp_path):
    rec, listos = grabador(tmp_path)
    for i in range(24):
        rec.add_frame(i * 0.5, Frame(i * 0.5))
        if i == 20:
            rec.request("fusion", "u", 10.0, None)
    rec.flush()
    assert len(listos) == 1 and EscritorFalso.escritos[0].frames[-1] == 11.5


def test_la_memoria_del_buffer_esta_acotada(tmp_path):
    rec, _ = grabador(tmp_path)
    for i in range(2000):
        rec.add_frame(i * 0.5, Frame(i * 0.5))
    assert len(rec._buffer) <= (4.0 + 5.0) * 2 + 2  # solo pre_s + margen, sin pendientes


def test_un_fallo_al_escribir_no_detiene_el_monitoreo(tmp_path):
    def explota(path, fps, size):
        raise RuntimeError("códec no disponible")

    listos = []
    rec = ClipRecorder(on_ready=lambda **k: listos.append(k), pre_s=1, post_s=1, fps=1, out_dir=str(tmp_path),
                       writer_factory=explota, resize=lambda f, w: f)
    for i in range(6):
        rec.add_frame(float(i), Frame(float(i)))
        if i == 2:
            rec.request("ocupacion", "u", 2.0, None)
    assert listos == []  # se registró el error y el bucle siguió


def test_sin_fotogramas_no_hay_clip(tmp_path):
    rec, listos = grabador(tmp_path)
    rec.request("ocupacion", "u", 100.0, None)
    rec.flush()
    assert listos == []


def test_el_clip_se_recodifica_a_h264_despues_de_escribirse(tmp_path):
    llamadas = []
    listos = []
    rec = ClipRecorder(on_ready=lambda **k: listos.append(k), pre_s=1, post_s=1, fps=1, out_dir=str(tmp_path),
                       writer_factory=EscritorFalso, resize=lambda f, w: f, transcode=lambda ruta: llamadas.append(ruta) or True)
    for i in range(6):
        rec.add_frame(float(i), Frame(float(i)))
        if i == 2:
            rec.request("ocupacion", "u", 2.0, None)
    assert len(llamadas) == 1 and llamadas[0].endswith("ocupacion_u.mp4")
    assert len(listos) == 1  # el clip se entrega ya recodificado


def test_si_falla_la_recodificacion_el_clip_igual_se_entrega(tmp_path):
    listos = []
    rec = ClipRecorder(on_ready=lambda **k: listos.append(k), pre_s=1, post_s=1, fps=1, out_dir=str(tmp_path),
                       writer_factory=EscritorFalso, resize=lambda f, w: f, transcode=lambda ruta: False)
    for i in range(6):
        rec.add_frame(float(i), Frame(float(i)))
        if i == 2:
            rec.request("ocupacion", "u", 2.0, None)
    assert len(listos) == 1  # mp4v es mejor que perder la evidencia


def test_recodificacion_real_a_h264(tmp_path):
    """Con OpenCV + imageio-ffmpeg instalados: el clip resultante es H.264 y se puede abrir."""
    import pytest

    np = pytest.importorskip("numpy")
    cv2 = pytest.importorskip("cv2")
    pytest.importorskip("imageio_ffmpeg")
    from scaneats_vision.clip_recorder import _a_h264, _cv2_writer

    ruta = str(tmp_path / "prueba.mp4")
    w = _cv2_writer(ruta, 5.0, (320, 240))
    for i in range(20):
        w.write(np.full((240, 320, 3), i * 10, dtype=np.uint8))
    w.release()
    assert _a_h264(ruta) is True
    cap = cv2.VideoCapture(ruta)
    assert cap.isOpened() and int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == 20
    import subprocess, imageio_ffmpeg  # noqa: E401
    info = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-i", ruta], capture_output=True, text=True).stderr
    assert "h264" in info.lower() and "yuv420p" in info
