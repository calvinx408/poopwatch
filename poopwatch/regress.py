"""Saved test cases for the gate prompt.

A case is a folder under evidence/review/cases/<name>/ holding:
  frames/   copies of the event's extracted frames (jpg)
  expect    "pass"   -> the gate should say a dog is defecating
            "reject" -> the gate should not

Save a reviewed event as a case with `save-case`, then run `regress` after any
prompt change. A change is only worth keeping if every case still passes.
Only the gate is tested here; the verdict prompt needs its own cases.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .config import config

log = logging.getLogger(__name__)

EXPECTATIONS = ("pass", "reject")


def cases_dir() -> Path:
    return config.evidence_dir / "review" / "cases"


def save_case(event_id: int, name: str, expect: str) -> Path:
    """Copy an event's frames into a new case folder. Raises on bad input."""
    from .store import Store

    if expect not in EXPECTATIONS:
        raise ValueError("expect must be one of: " + ", ".join(EXPECTATIONS))
    if not name or any(ch in name for ch in "/\\:*?\"<>|"):
        raise ValueError("case name must be a plain folder name")

    event = Store(config).get_event(event_id)
    if not event:
        raise ValueError("no such event: " + str(event_id))
    rels = event.get("frame_paths") or []
    if not rels:
        raise ValueError("event " + str(event_id) + " has no frames to save")

    dest = cases_dir() / name
    if dest.exists():
        raise ValueError("a case named " + name + " already exists")
    (dest / "frames").mkdir(parents=True)
    for i, rel in enumerate(rels, start=1):
        src = config.evidence_dir / rel
        if src.exists():
            shutil.copy2(src, dest / "frames" / ("f_%04d.jpg" % i))
    (dest / "expect").write_text(expect + "\n", encoding="utf-8")
    return dest


def run_cases() -> list[tuple[str, str, str, bool]]:
    """Run the gate on every case. Returns (name, expected, got, ok) rows."""
    from .analyze import run_gate
    from .capture import Frame

    rows = []
    root = cases_dir()
    if not root.exists():
        return rows
    for case in sorted(p for p in root.iterdir() if p.is_dir()):
        expect_file = case / "expect"
        if not expect_file.exists():
            continue
        expect = expect_file.read_text(encoding="utf-8").strip()
        paths = sorted((case / "frames").glob("*.jpg"))
        frames = [Frame(path=p, offset_s=float(i), epoch=0.0)
                  for i, p in enumerate(paths)]
        try:
            gate = run_gate(frames, config)
        except Exception as e:  # a model error is a failed case, not a crash
            log.warning("case %s: gate failed: %s", case.name, e)
            rows.append((case.name, expect, "error", False))
            continue
        if gate is None:
            rows.append((case.name, expect, "gate off", False))
            continue
        got = "pass" if gate.likely_defecating else "reject"
        rows.append((case.name, expect, got, got == expect))
    return rows
