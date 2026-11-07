"""PoopWatch CLI.

    python -m poopwatch probe        check every dependency and all cameras
    python -m poopwatch watch        run the watchers in the foreground
    python -m poopwatch serve        run the dashboard (+ watchers)
    python -m poopwatch replay FILE  classify an existing video file
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from .config import config

OK, BAD, WARN = "[ ok ]", "[FAIL]", "[warn]"


def _setup_logging(verbose: bool) -> None:
    from logging.handlers import TimedRotatingFileHandler

    # Keep a log file under evidence/logs/: today's file, plus yesterday's
    # after midnight. Older days are deleted by the rotation itself.
    logs_dir = config.evidence_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        logs_dir / "poopwatch.log", when="midnight", backupCount=1,
        encoding="utf-8")

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(), file_handler],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# ----------------------------------------------------------------- commands
def _probe_camera(cam, args) -> int:
    """Check one camera. Returns the number of blocking failures."""
    from .capture import grab_single_frame
    from .reolink import ReolinkClient, ReolinkError

    failures = 0
    print()
    print("--- " + cam.label + " ---")

    if not cam.password:
        print(BAD, "no password set for this camera")
        return 1

    client = ReolinkClient(cam, config)
    try:
        info = client.device_info()
        name = info.get("name") or info.get("model") or "camera"
        print(OK, "API reachable:", name, "(" + str(info.get("model", "?")) + ")")
    except ReolinkError as e:
        print(BAD, "API unreachable:", e)
        return failures + 1
    except Exception as e:
        print(BAD, "unexpected API error:", e)
        return failures + 1

    try:
        state = client.get_ai_state()
        if "dog_cat" in state.supports:
            print(OK, "animal AI detection available (free stage 0 filter)")
        else:
            print(WARN, "no animal AI; falling back to motion")
            print("      expect more triggers, so more gate calls")
        print("      supports:", ", ".join(sorted(state.supports)) or "none")
        print("      right now: dog_cat=" + str(state.dog_cat)
              + " people=" + str(state.people)
              + " motion=" + str(state.motion))
    except ReolinkError as e:
        print(WARN, "AI state unavailable:", e)

    try:
        client.snapshot()
        print(OK, "snapshot works")
    except ReolinkError as e:
        print(WARN, "snapshot failed:", e)

    if args.skip_rtsp:
        print(WARN, "RTSP check skipped (--skip-rtsp)")
        return failures

    print("      testing RTSP (up to 40s)...", flush=True)
    probe_frame = config.evidence_dir / ("probe-" + cam.slug + ".jpg")
    try:
        if grab_single_frame(cam, probe_frame, config):
            kb = probe_frame.stat().st_size // 1024
            print(OK, "RTSP frame captured (" + str(kb) + " KB) ->", probe_frame)
        else:
            print(BAD, "could not pull a frame from", cam.redacted_rtsp())
            print("      check the password, and that RTSP is enabled")
            failures += 1
    except Exception as e:
        print(BAD, "RTSP probe error:", e)
        failures += 1
    return failures


def cmd_probe(args: argparse.Namespace) -> int:
    from . import detector, notify
    from .vision import ollama_available

    failures = 0
    print("PoopWatch probe")
    print("=" * 58)

    # --- config ---
    missing = config.missing()
    if missing:
        print(BAD, "config incomplete, missing:", ", ".join(missing))
        print("      add a camera in the dashboard: python -m poopwatch serve")
        failures += 1
    else:
        print(OK, "settings loaded")
    print("      source    " + config.source_summary)
    print("      cameras   " + str(len(config.cameras)))
    print("      gate      " + config.gate_backend)
    print("      verdict   " + config.verdict_model)
    print("      analyses  " + str(config.max_concurrent_analyses) + " concurrent")

    # --- ffmpeg ---
    try:
        from .capture import ffmpeg_bin
        print(OK, "ffmpeg at", ffmpeg_bin())
    except FileNotFoundError as e:
        print(BAD, str(e))
        failures += 1

    # --- gate backend ---
    if config.gate_backend == "ollama":
        ok, detail = ollama_available(config)
        print(OK if ok else BAD, "gate:", detail)
        if not ok:
            failures += 1
    elif config.gate_backend == "off":
        print(WARN, "gate disabled; every trigger goes straight to",
              config.verdict_model)
    else:
        print(OK, "gate: Claude Haiku 4.5 (cheap, ~$0.001/event)")

    # --- optional local detector ---
    ok, detail = detector.available()
    print(OK if ok else WARN, "local detector:", detail)

    # --- secrets ---
    from . import secrets as secretstore
    ok, detail = secretstore.keyring_available()
    print(OK if ok else WARN, "credential store:", detail)
    if config.plaintext_cameras:
        print(WARN, "passwords stored without a credential store for:",
              ", ".join(c.name for c in config.plaintext_cameras))

    # --- alerts ---
    channels = notify.configured_channels(config)
    if channels:
        print(OK, "alert channels:", ", ".join(channels))
    else:
        print(WARN, "no alert channels configured; events will only be logged")

    # --- cameras ---
    if not config.cameras:
        print(BAD, "no cameras configured -- add one in the dashboard Settings")
        failures += 1
    else:
        wanted = args.camera
        for cam in config.cameras:
            if wanted and cam.slug != wanted:
                continue
            failures += _probe_camera(cam, args)
        if len(config.cameras) > 1:
            print()
            print("      " + str(len(config.cameras)) + " cameras share one rolling "
                  "buffer window of " + str(config.segment_retention_min) + " min.")
            print("      Disk use scales with camera count -- switch a camera to the")
            print("      'sub' stream or shorten retention in Settings if space is tight.")

    print()
    print("=" * 58)
    if failures:
        print(str(failures) + " blocking problem(s) found.")
    else:
        print("All good. Start with:  python -m poopwatch serve")
    return 1 if failures else 0


def cmd_watch(args: argparse.Namespace) -> int:
    from .watcher import WatcherPool

    pool = WatcherPool(config)
    if not config.cameras:
        print(BAD, "no cameras configured")
        return 1
    pool.start()
    print("Watching " + str(len(config.cameras)) + " camera(s). Ctrl+C to stop.")
    for cam in config.cameras:
        print("  - " + cam.label)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopping...")
        pool.stop()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from . import auth
    from .server import app, pool

    if not auth.is_loopback(config.server_host) and auth.password() is None:
        print(BAD, "HOST is " + config.server_host + ", which opens the dashboard to your network.")
        print("      Set a dashboard password first: python -m poopwatch set-password")
        return 1
    if not args.no_watch:
        pool.start()
    url = "http://" + config.server_host + ":" + str(config.server_port)
    print("PoopWatch dashboard -> " + url)
    print("  " + str(len(config.cameras)) + " camera(s) configured")
    uvicorn.run(app, host=config.server_host, port=config.server_port,
                log_level="warning")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .pipeline import replay_clip
    from .store import Store

    clip = Path(args.file).resolve()
    if not clip.exists():
        print(BAD, "no such file:", clip)
        return 1

    print("Classifying " + clip.name + " with " + config.verdict_model + "...")
    event = replay_clip(clip, Store(config), config, notify_on_violation=args.notify)
    if not event:
        print(BAD, "pipeline returned nothing")
        return 1

    print("-" * 58)
    print("stage      ", event.get("stage"))
    if event.get("error"):
        print("error      ", event["error"])
        return 1
    print("subject    ", event.get("subject"))
    print("defecation ", event.get("defecation"))
    print("handler    ", event.get("handler_present"))
    print("cleanup    ", event.get("cleanup"))
    print("VIOLATION  ", event.get("violation"))
    print("confidence  %.0f%%" % ((event.get("confidence") or 0) * 100))
    print("reasoning  ", event.get("reasoning"))
    if event.get("cleanup_evidence"):
        print("on cleanup ", event["cleanup_evidence"])
    return 0


# --------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="poopwatch", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="check dependencies, cameras, and models")
    p_probe.add_argument("--camera", help="probe only this camera slug")
    p_probe.add_argument("--skip-rtsp", action="store_true",
                         help="skip the slow RTSP frame grab")

    sub.add_parser("watch", help="run the watchers in the foreground")

    p_serve = sub.add_parser("serve", help="run the dashboard and watchers")
    p_serve.add_argument("--no-watch", action="store_true",
                         help="serve the UI without starting capture")

    p_replay = sub.add_parser("replay", help="classify an existing video file")
    p_replay.add_argument("file")
    p_replay.add_argument("--notify", action="store_true",
                          help="actually send alerts (off by default)")

    sub.add_parser("set-password",
                   help="set the dashboard password (needed to open it on your network)")
    sub.add_parser("clear-password", help="remove the dashboard password")
    sub.add_parser("export-false-positives",
                   help="write false-positive events and their notes to a Markdown file")
    p_save = sub.add_parser("save-case",
                            help="save an event's frames as a gate test case")
    p_save.add_argument("event_id", type=int)
    p_save.add_argument("name")
    p_save.add_argument("expect", choices=["pass", "reject"],
                        help="pass = gate should see a dog defecating; reject = it should not")
    sub.add_parser("regress", help="run the gate on every saved test case")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    config.ensure_dirs()

    return {
        "probe": cmd_probe,
        "watch": cmd_watch,
        "serve": cmd_serve,
        "replay": cmd_replay,
        "set-password": cmd_set_password,
        "clear-password": cmd_clear_password,
        "export-false-positives": cmd_export_false_positives,
        "save-case": cmd_save_case,
        "regress": cmd_regress,
    }[args.command](args)


def cmd_save_case(args: argparse.Namespace) -> int:
    from .regress import save_case

    try:
        dest = save_case(args.event_id, args.name, args.expect)
    except ValueError as e:
        print(BAD, str(e))
        return 1
    print(OK, "saved case " + args.name + " (" + args.expect + ") to " + str(dest))
    return 0


def cmd_regress(args: argparse.Namespace) -> int:
    from .regress import run_cases

    rows = run_cases()
    if not rows:
        print(WARN, "no cases yet; save one with save-case")
        return 0
    for name, expect, got, ok in rows:
        print(OK if ok else BAD, "%-28s expected %-7s got %s" % (name, expect, got))
    failed = sum(1 for r in rows if not r[3])
    print(("all %d case(s) pass" % len(rows)) if not failed
          else ("%d of %d case(s) FAILED" % (failed, len(rows))))
    return 0 if not failed else 1


def cmd_export_false_positives(args: argparse.Namespace) -> int:
    """Write every false-positive event, with the reviewer's note, to Markdown.

    The output is the input for the prompt-review step (scripts/prompt_review.md).
    """
    import sqlite3

    from .settings_db import db_path

    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM events WHERE review = 'false_positive' ORDER BY trigger_at"
    ).fetchall()
    conn.close()

    from datetime import datetime

    out_dir = config.evidence_dir / "review"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "false_positives.md"
    lines = ["# False positives", "",
             "Each entry is an event a person marked as wrong. The note is the reason.", ""]
    for r in rows:
        when = datetime.fromtimestamp(r["trigger_at"]).strftime("%Y-%m-%d %H:%M:%S")
        lines += [
            f"## Event {r['id']} - {r['camera_slug']} - {when}",
            f"- Stage reached: {r['stage']}",
            f"- Model subject: {r['subject'] or 'unknown'}",
            f"- Model reasoning: {r['reasoning'] or '(none)'}",
            f"- Gate output: {r['gate_json'] or '(none)'}",
            f"- Reviewer note: {r['review_note'] or '(no note yet)'}",
            f"- Thumbnail: evidence/{r['thumb_path'] or '(none)'}",
            "",
        ]
    out.write_text("\n".join(lines), encoding="utf-8")
    missing = sum(1 for r in rows if not r["review_note"])
    print(OK, str(len(rows)) + " false positive(s) written to " + str(out))
    if missing:
        print(WARN, str(missing) + " of them have no note yet; add one in the dashboard.")
    return 0


def cmd_set_password(args: argparse.Namespace) -> int:
    import getpass

    from . import auth, secrets

    ok, detail = secrets.keyring_available()
    if not ok:
        print(BAD, "no OS credential store available here: " + detail)
        return 1
    first = getpass.getpass("New dashboard password: ")
    if len(first) < 8:
        print(BAD, "use at least 8 characters")
        return 1
    if getpass.getpass("Repeat it: ") != first:
        print(BAD, "passwords did not match")
        return 1
    auth.set_password(first)
    print(OK, "dashboard password saved (username: " + auth.USERNAME + ")")
    return 0


def cmd_clear_password(args: argparse.Namespace) -> int:
    from . import auth

    if auth.clear_password():
        print(OK, "dashboard password removed; the dashboard is local-only again")
    else:
        print(WARN, "no dashboard password was set")
    return 0


if __name__ == "__main__":
    sys.exit(main())
