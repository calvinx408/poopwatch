"""Settings persistence.

Everything configurable lives in the same SQLite database as the event log --
there is no config file to edit, lose, or accidentally commit. The dashboard's
Settings panel is the editor.

Deliberately free of any import from `config`, because `config` imports *this*;
the database path is derived here rather than read from the Config object.

Camera passwords are not stored here -- see `secrets.py`.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def db_path() -> Path:
    """Where the database lives. POOPWATCH_EVIDENCE_DIR relocates it."""
    override = os.getenv("POOPWATCH_EVIDENCE_DIR", "").strip()
    base = Path(override).expanduser() if override else ROOT / "evidence"
    return base / "poopwatch.db"


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def load() -> dict[str, Any]:
    """Every stored setting, as a nested dict of sections."""
    try:
        with _conn() as c:
            rows = c.execute("SELECT key, value FROM settings").fetchall()
    except sqlite3.Error as e:
        log.warning("could not read settings: %s", e)
        return {}

    out: dict[str, Any] = {}
    for row in rows:
        try:
            out[row["key"]] = json.loads(row["value"])
        except ValueError:
            log.warning("ignoring malformed stored setting %r", row["key"])
    return out


def save_section(section: str, values: dict[str, Any]) -> None:
    """Replace one section wholesale."""
    with _conn() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (section, json.dumps(values)),
        )
    log.info("saved settings section %r", section)


def save_all(sections: dict[str, Any]) -> None:
    with _conn() as c:
        for section, values in sections.items():
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (section, json.dumps(values)),
            )
    log.info("saved settings: %s", ", ".join(sorted(sections)))


def clear() -> None:
    with _conn() as c:
        c.execute("DELETE FROM settings")
