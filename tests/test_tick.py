"""Unit tests for the rolling tick (Design B §12.3).

No network, no DB: the pure planning core is tested directly, and the full
`run_tick` orchestration is driven with a fake sport + in-memory fake DB so the
alerted-flag self-healing (alert exactly once; re-alert after a mid-tick crash;
never after the flag lands) is exercised end to end.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from engine import tick
from sports.base import Candidate, Game, VetoLayer, VetoResult

UTC = timezone.utc
OFFSET = 115  # NBA entry offset (minutes)


def _game(gid: str, tip: datetime) -> Game:
    return Game(sport="nba", game_id=gid, game_date="2026-01-10",
                commence_time=tip, home="Boston Celtics", away="Miami Heat")


# --- pure window logic -------------------------------------------------------

def test_in_alert_window_boundaries():
    tip = datetime(2026, 1, 10, 23, 40, tzinfo=UTC)
    entry = tip - timedelta(minutes=OFFSET)
    # exactly at entry time -> in window (half-open lower bound is inclusive).
    assert tick.is_in_alert_window(entry, tip, OFFSET)
    # one minute before entry -> too early.
    assert not tick.is_in_alert_window(entry - timedelta(minutes=1), tip, OFFSET)
    # mid-window -> in.
    assert tick.is_in_alert_window(entry + timedelta(minutes=30), tip, OFFSET)
    # exactly at tip -> out (upper bound exclusive; entry is stale).
    assert not tick.is_in_alert_window(tip, tip, OFFSET)
    # after tip -> out.
    assert not tick.is_in_alert_window(tip + timedelta(minutes=1), tip, OFFSET)


def test_plan_tick_selects_in_window_and_unalerted_only():
    now = datetime(2026, 1, 10, 22, 0, tzinfo=UTC)
    due_now = _game("due", now + timedelta(minutes=30))       # entry passed, tip ahead
    too_early = _game("early", now + timedelta(hours=5))       # entry not reached
    already = _game("done", now + timedelta(minutes=30))       # in window but alerted
    started = _game("started", now - timedelta(minutes=1))     # tip passed
    games = [due_now, too_early, already, started]

    picked = tick.plan_tick(games, now, OFFSET, alerted_game_ids={"done"})
    assert [g.game_id for g in picked] == ["due"]


# --- fakes for the full orchestration ---------------------------------------

class FakeDB:
    """In-memory stand-in for engine.db with the same verb surface tick uses."""

    def __init__(self, snapshots_by_game, stats_rows):
        self._snaps = snapshots_by_game          # game_id -> [SnapshotRow]
        self._stats = stats_rows                 # [dict]
        self.survivors = {}                      # game_id -> row
        self.alerted = set()                     # game_ids with flag set
        self.vetoed = []
        self.appended = 0

    def append_snapshots(self, rows):
        self.appended += len(rows)

    def get_snapshots(self, sport, game_date, game_id):
        return self._snaps.get(game_id, [])

    def get_stats(self, sport, asof_date):
        return self._stats

    def is_alerted(self, sport, game_id):
        return game_id in self.alerted

    def upsert_survivor(self, row):
        self.survivors[row["game_id"]] = row

    def mark_alerted(self, sport, game_id):
        self.alerted.add(game_id)

    def insert_vetoed(self, rows):
        self.vetoed.extend(rows)


class FakeSport:
    key = "nba"
    entry_offset_minutes = OFFSET

    def __init__(self, games, candidate, fired_layers):
        self._games = games
        self._candidate = candidate          # returned by build_candidates
        self._fired = fired_layers           # layer names that should fire

    def todays_games(self, now):
        return self._games

    def pull_odds_snapshot(self):
        return [object(), object()]          # 2 rows; content irrelevant here

    def build_candidates(self, game, snapshots, stats):
        return self._candidate

    def veto_layers(self):
        fired = set(self._fired)
        return [
            VetoLayer(name, (lambda n: (lambda c, ctx: VetoResult(n in fired, n)))(name))
            for name in ("book", "grinder", "pace/TOV", "FT/def")
        ]


def _candidate(game):
    return Candidate(sport="nba", game=game, favorite="Boston Celtics",
                     dog="Miami Heat", entry_ml=-150, liquidity=500.0,
                     entry_time_actual=game.commence_time)


def _run(sport, db, now, alerts):
    return tick.run_tick(sport, now=now, db=db,
                         alert_fn=lambda c, stage: alerts.append((c.game.game_id, stage)))


# --- self-healing / alerted-flag orchestration -------------------------------

def test_survivor_alerts_exactly_once_across_ticks():
    now = datetime(2026, 1, 10, 22, 0, tzinfo=UTC)
    game = _game("g1", now + timedelta(minutes=30))
    db = FakeDB(snapshots_by_game={"g1": [1, 2, 3]}, stats_rows=[])
    sport = FakeSport([game], _candidate(game), fired_layers=[])
    alerts = []

    s1 = _run(sport, db, now, alerts)
    assert s1["survivors"] == 1 and s1["alerted"] == 1
    assert alerts == [("g1", "paper")]
    assert "g1" in db.alerted

    # Second tick a few minutes later: still in window, but flag is set -> no dup.
    s2 = _run(sport, db, now + timedelta(minutes=5), alerts)
    assert s2["alerted"] == 0
    assert alerts == [("g1", "paper")]          # unchanged


def test_crash_before_flag_causes_realert_next_tick():
    now = datetime(2026, 1, 10, 22, 0, tzinfo=UTC)
    game = _game("g1", now + timedelta(minutes=30))
    db = FakeDB(snapshots_by_game={"g1": [1]}, stats_rows=[])
    sport = FakeSport([game], _candidate(game), fired_layers=[])

    # Simulate a crash: survivor persisted but mark_alerted never ran.
    db.upsert_survivor({"sport": "nba", "game_id": "g1", "favorite": "x",
                        "dog": "y", "entry_ml": -150, "alerted": False,
                        "game_date": "2026-01-10",
                        "tip_time": game.commence_time.isoformat(),
                        "liquidity": None})
    assert not db.is_alerted("nba", "g1")

    alerts = []
    s = _run(sport, db, now, alerts)
    # Flag was unset -> next tick re-alerts (at-least-once delivery).
    assert s["alerted"] == 1
    assert alerts == [("g1", "paper")]
    assert db.is_alerted("nba", "g1")


def test_vetoed_candidate_is_logged_not_alerted():
    now = datetime(2026, 1, 10, 22, 0, tzinfo=UTC)
    game = _game("g1", now + timedelta(minutes=30))
    db = FakeDB(snapshots_by_game={"g1": [1]}, stats_rows=[])
    sport = FakeSport([game], _candidate(game), fired_layers=["book", "grinder"])

    alerts = []
    s = _run(sport, db, now, alerts)
    assert s["vetoed"] == 1 and s["survivors"] == 0 and s["alerted"] == 0
    assert alerts == []
    assert db.vetoed[0]["reason"] == "book+grinder"
    assert db.vetoed[0]["favorite"] == "Boston Celtics"


def test_too_early_game_only_appends_snapshot():
    now = datetime(2026, 1, 10, 18, 0, tzinfo=UTC)
    game = _game("g1", now + timedelta(hours=5))     # entry not reached
    db = FakeDB(snapshots_by_game={"g1": [1]}, stats_rows=[])
    sport = FakeSport([game], _candidate(game), fired_layers=[])

    alerts = []
    s = _run(sport, db, now, alerts)
    assert db.appended == 2                # snapshot still appended every tick
    assert s["evaluated"] == 0 and s["survivors"] == 0
    assert alerts == []


def test_no_candidate_is_skipped():
    now = datetime(2026, 1, 10, 22, 0, tzinfo=UTC)
    game = _game("g1", now + timedelta(minutes=30))
    db = FakeDB(snapshots_by_game={"g1": [1]}, stats_rows=[])
    sport = FakeSport([game], candidate=None, fired_layers=[])

    alerts = []
    s = _run(sport, db, now, alerts)
    assert s["evaluated"] == 0 and s["survivors"] == 0 and s["vetoed"] == 0
    assert alerts == []
