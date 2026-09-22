# vision_service/scaneats_vision/detector.py
"""Detector YOLOv8 (importación perezosa: el módulo se puede importar sin `ultralytics`)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .geometry import BBox

PERSON_CLASS = 0
# COCO: bottle, wine glass, cup, fork, knife, spoon, bowl -> "objetos sobre la mesa"
DEFAULT_OBJECT_CLASSES = (39, 40, 41, 42, 43, 44, 45)


@dataclass(frozen=True)
class Detection:
    cls: int
    confidence: float
    bbox: BBox  # coordenadas NORMALIZADAS 0..1

    @property
    def is_person(self) -> bool:
        return self.cls == PERSON_CLASS


class YoloDetector:
    def __init__(self, model_path: str = "yolov8n.pt", confidence: float = 0.35, object_classes=DEFAULT_OBJECT_CLASSES,
                 device: Optional[str] = None, imgsz: int = 960):
        from ultralytics import YOLO  # perezoso

        self.model = YOLO(model_path)
        self.confidence = confidence
        self.classes = [PERSON_CLASS, *object_classes]
        self.device = device
        self.imgsz = imgsz

    def detect(self, frame) -> list[Detection]:
        alto, ancho = frame.shape[:2]
        resultados = self.model.predict(frame, conf=self.confidence, classes=self.classes, device=self.device,
                                        imgsz=self.imgsz, verbose=False)
        salida = []
        for r in resultados:
            for caja in r.boxes:
                x1, y1, x2, y2 = (float(v) for v in caja.xyxy[0].tolist())
                salida.append(Detection(
                    cls=int(caja.cls[0]),
                    confidence=float(caja.conf[0]),
                    bbox=BBox(x1 / ancho, y1 / alto, x2 / ancho, y2 / alto),
                ))
        return salida
