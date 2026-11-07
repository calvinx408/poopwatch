"""Populate the event log with fake events so the dashboard can be viewed
before any camera is configured.

    python scripts/seed_demo.py          # add demo events
    python scripts/seed_demo.py --clear  # remove them again

Demo events are tagged with camera slugs prefixed `demo-`, so --clear can
remove them without touching real events.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw  # noqa: E402

from poopwatch.config import config  # noqa: E402
from poopwatch.store import Store  # noqa: E402

DEMO_PREFIX = "demo-"

SCENES = {
    "lawn": ((58, 92, 48), (72, 110, 58)),
    "drive": ((62, 62, 66), (78, 78, 82)),
    "dusk": ((38, 44, 58), (52, 58, 74)),
}


def make_frame(path: Path, scene: str, clock: str, label: str,
               subject_x: float | None) -> None:
    """A stand-in camera still: ground, a path, a timestamp burn-in, and a
    blob standing in for the subject. Enough to judge the layout."""
    w, h = 960, 540
    top, bottom = SCENES[scene]
    img = Image.new("RGB", (w, h), top)
    d = ImageDraw.Draw(img)

    for y in range(h):
        t = y / h
        d.line(
            [(0, y), (w, y)],
            fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)),
        )

    # a path / kerb across the lower third
    d.polygon([(0, 360), (w, 300), (w, 420), (0, 470)], fill=(96, 96, 100))

    if subject_x is not None:
        cx, cy = int(w * subject_x), 395
        d.ellipse([cx - 46, cy - 30, cx + 46, cy + 26], fill=(58, 44, 34))
        d.ellipse([cx + 30, cy - 46, cx + 70, cy - 8], fill=(66, 50, 38))
        for dx in (-32, -12, 16, 38):
            d.line([(cx + dx, cy + 18), (cx + dx, cy + 44)], fill=(48, 36, 28), width=7)

    d.rectangle([0, 0, 250, 26], fill=(0, 0, 0))
    d.text((8, 8), clock + "  " + label, fill=(230, 230, 230))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, quality=86)


DEMO_EVENTS = [
    {
        "camera_name": "front yard", "slug": "front-yard", "scene": "lawn",
        "ago": 420, "stage": "notified", "subject": "dog",
        "defecation": 1, "handler_present": 1, "cleanup": 0, "violation": 1,
        "confidence": 0.91, "notified": 1, "subject_x": 0.42,
        "reasoning": "A medium brown dog squatted on the grass strip at t+38s with its "
                     "back clearly arched, held the posture for roughly six seconds, "
                     "then scratched the ground. The handler waited on the sidewalk and "
                     "walked off at t+71s without approaching the spot.",
        "cleanup_evidence": "Nobody approached the spot before the clip ended at t+150s.",
        "verdict": {"defecation_offset_s": 38.0, "cleanup_offset_s": -1.0},
    },
    {
        "camera_name": "driveway", "slug": "driveway", "scene": "drive",
        "ago": 5400, "stage": "verdict", "subject": "dog",
        "defecation": 1, "handler_present": 1, "cleanup": 1, "violation": 0,
        "confidence": 0.86, "subject_x": 0.6,
        "reasoning": "Dog defecated at the edge of the driveway at t+22s. The handler "
                     "crouched directly over the spot at t+49s, and is holding a small "
                     "bag when they leave frame at t+58s.",
        "cleanup_evidence": "Handler bent over the exact spot and departed carrying a bag.",
        "verdict": {"defecation_offset_s": 22.0, "cleanup_offset_s": 49.0},
    },
    {
        "camera_name": "front yard", "slug": "front-yard", "scene": "lawn",
        "ago": 9000, "stage": "gate_rejected", "subject": "dog",
        "confidence": 0.44, "subject_x": 0.3,
        "reasoning": "Gate saw a dog standing; no defecation posture.",
    },
    {
        "camera_name": "driveway", "slug": "driveway", "scene": "dusk",
        "ago": 14400, "stage": "verdict", "subject": "dog",
        "defecation": 0, "handler_present": 0, "cleanup": 0, "violation": 0,
        "confidence": 0.37, "subject_x": 0.75,
        "reasoning": "A dog crossed the driveway but light levels were very low and the "
                     "animal was partly out of frame. I cannot confirm defecation.",
        "cleanup_evidence": "Not applicable; no defecation confirmed.",
        "verdict": {"defecation_offset_s": -1.0, "cleanup_offset_s": -1.0},
    },
    {
        "camera_name": "front yard", "slug": "front-yard", "scene": "lawn",
        "ago": 19000, "stage": "no_dog", "subject": "other_animal",
        "confidence": 0.0, "subject_x": None,
        "reasoning": "Local detector found no dog in any frame (cat detected).",
    },
]


def seed(store: Store) -> int:
    now = time.time()
    created = 0
    for spec in DEMO_EVENTS:
        trigger = now - spec["ago"]
        slug = DEMO_PREFIX + spec["slug"]
        event_dir = store.event_dir(trigger, slug)

        frames = []
        for i in range(6):
            fp = event_dir / "frames" / ("f_%04d.jpg" % (i + 1))
            clock = time.strftime("%H:%M:%S", time.localtime(trigger + i * 25))
            make_frame(fp, spec["scene"], clock, spec["camera_name"],
                       spec["subject_x"])
            frames.append(store.relative(fp))

        thumb = event_dir / "thumb.jpg"
        make_frame(thumb, spec["scene"],
                   time.strftime("%H:%M:%S", time.localtime(trigger)),
                   spec["camera_name"], spec["subject_x"])

        store.add_event(
            trigger_at=trigger,
            camera="demo",
            camera_slug=slug,
            camera_name=spec["camera_name"],
            stage=spec["stage"],
            subject=spec.get("subject"),
            defecation=spec.get("defecation", 0),
            handler_present=spec.get("handler_present", 0),
            cleanup=spec.get("cleanup", 0),
            violation=spec.get("violation", 0),
            confidence=spec.get("confidence", 0.0),
            reasoning=spec.get("reasoning", ""),
            cleanup_evidence=spec.get("cleanup_evidence", ""),
            verdict_json=json.dumps(spec["verdict"]) if spec.get("verdict") else None,
            thumb_path=store.relative(thumb),
            frame_paths=json.dumps(frames),
            notified=spec.get("notified", 0),
        )
        created += 1
    return created


def clear(store: Store) -> int:
    import shutil

    with store._conn() as c:
        removed = c.execute(
            "DELETE FROM events WHERE camera_slug LIKE ?", [DEMO_PREFIX + "%"]
        ).rowcount
    for d in store.cfg.events_dir.glob(DEMO_PREFIX + "*"):
        shutil.rmtree(d, ignore_errors=True)
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clear", action="store_true", help="remove demo events")
    args = ap.parse_args()

    store = Store(config)
    if args.clear:
        print("removed " + str(clear(store)) + " demo events")
    else:
        print("added " + str(seed(store)) + " demo events")
        print("start the dashboard with:  python -m poopwatch serve --no-watch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
