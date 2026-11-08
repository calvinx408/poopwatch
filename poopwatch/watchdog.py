"""Watchdog: restart a camera whose recording has silently stopped, and tell
the user when a camera stays down.

The recorder restarts ffmpeg when it exits, but ffmpeg can also keep running
without writing any video. This loop catches that case by checking that each
enabled camera has written a new segment recently.
"""
from __future__ import annotations

import logging
import threading
import time

from . import notify
from .config import Config, config

log = logging.getLogger(__name__)

CHECK_SECONDS = 30      # how often to look
RESTART_AFTER = 120     # no new video for this long -> restart the recorder
ALERT_AFTER = 600       # still down after this long -> tell the user


def start(pool, cfg: Config | None = None) -> threading.Thread:
    cfg = cfg or config
    thread = threading.Thread(target=_loop, args=(pool, cfg),
                              name="watchdog", daemon=True)
    thread.start()
    return thread


def _loop(pool, cfg: Config) -> None:
    down_since: dict[str, float] = {}
    last_restart: dict[str, float] = {}
    alerted: set[str] = set()

    while True:
        time.sleep(CHECK_SECONDS)
        now = time.time()
        for watcher in list(pool.watchers):
            slug = watcher.cam.slug
            # Disabled cameras are meant to be off; leave them alone.
            if not watcher.status.running:
                down_since.pop(slug, None)
                continue

            healthy, _ = watcher.recorder.producing_video()
            if healthy:
                down_since.pop(slug, None)
                if slug in alerted:
                    alerted.discard(slug)
                    notify.alert_camera_back(watcher.cam.name, cfg)
                continue

            since = down_since.setdefault(slug, now)
            down_for = now - since

            if down_for >= RESTART_AFTER and now - last_restart.get(slug, 0) >= RESTART_AFTER:
                log.warning("[%s] no new video for %ds; restarting the recorder",
                            slug, int(down_for))
                last_restart[slug] = now
                try:
                    watcher.recorder.stop()
                    watcher.recorder.start()
                except Exception as e:
                    log.error("[%s] recorder restart failed: %s", slug, e)

            if down_for >= ALERT_AFTER and slug not in alerted:
                alerted.add(slug)
                log.warning("[%s] still no video after %d minutes; alerting",
                            slug, int(down_for / 60))
                notify.alert_camera_down(watcher.cam.name, int(down_for / 60), cfg)
