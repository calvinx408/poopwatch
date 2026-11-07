"""SQLite event log.

Every camera trigger becomes a row, including the ones the gate rejects --
those rejections are the cheapest way to tune the pipeline later, and they feed
the dashboard's false-positive view.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import Config, config

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      REAL    NOT NULL,
    trigger_at      REAL    NOT NULL,
    camera          TEXT    NOT NULL DEFAULT '',
    camera_slug     TEXT    NOT NULL DEFAULT '',
    camera_name     TEXT    NOT NULL DEFAULT '',
    stage           TEXT    NOT NULL,   -- where the funnel stopped
    subject         TEXT,
    defecation      INTEGER NOT NULL DEFAULT 0,
    handler_present INTEGER NOT NULL DEFAULT 0,
    cleanup         INTEGER NOT NULL DEFAULT 0,
    violation       INTEGER NOT NULL DEFAULT 0,
    confidence      REAL    NOT NULL DEFAULT 0,
    reasoning       TEXT    NOT NULL DEFAULT '',
    cleanup_evidence TEXT   NOT NULL DEFAULT '',
    gate_json       TEXT,
    verdict_json    TEXT,
    clip_path       TEXT,
    frame_paths     TEXT,                -- JSON array, relative to evidence/
    thumb_path      TEXT,
    notified        INTEGER NOT NULL DEFAULT 0,
    review          TEXT    NOT NULL DEFAULT 'unreviewed',
    error           TEXT
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_events_trigger ON events(trigger_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_violation ON events(violation, trigger_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_camera ON events(camera_slug, trigger_at DESC);
"""

# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` silently
# does nothing on an existing table, so new columns have to be ALTERed in or an
# upgrade breaks with "no such column".
MIGRATIONS: list[tuple[str, str]] = [
    ("camera_slug", "TEXT NOT NULL DEFAULT ''"),
    ("camera_name", "TEXT NOT NULL DEFAULT ''"),
    # Free-text note from the reviewer, e.g. why an event is a false positive.
    ("review_note", "TEXT NOT NULL DEFAULT ''"),
]


class Store:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or config
        self.cfg.ensure_dirs()
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)
            c.executescript(INDEXES)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after a user's database was first created."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
        for column, decl in MIGRATIONS:
            if column not in existing:
                conn.execute("ALTER TABLE events ADD COLUMN " + column + " " + decl)
                log.info("migrated event log: added column %s", column)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.cfg.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ----------------------------------------------------------------- write
    def add_event(self, **fields: Any) -> int:
        fields.setdefault("created_at", time.time())
        fields.setdefault("stage", "triggered")
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO events (" + cols + ") VALUES (" + marks + ")",
                list(fields.values()),
            )
            return int(cur.lastrowid or 0)

    def update_event(self, event_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(k + " = ?" for k in fields)
        with self._conn() as c:
            c.execute(
                "UPDATE events SET " + sets + " WHERE id = ?",
                [*fields.values(), event_id],
            )

    def set_review(self, event_id: int, review: str) -> None:
        if review not in ("unreviewed", "confirmed", "false_positive", "handled"):
            raise ValueError("invalid review state: " + review)
        self.update_event(event_id, review=review)

    def set_review_note(self, event_id: int, note: str) -> None:
        self.update_event(event_id, review_note=note.strip()[:2000])

    def delete_event(self, event_id: int) -> bool:
        """Remove an event row and its evidence files. Only paths under
        evidence/ are touched, and the event's own folder is removed only
        when it lives under evidence/events/."""
        import shutil

        event = self.get_event(event_id)
        if not event:
            return False

        root = self.cfg.evidence_dir.resolve()
        events_root = self.cfg.events_dir.resolve()
        rels = [event.get("clip_path"), event.get("thumb_path"),
                *(event.get("frame_paths") or [])]
        folders = set()
        for rel in rels:
            if not rel:
                continue
            path = (self.cfg.evidence_dir / rel).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            path.unlink(missing_ok=True)
            folders.add(path.parent)

        for folder in folders:
            try:
                folder.relative_to(events_root)
            except ValueError:
                continue
            if folder != events_root:
                shutil.rmtree(folder, ignore_errors=True)

        with self._conn() as c:
            c.execute("DELETE FROM events WHERE id = ?", [event_id])
        return True

    # ------------------------------------------------------------------ read
    def list_events(self, *, limit: int = 100, offset: int = 0,
                    only_violations: bool = False,
                    include_rejected: bool = True,
                    camera: str | None = None) -> list[dict]:
        # Events where the gate saw something other than a dog are not kept
        # for the dashboard at all.
        where, params = ["stage != 'not_dog'"], []
        if only_violations:
            where.append("violation = 1")
        if not include_rejected:
            where.append("stage IN ('verdict', 'notified')")
        if camera:
            where.append("camera_slug = ?")
            params.append(camera)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM events" + clause +
                " ORDER BY trigger_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [self._hydrate(r) for r in rows]

    def get_event(self, event_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM events WHERE id = ?", [event_id]).fetchone()
        return self._hydrate(row) if row else None

    def stats(self) -> dict:
        with self._conn() as c:
            row = c.execute(
                """SELECT
                     COUNT(*)                                        AS total,
                     COALESCE(SUM(violation), 0)                     AS violations,
                     COALESCE(SUM(stage = 'gate_rejected'), 0)       AS gate_rejected,
                     COALESCE(SUM(review = 'false_positive'), 0)     AS false_positives,
                     COALESCE(SUM(error IS NOT NULL), 0)             AS errors
                   FROM events"""
            ).fetchone()
            today = c.execute(
                "SELECT COUNT(*) AS n FROM events WHERE trigger_at > ?",
                [time.time() - 86400],
            ).fetchone()
            per_camera = c.execute(
                """SELECT camera_slug, camera_name,
                          COUNT(*) AS total,
                          COALESCE(SUM(violation), 0) AS violations
                     FROM events
                    WHERE camera_slug != ''
                 GROUP BY camera_slug
                 ORDER BY violations DESC, total DESC"""
            ).fetchall()
        out = dict(row)
        out["last_24h"] = today["n"]
        out["per_camera"] = [dict(r) for r in per_camera]
        return out

    def count_demo(self) -> int:
        """Rows created by scripts/seed_demo.py. Surfaced in the UI so fake
        events are never mistaken for a live camera feed."""
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM events WHERE camera_slug LIKE 'demo-%'"
            ).fetchone()
        return int(row["n"])

    # ----------------------------------------------------------------- utils
    def _hydrate(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        for key in ("gate_json", "verdict_json"):
            if d.get(key):
                try:
                    d[key.replace("_json", "")] = json.loads(d[key])
                except ValueError:
                    pass
            d.pop(key, None)
        try:
            d["frame_paths"] = json.loads(d.get("frame_paths") or "[]")
        except ValueError:
            d["frame_paths"] = []
        for key in ("defecation", "handler_present", "cleanup", "violation", "notified"):
            d[key] = bool(d.get(key))
        return d

    def event_dir(self, trigger_epoch: float, camera_slug: str = "camera") -> Path:
        """Per-camera directory, so two cameras firing in the same second
        cannot overwrite each other's frames."""
        from datetime import datetime
        name = datetime.fromtimestamp(trigger_epoch).strftime("%Y%m%d-%H%M%S")
        path = self.cfg.events_dir / camera_slug / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def relative(self, path: Path | str) -> str:
        """Path relative to evidence/, for serving over HTTP."""
        try:
            return Path(path).resolve().relative_to(
                self.cfg.evidence_dir.resolve()).as_posix()
        except ValueError:
            return Path(path).name
