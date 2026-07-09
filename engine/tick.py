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
from sports.nba import team_stats_view


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
    """Point-in-time stats are as of D-1 relative to the game's ET date."""
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

    # 1) append a fresh odds snapshot (idempotent).
    snaps = sport.pull_odds_snapshot()
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
        stats_view = _stats_view(db, sport.key, game.game_date, stats_cache)
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
    """The set of today's game_ids already marked alerted in the DB."""
    return {g.game_id for g in games if db.is_alerted(sport_key, g.game_id)}


def _stats_view(db, sport_key: str, game_date: str, cache: dict) -> dict:
    """Cached per-day team-stat view (as of D-1) assembled from tall stat rows."""
    asof = _asof_date(game_date)
    if asof not in cache:
        rows = db.get_stats(sport_key, asof)
        cache[asof] = team_stats_view(_as_statrows(rows))
    return cache[asof]


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


if __name__ == "__main__":
    # Production entry point (invoked by .github/workflows/tick.yml). Wires the
    # real email alert + configured stage; secrets come from the environment.
    import json

    from engine import config
    from engine.alert import send_alert
    from sports.nba import NBA

    cfg = config.alert_config()
    summary = run_tick(
        NBA(), stage=cfg.stage,
        alert_fn=lambda cand, stage: send_alert(cand, cfg),
    )
    print(json.dumps(summary, indent=2))
