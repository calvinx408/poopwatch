"""Camera passwords, kept out of the repo.

Preferred storage is the operating system's credential store (Windows
Credential Manager, macOS Keychain, Secret Service on Linux) via the optional
`keyring` package. Nothing sensitive touches the project directory that way, so
there is no file to accidentally commit.

Resolution order for a camera password, highest priority first:

    1. environment variable , CAMERA_<n>_PASSWORD, or shared REOLINK_PASSWORD
    2. OS credential store  , what the Settings panel writes
    3. password_env setting , names another environment variable to read
    4. stored password      , only when no credential store exists; warned about

Environment wins so containers and service managers can override anything
without editing files.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

KEYRING_SERVICE = "poopwatch"


def keyring_available() -> tuple[bool, str]:
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return False, "keyring not installed (pip install keyring)"

    try:
        backend = keyring.get_keyring()
    except Exception as e:
        return False, "keyring backend error: " + str(e)

    if isinstance(backend, fail.Keyring):
        return False, "no usable OS credential store on this machine"
    return True, type(backend).__name__


def get(camera_key: str) -> str | None:
    """Read a password from the OS credential store."""
    try:
        import keyring
    except ImportError:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, camera_key)
    except Exception as e:
        log.debug("keyring read failed for %s: %s", camera_key, e)
        return None


def put(camera_key: str, password: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, camera_key, password)


def delete(camera_key: str) -> bool:
    try:
        import keyring
    except ImportError:
        return False
    try:
        keyring.delete_password(KEYRING_SERVICE, camera_key)
        return True
    except Exception:
        return False


def resolve(camera_key: str, env_index: int | None,
            stored_password_env: str = "",
            stored_password: str = "") -> tuple[str, str]:
    """Return (password, source) for one camera.

    `source` is a short label used by `probe` to show where each secret came
    from, and to warn when it came from plaintext on disk.
    """
    if env_index is not None:
        specific = os.getenv("CAMERA_" + str(env_index) + "_PASSWORD", "")
        if specific:
            return specific, "env:CAMERA_" + str(env_index) + "_PASSWORD"

    stored = get(camera_key)
    if stored:
        return stored, "keyring"

    if stored_password_env:
        from_named = os.getenv(stored_password_env, "")
        if from_named:
            return from_named, "env:" + stored_password_env
        log.warning("password_env %s is set for %s but that variable is empty",
                    stored_password_env, camera_key)

    if stored_password:
        return stored_password, "db-plaintext"

    shared = os.getenv("REOLINK_PASSWORD", "")
    if shared:
        return shared, "env:REOLINK_PASSWORD"

    return "", "none"
