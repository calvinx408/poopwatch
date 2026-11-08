"""FastAPI backend for the review dashboard.

Serves the event log, the evidence frames/clips, and live per-camera watcher
status to the React app in web/.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.params import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, configstore, notify, secrets, settings_db
from .capture import ffmpeg_bin
from .config import config
from .store import Store
from .vision import describe_backend, ollama_available
from .watcher import WatcherPool

log = logging.getLogger(__name__)

REVIEW_STATES = ("unreviewed", "confirmed", "false_positive", "handled")

store = Store(config)
pool = WatcherPool(config, store)

app = FastAPI(title="PoopWatch", version="0.2.0")

# The Vite dev server runs on a different port during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_dashboard_password(request, call_next):
    """With a dashboard password set, every request must send it. Without one,
    this does nothing. CORS preflights pass through so the browser can send
    the real request."""
    expected = auth.password()
    if expected is None or request.method == "OPTIONS":
        return await call_next(request)
    if auth.check_basic(request.headers.get("authorization"), expected):
        return await call_next(request)
    return JSONResponse(
        status_code=401,
        content={"detail": "dashboard password required"},
        headers={"WWW-Authenticate": 'Basic realm="PoopWatch"'},
    )


class ReviewBody(BaseModel):
    review: str


class NoteBody(BaseModel):
    note: str = ""


class CameraBody(BaseModel):
    name: str
    host: str
    channel: int = 0
    user: str = "admin"
    stream: str = "main"
    scheme: str = "auto"
    verify_tls: bool = False
    # Write-only. Blank on an edit means "keep the stored password".
    password: str = ""


class SettingsBody(BaseModel):
    capture: dict = {}
    models: dict = {}
    alerts: dict = {}
    cameras: list[CameraBody] = []


class VerdictModelBody(BaseModel):
    verdict_model: str


def _port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    import socket as _socket

    try:
        with _socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ----------------------------------------------------------------- settings
def _camera_view(cam) -> dict:
    """Camera settings for the UI. Never includes the password itself."""
    watcher = pool.get(cam.slug)
    state, detail = watcher.state() if watcher else ("stopped", "Not started")
    return {
        "slug": cam.slug,
        "name": cam.name,
        "host": cam.host,
        "channel": cam.channel,
        "user": cam.user,
        "stream": cam.stream,
        "scheme": cam.scheme,
        "verify_tls": cam.verify_tls,
        "has_password": bool(cam.password),
        "password_source": cam.password_source,
        "state": state,
        "state_detail": detail,
    }


@app.get("/api/settings")
def get_settings() -> dict:
    can_write, reason = configstore.writes_allowed()
    keyring_ok, keyring_detail = secrets.keyring_available()
    return {
        "editable": can_write,
        "editable_detail": reason,
        "source": config.source_summary,
        "keyring_available": keyring_ok,
        "keyring_detail": keyring_detail,
        "plaintext_passwords": [c.name for c in config.plaintext_cameras],
        "capture": {
            "pre_roll_seconds": config.pre_roll,
            "post_roll_seconds": config.post_roll,
            "segment_retention_minutes": config.segment_retention_min,
        },
        "models": {
            "gate_backend": config.gate_backend,
            "ollama_url": config.ollama_url,
            "ollama_model": config.ollama_model,
            "verdict_model": config.verdict_model,
            "disable_local_detector": config.disable_local_detector,
            "max_concurrent_analyses": config.max_concurrent_analyses,
        },
        "alerts": {
            "ntfy_topic": config.ntfy_topic,
            "ntfy_server": config.ntfy_server,
            "smtp_host": config.smtp_host,
            "smtp_port": config.smtp_port,
            "smtp_user": config.smtp_user,
            "smtp_password": "",          # write-only
            "has_smtp_password": bool(config.smtp_password),
            "smtp_password_source": (
                "keyring" if secrets.get(configstore.SMTP_KEY) else
                "environment" if os.getenv("SMTP_PASSWORD") else
                "database" if config.smtp_password else "none"
            ),
            "email_to": config.alert_email_to,
            "notify_good": config.notify_good,
            "notify_walkbys": config.notify_walkbys,
        },
        "cameras": [_camera_view(c) for c in config.cameras],
        "choices": {
            "gate_backends": list(configstore.GATE_BACKENDS),
            "streams": list(configstore.STREAMS),
        },
    }


@app.put("/api/settings")
def put_settings(body: SettingsBody) -> dict:
    payload = {
        "capture": body.capture,
        "models": body.models,
        "alerts": dict(body.alerts),
        "cameras": [cam.model_dump() for cam in body.cameras],
    }

    try:
        warnings = configstore.save(payload)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except configstore.SettingsError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OSError as e:
        raise HTTPException(
            status_code=500, detail="could not save settings: " + str(e)
        ) from e

    pool.rebuild()
    return {"ok": True, "warnings": warnings, "settings": get_settings()}


@app.put("/api/settings/verdict-model")
def put_verdict_model(body: VerdictModelBody) -> dict:
    """Quick update to just the verdict model."""
    try:
        configstore.require_writes()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e

    try:
        current = settings_db.load()
        current.setdefault('models', {})['verdict_model'] = body.verdict_model
        settings_db.save_all(current)
        config.reload()
    except OSError as e:
        raise HTTPException(
            status_code=500, detail="could not save: " + str(e)
        ) from e

    return {"ok": True, "verdict_model": config.verdict_model}


class AlertTestBody(BaseModel):
    channel: str = "both"            # "ntfy" | "email" | "both"
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_to: str = ""


@app.post("/api/alerts/test")
def test_alerts(body: AlertTestBody) -> dict:
    """Send a real test notification using the values supplied.

    Uses the submitted (possibly unsaved) values so settings can be checked
    before committing them. A blank SMTP password falls back to the stored one.
    """
    try:
        configstore.require_writes()
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e

    # Only the password falls back to the stored value, so it needn't be
    # retyped. Every other field is taken exactly as submitted: if the user
    # cleared the host or recipient, that is what gets validated -- falling
    # back there would silently test a different target than the form shows.
    password = body.smtp_password or configstore.smtp_password()

    steps: list[dict] = []
    if body.channel in ("ntfy", "both"):
        steps.append(notify.test_ntfy(body.ntfy_topic, body.ntfy_server))
    if body.channel in ("email", "both"):
        steps.append(notify.test_email(
            body.smtp_host,
            int(body.smtp_port or 587),
            body.smtp_user,
            password,
            body.email_to,
        ))

    if not steps:
        raise HTTPException(status_code=400,
                            detail="channel must be ntfy, email, or both")

    ok = any(s["ok"] for s in steps)
    return {
        "ok": ok,
        "steps": steps,
        "detail": "Test sent." if ok else "Nothing could be sent.",
    }


@app.post("/api/cameras/test")
def test_camera(body: CameraBody) -> dict:
    """Try the API and RTSP for a camera, without saving it.

    This is the answer to 'is it actually connected?' -- it reports each step
    separately so a wrong password looks different from a wrong IP.
    """
    from .capture import grab_single_frame, scrub
    from .config import CameraConfig
    from .reolink import ReolinkClient, ReolinkError

    password = body.password
    source = "provided"
    if not password:
        existing = next(
            (c for c in config.cameras
             if c.host == body.host and c.channel == body.channel), None
        )
        if existing:
            password, source = existing.password, existing.password_source
    if not password:
        return {
            "ok": False, "step": "password",
            "detail": "No password given, and none is stored for this camera.",
        }

    cam = CameraConfig(
        name=body.name or "test", host=body.host, user=body.user or "admin",
        password=password, channel=body.channel, stream=body.stream or "main",
        scheme=body.scheme or "auto", verify_tls=body.verify_tls,
    )

    steps: list[dict] = []
    client = ReolinkClient(cam, config)
    try:
        info = client.device_info()
        steps.append({
            "name": "API login", "ok": True,
            "detail": str(info.get("name") or info.get("model") or "connected"),
        })
    except ReolinkError as e:
        steps.append({"name": "API login", "ok": False, "detail": scrub(str(e), config)})
        return {"ok": False, "step": "api", "steps": steps,
                "password_source": source, "snapshot": None,
                "detail": "Could not log in. Check the IP, username, and password."}

    try:
        state = client.get_ai_state()
        has_ai = "dog_cat" in state.supports
        steps.append({
            "name": "Animal AI detection", "ok": has_ai,
            "detail": ("supported -- free first-stage filter"
                       if has_ai else
                       "not supported; will fall back to motion detection"),
        })
    except ReolinkError as e:
        steps.append({"name": "Animal AI detection", "ok": False,
                      "detail": scrub(str(e), config)})

    # A closed port 554 is by far the most common cause of "no video", and
    # checking it takes a second instead of waiting 40s for ffmpeg to give up.
    rtsp_open = _port_open(cam.host, 554)
    if not rtsp_open:
        steps.append({
            "name": "RTSP video", "ok": False,
            "detail": "Port 554 is closed. Enable RTSP on the camera: "
                      "Settings > Network > Advanced > Port Settings > RTSP.",
        })
        return {"ok": False, "step": "rtsp", "steps": steps,
                "password_source": source, "snapshot": None,
                "detail": "Logged in fine, but RTSP is turned off on this camera."}

    probe = config.evidence_dir / ("probe-" + cam.slug + ".jpg")
    try:
        got = grab_single_frame(cam, probe, config)
    except Exception as e:
        got, _ = False, e
    steps.append({
        "name": "RTSP video", "ok": got,
        "detail": ("captured a frame" if got else
                   "no video. Check that RTSP is enabled on the camera."),
    })

    ok = all(s["ok"] for s in steps if s["name"] != "Animal AI detection")
    return {
        "ok": ok,
        "steps": steps,
        "password_source": source,
        "snapshot": "/evidence/" + probe.name if got else None,
        "detail": "Camera is reachable and streaming." if ok
                  else "Camera did not fully check out.",
    }


@app.get("/api/status")
def get_status() -> dict:
    ok, detail = (
        ollama_available(config) if config.gate_backend == "ollama" else (True, "")
    )
    return {
        "watcher": pool.snapshot_status(),
        "stats": store.stats(),
        "demo_events": store.count_demo(),
        "cameras": [
            {"slug": c.slug, "name": c.name, "host": c.host,
             "channel": c.channel, "stream": c.stream}
            for c in config.cameras
        ],
        "config": {
            "camera_count": len(config.cameras),
            "config_source": config.source_summary,
            "pre_roll": config.pre_roll,
            "post_roll": config.post_roll,
            "gate": describe_backend(config.gate_backend, config),
            "verdict_model": config.verdict_model,
            "max_concurrent_analyses": config.max_concurrent_analyses,
            "gate_ready": ok,
            "gate_detail": detail,
            "available_models": ["ollama", "claude-haiku-3-5", "claude-opus-5"],
        },
        "server_time": time.time(),
    }


@app.get("/api/camera/{slug}/snapshot-debug")
def debug_camera_snapshot(slug: str = PathParam(...)) -> dict:
    """Debug endpoint to check segment availability."""
    cam = config.camera(slug)
    if not cam:
        return {"error": "camera not found", "slug": slug}

    seg_dir = config.segments_dir(cam)
    segs = list(seg_dir.glob("*.mp4"))
    return {
        "camera": cam.name,
        "slug": slug,
        "segment_dir": str(seg_dir),
        "segments_found": len(segs),
        "segments": [s.name for s in sorted(segs, key=lambda p: p.stat().st_mtime, reverse=True)[:3]],
    }


@app.get("/api/camera/{slug}/snapshot")
def get_camera_snapshot(slug: str = PathParam(...)) -> FileResponse:
    """Extract a frame from the latest segment (rolling buffer)."""
    cam = config.camera(slug)
    if not cam:
        raise HTTPException(status_code=404, detail="no such camera")

    seg_dir = config.segments_dir(cam)
    segs = list(seg_dir.glob("*.mp4"))
    log.debug("[%s] segments dir: %s, found %d files", slug, seg_dir, len(segs))

    if not segs:
        log.warning("[%s] no segments found", slug)
        raise HTTPException(status_code=503, detail="camera not recording")

    # ffmpeg is still writing the newest segment; its moov atom only exists
    # once the segment closes, so skip anything modified in the last few seconds.
    now = time.time()
    settled = [p for p in segs if now - p.stat().st_mtime >= 5]
    if not settled:
        raise HTTPException(status_code=503, detail="camera not recording yet")
    # A segment can be damaged when the camera drops its stream mid-write, so
    # try the newest few and use the first one that decodes.
    import subprocess

    candidates = sorted(settled, key=lambda p: p.stat().st_mtime, reverse=True)[:3]
    out_jpg = config.evidence_dir / f"snapshot-{slug}.jpg"
    last_error = "no usable segment"
    for seg in candidates:
        out_jpg.unlink(missing_ok=True)
        try:
            res = subprocess.run([
                ffmpeg_bin(), "-nostdin", "-loglevel", "error", "-y",
                "-i", str(seg),
                "-frames:v", "1", "-q:v", "3",
                str(out_jpg),
            ], capture_output=True, timeout=5)
        except subprocess.TimeoutExpired:
            last_error = "extraction timed out"
            continue
        if res.returncode != 0:
            last_error = f"ffmpeg rc={res.returncode}"
            continue
        # ffmpeg can exit 0 yet still report "error while decoding": the frame
        # is then partly corrupt (colour stripes), so treat it as a bad segment.
        if res.stderr.strip():
            last_error = "segment has decoding errors"
            continue
        if out_jpg.exists():
            return FileResponse(out_jpg, media_type="image/jpeg")
        last_error = "no output file"

    log.warning("[%s] snapshot failed after %d segment(s): %s",
                slug, len(candidates), last_error)
    raise HTTPException(status_code=500, detail=last_error)


@app.get("/api/events")
def list_events(limit: int = 60, offset: int = 0,
                only_violations: bool = False,
                include_rejected: bool = True,
                walkbys: bool = False,
                camera: str | None = None) -> dict:
    limit = max(1, min(limit, 200))
    if camera and not config.camera(camera) and camera != "replay":
        raise HTTPException(status_code=404, detail="no such camera: " + camera)
    events = store.list_events(
        limit=limit, offset=offset,
        only_violations=only_violations,
        include_rejected=include_rejected,
        walkbys=walkbys,
        camera=camera,
    )
    return {"events": events, "limit": limit, "offset": offset, "camera": camera}


@app.get("/api/events/{event_id}")
def get_event(event_id: int) -> dict:
    event = store.get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="no such event")
    return event


RETRYABLE_STAGES = ("interrupted", "capture_failed")


@app.post("/api/events/{event_id}/retry")
def retry_event(event_id: int) -> dict:
    """Capture again for an event whose analysis never finished. The old card
    is replaced by a new event at the same trigger time."""
    from .pipeline import process_trigger

    event = store.get_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="no such event")
    if event["stage"] not in RETRYABLE_STAGES:
        raise HTTPException(
            status_code=409,
            detail="only interrupted or failed captures can be retried",
        )
    cam = config.camera(event["camera_slug"])
    if not cam:
        raise HTTPException(status_code=409,
                            detail="camera " + event["camera_slug"] + " is not configured")
    trigger_epoch = event["trigger_at"]
    store.delete_event(event_id)
    threading.Thread(
        target=process_trigger,
        args=(cam, trigger_epoch, store, config),
        daemon=True,
    ).start()
    return {"ok": True}


@app.delete("/api/events/{event_id}")
def delete_event(event_id: int) -> dict:
    if not store.delete_event(event_id):
        raise HTTPException(status_code=404, detail="no such event")
    return {"ok": True}


@app.post("/api/events/{event_id}/note")
def set_review_note(event_id: int, body: NoteBody) -> dict:
    if not store.get_event(event_id):
        raise HTTPException(status_code=404, detail="no such event")
    store.set_review_note(event_id, body.note)
    return {"ok": True, "event": store.get_event(event_id)}


@app.post("/api/events/{event_id}/review")
def set_review(event_id: int, body: ReviewBody) -> dict:
    if body.review not in REVIEW_STATES:
        raise HTTPException(
            status_code=400,
            detail="review must be one of " + ", ".join(REVIEW_STATES),
        )
    if not store.get_event(event_id):
        raise HTTPException(status_code=404, detail="no such event")
    store.set_review(event_id, body.review)
    return {"ok": True, "event": store.get_event(event_id)}


@app.post("/api/watcher/start")
def start_watcher(camera: str | None = None) -> dict:
    """Start every camera, or just one if `camera` names a slug."""
    if camera:
        watcher = pool.get(camera)
        if not watcher:
            raise HTTPException(status_code=404, detail="no such camera: " + camera)
        watcher.start()
    else:
        pool.start()
    return pool.snapshot_status()


@app.post("/api/watcher/stop")
def stop_watcher(camera: str | None = None) -> dict:
    if camera:
        watcher = pool.get(camera)
        if not watcher:
            raise HTTPException(status_code=404, detail="no such camera: " + camera)
        watcher.stop()
    else:
        pool.stop()
    return pool.snapshot_status()


@app.post("/api/test-trigger")
def test_trigger(camera: str | None = None) -> JSONResponse:
    """Fire the pipeline by hand against live footage -- handy for aiming a
    camera and sanity-checking the classifier. Alerts are suppressed."""
    from .pipeline import process_trigger

    candidates = [pool.get(camera)] if camera else list(pool.watchers)
    if camera and not candidates[0]:
        raise HTTPException(status_code=404, detail="no such camera: " + camera)

    ready = [w for w in candidates if w and w.recorder.running]
    if not ready:
        raise HTTPException(
            status_code=409,
            detail="no recorder is running, so there is no footage to analyze",
        )

    for watcher in ready:
        threading.Thread(
            target=process_trigger,
            args=(watcher.cam, time.time(), store, config),
            kwargs={"notify_on_violation": False},
            daemon=True,
            name="test-" + watcher.cam.slug,
        ).start()

    return JSONResponse({
        "ok": True,
        "cameras": [w.cam.name for w in ready],
        "detail": "capturing for " + str(config.post_roll) + "s, then analyzing",
    })


# ------------------------------------------------------------------ statics
config.ensure_dirs()
app.mount("/evidence", StaticFiles(directory=str(config.evidence_dir)), name="evidence")

_dist = Path(__file__).resolve().parent.parent / "web" / "dist"
if _dist.exists():
    app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_dist / "index.html")

    @app.get("/{path:path}")
    def spa(path: str) -> FileResponse:
        """Serve the SPA for any unmatched route."""
        candidate = _dist / path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_dist / "index.html")
else:
    @app.get("/")
    def no_build() -> JSONResponse:
        return JSONResponse(
            {
                "detail": "UI not built yet",
                "fix": "cd web && npm install && npm run build",
                "dev": "or run `npm run dev` for hot reload on :5173",
            },
            status_code=503,
        )
