"""CFB through the REAL engine tick, with the network and DB faked.

Proves the two jobs the brief specifies actually fall out of the existing
rolling tick rather than needing new machinery:

  accumulate  every tick appends the whole board's per-book snapshots
  fire        a game that reaches T-24h is evaluated against the three
              conditions and, surviving, alerted exactly once

and that the alert goes through the SAME engine.alert path NBA uses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from engine import tick as tick_mod
from sports import cfb
from tests.test_cfb import FAV, DOG, CFBNoNetwork, _calendar

UTC = timezone.utc

# Kick off two days into week 6 of the synthetic calendar.
_CAL = _calendar(weeks=14)
KICK = cfb.week_bounds(_CAL)[6][0] + timedelta(days=2)
ASOF = cfb.week_bounds(_CAL)[5][1].date().isoformat()
# Snapshots and survivors file under the ET calendar date of kickoff (see
# cfb._et_date), which is NOT the UTC date for a late kickoff.
GAME_DATE = KICK.astimezone(cfb.ET).date().isoformat()


class FakeDB:
    """In-memory stand-in for engine.db, mirroring the real module's surface."""

    def __init__(self, stats_rows=None):
        self.snapshots: list = []
        self.stats_rows = stats_rows or []
        self.survivors: dict = {}
        self.alerted: set = set()
        self.vetoed: list = []
        self.calls: list = []

    def append_snapshots(self, rows):
        self.calls.append("append_snapshots")
        self.snapshots.extend(rows)

    def get_snapshots(self, sport, game_date, game_id):
        return sorted(
            [s for s in self.snapshots if s.sport == sport
             and s.game_date == game_date and s.game_id == game_id],
            key=lambda s: s.taken_at)

    def get_stats(self, sport, asof_date):
        return [r for r in self.stats_rows if r["asof_date"] == asof_date]

    def get_alerted_game_ids(self, sport):
        self.calls.append("get_alerted_game_ids")
        return set(self.alerted)

    def is_alerted(self, sport, game_id):
        return game_id in self.alerted

    def upsert_survivor(self, row):
        self.survivors[row["game_id"]] = row

    def mark_alerted(self, sport, game_id):
        self.alerted.add(game_id)

    def insert_vetoed(self, rows):
        self.vetoed.extend(rows)


# Week 6 kickoff -> stats must be as of endWeek 5.
ASOF_WEEK = 5


def _stat_rows(dog_stuff=0.05, dog_sr=0.46, fav_sr=0.42, games=5,
               asof_week=ASOF_WEEK):
    """Tall DB stat dicts as engine.db.get_stats returns them."""
    rows = []

    def add(team, field, value):
        rows.append({"sport": "cfb", "asof_date": ASOF, "team": team,
                     "group": "advanced", "field": field, "value": value})

    for i in range(9):
        add(f"T{i}", cfb.F_STUFF, 0.10 + 0.01 * i)
        add(f"T{i}", cfb.F_SR, 0.40)
    add(DOG, cfb.F_STUFF, dog_stuff)
    add(DOG, cfb.F_SR, dog_sr)
    add(DOG, cfb.F_GAMES, games)
    add(FAV, cfb.F_STUFF, 0.11)
    add(FAV, cfb.F_SR, fav_sr)
    add(FAV, cfb.F_GAMES, games)
    for team in [f"T{i}" for i in range(9)] + [FAV, DOG]:
        add(team, cfb.F_ASOF_WEEK, asof_week)
    return rows


def _event(home=FAV, away=DOG, kickoff=KICK, home_point=-7.0, home_ml=-150):
    """One Odds API event carrying all six books at one poll time."""
    books = []
    for key in cfb.NON_PROPHETX_BOOKS + (cfb.PROPHETX,):
        markets = [{"key": "spreads", "outcomes": [
            {"name": home, "point": home_point},
            {"name": away, "point": -home_point}]}]
        if key == cfb.PROPHETX:
            markets.append({"key": "h2h", "outcomes": [
                {"name": home, "price": home_ml, "bet_limit": 2500.0},
                {"name": away, "price": 130, "bet_limit": 800.0}]})
        books.append({"key": key, "markets": markets})
    return {"id": "EVT1", "commence_time": kickoff.isoformat().replace("+00:00", "Z"),
            "home_team": home, "away_team": away, "bookmakers": books}


class TickCFB(CFBNoNetwork):
    """CFB with the odds board injected, so a tick does no network IO."""

    def __init__(self, events, **kw):
        super().__init__(calendar=_CAL, fbs={FAV, DOG}, **kw)
        self._events = events

    def _board(self):
        return self._events


def _run(db, sport, now, alerts):
    return tick_mod.run_tick(sport, now=now, db=db,
                             alert_fn=lambda cand, stage: alerts.append(cand))


class TestAccumulate:
    def test_every_tick_appends_the_whole_board(self):
        db, alerts = FakeDB(_stat_rows()), []
        sport = TickCFB([_event()])
        # Four days out — far outside the T-24h fire window.
        out = _run(db, sport, KICK - timedelta(days=4), alerts)
        assert out["snapshots"] == 6        # all six books
        assert out["alerted"] == 0          # accumulate only, no fire
        assert alerts == []

    def test_accumulation_builds_a_trajectory_across_ticks(self):
        db, alerts = FakeDB(_stat_rows()), []
        for hours, point in ((96, -7.0), (95, -7.0), (94, -7.5)):
            sport = TickCFB([_event(home_point=point)])
            _run(db, sport, KICK - timedelta(hours=hours), alerts)
        rows = db.get_snapshots("cfb", GAME_DATE, "EVT1")
        assert len({r.taken_at for r in rows}) == 3
        assert len(rows) == 18              # 3 polls x 6 books

    def test_snapshots_carry_the_kickoff_date_so_the_game_stays_one_series(self):
        """Accumulation spans days; every snapshot must file under the same
        game_date or the trajectory would fragment and the veto see nothing."""
        db, alerts = FakeDB(_stat_rows()), []
        for days in (6, 4, 2):
            _run(db, TickCFB([_event()]), KICK - timedelta(days=days), alerts)
        assert {s.game_date for s in db.snapshots} == {GAME_DATE}


class TestFire:
    def _accumulate(self, db, *, retail_moves=0, pinny_move=False):
        """Hourly ticks from T-72h to T-70h, inside the T-48h window."""
        alerts = []
        for hours, moved in ((72, False), (71, True), (70, True)):
            for i, book in enumerate(cfb.NON_PROPHETX_BOOKS):
                is_retail = book in cfb.RETAIL_BOOKS
                idx = cfb.RETAIL_BOOKS.index(book) if is_retail else -1
                shift = (moved and ((is_retail and idx < retail_moves)
                                    or (book == cfb.PINNACLE and pinny_move)))
                point = -8.5 if shift else -7.0
                ev = _event(home_point=point)
                ev["bookmakers"] = [b for b in ev["bookmakers"]
                                    if b["key"] == book]
                _run(db, TickCFB([ev]), KICK - timedelta(hours=hours), alerts)
        return alerts

    def test_survivor_fires_once_inside_the_window(self):
        db = FakeDB(_stat_rows())
        self._accumulate(db)
        alerts = []
        out = _run(db, TickCFB([_event()]), KICK - timedelta(hours=12), alerts)
        assert out["survivors"] == 1 and out["alerted"] == 1
        assert len(alerts) == 1
        assert (alerts[0].favorite, alerts[0].dog) == (FAV, DOG)
        assert alerts[0].sport == "cfb"
        assert db.survivors["EVT1"]["sport"] == "cfb"

    def test_alert_carries_the_maturity_flag(self):
        db = FakeDB(_stat_rows(games=5))
        self._accumulate(db)
        alerts = []
        _run(db, TickCFB([_event()]), KICK - timedelta(hours=12), alerts)
        ann = dict(alerts[0].annotations)
        assert ann["Data maturity"] == "as-of through Week 5: 5 games of data"
        assert "reliable" in ann["Stats confidence"]
        assert ann["Retail-extend"].startswith("complete")

    def test_no_second_alert_on_a_later_tick(self):
        """The `alerted` flag is the idempotence pivot; a repeat tick must not
        re-alert."""
        db = FakeDB(_stat_rows())
        self._accumulate(db)
        alerts = []
        _run(db, TickCFB([_event()]), KICK - timedelta(hours=12), alerts)
        _run(db, TickCFB([_event()]), KICK - timedelta(hours=6), alerts)
        assert len(alerts) == 1

    def test_a_crash_before_mark_alerted_re_alerts_next_tick(self):
        """Self-healing: the flag lands only after the alert, so a runner that
        dies mid-tick leaves work the next tick picks up (at-least-once)."""
        db = FakeDB(_stat_rows())
        self._accumulate(db)
        alerts = []
        _run(db, TickCFB([_event()]), KICK - timedelta(hours=12), alerts)
        db.alerted.clear()                       # simulate the lost write
        _run(db, TickCFB([_event()]), KICK - timedelta(hours=11), alerts)
        assert len(alerts) == 2

    def test_too_early_does_not_fire(self):
        db = FakeDB(_stat_rows())
        self._accumulate(db)
        alerts = []
        out = _run(db, TickCFB([_event()]), KICK - timedelta(hours=30), alerts)
        assert out["alerted"] == 0 and alerts == []

    def test_after_kickoff_does_not_fire(self):
        db = FakeDB(_stat_rows())
        self._accumulate(db)
        alerts = []
        out = _run(db, TickCFB([_event()]), KICK + timedelta(minutes=1), alerts)
        assert out["alerted"] == 0 and alerts == []

    def test_retail_extend_veto_reads_the_accumulated_trajectory(self):
        """The veto's whole input is what earlier ticks wrote to the DB. Two
        retail books moving with Pinnacle flat must veto, and be logged."""
        db = FakeDB(_stat_rows())
        self._accumulate(db, retail_moves=2)
        alerts = []
        out = _run(db, TickCFB([_event()]), KICK - timedelta(hours=12), alerts)
        assert out["vetoed"] == 1 and out["alerted"] == 0
        assert db.vetoed[0]["reason"] == "retail-extend"
        assert db.vetoed[0]["sport"] == "cfb"

    def test_pinnacle_confirmation_lets_the_same_move_through(self):
        db = FakeDB(_stat_rows())
        self._accumulate(db, retail_moves=4, pinny_move=True)
        alerts = []
        out = _run(db, TickCFB([_event()]), KICK - timedelta(hours=12), alerts)
        assert out["alerted"] == 1

    def test_stats_are_read_from_the_prior_week_key(self):
        """The tick must look the stats up under endWeek=N-1's key. Stats filed
        under any other key must not be found — that is the no-lookahead
        guarantee, enforced through the real engine."""
        rows = _stat_rows()
        for r in rows:                       # file them one week too late
            r["asof_date"] = cfb.week_bounds(_CAL)[6][1].date().isoformat()
        db = FakeDB(rows)
        self._accumulate(db)
        alerts = []
        _run(db, TickCFB([_event()]), KICK - timedelta(hours=12), alerts)
        # No stats found -> the stat vetoes abstain, and the flag says so.
        assert dict(alerts[0].annotations)["Stats confidence"].startswith("unknown")

    def test_contaminated_stats_stop_the_tick(self):
        """The no-lookahead guard runs on the rows that actually feed the
        vetoes. Stats filed under the right key but pulled through the game's
        own week must raise, not quietly produce an alert."""
        db = FakeDB(_stat_rows(asof_week=6))       # week-6 game, week-6 stats
        self._accumulate(db)
        with pytest.raises(ValueError, match="LOOKAHEAD"):
            _run(db, TickCFB([_event()]), KICK - timedelta(hours=12), [])

    def test_clean_as_of_stats_do_not_trip_the_guard(self):
        db = FakeDB(_stat_rows(asof_week=5))
        self._accumulate(db)
        alerts = []
        out = _run(db, TickCFB([_event()]), KICK - timedelta(hours=12), alerts)
        assert out["alerted"] == 1

    def test_board_wide_alerted_lookup_is_one_query(self):
        """A full NCAAF board is 60-100 listings on an hourly tick; the alerted
        check must not cost a request per game."""
        db = FakeDB(_stat_rows())
        events = [dict(_event(), id=f"EVT{i}") for i in range(50)]
        _run(db, TickCFB(events), KICK - timedelta(days=4), [])
        assert db.calls.count("get_alerted_game_ids") == 1


class TestUniverseThroughTheTick:
    def test_heavy_favorite_is_not_a_candidate(self):
        db = FakeDB(_stat_rows())
        alerts = []
        out = _run(db, TickCFB([_event(home_ml=-450)]),
                   KICK - timedelta(hours=12), alerts)
        assert out["evaluated"] == 0 and out["alerted"] == 0

    def test_non_fbs_game_is_skipped_not_alerted(self):
        db = FakeDB(_stat_rows())
        sport = TickCFB([_event()])
        sport._fbs_cache = {FAV}             # dog is no longer FBS
        out = _run(db, sport, KICK - timedelta(hours=12), [])
        assert out["evaluated"] == 0

    def test_unmapped_team_surfaces_loudly_without_killing_the_tick(self, capsys):
        """An unmapped name is announced, never silently dropped — but one
        unknown FCS opponent must not suppress every other game on the board."""
        cfb.UNRESOLVED_BOARD_NAMES.discard("Nowhere Tech")
        db = FakeDB(_stat_rows())
        sport = TickCFB([_event(home="Nowhere Tech")])
        sport._fbs_cache = {"Nowhere Tech", DOG}
        out = _run(db, sport, KICK - timedelta(hours=12), [])
        assert out["evaluated"] == 0
        assert "UNRESOLVED TEAM NAME" in capsys.readouterr().err

    def test_one_bad_name_does_not_block_the_rest_of_the_board(self):
        cfb.UNRESOLVED_BOARD_NAMES.discard("Nowhere Tech")
        db = FakeDB(_stat_rows())
        bad = dict(_event(home="Nowhere Tech"), id="BAD")
        good = _event()
        sport = TickCFB([bad, good])
        sport._fbs_cache = {"Nowhere Tech", FAV, DOG}
        TestFire()._accumulate(db)
        out = _run(db, sport, KICK - timedelta(hours=12), [])
        assert out["alerted"] == 1


class TestAlertPathIsShared:
    def test_the_tick_alert_renders_through_engine_alert(self):
        """End to end: a CFB survivor from the real tick composes through the
        same build_alert NBA uses, with the shared ladder and staged sizing."""
        from engine.alert import AlertConfig, build_alert

        db = FakeDB(_stat_rows())
        TestFire()._accumulate(db)
        alerts = []
        _run(db, TickCFB([_event()]), KICK - timedelta(hours=12), alerts)

        cfg = AlertConfig(stage="stage2", email_from="a@b.c", email_to="d@e.f",
                          page_url="https://example/page", stage2_target=250.0,
                          stake_ladder=(100.0, 500.0, 1000.0))
        msg = build_alert(alerts[0], cfg)
        assert "[CFB]" in msg.subject
        assert "as-of through Week 5" in msg.text
        assert "Reference ladder" in msg.text        # shared ladder
        assert "RECOMMENDED STAKE (STAGE2)" in msg.text  # shared staged sizing
