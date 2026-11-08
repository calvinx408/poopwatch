"""Optional free local object detector (YOLO11 via ultralytics).

Sits in front of the VLM gate as an even cheaper filter: if no dog appears in
any frame, there is nothing to classify. Entirely optional, without
ultralytics installed, the pipeline just skips this stage.

A dog at the far side of a yard is only a few dozen pixels tall, and a model
run on the whole frame misses it. So each sampled frame is cut into zoomed
tiles, and the model runs on each tile.

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

MODEL_NAME = "yolo11s.pt"
FRAMES_TO_SCAN = 8      # evenly spaced frames per event; the rest are skipped
TILE_COLS, TILE_ROWS = 3, 2
TILE_ZOOM = 2           # each tile is enlarged before the model sees it
DEFAULT_CONF = 0.3

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
    """Lazy-load the model; weights download once on first use."""
    global _model, _unavailable
    if _model is not None or _unavailable:
        return _model
    try:
        from ultralytics import YOLO

        weights = Path(__file__).resolve().parent.parent / "models" / MODEL_NAME
        weights.parent.mkdir(parents=True, exist_ok=True)
        # YOLO() fetches the weights if the path doesn't exist yet.
        _model = YOLO(str(weights) if weights.exists() else MODEL_NAME)
        log.info("local detector loaded (%s)", MODEL_NAME)
    except Exception as e:  # ultralytics raises a variety of import/runtime errors
        _unavailable = str(e)
        log.warning("local detector unavailable: %s", e)
    return _model


def _tiles(img):
    """Overlapping, enlarged tiles that together cover the whole frame."""
    from PIL import Image

    w, h = img.size
    tw, th = w // TILE_COLS, h // TILE_ROWS
    pad_w, pad_h = int(tw * 0.1), int(th * 0.1)
    out = []
    for r in range(TILE_ROWS):
        for c in range(TILE_COLS):
            box = (max(0, c * tw - pad_w), max(0, r * th - pad_h),
                   min(w, (c + 1) * tw + pad_w), min(h, (r + 1) * th + pad_h))
            tile = img.crop(box)
            out.append(tile.resize((tile.width * TILE_ZOOM, tile.height * TILE_ZOOM),
                                   Image.LANCZOS))
    return out


def _sample(paths: list[Path]) -> list[Path]:
    if len(paths) <= FRAMES_TO_SCAN:
        return paths
    step = (len(paths) - 1) / (FRAMES_TO_SCAN - 1)
    return [paths[round(i * step)] for i in range(FRAMES_TO_SCAN)]


def detect(frame_paths: Sequence[Path], conf: float = DEFAULT_CONF) -> LocalDetection | None:
    """Scan frames for dogs, cats, and people. None if the stage is unavailable."""
    from PIL import Image

    model = _load()
    if model is None:
        return None

    found = {"dog": False, "cat": False, "person": False}
    best = 0.0
    try:
        for path in _sample(list(frame_paths)):
            img = Image.open(path).convert("RGB")
            for result in model(_tiles(img), verbose=False, conf=conf):
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
