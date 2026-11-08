"""Quick health check for every configured camera, for you to run yourself.

    .venv\\Scripts\\python -m poopwatch check-cameras

For each camera it reports:
  - network: ping loss and average/worst time (Wi-Fi quality)
  - stream:  whether the video stream answers and decodes
  - recording: how old the newest recording is, and whether it decodes cleanly
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from .capture import ffmpeg_bin, scrub
from .config import CameraConfig, Config, config


def _ping(host: str, count: int = 20) -> dict:
    try:
        out = subprocess.run(["ping", "-n", str(count), host],
                             capture_output=True, text=True, timeout=count * 3 + 10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "detail": "ping failed"}
    lost = re.search(r"Lost = (\d+)", out)
    avg = re.search(r"Average = (\d+)ms", out)
    worst = re.search(r"Maximum = (\d+)ms", out)
    if not lost:
        return {"ok": False, "detail": "no reply"}
    lost_n = int(lost.group(1))
    return {
        "ok": lost_n == 0,
        "detail": "%d/%d lost, avg %sms, worst %sms" % (
            lost_n, count, avg.group(1) if avg else "?", worst.group(1) if worst else "?"),
    }


def _stream(cam: CameraConfig) -> dict:
    try:
        r = subprocess.run([
            ffmpeg_bin(), "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp",
            "-timeout", "10000000", "-t", "4", "-i", cam.rtsp_url, "-f", "null", "-",
        ], capture_output=True, timeout=40)
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "no response in 40s"}
    err = scrub(r.stderr.decode("utf-8", "replace")).strip().splitlines()
    if r.returncode != 0:
        return {"ok": False, "detail": (err[-1][:100] if err else "failed")}
    return {"ok": True, "detail": "answering"}


def _recording(cam: CameraConfig, cfg: Config) -> dict:
    segs = sorted(cfg.segments_dir(cam).glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    if not segs:
        return {"ok": False, "detail": "no recordings yet"}
    newest = segs[-1]
    age = int(time.time() - newest.stat().st_mtime)
    # A recording still being written has no index yet; check one that is closed.
    settled = [p for p in segs if time.time() - p.stat().st_mtime >= 15]
    if not settled:
        return {"ok": False, "detail": "newest recording is %ds old" % age}
    target = settled[-1]
    r = subprocess.run([ffmpeg_bin(), "-nostdin", "-v", "error", "-i", str(target),
                        "-f", "null", "-"], capture_output=True, timeout=120)
    errors = [l for l in r.stderr.decode("utf-8", "replace").splitlines() if l.strip()]
    if age > 120:
        return {"ok": False, "detail": "last recording %d min ago" % (age // 60)}
    if errors:
        return {"ok": False, "detail": "%d decode errors in latest recording" % len(errors)}
    return {"ok": True, "detail": "recording, last %ds ago, clean" % age}


def check_all(cfg: Config | None = None) -> int:
    cfg = cfg or config
    if not cfg.cameras:
        print("No cameras configured.")
        return 1
    problems = 0
    for cam in cfg.cameras:
        print("== " + cam.name + " (" + cam.host + ")")
        for label, result in (
            ("network  ", _ping(cam.host)),
            ("stream   ", _stream(cam)),
            ("recording", _recording(cam, cfg)),
        ):
            mark = "OK  " if result["ok"] else "FAIL"
            problems += 0 if result["ok"] else 1
            print("   [%s] %s  %s" % (mark, label, result["detail"]))
    print("")
    print("All checks passed." if problems == 0 else
          "%d check(s) failed. See the notes above." % problems)
    return 0 if problems == 0 else 1
