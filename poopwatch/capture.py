"""Rolling RTSP capture and event frame extraction.

A single long-lived ffmpeg process stream-copies the camera into 10-second
segments (near-zero CPU -- no re-encoding). Events are then just *time ranges*
into that ring buffer, which is what gives us pre-roll: by the time the camera's
AI fires, the dog may already be squatting, and the seconds before the trigger
are often the ones that matter.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import CameraConfig, Config, config

log = logging.getLogger(__name__)

SEGMENT_SECONDS = 10
_SEG_RE = re.compile(r"(\d{8}-\d{6})\.mp4$")


def _find_ffmpeg(name: str = "ffmpeg") -> str:
    """Locate ffmpeg/ffprobe. winget installs don't reach PATH until the shell
    restarts, so fall back to the package directory."""
    env = os.getenv(f"{name.upper()}_BIN")
    if env and Path(env).exists():
        return env
    found = shutil.which(name)
    if found:
        return found
    local = os.getenv("LOCALAPPDATA", "")
    if local:
        pattern = str(Path(local) / "Microsoft" / "WinGet" / "Packages" /
                      "Gyan.FFmpeg*" / "*" / "bin" / (name + ".exe"))
        for hit in sorted(glob.glob(pattern), reverse=True):
            return hit
    raise FileNotFoundError(
        name + " not found. Install with `winget install Gyan.FFmpeg` and "
        "restart your shell, or set " + name.upper() + "_BIN to its full path."
    )


def scrub(text: str, cfg: Config | None = None) -> str:
    """Strip camera credentials out of text before it is logged or served.

    ffmpeg echoes its input URL in most error messages, and that URL carries
    the camera password. Those strings reach `/api/status` and the dashboard,
    so they must never leave here intact. Scrubs every configured camera's
    password, not just the one that errored.
    """
    cfg = cfg or config
    cleaned = re.sub(r"(rtsp://)[^/@\s]*@", r"\1***:***@", text)
    for secret in cfg.all_passwords:
        cleaned = cleaned.replace(secret, "***")
    return cleaned


_FFMPEG: str | None = None


def ffmpeg_bin() -> str:
    """Resolved lazily so importing this module never explodes."""
    global _FFMPEG
    if _FFMPEG is None:
        _FFMPEG = _find_ffmpeg("ffmpeg")
    return _FFMPEG


def _seg_start_epoch(path: str | Path) -> float | None:
    m = _SEG_RE.search(str(path))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").timestamp()
    except ValueError:
        return None


@dataclass
class Frame:
    path: Path
    offset_s: float      # seconds from the start of the event clip
    epoch: float         # wall-clock time of the frame

    @property
    def clock(self) -> str:
        return datetime.fromtimestamp(self.epoch).strftime("%H:%M:%S")


class SegmentRecorder:
    """Keeps a rolling window of recent footage on disk, for one camera."""

    def __init__(self, cam: CameraConfig, cfg: Config | None = None) -> None:
        self.cam = cam
        self.cfg = cfg or config
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.started_at: float = 0.0

    # ------------------------------------------------------------- liveness
    def last_segment_age(self) -> float | None:
        """Seconds since the newest segment was written, or None if there are
        none at all."""
        newest = 0.0
        for p in self.cfg.segments_dir(self.cam).glob("*.mp4"):
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                continue
        return (time.time() - newest) if newest else None

    def producing_video(self) -> tuple[bool, str]:
        """Whether video is actually landing on disk.

        A running ffmpeg process is not proof of anything -- it will sit there
        retrying against a camera with RTSP switched off. Only new segment
        files show that capture genuinely works.
        """
        if not self.running:
            return False, "recorder is not running"

        age = self.last_segment_age()
        grace = SEGMENT_SECONDS * 3          # time for the first file to close
        if age is None:
            waited = time.time() - self.started_at if self.started_at else 0.0
            if waited < grace:
                return True, "starting up"
            return False, "no video recorded after %ds" % int(waited)
        if age > grace:
            return False, "last video was %ds ago" % int(age)
        return True, "recording"

    # --------------------------------------------------------------- control
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.cfg.ensure_dirs()
        self._stop.clear()
        self.started_at = time.time()
        self._thread = threading.Thread(
            target=self._run, name="segmenter-" + self.cam.slug, daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # A stalled camera stream can keep ffmpeg from exiting, so don't wait
        # long: kill it, so the disable button answers quickly.
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def running(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        """Supervise ffmpeg, restarting it if the camera drops the connection."""
        backoff = 2
        while not self._stop.is_set():
            try:
                self._spawn()
            except FileNotFoundError as e:
                self.last_error = str(e)
                log.error("%s", e)
                return
            rc = self._proc.wait() if self._proc else -1
            if self._stop.is_set():
                return
            tail = ""
            if self._proc and self._proc.stderr:
                tail = self._proc.stderr.read().decode("utf-8", "replace")[-400:]
            self.last_error = scrub(
                "ffmpeg exited rc=%s: %s" % (rc, tail.strip()[-200:]), self.cfg
            )
            log.warning("[%s] segmenter died (rc=%s), restarting in %ss",
                        self.cam.slug, rc, backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 60)

    def _spawn(self) -> None:
        seg_dir = self.cfg.segments_dir(self.cam)
        seg_dir.mkdir(parents=True, exist_ok=True)
        out = str(seg_dir / "%Y%m%d-%H%M%S.mp4")
        cmd = [
            ffmpeg_bin(),
            "-nostdin", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-use_wallclock_as_timestamps", "1",
            "-i", self.cam.rtsp_url,
            "-an",                      # audio is useless here and often absent
            "-c:v", "copy",             # no re-encode: ~0% CPU
            "-f", "segment",
            "-segment_time", str(SEGMENT_SECONDS),
            "-segment_format", "mp4",
            "-reset_timestamps", "1",
            "-strftime", "1",
            out,
        ]
        log.info("[%s] starting segmenter -> %s", self.cam.slug, seg_dir)
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )

    # ---------------------------------------------------------------- upkeep
    def prune(self) -> int:
        """Delete segments past the retention window. Returns count removed."""
        cutoff = time.time() - self.cfg.segment_retention_min * 60
        removed = 0
        for p in self.cfg.segments_dir(self.cam).glob("*.mp4"):
            start = _seg_start_epoch(p)
            ref = start if start is not None else p.stat().st_mtime
            if ref < cutoff:
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed


def segments_covering(cam: CameraConfig, start_epoch: float, end_epoch: float,
                      cfg: Config | None = None) -> list[Path]:
    """Segment files whose footage intersects [start, end], for one camera."""
    cfg = cfg or config
    hits: list[tuple[float, Path]] = []
    for p in cfg.segments_dir(cam).glob("*.mp4"):
        s = _seg_start_epoch(p)
        if s is None:
            continue
        # Allow slack: a segment can run slightly long if keyframes are sparse.
        e = s + SEGMENT_SECONDS * 1.5
        if e >= start_epoch and s <= end_epoch:
            hits.append((s, p))
    return [p for _, p in sorted(hits)]


def _run(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def build_event_clip(cam: CameraConfig, trigger_epoch: float, out_dir: Path,
                     cfg: Config | None = None,
                     wait: bool = True) -> tuple[Path | None, float]:
    """Concatenate the segments around a trigger into one clip.

    Returns (clip_path, clip_start_epoch). Waits for post-roll footage to land
    on disk before assembling.
    """
    cfg = cfg or config
    start = trigger_epoch - cfg.pre_roll
    end = trigger_epoch + cfg.post_roll

    if wait:
        # Wait for the post-roll window to be recorded, plus a segment of slack
        # so ffmpeg has closed the final file.
        wait_until = end + SEGMENT_SECONDS + 2
        while time.time() < wait_until:
            time.sleep(1)

    segs = segments_covering(cam, start, end, cfg)
    if not segs:
        log.warning("[%s] no segments cover the event window -- "
                    "is the segmenter running?", cam.slug)
        return None, start

    out_dir.mkdir(parents=True, exist_ok=True)
    listing = out_dir / "segments.txt"
    listing.write_text(
        "".join("file '" + p.as_posix() + "'\n" for p in segs), encoding="utf-8"
    )

    clip_start = _seg_start_epoch(segs[0]) or start
    offset = max(0.0, start - clip_start)
    duration = cfg.pre_roll + cfg.post_roll

    clip = out_dir / "clip.mp4"
    res = _run([
        ffmpeg_bin(), "-nostdin", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-ss", "%.2f" % offset, "-t", str(duration),
        "-c", "copy", "-movflags", "+faststart",
        str(clip),
    ])
    if res.returncode != 0 or not clip.exists():
        log.error("clip assembly failed: %s",
                  scrub(res.stderr.decode("utf-8", "replace")[-300:], cfg))
        return None, clip_start + offset

    listing.unlink(missing_ok=True)
    return clip, clip_start + offset


def extract_frames(clip: Path, out_dir: Path, fps: float = 1.0,
                   width: int = 1280, clip_start_epoch: float = 0.0) -> list[Frame]:
    """Sample frames out of an event clip at `fps`."""
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("*.jpg"):
        old.unlink()

    res = _run([
        ffmpeg_bin(), "-nostdin", "-loglevel", "error", "-y",
        "-i", str(clip),
        "-vf", "fps=%s,scale=%d:-2" % (fps, width),
        "-q:v", "3",
        str(frames_dir / "f_%04d.jpg"),
    ])
    if res.returncode != 0:
        log.error("frame extraction failed: %s",
                  scrub(res.stderr.decode("utf-8", "replace")[-300:]))

    frames: list[Frame] = []
    for i, p in enumerate(sorted(frames_dir.glob("f_*.jpg"))):
        offset = i / fps
        frames.append(Frame(path=p, offset_s=offset, epoch=clip_start_epoch + offset))
    return frames


def grab_single_frame(cam: CameraConfig, out_path: Path,
                      cfg: Config | None = None) -> bool:
    """One frame straight off the RTSP stream -- used by `probe`."""
    cfg = cfg or config
    out_path.parent.mkdir(parents=True, exist_ok=True)
    res = _run([
        ffmpeg_bin(), "-nostdin", "-loglevel", "error", "-y",
        "-rtsp_transport", "tcp", "-i", cam.rtsp_url,
        "-frames:v", "1", "-q:v", "3", str(out_path),
    ], timeout=40)
    return res.returncode == 0 and out_path.exists()
