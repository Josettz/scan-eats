# vision_service/tests/test_settings.py
from scaneats_vision.settings import ServiceSettings, _valor, cargar_env


def test_valor_descarta_comentarios_en_la_misma_linea():
    assert _valor("sort            # sort | deepsort") == "sort"
    assert _valor("  5  ") == "5"
    assert _valor('"con # dentro" # nota') == "con # dentro"
    assert _valor("'http://x:8000'") == "http://x:8000"
    assert _valor("") == ""


def test_el_env_example_se_puede_cargar_tal_cual(tmp_path, monkeypatch):
    """Regresión: copiar .env.example a .env no debe romper valores como TRACKER=sort  # comentario."""
    ejemplo = (__import__("pathlib").Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(ejemplo, encoding="utf-8")
    for k in ("TRACKER", "VISION_API_URL", "PROCESS_FPS", "CLIPS_ENABLED", "VISION_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    s = ServiceSettings.from_env()
    assert s.tracker == "sort" and s.process_fps == 5.0 and s.clips_enabled is False
    assert s.api_base_url == "http://localhost:8000"


def test_no_pisa_variables_ya_definidas(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("YOLO_MODEL=otro.pt\n", encoding="utf-8")
    monkeypatch.setenv("YOLO_MODEL", "yolov8s.pt")
    cargar_env(env)
    import os
    assert os.environ["YOLO_MODEL"] == "yolov8s.pt"
