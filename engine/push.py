"""ntfy.sh push notification — a best-effort "go look" channel fired alongside
the detail email (Design B alert path).

A thin HTTP POST to https://ntfy.sh/{topic}. The topic is config-driven (never
hard-coded, see engine/config.py) so it can be regenerated if it leaks. This
module only performs the POST and raises on failure; the alert path is
responsible for wrapping the call so a push failure never blocks the email
(email is the source of truth, ntfy is best-effort).
"""

from __future__ import annotations

import os

import requests

_DEFAULT_BASE = "https://ntfy.sh"
_TIMEOUT = 10


def send(topic: str, title: str, body: str) -> None:
    """POST one push to ntfy.sh. Raises on transport/HTTP error — the caller in
    the alert path swallows it. The title goes in the ASCII `Title` header; the
    body is sent as UTF-8 data so it may carry non-ASCII (e.g. an arrow)."""
    base = os.environ.get("NTFYBASEURL", _DEFAULT_BASE).rstrip("/")
    resp = requests.post(
        f"{base}/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": "urgent", "Tags": "rotating_light"},
        timeout=_TIMEOUT)
    resp.raise_for_status()
