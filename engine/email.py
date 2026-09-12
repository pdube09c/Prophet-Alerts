"""Transactional email wrapper (Design B §12.4 / §12.6).

Thin HTTP client over a transactional email provider. Like every other
credential, the API key is read ONLY from the environment (GitHub Actions
Secret `EMAILAPIKEY`) — never hard-coded or logged.

Default provider is Resend (POST https://api.resend.com/emails, Bearer key).
To swap providers, override EMAILAPIURL and adjust `_payload`; the public
`send()` signature stays put so callers don't change.

NOTE: without a verified sending domain on Resend, the account is restricted
to sending FROM the shared onboarding@resend.dev address and TO the address
the Resend account itself is registered under. `sender` is therefore ignored
in that unverified state — Resend rejects any other `from` value. Once a
domain is verified, `sender` can be a real address on that domain again.

Sender/recipient identities are NON-secret and come from config
(settings.toml [email] from/to), passed in by the caller.
"""

from __future__ import annotations

import os
from typing import Optional

import requests

_DEFAULT_URL = "https://api.resend.com/emails"
_DEFAULT_SENDER = "onboarding@resend.dev"  # only usable sender pre-domain-verification
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
    # Resend's shape: flat fields, no personalizations array.
    return {
        "from": sender,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text,
    }


def send(sender: str, to: str, subject: str, *, html: str, text: str) -> None:
    """Send one transactional email. Raises on transport/HTTP error so the tick
    surfaces failures in the workflow log rather than dropping alerts silently."""
    url, key = _config()
    # Unverified Resend accounts can only send from the shared test address.
    effective_sender = os.environ.get("EMAIL_FROM_OVERRIDE", _DEFAULT_SENDER)
    resp = requests.post(
        url, json=_payload(effective_sender, to, subject, html, text),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        timeout=_TIMEOUT)
    resp.raise_for_status()
