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

# Canonical reference stake ladder. Lives here (the config layer), not in
# alert.py, so no stake numbers are hard-coded in the renderer.
DEFAULT_STAKE_LADDER = (100.0, 500.0, 1000.0, 2000.0, 3000.0, 5000.0)


def _parse_stake_ladder_env(raw: str) -> list:
    """Parse a STAKE_LADDER env override ('100,500,1000') into numbers. CI has
    no settings.toml, so the ladder can be set via a GitHub Actions Variable."""
    try:
        return [float(x) for x in raw.split(",") if x.strip() != ""]
    except ValueError as exc:
        raise ValueError(
            f"STAKE_LADDER must be comma-separated numbers (e.g. "
            f"'100,500,1000'); got {raw!r}") from exc


def _validate_stake_ladder(value) -> tuple:
    """Validate + normalize the stake ladder, failing LOUDLY on malformed input
    so a bad config can never render a broken/negative/unsorted ladder into a
    live alert. Requires: a non-empty list of positive numbers, strictly
    ascending. Returns a tuple of floats."""
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(
            f"stake_ladder must be a list of positive numbers, got "
            f"{type(value).__name__}: {value!r}")
    if not value:
        raise ValueError("stake_ladder must be non-empty")
    out = []
    for x in value:
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            raise ValueError(
                f"stake_ladder entries must be numbers; got {x!r} "
                f"({type(x).__name__})")
        if x <= 0:
            raise ValueError(f"stake_ladder entries must be positive; got {x}")
        out.append(float(x))
    if any(a >= b for a, b in zip(out, out[1:])):
        raise ValueError(
            f"stake_ladder must be sorted strictly ascending; got {value!r}")
    return tuple(out)


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

    ladder_env = os.environ.get("STAKE_LADDER")
    ladder_raw = (_parse_stake_ladder_env(ladder_env) if ladder_env
                  else nba.get("stake_ladder", DEFAULT_STAKE_LADDER))
    return AlertConfig(
        stage=os.environ.get("STAGE") or stage(),
        email_from=os.environ.get("EMAIL_FROM") or email.get("from", ""),
        email_to=os.environ.get("EMAIL_TO") or email.get("to", ""),
        page_url=os.environ.get("PAGE_URL") or page.get("url", ""),
        stage2_target=float(os.environ.get("STAKE_TARGET")
                            or nba.get("stake_target", 250.0)),
        stake_ladder=_validate_stake_ladder(ladder_raw),
    )
