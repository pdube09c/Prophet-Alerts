"""Non-secret config loader (Design B §12).

Reads config/settings.toml (git-ignored) with settings.example.toml as the
documented template. Secrets are NEVER here — they come from the environment
(ODDSAPIKEY, SUPABASEURL, SUPABASEKEY, EMAILAPIKEY).

Missing file -> falls back to the example template so the app still runs with
documented defaults (stage=paper), and a missing individual key -> its default.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache

from engine.alert import AlertConfig

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SETTINGS = os.path.join(_ROOT, "config", "settings.toml")
_EXAMPLE = os.path.join(_ROOT, "config", "settings.example.toml")


@lru_cache(maxsize=1)
def load() -> dict:
    path = _SETTINGS if os.path.exists(_SETTINGS) else _EXAMPLE
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def stage() -> str:
    return load().get("app", {}).get("stage", "paper")


def alert_config() -> AlertConfig:
    """Alert settings from settings.toml, with NON-SECRET env overrides.

    settings.toml is git-ignored, so CI has no file — these values come from
    GitHub Actions *Variables* (the `vars` context, NOT secrets): STAGE,
    EMAIL_FROM, EMAIL_TO, PAGE_URL, STAKE_TARGET. Env wins over the file so the
    same code path serves local (file) and CI (vars). Secrets stay in the
    environment as before and never appear here.
    """
    cfg = load()
    email = cfg.get("email", {})
    page = cfg.get("page", {})
    nba = cfg.get("nba", {})
    return AlertConfig(
        stage=os.environ.get("STAGE") or stage(),
        email_from=os.environ.get("EMAIL_FROM") or email.get("from", ""),
        email_to=os.environ.get("EMAIL_TO") or email.get("to", ""),
        page_url=os.environ.get("PAGE_URL") or page.get("url", ""),
        stage2_target=float(os.environ.get("STAKE_TARGET")
                            or nba.get("stake_target", 250.0)),
    )
