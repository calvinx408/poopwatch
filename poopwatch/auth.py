"""Optional HTTP Basic auth for the dashboard.

The password lives in the OS credential store (the same place as the camera
passwords), never in the repo or the settings database. With no password set,
the dashboard behaves as before: it is reachable only from this machine. Once a
password is set, every request must send it, and `serve` may listen on the
network.

The username is fixed to `poopwatch`; only the password is secret.
"""
from __future__ import annotations

import base64
import hashlib
import time
import hmac
import ipaddress

from . import secrets

DASHBOARD_KEY = "__dashboard__"
USERNAME = "poopwatch"


def password() -> str | None:
    """The dashboard password, or None when auth is not configured."""
    return secrets.get(DASHBOARD_KEY) or None


def set_password(value: str) -> None:
    secrets.put(DASHBOARD_KEY, value)


def clear_password() -> bool:
    return secrets.delete(DASHBOARD_KEY)


def is_loopback(host: str) -> bool:
    """True when `host` only accepts connections from this machine."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_basic(header: str | None, expected: str) -> bool:
    """Validate an `Authorization: Basic ...` header against the password."""
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    user, sep, given = decoded.partition(":")
    if not sep:
        return False
    # Compare both fields in constant time, so timing reveals nothing.
    user_ok = hmac.compare_digest(user.encode(), USERNAME.encode())
    pass_ok = hmac.compare_digest(given.encode(), expected.encode())
    return user_ok and pass_ok


# ---------------------------------------------------------------- sessions
# A login form sets a signed cookie. Home-screen web apps on iPhone don't
# show the browser's Basic-auth prompt, so the form is what they use.
COOKIE_NAME = "poopwatch_session"
SESSION_SECONDS = 30 * 24 * 3600


def _session_key(expected: str) -> bytes:
    # Derived from the password, so changing the password ends every session.
    return hashlib.sha256(("poopwatch-session:" + expected).encode("utf-8")).digest()


def make_session(expected: str) -> str:
    expires = int(time.time()) + SESSION_SECONDS
    sig = hmac.new(_session_key(expected), str(expires).encode(), hashlib.sha256)
    return str(expires) + "." + sig.hexdigest()


def valid_session(token: str | None, expected: str) -> bool:
    if not token or "." not in token:
        return False
    expires_text, _, sig = token.partition(".")
    try:
        expires = int(expires_text)
    except ValueError:
        return False
    if expires < time.time():
        return False
    good = hmac.new(_session_key(expected), expires_text.encode(), hashlib.sha256)
    return hmac.compare_digest(sig, good.hexdigest())


def check_password(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))
