"""Alerting: ntfy push to your phone, and email with the snapshot attached.

Both are best-effort and independent -- a dead SMTP server must never stop the
push, and neither should ever crash the watcher.
"""
from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import requests

from .config import Config, config

log = logging.getLogger(__name__)


def _headline(verdict, trigger_epoch: float, camera: str = "") -> tuple[str, str]:
    when = datetime.fromtimestamp(trigger_epoch).strftime("%a %H:%M:%S")
    where = " on " + camera if camera else ""
    if verdict.subject == "dog":
        title = "Dog pooped, not picked up" + where
    elif verdict.subject == "human":
        title = "Human defecation detected" + where
    else:
        title = "Defecation detected, not cleaned up" + where

    body_lines = [
        when + (" -- " + camera if camera else ""),
        "",
        "Subject: " + verdict.subject,
        "Handler present: " + ("yes" if verdict.handler_present else "no"),
        "Cleaned up: no",
        "Confidence: %.0f%%" % (verdict.confidence * 100),
        "",
        verdict.reasoning,
    ]
    if verdict.cleanup_evidence:
        body_lines += ["", "On cleanup: " + verdict.cleanup_evidence]
    return title, "\n".join(body_lines)


def send_ntfy(title: str, body: str, image: Path | None = None,
              cfg: Config | None = None) -> bool:
    cfg = cfg or config
    if not cfg.ntfy_topic:
        log.debug("ntfy not configured, skipping push")
        return False

    url = cfg.ntfy_server.rstrip("/") + "/" + cfg.ntfy_topic
    headers = {
        "Title": title.encode("ascii", "replace").decode("ascii"),
        "Priority": "high",
        "Tags": "poop,dog",
    }
    try:
        # ntfy attaches a binary body as a file; send the snapshot that way and
        # put the detail text in the Message header.
        if image and image.exists():
            headers["Message"] = body.encode("ascii", "replace").decode("ascii")
            headers["Filename"] = "snapshot.jpg"
            r = requests.put(url, data=image.read_bytes(), headers=headers, timeout=20)
        else:
            r = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=20)
        r.raise_for_status()
        log.info("ntfy push sent to topic %s", cfg.ntfy_topic)
        return True
    except requests.RequestException as e:
        log.error("ntfy push failed: %s", e)
        return False


def send_email(subject: str, body: str, images: list[Path] | None = None,
               cfg: Config | None = None) -> bool:
    cfg = cfg or config
    if not (cfg.smtp_host and cfg.smtp_user and cfg.alert_email_to):
        log.debug("smtp not configured, skipping email")
        return False

    msg = EmailMessage()
    msg["Subject"] = "[PoopWatch] " + subject
    msg["From"] = cfg.smtp_user
    msg["To"] = cfg.alert_email_to
    msg.set_content(body)

    for img in (images or [])[:6]:
        if not img.exists():
            continue
        msg.add_attachment(
            img.read_bytes(), maintype="image", subtype="jpeg", filename=img.name
        )

    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
            s.starttls()
            s.login(cfg.smtp_user, cfg.smtp_password)
            s.send_message(msg)
        log.info("alert email sent to %s", cfg.alert_email_to)
        return True
    except (smtplib.SMTPException, OSError) as e:
        log.error("email failed: %s", e)
        return False


def alert(verdict, trigger_epoch: float, frames: list[Path],
          cfg: Config | None = None, *, camera: str = "") -> bool:
    """Fire both channels. Returns True if at least one got through."""
    cfg = cfg or config
    title, body = _headline(verdict, trigger_epoch, camera)
    best = frames[0] if frames else None
    pushed = send_ntfy(title, body, best, cfg)
    mailed = send_email(title, body, frames, cfg)
    if not (pushed or mailed):
        log.warning("event detected but no alert channel is configured")
    return pushed or mailed


def alert_good(trigger_epoch: float, frames: list[Path],
               cfg: Config | None = None, *, camera: str = "") -> bool:
    """Positive alert: the dog went and the owner picked it up. Same channels as
    a violation, with a happier message."""
    from datetime import datetime

    cfg = cfg or config
    when = datetime.fromtimestamp(trigger_epoch).strftime("%I:%M %p").lstrip("0")
    where = (camera + " ") if camera else ""
    title = "Good human!"
    body = (where + "at " + when + ": the dog did its business and the human "
            "picked it up. Treat earned.")
    best = frames[0] if frames else None
    pushed = send_ntfy(title, body, best, cfg)
    mailed = send_email(title, body, frames, cfg)
    return pushed or mailed


def test_ntfy(topic: str, server: str) -> dict:
    """Send a real push to `topic`. Returns a step result for the UI."""
    if not topic:
        return {"name": "ntfy push", "ok": False,
                "detail": "No ntfy topic set, so nothing was sent."}
    url = server.rstrip("/") + "/" + topic
    try:
        r = requests.post(
            url,
            data=b"PoopWatch test notification. If you can read this, alerts work.",
            headers={"Title": "PoopWatch test", "Tags": "white_check_mark"},
            timeout=20,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        return {"name": "ntfy push", "ok": False, "detail": str(e)[:200]}
    return {"name": "ntfy push", "ok": True,
            "detail": "Sent to " + url + " -- check your phone."}


def test_email(host: str, port: int, user: str, password: str,
               to: str) -> dict:
    """Send a real test email. Each failure mode gets its own message, because
    'it didn't work' is useless when SMTP has this many ways to fail."""
    if not host:
        return {"name": "Email", "ok": False, "detail": "No SMTP host set."}
    if not to:
        return {"name": "Email", "ok": False, "detail": "No recipient address set."}
    if not password:
        return {"name": "Email", "ok": False,
                "detail": "No SMTP password available. Enter one and try again."}

    msg = EmailMessage()
    msg["Subject"] = "[PoopWatch] Test notification"
    msg["From"] = user or to
    msg["To"] = to
    msg.set_content(
        "This is a PoopWatch test email.\n\n"
        "If you received it, alert emails are configured correctly."
    )

    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        return {"name": "Email", "ok": False,
                "detail": "Server rejected the username or password. For Gmail "
                          "you need an app password, not your account password."}
    except smtplib.SMTPConnectError:
        return {"name": "Email", "ok": False,
                "detail": "Could not connect to " + host + ":" + str(port) + "."}
    except smtplib.SMTPRecipientsRefused:
        return {"name": "Email", "ok": False,
                "detail": "Server refused the recipient address " + to + "."}
    except smtplib.SMTPNotSupportedError as e:
        return {"name": "Email", "ok": False,
                "detail": "Server does not support STARTTLS on this port: " + str(e)[:140]}
    except (smtplib.SMTPException, OSError) as e:
        return {"name": "Email", "ok": False, "detail": str(e)[:200]}

    return {"name": "Email", "ok": True, "detail": "Test email sent to " + to + "."}


def configured_channels(cfg: Config | None = None) -> list[str]:
    cfg = cfg or config
    out = []
    if cfg.ntfy_topic:
        out.append("ntfy:" + cfg.ntfy_topic)
    if cfg.smtp_host and cfg.smtp_user and cfg.alert_email_to:
        out.append("email:" + cfg.alert_email_to)
    return out


def alert_walkby(trigger_epoch: float, frames: list[Path],
                 cfg: Config | None = None, *, camera: str = "") -> bool:
    """A dog walked past without pooping. Sent only when the user asks for it."""
    from datetime import datetime

    cfg = cfg or config
    when = datetime.fromtimestamp(trigger_epoch).strftime("%I:%M %p").lstrip("0")
    where = (camera + " ") if camera else ""
    title = "Dog walk-by"
    body = where + "at " + when + ": a dog walked past without pooping."
    best = frames[0] if frames else None
    pushed = send_ntfy(title, body, best, cfg)
    mailed = send_email(title, body, frames, cfg)
    return pushed or mailed
