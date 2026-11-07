"""Validating and persisting settings on behalf of the dashboard.

Settings go into the application database (`settings_db`); camera and SMTP
passwords go into the OS credential store (`secrets`) when one is available, so
no secret lands in the project directory.
"""
from __future__ import annotations

import logging
import os
import socket
from typing import Any

from . import secrets, settings_db
from .config import config

log = logging.getLogger(__name__)

# Only these keys may be written, with the type to coerce them to.
CAPTURE_FIELDS: dict[str, type] = {
    "pre_roll_seconds": int,
    "post_roll_seconds": int,
    "segment_retention_minutes": int,
}
MODEL_FIELDS: dict[str, type] = {
    "gate_backend": str,
    "ollama_url": str,
    "ollama_model": str,
    "verdict_model": str,
    "disable_local_detector": bool,
    "max_concurrent_analyses": int,
}
ALERT_FIELDS: dict[str, type] = {
    "ntfy_topic": str,
    "ntfy_server": str,
    "smtp_host": str,
    "smtp_port": int,
    "smtp_user": str,
    "email_to": str,
}

GATE_BACKENDS = ("ollama", "haiku", "off")
STREAMS = ("main", "sub")
SCHEMES = ("auto", "http", "https")

SMTP_KEY = "__smtp__"


class SettingsError(ValueError):
    """A rejected settings change, reported back to the UI verbatim."""


# ---------------------------------------------------------------- guardrails
def _is_loopback(host: str) -> bool:
    if host in ("localhost", "127.0.0.1", "::1", ""):
        return True
    try:
        return socket.inet_aton(host).startswith(b"\x7f")
    except OSError:
        return False


def writes_allowed() -> tuple[bool, str]:
    """Settings writes are only safe on a loopback-bound server.

    The dashboard has no authentication. If someone exposes it on a LAN or the
    internet, an unauthenticated stranger must not be able to rewrite the
    config or trigger mail from it. Opt out deliberately with
    POOPWATCH_ALLOW_REMOTE_CONFIG=1 if you have put your own auth in front.
    """
    if os.getenv("POOPWATCH_ALLOW_REMOTE_CONFIG", "").strip().lower() in (
        "1", "true", "yes", "on"
    ):
        return True, "explicitly allowed via POOPWATCH_ALLOW_REMOTE_CONFIG"
    if _is_loopback(config.server_host):
        return True, "server is bound to loopback"
    return False, (
        "settings editing is disabled because the server is bound to "
        + config.server_host + " rather than localhost. Anyone who can reach "
        "this page could otherwise change your configuration. Set "
        "POOPWATCH_ALLOW_REMOTE_CONFIG=1 only if you have put authentication "
        "in front of it."
    )


def require_writes() -> None:
    ok, reason = writes_allowed()
    if not ok:
        raise PermissionError(reason)


# ------------------------------------------------------------------ secrets
def store_password(key: str, password: str) -> str:
    """Persist one password. Returns the storage actually used."""
    ok, _ = secrets.keyring_available()
    if ok:
        secrets.put(key, password)
        return "keyring"
    return "db-plaintext"


def smtp_password() -> str:
    """The effective SMTP password, from env or the credential store."""
    from_env = os.getenv("SMTP_PASSWORD", "")
    if from_env:
        return from_env
    return secrets.get(SMTP_KEY) or str(
        (settings_db.load().get("alerts") or {}).get("smtp_password", "")
    )


# ------------------------------------------------------------------ validate
def _coerce(value: Any, kind: type) -> Any:
    if kind is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if kind is int:
        return int(value)
    return str(value).strip()


def validate_camera(entry: dict[str, Any]) -> dict[str, Any]:
    host = str(entry.get("host", "")).strip()
    name = str(entry.get("name", "")).strip()
    if not host:
        raise SettingsError("host is required")
    if not name:
        raise SettingsError("name is required")
    if any(ch in host for ch in " /\\@"):
        raise SettingsError("host should be a bare IP or hostname, e.g. 192.168.1.50")

    try:
        channel = int(entry.get("channel", 0) or 0)
    except (TypeError, ValueError):
        raise SettingsError("channel must be a whole number") from None
    if channel < 0 or channel > 31:
        raise SettingsError("channel must be between 0 and 31")

    stream = str(entry.get("stream", "main")).strip() or "main"
    if stream not in STREAMS:
        raise SettingsError("stream must be one of " + ", ".join(STREAMS))

    scheme = str(entry.get("scheme", "auto")).strip() or "auto"
    if scheme not in SCHEMES:
        raise SettingsError("scheme must be one of " + ", ".join(SCHEMES))

    return {
        "name": name,
        "host": host,
        "channel": channel,
        "user": str(entry.get("user", "")).strip() or "admin",
        "stream": stream,
        "scheme": scheme,
        "verify_tls": _coerce(entry.get("verify_tls", False), bool),
    }


def _clean_section(data: dict[str, Any], name: str,
                   fields: dict[str, type]) -> dict[str, Any]:
    section = data.get(name) or {}
    out: dict[str, Any] = {}
    for key, kind in fields.items():
        if key in section:
            try:
                out[key] = _coerce(section[key], kind)
            except (TypeError, ValueError):
                raise SettingsError(
                    name + "." + key + " must be a " + kind.__name__
                ) from None
    return out


def save(data: dict[str, Any]) -> list[str]:
    """Validate and persist. Returns warnings worth showing the user."""
    require_writes()
    warnings: list[str] = []

    capture = _clean_section(data, "capture", CAPTURE_FIELDS)
    models = _clean_section(data, "models", MODEL_FIELDS)
    alerts = _clean_section(data, "alerts", ALERT_FIELDS)

    gate = models.get("gate_backend")
    if gate and gate not in GATE_BACKENDS:
        raise SettingsError("gate_backend must be one of " + ", ".join(GATE_BACKENDS))
    slots = models.get("max_concurrent_analyses")
    if slots is not None and not 1 <= slots <= 16:
        raise SettingsError("max_concurrent_analyses must be between 1 and 16")
    for key in ("pre_roll_seconds", "post_roll_seconds"):
        value = capture.get(key)
        if value is not None and not 0 <= value <= 900:
            raise SettingsError(key + " must be between 0 and 900 seconds")
    port = alerts.get("smtp_port")
    if port is not None and not 1 <= port <= 65535:
        raise SettingsError("smtp_port must be between 1 and 65535")
    retention = capture.get("segment_retention_minutes")
    if retention is not None and not 1 <= retention <= 1440:
        raise SettingsError("segment_retention_minutes must be between 1 and 1440")

    # SMTP password: credential store if we can, otherwise keep the previous
    # value rather than silently dropping it.
    raw_smtp = str((data.get("alerts") or {}).get("smtp_password") or "")
    if raw_smtp:
        where = store_password(SMTP_KEY, raw_smtp)
        if where == "db-plaintext":
            alerts["smtp_password"] = raw_smtp
            warnings.append(
                "No OS credential store is available, so the SMTP password was "
                "saved in the database instead."
            )
    else:
        previous = (settings_db.load().get("alerts") or {}).get("smtp_password")
        if previous:
            alerts["smtp_password"] = previous

    cameras: list[dict[str, Any]] = []
    for entry in data.get("cameras") or []:
        cam = validate_camera(entry)
        key = cam["host"] + ":" + str(cam["channel"])
        password = str(entry.get("password") or "")
        if password:
            if store_password(key, password) == "db-plaintext":
                cam["password"] = password
                warnings.append(
                    "No OS credential store is available, so the password for "
                    + cam["name"] + " was saved in the database instead."
                )
        else:
            # Preserve a previously stored fallback password on edit.
            for old in settings_db.load().get("cameras") or []:
                if (old.get("host") == cam["host"]
                        and int(old.get("channel", 0)) == cam["channel"]
                        and old.get("password")):
                    cam["password"] = old["password"]
            if entry.get("password_env"):
                cam["password_env"] = str(entry["password_env"]).strip()
        cameras.append(cam)

    names = [c["name"].strip().lower() for c in cameras]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise SettingsError(
            "camera names must be unique; duplicated: " + ", ".join(sorted(duplicates))
        )

    settings_db.save_all({
        "capture": capture,
        "models": models,
        "alerts": alerts,
        "cameras": cameras,
    })
    config.reload()
    return warnings
