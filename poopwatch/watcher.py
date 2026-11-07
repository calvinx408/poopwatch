"""The always-on watcher: rolling capture + trigger polling, per camera.

Each camera gets its own recorder, poll loop, and cooldown, so a camera that
goes offline retries on its own without stalling the others. A `WatcherPool`
starts and stops them as a unit.

On a rising edge (nothing -> dog) the timestamp is handed to the pipeline in a
worker thread, because processing blocks for the whole post-roll window and the
poll loop must stay responsive.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from .capture import SegmentRecorder
from .config import CameraConfig, Config, config
from .pipeline import process_trigger
from .reolink import ReolinkClient, ReolinkError
from .store import Store

log = logging.getLogger(__name__)

POLL_INTERVAL = 2.0
PRUNE_INTERVAL = 60.0


@dataclass
class WatcherStatus:
    running: bool = False
    camera_ok: bool = False
    last_poll: float = 0.0
    last_trigger: float = 0.0
    triggers_seen: int = 0
    in_flight: int = 0
    detect_source: str = "unknown"
    last_error: str | None = None
    supports: list[str] = field(default_factory=list)


class CameraWatcher:
    """Watches exactly one camera."""

    def __init__(self, cam: CameraConfig, store: Store,
                 cfg: Config | None = None) -> None:
        self.cam = cam
        self.cfg = cfg or config
        self.store = store
        self.recorder = SegmentRecorder(cam, self.cfg)
        self.client = ReolinkClient(cam, self.cfg)
        self.status = WatcherStatus()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._cooldown_until = 0.0

    # --------------------------------------------------------------- control
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.recorder.start()
        self._thread = threading.Thread(
            target=self._loop, name="watch-" + self.cam.slug, daemon=True
        )
        self._thread.start()
        self.status.running = True
        log.info("[%s] watching %s", self.cam.slug, self.cam.label)

    def stop(self) -> None:
        self._stop.set()
        self.recorder.stop()
        if self._thread:
            self._thread.join(timeout=10)
        self.status.running = False
        log.info("[%s] stopped", self.cam.slug)

    @property
    def healthy(self) -> bool:
        return (self.status.running and self.status.camera_ok
                and self.recorder.producing_video()[0])

    def state(self) -> tuple[str, str]:
        """A single unambiguous state, so the UI never has to guess whether a
        camera is off, still connecting, or actually broken."""
        if not self.cam.password:
            return "unconfigured", "No password set for this camera"
        if not self.status.running:
            return "stopped", "Not started"
        if not self.status.last_poll:
            return "connecting", "Contacting " + self.cam.host + "…"
        if not self.status.camera_ok:
            return "unreachable", (
                self.status.last_error or "Cannot reach " + self.cam.host
            )
        if not self.recorder.running:
            return "no_video", (
                self.recorder.last_error or "Camera responds, but no RTSP video"
            )
        flowing, why = self.recorder.producing_video()
        if not flowing:
            return "no_video", (
                "Logged in, but no video is being recorded (" + why + "). "
                "Check that RTSP is enabled on the camera: "
                "Settings > Network > Advanced > Port Settings."
            )
        if why == "starting up":
            return "connecting", "Logged in; waiting for the first video segment…"
        return "live", "Connected and recording"

    # ------------------------------------------------------------------ loop
    def _loop(self) -> None:
        last_prune = 0.0
        armed = True  # False while the detector still asserts the previous event

        while not self._stop.is_set():
            now = time.time()
            self.status.last_poll = now
            if self.recorder.last_error:
                self.status.last_error = self.recorder.last_error

            try:
                state = self.client.get_ai_state()
                self.status.camera_ok = True
                self.status.supports = sorted(state.supports)
                detected, self.status.detect_source = state.trigger()
            except ReolinkError as e:
                self.status.camera_ok = False
                self.status.last_error = str(e)
                log.warning("[%s] poll failed: %s", self.cam.slug, e)
                self._stop.wait(5)
                continue

            # Rising edge only, and respect the cooldown so one long visit
            # doesn't spawn a dozen overlapping events.
            if detected and armed and now >= self._cooldown_until:
                armed = False
                self._cooldown_until = now + self.cfg.post_roll + 30
                self.status.last_trigger = now
                self.status.triggers_seen += 1
                self._dispatch(now)
            elif not detected:
                armed = True

            if now - last_prune > PRUNE_INTERVAL:
                removed = self.recorder.prune()
                if removed:
                    log.debug("[%s] pruned %d old segments", self.cam.slug, removed)
                last_prune = now

            self._stop.wait(POLL_INTERVAL)

    # ---------------------------------------------------------------- worker
    def _dispatch(self, trigger_epoch: float) -> None:
        def work() -> None:
            with self._lock:
                self.status.in_flight += 1
            try:
                process_trigger(self.cam, trigger_epoch, self.store, self.cfg)
            except Exception as e:  # never let one bad event kill the watcher
                log.exception("[%s] event processing crashed: %s", self.cam.slug, e)
                self.status.last_error = "event processing crashed: " + str(e)
            finally:
                with self._lock:
                    self.status.in_flight -= 1

        threading.Thread(target=work, name="event-" + self.cam.slug,
                         daemon=True).start()
        log.info("[%s] dispatched event worker for trigger at %.0f",
                 self.cam.slug, trigger_epoch)

    # ---------------------------------------------------------------- status
    def snapshot_status(self) -> dict:
        s = self.status
        state, detail = self.state()
        return {
            "slug": self.cam.slug,
            "name": self.cam.name,
            "host": self.cam.host,
            "channel": self.cam.channel,
            "stream": self.cam.stream,
            "state": state,
            "state_detail": detail,
            "password_source": self.cam.password_source,
            "running": s.running,
            "recorder_up": self.recorder.running,
            "video_flowing": self.recorder.producing_video()[0],
            "camera_ok": s.camera_ok,
            "healthy": self.healthy,
            "last_poll": s.last_poll,
            "last_trigger": s.last_trigger,
            "triggers_seen": s.triggers_seen,
            "in_flight": s.in_flight,
            "detect_source": s.detect_source,
            "supports": s.supports,
            "last_error": s.last_error,
            "cooldown_remaining": max(0.0, self._cooldown_until - time.time()),
        }


class WatcherPool:
    """Starts and stops one CameraWatcher per configured camera."""

    def __init__(self, cfg: Config | None = None, store: Store | None = None) -> None:
        self.cfg = cfg or config
        self.store = store or Store(self.cfg)
        self.watchers: list[CameraWatcher] = [
            CameraWatcher(cam, self.store, self.cfg) for cam in self.cfg.cameras
        ]

    def start(self) -> None:
        if not self.watchers:
            log.error("no cameras configured -- nothing to watch")
            return
        for w in self.watchers:
            w.start()

    def stop(self) -> None:
        for w in self.watchers:
            w.stop()

    def get(self, slug: str) -> CameraWatcher | None:
        return next((w for w in self.watchers if w.cam.slug == slug), None)

    def rebuild(self) -> None:
        """Rebuild watchers after a settings change.

        Cameras that were running before are restarted, so saving an unrelated
        setting doesn't silently stop recording.
        """
        was_running = {w.cam.slug for w in self.watchers if w.status.running}
        any_running = bool(was_running)
        for w in self.watchers:
            w.stop()

        self.watchers = [
            CameraWatcher(cam, self.store, self.cfg) for cam in self.cfg.cameras
        ]
        for w in self.watchers:
            # Restart a camera that was running, and start brand-new cameras
            # too if the pool was active at all.
            if w.cam.slug in was_running or (any_running and w.cam.password):
                w.start()
        log.info("rebuilt watcher pool: %d camera(s)", len(self.watchers))

    @property
    def running(self) -> bool:
        """True if every configured camera is up."""
        return bool(self.watchers) and all(w.healthy for w in self.watchers)

    @property
    def any_running(self) -> bool:
        return any(w.healthy for w in self.watchers)

    def snapshot_status(self) -> dict:
        cams = [w.snapshot_status() for w in self.watchers]
        return {
            "running": any(c["running"] for c in cams),
            "all_healthy": self.running,
            "camera_count": len(cams),
            "live_count": sum(1 for c in cams if c["state"] == "live"),
            "unconfigured_count": sum(
                1 for c in cams if c["state"] == "unconfigured"
            ),
            "cameras": cams,
            "in_flight": sum(c["in_flight"] for c in cams),
            "triggers_seen": sum(c["triggers_seen"] for c in cams),
            "errors": [
                {"camera": c["name"], "error": c["last_error"]}
                for c in cams if c["last_error"]
            ],
        }


# Backwards-compatible alias: the single-camera class name used previously.
Watcher = WatcherPool
