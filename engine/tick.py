"""The rolling tick (Design B §12.3) — the app's heartbeat.

Runs on a dense cron during the active window. Every tick is fully idempotent
and self-healing because ALL state lives in the hosted DB, never on the
ephemeral runner:

  1. Append a fresh odds snapshot for today's slate (idempotent on
     (sport, game_id, taken_at, book)).
  2. For every game that has entered its alert window (tip - entry_offset) and
     is NOT yet marked `alerted`, rebuild the candidate from the accumulated
     snapshots + point-in-time stats and run the veto layers.
  3. Survivor -> upsert the survivor row and, if not already alerted, send the
     alert exactly once and flip the `alerted` flag. Vetoed -> log to `vetoed`.

The `alerted` flag is the self-healing pivot: a runner that dies mid-tick leaves
the flag unset, so the next tick re-evaluates and re-alerts (at-least-once). A
runner that already alerted sees the flag set and suppresses (at-most-... well,
at-least-once overall, but never a duplicate once the flag lands).

The pure planning core (`is_in_alert_window`, `plan_tick`) has no IO so it is
unit-tested directly; `run_tick` injects the DB module and alert callable so the
same test can drive the whole orchestration with fakes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from engine import db as _db
from engine.veto import evaluate_all
from sports.base import Game, VetoContext


# --- pure planning core (no IO — unit-tested directly) -----------------------

def is_in_alert_window(now: datetime, commence: datetime,
                       entry_offset_minutes: int) -> bool:
    """True once we've reached entry time (tip - offset) and tip hasn't passed.

    Below entry time: too early, keep collecting snapshots. At/after tip: the
    game has started, the entry is stale — don't alert. The window is the
    half-open interval [tip - offset, tip).
    """
    entry_time = commence - timedelta(minutes=entry_offset_minutes)
    return entry_time <= now < commence


def plan_tick(games: list[Game], now: datetime, entry_offset_minutes: int,
              alerted_game_ids: set) -> list[Game]:
    """Games to evaluate this tick: in their alert window AND not yet alerted.

    This is the self-healing selector — it depends only on current time and the
    persisted `alerted` set, so restarts/retries converge to the same work.
    """
    return [
        g for g in games
        if is_in_alert_window(now, g.commence_time, entry_offset_minutes)
        and g.game_id not in alerted_game_ids
    ]


def _asof_date(game_date: str) -> str:
    """Default point-in-time stats key: D-1 relative to the game's ET date.

    Used for sports that don't override `stats_asof_key` (NBA). CFB addresses
    its stats by the end of the PRIOR WEEK instead — see sports/cfb.py.
    """
    d = datetime.fromisoformat(game_date).date() - timedelta(days=1)
    return d.isoformat()


# --- orchestration (IO injected for testability) -----------------------------

AlertFn = Callable[[object, str], None]


def run_tick(sport, *, now: Optional[datetime] = None, db=_db,
             alert_fn: Optional[AlertFn] = None, stage: str = "paper") -> dict:
    """One tick for one sport. Returns a small summary dict (for logs/tests).

    `db` and `alert_fn` are injected so unit tests drive the full flow with
    fakes. In production they default to the real DB module and the alert
    sender (wired by the caller).
    """
    now = now or datetime.now(timezone.utc)
    if alert_fn is None:
        alert_fn = _log_only_alert

    summary = {"snapshots": 0, "evaluated": 0, "survivors": 0,
               "alerted": 0, "vetoed": 0}

    games = sport.todays_games(now)

    # 1) append a fresh odds snapshot (idempotent). Stamped with this tick's
    # `now` so every row from one tick shares one timestamp — the veto layers
    # build their trajectories by grouping on it.
    snaps = sport.pull_odds_snapshot(now)
    db.append_snapshots(snaps)
    summary["snapshots"] = len(snaps)

    # 2) which games are due, and not yet alerted (self-healing selection).
    alerted_ids = _alerted_ids(db, sport.key, games)
    due = plan_tick(games, now, sport.entry_offset_minutes, alerted_ids)
    if not due:
        return summary

    layers = sport.veto_layers()
    stats_cache: dict = {}

    for game in due:
        stats_view = _stats_view(db, sport, game, stats_cache)
        snapshots = db.get_snapshots(sport.key, game.game_date, game.game_id)
        cand = sport.build_candidates(game, snapshots, stats_view)
        if cand is None:
            continue
        summary["evaluated"] += 1

        ctx = VetoContext(team_stats=stats_view, snapshots=snapshots)
        fired = evaluate_all(layers, cand, ctx)
        if fired:
            db.insert_vetoed([{
                "sport": sport.key, "game_date": game.game_date,
                "favorite": cand.favorite, "dog": cand.dog,
                "ml": cand.entry_ml, "reason": "+".join(fired),
                "favwin_actual": None,
            }])
            summary["vetoed"] += 1
            continue

        # Survivor: persist, then alert exactly once via the alerted flag.
        db.upsert_survivor({
            "sport": sport.key, "game_date": game.game_date,
            "game_id": game.game_id, "favorite": cand.favorite,
            "dog": cand.dog, "entry_ml": cand.entry_ml,
            "liquidity": cand.liquidity,
            "tip_time": game.commence_time.isoformat(), "alerted": False,
        })
        summary["survivors"] += 1
        if not db.is_alerted(sport.key, game.game_id):
            alert_fn(cand, stage)
            db.mark_alerted(sport.key, game.game_id)
            summary["alerted"] += 1

    return summary


def _alerted_ids(db, sport_key: str, games: list[Game]) -> set:
    """The set of game_ids already marked alerted in the DB.

    Prefers the bulk query (one round trip). A full NCAAF board is 60-100
    listings and this runs every tick, so the per-game fallback below — kept for
    DB fakes that predate the bulk helper — would cost a request per game.
    """
    bulk = getattr(db, "get_alerted_game_ids", None)
    if bulk is not None:
        alerted = bulk(sport_key)
        return {g.game_id for g in games if g.game_id in alerted}
    return {g.game_id for g in games if db.is_alerted(sport_key, g.game_id)}


def _stats_view(db, sport, game: Game, cache: dict) -> dict:
    """Cached point-in-time team-stat view assembled from tall stat rows.

    Both the as-of key and the flattening are delegated to the sport (see the
    `stats_asof_key` / `stats_view` hooks on sports.base.Sport), so the engine
    stays sport-agnostic. A sport that defines neither gets the NBA-shaped
    defaults: as of D-1, flattened field -> value.
    """
    asof_key = getattr(sport, "stats_asof_key", None)
    asof = asof_key(game) if asof_key else _asof_date(game.game_date)
    if asof not in cache:
        rows = db.get_stats(sport.key, asof)
        flatten = getattr(sport, "stats_view", None) or _default_stats_view
        cache[asof] = flatten(_as_statrows(rows))
    return cache[asof]


def _default_stats_view(stat_rows) -> dict:
    view: dict = {}
    for r in stat_rows:
        view.setdefault(r.team, {})[r.field] = r.value
    return view


class _Row:
    __slots__ = ("team", "group", "field", "value")

    def __init__(self, team, group, field, value):
        self.team, self.group, self.field, self.value = team, group, field, value


def _as_statrows(rows: list) -> list:
    """DB stat dicts -> objects team_stats_view can read (team/field/value)."""
    return [_Row(r["team"], r["group"], r["field"], r["value"]) for r in rows]


def _log_only_alert(cand, stage: str) -> None:
    """Default alert sink until alert.py is wired: prints, sends nothing."""
    print(f"[{stage}] ALERT (no sender wired): {cand.favorite} ML "
          f"{cand.entry_ml} vs {cand.dog} — {cand.game.game_id}")


def build_sport(key: str):
    """Construct a sport plug-in by key. The engine is sport-agnostic; this is
    the one place that maps a CLI/workflow argument to a concrete plug-in."""
    key = key.strip().lower()
    if key == "nba":
        from sports.nba import NBA
        return NBA()
    if key == "cfb":
        from engine.config import cfb_entry_offset_minutes
        from sports.cfb import CFB
        return CFB(entry_offset_minutes=cfb_entry_offset_minutes())
    raise SystemExit(f"unknown sport {key!r}; expected one of: nba, cfb")


if __name__ == "__main__":
    # Production entry point (invoked by .github/workflows/tick.yml). Wires the
    # real email alert + configured stage; secrets come from the environment.
    #
    # One tick per sport. CFB shares this loop unchanged: its accumulate job is
    # the snapshot append every tick makes, and its fire job is the same
    # alert-window selection keyed off the sport's entry_offset_minutes.
    import argparse
    import json

    from engine import config
    from engine.alert import send_alert

    parser = argparse.ArgumentParser(prog="python -m engine.tick")
    parser.add_argument("--sport", default="nba",
                        help="which sport to tick (nba|cfb); default nba")
    args = parser.parse_args()

    cfg = config.alert_config()
    summary = run_tick(
        build_sport(args.sport), stage=cfg.stage,
        alert_fn=lambda cand, stage: send_alert(cand, cfg),
    )
    print(json.dumps({"sport": args.sport, **summary}, indent=2))
