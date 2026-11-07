"""Event pipeline: trigger in, verdict (and maybe an alert) out.

The funnel, cheapest stage first:

    0. camera AI      free   Reolink's own dog_cat detection fires
    1. local detector free   YOLO confirms a dog is really there   (optional)
    2. gate           free   local VLM screens for a squat posture
    3. verdict        $      Claude judges defecation + cleanup

Every stage records where it stopped, so the dashboard can show what was
filtered and why.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
from pathlib import Path

from . import detector, notify
from .analyze import run_gate, run_verdict, sample
from .capture import Frame, build_event_clip, extract_frames
from .config import CameraConfig, Config, config
from .store import Store
from .vision import ModelRefusal, VisionError

log = logging.getLogger(__name__)

# Several cameras can fire at once. Analysis is the expensive part (a local VLM
# wants the whole GPU; the API has rate limits), so cap how many run together.
# Capture deliberately sits *outside* this gate: it is mostly sleeping through
# the post-roll window, and holding a slot while idle would serialize cameras.
_analysis_slots = threading.BoundedSemaphore(max(1, config.max_concurrent_analyses))


def _pick_thumb(frames: list[Frame], offset_s: float, dest: Path) -> Path | None:
    """Copy the frame nearest `offset_s` out as the event thumbnail."""
    if not frames:
        return None
    if offset_s is None or offset_s < 0:
        chosen = frames[len(frames) // 2]
    else:
        chosen = min(frames, key=lambda f: abs(f.offset_s - offset_s))
    try:
        shutil.copy2(chosen.path, dest)
        return dest
    except OSError as e:
        log.warning("could not write thumbnail: %s", e)
        return None


def _thumb_rel(store: Store, frames: list[Frame], offset_s: float,
               event_dir: Path) -> str | None:
    thumb = _pick_thumb(frames, offset_s, event_dir / "thumb.jpg")
    return store.relative(thumb) if thumb else None


def process_trigger(cam: CameraConfig, trigger_epoch: float, store: Store,
                    cfg: Config | None = None,
                    *, notify_on_violation: bool = True,
                    wait_for_footage: bool = True) -> dict | None:
    """Run one camera trigger through the funnel. Returns the stored event."""
    cfg = cfg or config
    event_dir = store.event_dir(trigger_epoch, cam.slug)

    event_id = store.add_event(
        trigger_at=trigger_epoch,
        camera=cam.key,
        camera_slug=cam.slug,
        camera_name=cam.name,
        stage="triggered",
    )
    log.info("[%s] event %d: trigger at %.0f", cam.slug, event_id, trigger_epoch)

    # --- capture (no analysis slot held: this is mostly waiting) ------------
    clip, clip_start = build_event_clip(
        cam, trigger_epoch, event_dir, cfg, wait=wait_for_footage
    )
    if clip is None:
        store.update_event(event_id, stage="capture_failed",
                           error="no footage covered the event window")
        return store.get_event(event_id)

    frames = extract_frames(clip, event_dir, fps=1.0, clip_start_epoch=clip_start)
    if not frames:
        store.update_event(event_id, stage="capture_failed",
                           error="no frames could be extracted from the clip")
        return store.get_event(event_id)

    trigger_offset = max(0.0, trigger_epoch - clip_start)
    store.update_event(
        event_id,
        stage="captured",
        clip_path=store.relative(clip),
        frame_paths=json.dumps([store.relative(f.path) for f in frames]),
    )
    log.info("[%s] event %d: %d frames, trigger at t=%.0fs", cam.slug, event_id,
             len(frames), trigger_offset)

    # --- analysis (rate-limited across all cameras) -------------------------
    with _analysis_slots:
        return _analyze(cam, event_id, frames, trigger_offset, trigger_epoch,
                        event_dir, store, cfg,
                        notify_on_violation=notify_on_violation)


def _analyze(cam: CameraConfig, event_id: int, frames: list[Frame],
             trigger_offset: float, trigger_epoch: float, event_dir: Path,
             store: Store, cfg: Config,
             *, notify_on_violation: bool) -> dict | None:
    """Stages 1-3. Runs while holding an analysis slot."""

    # --- stage 1: free local detector ---------------------------------------
    if not cfg.disable_local_detector:
        local = detector.detect([f.path for f in frames])
        if local and not local.worth_escalating:
            log.info("[%s] event %d: no dog found locally, dropping",
                     cam.slug, event_id)
            store.update_event(
                event_id, stage="no_dog",
                reasoning="Local detector found no dog in any frame"
                          + (" (cat detected)" if local.cat else ""),
                thumb_path=_thumb_rel(store, frames, -1, event_dir),
            )
            return store.get_event(event_id)

    # --- stage 2: cheap posture gate ----------------------------------------
    try:
        gate = run_gate(frames, cfg)
    except (VisionError, ModelRefusal) as e:
        log.error("[%s] event %d: gate failed: %s", cam.slug, event_id, e)
        store.update_event(event_id, stage="gate_failed", error=str(e))
        return store.get_event(event_id)

    if gate is not None:
        store.update_event(event_id, gate_json=gate.model_dump_json())
        if gate.subject != "dog":
            # Only dogs are of interest. Stop here so the paid verdict never runs.
            log.info("[%s] event %d: not a dog (%s), skipping", cam.slug,
                     event_id, gate.subject)
            store.update_event(event_id, stage="not_dog", subject=gate.subject,
                               confidence=gate.confidence)
            return store.get_event(event_id)
        if not gate.likely_defecating:
            log.info("[%s] event %d: gate rejected (%s, %s)", cam.slug,
                     event_id, gate.subject, gate.posture)
            store.update_event(
                event_id, stage="gate_rejected", subject=gate.subject,
                confidence=gate.confidence,
                reasoning="Good " + gate.subject + "! Just " + gate.posture
                          + ", no squat in sight. Not today, champ.",
                thumb_path=_thumb_rel(store, frames, -1, event_dir),
            )
            return store.get_event(event_id)
        log.info("[%s] event %d: gate passed, escalating to verdict",
                 cam.slug, event_id)

    # --- stage 3: the real verdict ------------------------------------------
    try:
        verdict = run_verdict(frames, trigger_offset, cfg)
    except ModelRefusal as e:
        log.error("[%s] event %d: model declined: %s", cam.slug, event_id, e)
        store.update_event(event_id, stage="verdict_refused", error=str(e))
        return store.get_event(event_id)
    except VisionError as e:
        log.error("[%s] event %d: verdict failed: %s", cam.slug, event_id, e)
        store.update_event(event_id, stage="verdict_failed", error=str(e))
        return store.get_event(event_id)

    thumb = _thumb_rel(store, frames, verdict.defecation_offset_s, event_dir)
    store.update_event(
        event_id,
        stage="verdict",
        subject=verdict.subject,
        defecation=int(verdict.defecation_occurred),
        handler_present=int(verdict.handler_present),
        cleanup=int(verdict.cleanup_occurred),
        violation=int(verdict.is_violation),
        confidence=verdict.confidence,
        reasoning=verdict.reasoning,
        cleanup_evidence=verdict.cleanup_evidence,
        verdict_json=verdict.model_dump_json(),
        thumb_path=thumb,
    )
    log.info("[%s] event %d: subject=%s defecation=%s cleanup=%s violation=%s",
             cam.slug, event_id, verdict.subject, verdict.defecation_occurred,
             verdict.cleanup_occurred, verdict.is_violation)

    # --- alert ---------------------------------------------------------------
    if verdict.is_violation and notify_on_violation:
        evidence = [f.path for f in sample(frames, 4)]
        if thumb:
            evidence.insert(0, cfg.evidence_dir / thumb)
        ok = notify.alert(verdict, trigger_epoch, evidence, cfg, camera=cam.name)
        store.update_event(event_id, notified=int(ok),
                           stage="notified" if ok else "verdict")

    return store.get_event(event_id)


def replay_clip(clip: Path, store: Store, cfg: Config | None = None,
                *, notify_on_violation: bool = False) -> dict | None:
    """Run an existing video file through the verdict stage. Useful for testing
    the classifier against footage you already have, with no camera involved."""
    cfg = cfg or config
    trigger_epoch = clip.stat().st_mtime
    event_dir = store.event_dir(trigger_epoch, "replay")
    event_id = store.add_event(
        trigger_at=trigger_epoch,
        camera="replay:" + clip.name,
        camera_slug="replay",
        camera_name="replay",
        stage="captured",
    )

    local_clip = event_dir / "clip.mp4"
    if clip.resolve() != local_clip.resolve():
        shutil.copy2(clip, local_clip)

    frames = extract_frames(local_clip, event_dir, fps=1.0,
                           clip_start_epoch=trigger_epoch)
    if not frames:
        store.update_event(event_id, stage="capture_failed",
                           error="could not extract frames from " + clip.name)
        return store.get_event(event_id)

    store.update_event(
        event_id,
        clip_path=store.relative(local_clip),
        frame_paths=json.dumps([store.relative(f.path) for f in frames]),
    )

    try:
        verdict = run_verdict(frames, 0.0, cfg)
    except (VisionError, ModelRefusal) as e:
        store.update_event(event_id, stage="verdict_failed", error=str(e))
        return store.get_event(event_id)

    store.update_event(
        event_id,
        stage="verdict",
        subject=verdict.subject,
        defecation=int(verdict.defecation_occurred),
        handler_present=int(verdict.handler_present),
        cleanup=int(verdict.cleanup_occurred),
        violation=int(verdict.is_violation),
        confidence=verdict.confidence,
        reasoning=verdict.reasoning,
        cleanup_evidence=verdict.cleanup_evidence,
        verdict_json=verdict.model_dump_json(),
        thumb_path=_thumb_rel(store, frames, verdict.defecation_offset_s, event_dir),
    )
    if verdict.is_violation and notify_on_violation:
        notify.alert(verdict, trigger_epoch, [f.path for f in frames[:4]], cfg,
                     camera="replay")
    return store.get_event(event_id)
