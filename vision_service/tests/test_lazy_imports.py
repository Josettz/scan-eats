# vision_service/tests/test_lazy_imports.py
"""
Requisito: la lógica de negocio debe poder importarse y probarse SIN OpenCV, YOLOv8, DeepSORT ni PyTorch.
Se importan todos los módulos en un intérprete limpio y se comprueba que ninguna dependencia pesada se cargó.
"""
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # vision_service/

MODULOS = [
    "scaneats_vision", "scaneats_vision.geometry", "scaneats_vision.occupancy_engine",
    "scaneats_vision.table_fusion", "scaneats_vision.staff_classifier", "scaneats_vision.delivery_detector",
    "scaneats_vision.tracking", "scaneats_vision.detector", "scaneats_vision.color_utils",
    "scaneats_vision.clip_recorder", "scaneats_vision.api_client", "scaneats_vision.dispatcher",
    "scaneats_vision.config_loader", "scaneats_vision.pipeline", "scaneats_vision.simulate",
    "scaneats_vision.evaluation", "scaneats_vision.settings",
]
PESADAS = ["cv2", "ultralytics", "torch", "deep_sort_realtime", "requests"]


def test_los_modulos_no_cargan_dependencias_pesadas_al_importarse():
    codigo = (
        "import sys\n"
        + "".join(f"import {m}\n" for m in MODULOS)
        + f"cargadas = [m for m in {PESADAS!r} if m in sys.modules]\n"
        + "print(','.join(cargadas))\n"
    )
    r = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, timeout=60, cwd=RAIZ)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", f"Se importaron dependencias pesadas: {r.stdout.strip()}"


def test_construir_el_pipeline_no_requiere_opencv(tmp_path):
    """Con un extractor de color inyectado, el pipeline completo se construye sin OpenCV/numpy."""
    codigo = (
        "import sys\n"
        "from scaneats_vision.config_loader import load_config\n"
        "from scaneats_vision.dispatcher import EventDispatcher\n"
        "from scaneats_vision.pipeline import ScanEatsPipeline\n"
        "p = ScanEatsPipeline(load_config('config/zonas_ejemplo.json'), EventDispatcher(None, sync=True),\n"
        "                     color_ratio_fn=lambda c: {})\n"
        "print(','.join(m for m in ('cv2', 'ultralytics', 'torch') if m in sys.modules))\n"
    )
    r = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, timeout=60, cwd=RAIZ)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == ""
