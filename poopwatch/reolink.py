"""Minimal Reolink HTTP API client.

Reolink cameras/NVRs expose a CGI JSON API. We use it for two things:
  1. GetAiState -- the camera's own on-device AI tells us a dog/cat is in frame.
     This is the free first stage of the funnel; it means we never run a model
     on an empty driveway.
  2. Snap -- a still JPEG, handy for connectivity checks and alert thumbnails.

Falls back to plain motion detection (GetMdState) on cameras without AI detect.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from .config import CameraConfig, Config, config

log = logging.getLogger(__name__)


class ReolinkError(RuntimeError):
    pass


_tls_warned = False


def _silence_tls_warnings() -> None:
    """Quiet urllib3's per-request InsecureRequestWarning.

    Accepting the camera's self-signed certificate is a deliberate choice made
    once in the config; repeating the warning on every poll (every 2 seconds,
    per camera) would bury real messages.
    """
    global _tls_warned
    if _tls_warned:
        return
    _tls_warned = True
    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:  # urllib3 internals vary by version
        pass


@dataclass
class AiState:
    """Which object classes the camera currently sees."""

    dog_cat: bool = False
    people: bool = False
    vehicle: bool = False
    motion: bool = False
    # Which of the above the firmware actually supports
    supports: frozenset[str] = frozenset()

    @property
    def any_animal(self) -> bool:
        return self.dog_cat

    def trigger(self) -> tuple[bool, str]:
        """Whether to capture now, and which signal decided it.

        A person is the trigger, and the dog check happens afterwards: the
        local YOLO stage and the gate both look for a dog in the clip. The
        camera's own dog/cat detection is not used, since it is unreliable on
        these cameras. Motion is the fallback only when the camera has no
        person detection.
        """
        if "people" in self.supports:
            return self.people, "camera AI (person)"
        return self.motion, "motion only"

    @property
    def detect_source(self) -> str:
        return self.trigger()[1]


class ReolinkClient:
    """Talks to one camera, or one channel of an NVR."""

    def __init__(self, cam: CameraConfig, cfg: Config | None = None) -> None:
        self.cam = cam
        self.cfg = cfg or config
        self._token: str | None = None
        self._token_expires: float = 0.0
        self._session = requests.Session()

        # Many Reolink models are https-only and answer port 80 with a 302 to
        # https://<host>/ -- note it drops the path, so blindly following the
        # redirect requests the web UI root instead of the API and fails in a
        # confusing way. Resolve the scheme explicitly instead.
        self._scheme: str | None = None if cam.scheme == "auto" else cam.scheme

        if not cam.verify_tls:
            if cam.tls_verification_sane:
                self._session.verify = False
                _silence_tls_warnings()
            else:
                log.warning(
                    "[%s] %s is not a private address; certificate verification "
                    "stays on. Enable it deliberately if you really mean to "
                    "trust a self-signed certificate over the internet.",
                    cam.slug, cam.host,
                )

    # ---------------------------------------------------------------- scheme
    def _probe_scheme(self) -> str:
        """Decide http vs https once, then remember it."""
        url = "http://" + self.cam.host + "/cgi-bin/api.cgi"
        try:
            r = self._session.get(
                url, params={"cmd": "Login"}, timeout=8, allow_redirects=False
            )
        except requests.RequestException:
            log.debug("[%s] port 80 unreachable, assuming https", self.cam.slug)
            return "https"

        if r.status_code in (301, 302, 303, 307, 308):
            location = r.headers.get("Location", "")
            if location.startswith("https://"):
                log.info("[%s] camera redirects to https; using https",
                         self.cam.slug)
                return "https"
        return "http"

    @property
    def base(self) -> str:
        if self._scheme is None:
            self._scheme = self._probe_scheme()
        return self._scheme + "://" + self.cam.host + "/cgi-bin/api.cgi"

    # ------------------------------------------------------------------ auth
    def _login(self) -> str:
        payload = [
            {
                "cmd": "Login",
                "param": {
                    "User": {
                        "Version": "0",
                        "userName": self.cam.user,
                        "password": self.cam.password,
                    }
                },
            }
        ]
        try:
            r = self._session.post(
                self.base, params={"cmd": "Login"}, json=payload, timeout=10
            )
            r.raise_for_status()
            data = r.json()
        except requests.exceptions.SSLError as e:
            raise ReolinkError(
                "TLS error talking to " + self.cam.host + ". This camera uses a "
                "self-signed certificate; tick 'Allow self-signed certificate' "
                "for it. (" + str(e)[:120] + ")"
            ) from e
        except requests.RequestException as e:
            raise ReolinkError(
                "cannot reach camera at " + self.cam.host + ": " + str(e)
            ) from e
        except ValueError as e:
            raise ReolinkError(f"camera returned non-JSON on login: {e}") from e

        try:
            entry = data[0]
            if entry.get("code") != 0 or "value" not in entry:
                detail = entry.get("error", entry)
                raise ReolinkError(f"login rejected: {detail}")
            tok = entry["value"]["Token"]
            self._token = tok["name"]
            # Renew a minute early; leaseTime is seconds (usually 3600)
            self._token_expires = time.time() + max(60, int(tok.get("leaseTime", 3600)) - 60)
        except (KeyError, IndexError, TypeError) as e:
            raise ReolinkError(f"unexpected login response: {data!r}") from e

        log.debug("[%s] reolink login ok, token valid ~%ds",
                  self.cam.slug, self._token_expires - time.time())
        return self._token

    def _get_token(self) -> str:
        if not self._token or time.time() >= self._token_expires:
            return self._login()
        return self._token

    # --------------------------------------------------------------- command
    def _cmd(self, cmd: str, *, param: dict | None = None, retry: bool = True) -> Any:
        body = [{"cmd": cmd, "action": 0, "param": param or {"channel": self.cam.channel}}]
        try:
            r = self._session.post(
                self.base,
                params={"cmd": cmd, "token": self._get_token()},
                json=body,
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            raise ReolinkError(f"{cmd} failed: {e}") from e

        entry = data[0] if isinstance(data, list) and data else {}
        # Token can expire early (reboot, session limit) -- re-login once.
        if entry.get("code") != 0:
            err = entry.get("error", {})
            if retry and str(err.get("detail", "")).lower().find("login") >= 0:
                self._token = None
                return self._cmd(cmd, param=param, retry=False)
            raise ReolinkError(f"{cmd} returned error: {err or entry}")
        return entry.get("value", {})

    # ----------------------------------------------------------------- state
    def get_ai_state(self) -> AiState:
        """Current AI detection flags. Falls back to motion-only if unsupported."""
        try:
            value = self._cmd("GetAiState")
        except ReolinkError as e:
            log.debug("[%s] GetAiState unavailable (%s), falling back to motion",
                      self.cam.slug, e)
            return AiState(motion=self.get_motion(), supports=frozenset({"motion"}))

        def flag(key: str) -> tuple[bool, bool]:
            node = value.get(key)
            if not isinstance(node, dict):
                return False, False
            return bool(node.get("alarm_state")), bool(node.get("support", 1))

        dog, dog_ok = flag("dog_cat")
        ppl, ppl_ok = flag("people")
        veh, veh_ok = flag("vehicle")

        supports = {name for name, ok in
                    (("dog_cat", dog_ok), ("people", ppl_ok), ("vehicle", veh_ok)) if ok}

        # Motion is only the trigger on cameras without person detection.
        needs_motion = "people" not in supports
        motion = self.get_motion() if needs_motion else False
        if needs_motion:
            supports.add("motion")

        return AiState(
            dog_cat=dog, people=ppl, vehicle=veh, motion=motion,
            supports=frozenset(supports),
        )

    def get_motion(self) -> bool:
        try:
            value = self._cmd("GetMdState")
        except ReolinkError:
            return False
        return bool(value.get("state"))

    def snapshot(self) -> bytes:
        """Grab a still JPEG from the camera."""
        try:
            r = self._session.get(
                self.base,
                params={
                    "cmd": "Snap",
                    "channel": self.cam.channel,
                    "rs": str(int(time.time())),
                    "token": self._get_token(),
                },
                timeout=15,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            raise ReolinkError(f"snapshot failed: {e}") from e
        if not r.content.startswith(b"\xff\xd8"):
            raise ReolinkError("snapshot did not return a JPEG (check credentials)")
        return r.content

    def device_info(self) -> dict:
        """Camera model/name. Raises ReolinkError if unreachable -- callers use
        this as a liveness check, so it must not swallow failures."""
        return self._cmd("GetDevInfo", param={}) or {}
