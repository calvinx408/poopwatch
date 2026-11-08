"""Configuration.

Settings are stored in the application database and edited through the
dashboard's Settings panel -- there is no config file to maintain.

Environment variables still override anything, so containers, systemd units,
and CI can set values without touching the database.

Camera passwords are resolved separately (see `secrets.py`): the OS credential
store is preferred, so no secret lands in the project directory at all.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from urllib.parse import quote

from dotenv import load_dotenv

from . import secrets, settings_db

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# CAMERA_<n>_HOST, any n. Discovered from the environment rather than scanned up
# to a fixed ceiling, so an arbitrary cap can't silently drop someone's camera.
_CAMERA_KEY = re.compile(r"^CAMERA_(\d+)_HOST$")

STORED: dict[str, Any] = settings_db.load()


def _section(name: str) -> dict[str, Any]:
    value = STORED.get(name, {})
    return value if isinstance(value, dict) else {}


def _get(env_key: str, section: str, key: str, default: Any) -> Any:
    """Resolve one setting: environment, then stored value, then the default."""
    raw = os.getenv(env_key)
    if raw is not None and raw.strip() != "":
        return raw
    value = _section(section).get(key)
    return default if value is None else value


def _str(env_key: str, section: str, key: str, default: str = "") -> str:
    return str(_get(env_key, section, key, default))


def _int(env_key: str, section: str, key: str, default: int) -> int:
    try:
        return int(_get(env_key, section, key, default))
    except (TypeError, ValueError):
        return default


def _bool(env_key: str, section: str, key: str, default: bool = False) -> bool:
    value = _get(env_key, section, key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _smtp_password() -> str:
    """Env, then the OS credential store, then a stored fallback."""
    from_env = os.getenv("SMTP_PASSWORD", "")
    if from_env:
        return from_env
    stored = secrets.get("__smtp__")
    if stored:
        return stored
    return str(_section("alerts").get("smtp_password", ""))


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "camera"


@dataclass(frozen=True)
class CameraConfig:
    """One camera, or one channel of an NVR."""

    name: str
    host: str
    user: str
    password: str
    channel: int = 0
    stream: str = "main"
    password_source: str = "none"
    # "auto" probes http and follows a redirect to https. Many Reolink models
    # are https-only and 302 from port 80.
    scheme: str = "auto"
    # LAN cameras ship self-signed certificates that can never validate, so
    # verification is off by default -- but only for private addresses (see
    # `tls_verification_sane`).
    verify_tls: bool = False

    @property
    def slug(self) -> str:
        """Filesystem- and URL-safe identifier."""
        return _slugify(self.name)

    @property
    def key(self) -> str:
        """Stable identity for secrets and the event log."""
        return self.host + ":" + str(self.channel)

    @property
    def rtsp_url(self) -> str:
        """Reolink RTSP path. Channels are 1-based in the URL, 0-based in the API."""
        cred = quote(self.user, safe="") + ":" + quote(self.password, safe="")
        ch = "%02d" % (self.channel + 1)
        return ("rtsp://" + cred + "@" + self.host + ":554/h264Preview_"
                + ch + "_" + self.stream)

    def redacted_rtsp(self) -> str:
        if not self.password:
            return self.rtsp_url
        return self.rtsp_url.replace(quote(self.password, safe=""), "***")

    @property
    def label(self) -> str:
        return self.name + " (" + self.host + " ch" + str(self.channel) + ")"

    @property
    def is_private_host(self) -> bool:
        """True for RFC1918 / loopback / link-local addresses and .local names."""
        import ipaddress

        try:
            return ipaddress.ip_address(self.host).is_private
        except ValueError:
            return (self.host == "localhost"
                    or self.host.endswith((".local", ".lan", ".localhost")))

    @property
    def tls_verification_sane(self) -> bool:
        """Whether skipping certificate checks is defensible for this host.

        Accepting any certificate is normal for a camera on your own LAN. Doing
        it for a host out on the internet is how traffic gets intercepted, so
        that combination is refused rather than quietly allowed.
        """
        return self.verify_tls or self.is_private_host


def _defaults() -> tuple[str, str]:
    d = _section("defaults")
    user = os.getenv("REOLINK_USER") or str(d.get("user", "admin"))
    stream = os.getenv("REOLINK_STREAM") or str(d.get("stream", "main"))
    return user, stream


def _cameras_from_store(shared_user: str, shared_stream: str) -> list[CameraConfig]:
    entries = STORED.get("cameras", [])
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return []

    cameras: list[CameraConfig] = []
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            continue
        host = str(entry.get("host", "")).strip()
        if not host:
            log.warning("stored camera entry %d has no host; skipping", i)
            continue
        name = str(entry.get("name", "")).strip() or ("camera " + str(i))
        channel = int(entry.get("channel", 0) or 0)
        password, source = secrets.resolve(
            host + ":" + str(channel),
            env_index=i,
            stored_password_env=str(entry.get("password_env", "")),
            stored_password=str(entry.get("password", "")),
        )
        cameras.append(CameraConfig(
            name=name,
            host=host,
            user=str(entry.get("user", "")).strip() or shared_user,
            password=password,
            channel=channel,
            stream=str(entry.get("stream", "")).strip() or shared_stream,
            password_source=source,
            scheme=str(entry.get("scheme", "auto")).strip() or "auto",
            verify_tls=bool(entry.get("verify_tls", False)),
        ))
    return cameras


def _cameras_from_env(shared_user: str, shared_stream: str) -> list[CameraConfig]:
    indices = sorted(
        int(m.group(1))
        for key in os.environ
        if (m := _CAMERA_KEY.match(key)) and os.environ[key].strip()
    )

    cameras: list[CameraConfig] = []
    for n in indices:
        prefix = "CAMERA_" + str(n) + "_"
        host = os.getenv(prefix + "HOST", "").strip()
        if not host:
            continue
        channel = 0
        try:
            channel = int(os.getenv(prefix + "CHANNEL", "") or 0)
        except ValueError:
            pass
        password, source = secrets.resolve(host + ":" + str(channel), env_index=n)
        cameras.append(CameraConfig(
            name=os.getenv(prefix + "NAME", "").strip() or ("camera " + str(n)),
            host=host,
            user=os.getenv(prefix + "USER", "").strip() or shared_user,
            password=password,
            channel=channel,
            stream=os.getenv(prefix + "STREAM", "").strip() or shared_stream,
            password_source=source,
        ))

    if cameras:
        return cameras

    # Legacy single-camera form.
    host = os.getenv("REOLINK_HOST", "").strip()
    if not host:
        return []
    channel = 0
    try:
        channel = int(os.getenv("REOLINK_CHANNEL", "") or 0)
    except ValueError:
        pass
    password, source = secrets.resolve(host + ":" + str(channel), env_index=None)
    return [CameraConfig(
        name=os.getenv("REOLINK_NAME", "").strip() or "camera",
        host=host, user=shared_user, password=password,
        channel=channel, stream=shared_stream, password_source=source,
    )]


def _load_cameras() -> list[CameraConfig]:
    """Stored cameras first; environment cameras are used only if none are
    stored. Whichever source defines cameras wins outright, because merging the
    two would make precedence impossible to reason about."""
    shared_user, shared_stream = _defaults()
    cameras = _cameras_from_store(shared_user, shared_stream)
    if not cameras:
        cameras = _cameras_from_env(shared_user, shared_stream)
    return _dedupe(cameras)


def _dedupe(cameras: list[CameraConfig]) -> list[CameraConfig]:
    """Make slugs unique so two cameras never share an evidence directory."""
    seen: dict[str, int] = {}
    out: list[CameraConfig] = []
    for cam in cameras:
        slug = cam.slug
        if slug in seen:
            seen[slug] += 1
            cam = CameraConfig(
                name=cam.name + " " + str(seen[slug]),
                host=cam.host, user=cam.user, password=cam.password,
                channel=cam.channel, stream=cam.stream,
                password_source=cam.password_source,
            )
        else:
            seen[slug] = 1
        out.append(cam)
    return out


@dataclass
class Config:
    cameras: list[CameraConfig] = field(default_factory=_load_cameras)

    # Capture window
    pre_roll: int = field(
        default_factory=lambda: _int("PRE_ROLL_SECONDS", "capture", "pre_roll_seconds", 30))
    post_roll: int = field(
        default_factory=lambda: _int("POST_ROLL_SECONDS", "capture", "post_roll_seconds", 120))
    segment_retention_min: int = field(
        default_factory=lambda: _int("SEGMENT_RETENTION_MINUTES", "capture",
                                     "segment_retention_minutes", 20))

    # Models
    gate_backend: str = field(
        default_factory=lambda: _str("GATE_BACKEND", "models", "gate_backend", "ollama"))
    ollama_url: str = field(
        default_factory=lambda: _str("OLLAMA_URL", "models", "ollama_url",
                                     "http://localhost:11434"))
    ollama_model: str = field(
        default_factory=lambda: _str("OLLAMA_MODEL", "models", "ollama_model",
                                     "qwen2.5vl:7b"))
    verdict_model: str = field(
        default_factory=lambda: _str("VERDICT_MODEL", "models", "verdict_model",
                                     "ollama"))
    disable_local_detector: bool = field(
        default_factory=lambda: _bool("DISABLE_LOCAL_DETECTOR", "models",
                                      "disable_local_detector", False))
    max_concurrent_analyses: int = field(
        default_factory=lambda: _int("MAX_CONCURRENT_ANALYSES", "models",
                                     "max_concurrent_analyses", 2))

    # Alerts
    ntfy_topic: str = field(
        default_factory=lambda: _str("NTFY_TOPIC", "alerts", "ntfy_topic"))
    ntfy_server: str = field(
        default_factory=lambda: _str("NTFY_SERVER", "alerts", "ntfy_server",
                                     "https://ntfy.sh"))
    smtp_host: str = field(
        default_factory=lambda: _str("SMTP_HOST", "alerts", "smtp_host"))
    smtp_port: int = field(
        default_factory=lambda: _int("SMTP_PORT", "alerts", "smtp_port", 587))
    smtp_user: str = field(
        default_factory=lambda: _str("SMTP_USER", "alerts", "smtp_user"))
    smtp_password: str = field(default_factory=_smtp_password)
    alert_email_to: str = field(
        default_factory=lambda: _str("ALERT_EMAIL_TO", "alerts", "email_to"))
    # Send a "Good human!" alert when a dog defecates and the owner cleans up.
    notify_good: bool = field(
        default_factory=lambda: _bool("NOTIFY_GOOD", "alerts", "notify_good", True))
    # Send an alert when a dog walks past without pooping. Off by default: walk-bys are frequent.
    notify_walkbys: bool = field(
        default_factory=lambda: _bool("NOTIFY_WALKBYS", "alerts", "notify_walkbys", False))

    # Server
    server_host: str = field(
        default_factory=lambda: _str("HOST", "server", "host", "127.0.0.1"))
    server_port: int = field(
        default_factory=lambda: _int("PORT", "server", "port", 8420))

    # Paths
    root: Path = ROOT
    evidence_dir: Path = ROOT / "evidence"

    # -------------------------------------------------------------- cameras
    def camera(self, slug: str) -> CameraConfig | None:
        return next((c for c in self.cameras if c.slug == slug), None)

    @property
    def all_passwords(self) -> list[str]:
        """Every secret that must be scrubbed from logs and API output."""
        out: list[str] = []
        for cam in self.cameras:
            if cam.password:
                out.append(cam.password)
                out.append(quote(cam.password, safe=""))
        if self.smtp_password:
            out.append(self.smtp_password)
        return out

    @property
    def plaintext_cameras(self) -> list[CameraConfig]:
        """Cameras whose password had to be stored without a credential store."""
        return [c for c in self.cameras if c.password_source == "db-plaintext"]

    # ---------------------------------------------------------------- paths
    def segments_dir(self, cam: CameraConfig) -> Path:
        return self.evidence_dir / "segments" / cam.slug

    @property
    def events_dir(self) -> Path:
        return self.evidence_dir / "events"

    @property
    def db_path(self) -> Path:
        return self.evidence_dir / "poopwatch.db"

    def ensure_dirs(self) -> None:
        for d in (self.evidence_dir, self.events_dir,
                  self.evidence_dir / "segments"):
            d.mkdir(parents=True, exist_ok=True)
        for cam in self.cameras:
            self.segments_dir(cam).mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------------- checks
    @property
    def uses_claude(self) -> bool:
        """True if any stage calls the Anthropic API."""
        gate_is_claude = self.gate_backend.lower() not in ("ollama", "off")
        verdict_is_claude = self.verdict_model.lower() not in ("ollama", "local")
        return gate_is_claude or verdict_is_claude

    @property
    def source_summary(self) -> str:
        if STORED:
            return "saved settings (+ environment overrides)"
        return "defaults / environment only"

    def reload(self) -> None:
        """Re-read stored settings and refresh every field in place.

        Mutates this object rather than replacing it, because modules across
        the app hold a reference to the same singleton -- rebinding would leave
        them pointing at stale settings.
        """
        global STORED
        STORED = settings_db.load()
        fresh = Config()
        for f in fields(fresh):
            if f.name in ("root", "evidence_dir"):
                continue
            setattr(self, f.name, getattr(fresh, f.name))
        log.info("configuration reloaded (%d camera(s))", len(self.cameras))

    def missing(self) -> list[str]:
        """Required settings that aren't filled in yet."""
        gaps = []
        if not self.cameras:
            gaps.append("at least one camera (add one in the dashboard Settings)")
        else:
            for cam in self.cameras:
                if not cam.password:
                    gaps.append("password for " + cam.name)
        # The verdict stage defaults to Claude even when the gate runs locally,
        # so check both rather than the gate alone.
        if self.uses_claude and not (
            os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
        ):
            gaps.append("ANTHROPIC_API_KEY")
        return gaps


config = Config()
