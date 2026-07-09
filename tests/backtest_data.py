"""Test-support loaders that read the real point-in-time data from the sibling
`odds-backtest-verification` project, so the veto layers can be checked against
the backtest's actual keep/veto decisions.

This module is TEST-ONLY. It adapts the backtest project's on-disk JSON (PIT
team stats + 22 odds snapshots/day) into the shared dataclasses the veto layers
consume. The live app produces the same shapes from the DB.

Point the loaders at a different tree with env var BACKTEST_ROOT.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from sports.base import Candidate, Game, SnapshotRow, VetoContext
from sports.nba import ALL_FIELDS, FIELDS_BY_GROUP, canonical_team

BACKTEST_ROOT = os.environ.get(
    "BACKTEST_ROOT", r"C:\Users\pdube\odds-backtest-verification"
)
ENTRY_OFFSET_MIN = 115


def _parse_ts(s: str) -> datetime:
    # ISO like '2025-11-03T19:00:00Z'
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --- point-in-time team stats ------------------------------------------------

def stats_available(date: str) -> bool:
    for ver in ("data_stats_v2", "data_stats"):
        d = os.path.join(BACKTEST_ROOT, ver, date)
        if os.path.isdir(d) and any(f.startswith("teamstats_asof_") for f in os.listdir(d)):
            return True
    return False


@lru_cache(maxsize=None)
def load_team_stats(date: str) -> dict:
    """team_name -> {field: value} for the six fields the veto layers read."""
    for ver in ("data_stats_v2", "data_stats"):
        d = os.path.join(BACKTEST_ROOT, ver, date)
        if not os.path.isdir(d):
            continue
        files = [f for f in os.listdir(d) if f.startswith("teamstats_asof_")]
        if not files:
            continue
        doc = json.load(open(os.path.join(d, files[0]), encoding="utf-8"))
        view: dict = {}
        for team in doc["teams"].values():
            name = canonical_team(team["team_name"])
            flat = {}
            for group, fields in FIELDS_BY_GROUP.items():
                for f in fields:
                    flat[f] = team[group][f]
            view[name] = flat
        return view
    raise FileNotFoundError(f"no PIT stats for {date}")


# --- odds snapshots ----------------------------------------------------------

@lru_cache(maxsize=None)
def _load_spread_snapshots(date: str) -> tuple:
    """All spread snapshot files for a day, parsed to
    (taken_at, {game_key: {home, away, commence, {book: home_point}}}).
    game_key is frozenset({home, away}).
    """
    d = os.path.join(BACKTEST_ROOT, "data", date)
    snaps = []
    for fn in sorted(os.listdir(d)):
        if not fn.startswith("spread_"):
            continue
        doc = json.load(open(os.path.join(d, fn), encoding="utf-8"))
        taken_at = _parse_ts(doc["timestamp"])
        games = {}
        for g in doc["data"]:
            home, away = g["home_team"], g["away_team"]
            books = {}
            for bk in g["bookmakers"]:
                for m in bk["markets"]:
                    if m["key"] != "spreads":
                        continue
                    for o in m["outcomes"]:
                        if o["name"] == home:
                            books[bk["key"]] = o["point"]
            games[frozenset((home, away))] = {
                "home": home, "away": away,
                "commence": _parse_ts(g["commence_time"]),
                "books": books,
            }
        snaps.append((taken_at, games))
    return tuple(snaps)


def build_context(date: str, fav: str, dog: str, entry_ml: int):
    """Build (Candidate, VetoContext) for one ground-truth candidate, or None if
    the game is not found in the odds snapshots.

    Snapshots included: every spread snapshot at or before the entry time
    (tip - 115 min), which is what the book layer's trajectory reads.
    """
    key = frozenset((fav, dog))
    all_snaps = _load_spread_snapshots(date)

    game_meta = None
    for _, games in all_snaps:
        if key in games:
            game_meta = games[key]
            break
    if game_meta is None:
        return None

    commence = game_meta["commence"]
    entry_time = commence - timedelta(minutes=ENTRY_OFFSET_MIN)
    home, away = game_meta["home"], game_meta["away"]

    game = Game(sport="nba", game_id=f"{date}:{away}@{home}", game_date=date,
                commence_time=commence, home=home, away=away)

    rows = []
    for taken_at, games in all_snaps:
        if taken_at > entry_time:
            continue
        g = games.get(key)
        if not g:
            continue
        for book, hp in g["books"].items():
            rows.append(SnapshotRow(sport="nba", game_date=date, game_id=game.game_id,
                                    taken_at=taken_at, book=book, home_point=hp))

    cand = Candidate(sport="nba", game=game, favorite=fav, dog=dog,
                     entry_ml=entry_ml, liquidity=None, entry_time_actual=entry_time)
    ctx = VetoContext(team_stats=load_team_stats(date), snapshots=rows)
    return cand, ctx
