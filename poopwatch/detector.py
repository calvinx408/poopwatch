"""Optional free local object detector (YOLO11 via ultralytics).

Sits in front of the VLM gate as an even cheaper filter: if no dog appears in
any frame, there is nothing to classify. Entirely optional -- without
ultralytics installed, the pipeline just skips this stage.

    pip install -r requirements-local.txt
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)

# COCO class ids used by the stock YOLO weights
DOG, CAT, PERSON = 16, 15, 0

_model = None
_unavailable: str | None = None


@dataclass
class LocalDetection:
    dog: bool
    cat: bool
    person: bool
    best_dog_conf: float

    @property
    def worth_escalating(self) -> bool:
        return self.dog


def available() -> tuple[bool, str]:
    try:
        import ultralytics  # noqa: F401
    except ImportError:
        return False, "ultralytics not installed (optional; stage is skipped)"
    return True, "ultralytics available"


def _load():
    """Lazy-load the model; weights download once to models/ on first use."""
    global _model, _unavailable
    if _model is not None or _unavailable:
        return _model
    try:
        from ultralytics import YOLO

        weights = Path(__file__).resolve().parent.parent / "models" / "yolo11n.pt"
        weights.parent.mkdir(parents=True, exist_ok=True)
        # YOLO() fetches the weights if the path doesn't exist yet.
        _model = YOLO(str(weights) if weights.exists() else "yolo11n.pt")
        log.info("local detector loaded")
    except Exception as e:  # ultralytics raises a variety of import/runtime errors
        _unavailable = str(e)
        log.warning("local detector unavailable: %s", e)
    return _model


def detect(frame_paths: Sequence[Path], conf: float = 0.35) -> LocalDetection | None:
    """Scan frames for dogs, cats, and people. None if the stage is unavailable."""
    model = _load()
    if model is None:
        return None

    found = {"dog": False, "cat": False, "person": False}
    best = 0.0
    try:
        for result in model([str(p) for p in frame_paths], verbose=False, conf=conf):
            for box in result.boxes:
                cls = int(box.cls.item())
                score = float(box.conf.item())
                if cls == DOG:
                    found["dog"] = True
                    best = max(best, score)
                elif cls == CAT:
                    found["cat"] = True
                elif cls == PERSON:
                    found["person"] = True
    except Exception as e:
        log.warning("local detection failed, skipping stage: %s", e)
        return None

    return LocalDetection(
        dog=found["dog"], cat=found["cat"], person=found["person"],
        best_dog_conf=best,
    )
