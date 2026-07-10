"""Transactional email wrapper (Design B §12.4 / §12.6).

Thin HTTP client over a transactional email provider. Like every other
credential, the API key is read ONLY from the environment (GitHub Actions
Secret `EMAILAPIKEY`) — never hard-coded or logged.

Default provider is SendGrid (POST https://api.sendgrid.com/v3/mail/send,
Bearer key). To swap providers, override EMAILAPIURL and adjust `_payload`; the
public `send()` signature stays put so callers don't change.

Sender/recipient identities are NON-secret and come from config
(settings.toml [email] from/to), passed in by the caller.
"""

from __future__ import annotations

import os
from typing import Optional

import requests

_DEFAULT_URL = "https://api.sendgrid.com/v3/mail/send"
_TIMEOUT = 30


def _config() -> tuple[str, str]:
    key = os.environ.get("EMAILAPIKEY")
    if not key:
        raise RuntimeError(
            "EMAILAPIKEY must be set in the environment (GitHub Actions "
            "Secret / local .env). Never hard-code it.")
    url = os.environ.get("EMAILAPIURL", _DEFAULT_URL)
    return url, key


def _payload(sender: str, to: str, subject: str, html: str, text: str) -> dict:
    # SendGrid v3 mail-send shape. `content` MUST list text/plain before
    # text/html (SendGrid orders parts as given and requires that order).
    return {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": sender},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": text},
            {"type": "text/html", "value": html},
        ],
    }


def send(sender: str, to: str, subject: str, *, html: str, text: str) -> None:
    """Send one transactional email. Raises on transport/HTTP error so the tick
    surfaces failures in the workflow log rather than dropping alerts silently."""
    url, key = _config()
    resp = requests.post(
        url, json=_payload(sender, to, subject, html, text),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        timeout=_TIMEOUT)
    resp.raise_for_status()
