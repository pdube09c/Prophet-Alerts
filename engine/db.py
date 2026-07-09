"""Hosted-DB read/write (Design B §7) — all state lives here, never on the
ephemeral runner.

Thin PostgREST client over Supabase. Credentials are read ONLY from the
environment (GitHub Actions Secrets): SUPABASEURL + SUPABASEKEY (service_role).
Never hard-code or log them.

The workflows use the service_role key and bypass RLS. The static page uses the
anon key (client-side, RLS-scoped) — not this module.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

import requests

from sports.base import SnapshotRow

_TIMEOUT = 30


def _config() -> tuple[str, str]:
    url = os.environ.get("SUPABASEURL")
    key = os.environ.get("SUPABASEKEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASEURL and SUPABASEKEY must be set in the environment "
            "(GitHub Actions Secrets / local .env). Never hard-code them.")
    return url.rstrip("/"), key


def _headers(extra: Optional[dict] = None) -> dict:
    _, key = _config()
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _rest(table: str) -> str:
    url, _ = _config()
    return f"{url}/rest/v1/{table}"


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


# --- generic verbs -----------------------------------------------------------

def insert(table: str, rows: list[dict], *, upsert: bool = False,
           on_conflict: Optional[str] = None) -> None:
    if not rows:
        return
    params = {}
    prefer = ["return=minimal"]
    if upsert:
        prefer.append("resolution=merge-duplicates")
        if on_conflict:
            params["on_conflict"] = on_conflict
    rows = [{k: _iso(v) for k, v in r.items()} for r in rows]
    resp = requests.post(_rest(table), json=rows, params=params,
                         headers=_headers({"Prefer": ",".join(prefer)}),
                         timeout=_TIMEOUT)
    resp.raise_for_status()


def select(table: str, *, params: Optional[dict] = None) -> list[dict]:
    resp = requests.get(_rest(table), params=params or {},
                        headers=_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def update(table: str, patch: dict, *, params: dict) -> None:
    resp = requests.patch(_rest(table), json={k: _iso(v) for k, v in patch.items()},
                          params=params,
                          headers=_headers({"Prefer": "return=minimal"}),
                          timeout=_TIMEOUT)
    resp.raise_for_status()


# --- typed helpers used by the engine ----------------------------------------

def append_snapshots(rows: list[SnapshotRow]) -> None:
    """Append odds snapshot rows (idempotent on (sport, game_id, taken_at, book))."""
    insert("snapshots", [
        dict(sport=r.sport, game_date=r.game_date, game_id=r.game_id,
             taken_at=r.taken_at, book=r.book, home_point=r.home_point,
             home_ml=r.home_ml, away_ml=r.away_ml,
             home_limit=r.home_limit, away_limit=r.away_limit)
        for r in rows
    ], upsert=True, on_conflict="sport,game_id,taken_at,book")


def get_snapshots(sport: str, game_date: str, game_id: str) -> list[SnapshotRow]:
    rows = select("snapshots", params={
        "sport": f"eq.{sport}", "game_date": f"eq.{game_date}",
        "game_id": f"eq.{game_id}", "order": "taken_at.asc",
    })
    return [
        SnapshotRow(sport=r["sport"], game_date=r["game_date"], game_id=r["game_id"],
                    taken_at=datetime.fromisoformat(r["taken_at"]), book=r["book"],
                    home_point=r["home_point"], home_ml=r["home_ml"],
                    away_ml=r["away_ml"], home_limit=r["home_limit"],
                    away_limit=r["away_limit"])
        for r in rows
    ]


def upsert_stats(rows: list[dict]) -> None:
    insert("stats", rows, upsert=True,
           on_conflict='sport,asof_date,team,"group",field')


def get_stats(sport: str, asof_date: str) -> list[dict]:
    return select("stats", params={
        "sport": f"eq.{sport}", "asof_date": f"eq.{asof_date}"})


def is_alerted(sport: str, game_id: str) -> bool:
    rows = select("survivors", params={
        "sport": f"eq.{sport}", "game_id": f"eq.{game_id}",
        "select": "alerted"})
    return bool(rows) and all(r["alerted"] for r in rows)


def upsert_survivor(row: dict) -> None:
    insert("survivors", [row], upsert=True, on_conflict="sport,game_id")


def mark_alerted(sport: str, game_id: str) -> None:
    update("survivors", {"alerted": True},
           params={"sport": f"eq.{sport}", "game_id": f"eq.{game_id}"})


def insert_vetoed(rows: list[dict]) -> None:
    insert("vetoed", rows)


# --- settlement helpers ------------------------------------------------------

def get_unsettled_bets(sport: str, game_date: str) -> list[dict]:
    """Logged bets for a date that haven't been graded yet (result is null)."""
    return select("bets", params={
        "sport": f"eq.{sport}", "game_date": f"eq.{game_date}",
        "result": "is.null"})


def settle_bet(bet_id: str, result: str, net_pnl: float) -> None:
    update("bets", {"result": result, "net_pnl": net_pnl},
           params={"id": f"eq.{bet_id}"})


def get_settled_bets(sport: str, game_date: str) -> list[dict]:
    """All graded bets for a date (for the morning summary)."""
    return select("bets", params={
        "sport": f"eq.{sport}", "game_date": f"eq.{game_date}",
        "result": "not.is.null", "order": "created_at.asc"})


def get_vetoed(sport: str, game_date: str) -> list[dict]:
    return select("vetoed", params={
        "sport": f"eq.{sport}", "game_date": f"eq.{game_date}"})


def settle_vetoed(vetoed_id: int, favwin_actual: bool) -> None:
    update("vetoed", {"favwin_actual": favwin_actual},
           params={"id": f"eq.{vetoed_id}"})
