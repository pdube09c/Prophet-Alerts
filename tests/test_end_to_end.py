"""Stage 0 paper end-to-end (Design B §12.8), offline.

Drives the whole loop through the REAL NBA veto layers, candidate builder,
alert/settle/summary composers — faking only the two external boundaries the
plan says never touch the runner's disk: the network (Odds API / nba_api) and
the hosted DB (an in-memory stand-in with the same verb surface).

    daily_stats.run  -> stats in DB
    tick.run_tick    -> snapshot appended, survivor persisted, PAPER alert sent
    (static page)    -> user logs a $0 paper bet
    settle.run       -> bet graded + veto audit recorded
    summary.build    -> morning email composed

The canned slate is one home favorite (Boston -180) whose line never moves and
whose stats trip none of the four layers, so it survives to a PAPER alert.
"""

from __future__ import annotations

from datetime import datetime, timezone

from engine import daily_stats, settle, summary, tick
from sports import nba
from sports.nba import NBA, StatRow

UTC = timezone.utc

TIP_ISO = "2026-01-11T00:40:00Z"                 # ET 2026-01-10 19:40
TIP = datetime(2026, 1, 11, 0, 40, tzinfo=UTC)
NOW = datetime(2026, 1, 10, 23, 40, tzinfo=UTC)  # tip - 60m, inside [tip-115, tip)
GAME_DATE = "2026-01-10"
FAV, DOG = "Boston Celtics", "Miami Heat"

# Stats crafted so NONE of grinder / pace-TOV / FT-def fire (see module docstring
# of nba.py for each predicate). Two-team slate -> median is the midpoint.
_STATS = {
    FAV: {"PACE": 100.0, "DREB_PCT": 0.30, "TM_TOV_PCT": 0.13,
          "DEF_RATING": 110.0, "GP": 20, "PCT_FGA_3PT": 0.40, "PCT_PTS_FT": 0.18},
    DOG: {"PACE": 101.0, "DREB_PCT": 0.28, "TM_TOV_PCT": 0.14,
          "DEF_RATING": 112.0, "GP": 20, "PCT_FGA_3PT": 0.42, "PCT_PTS_FT": 0.19},
}
_GROUP = {"PACE": "advanced", "DREB_PCT": "advanced", "TM_TOV_PCT": "advanced",
          "DEF_RATING": "advanced", "GP": "advanced",
          "PCT_FGA_3PT": "scoring", "PCT_PTS_FT": "scoring"}


def _event_odds() -> dict:
    """One /odds event: home favorite -5, ML -180, flat across books, prophetx
    present with a bet limit; away ML +150. No toward-dog movement."""
    def book(key):
        return {"key": key, "markets": [
            {"key": "spreads", "outcomes": [
                {"name": FAV, "point": -5.0}, {"name": DOG, "point": 5.0}]},
            {"key": "h2h", "outcomes": [
                {"name": FAV, "price": -180, "bet_limit": 500},
                {"name": DOG, "price": 150, "bet_limit": 500}]},
        ]}
    return {"id": "evt1", "commence_time": TIP_ISO,
            "home_team": FAV, "away_team": DOG,
            "bookmakers": [book(b) for b in
                           ("pinnacle", "prophetx", "williamhill_us",
                            "betmgm", "fanduel", "draftkings")]}


def _fake_odds_api(path, **extra):
    if path == "events":
        return [{"id": "evt1", "commence_time": TIP_ISO,
                 "home_team": FAV, "away_team": DOG}]
    if path == "odds":
        return [_event_odds()]
    if path == "scores":
        return [{"id": "evt1", "commence_time": TIP_ISO, "completed": True,
                 "scores": [{"name": FAV, "score": "112"},
                            {"name": DOG, "score": "101"}]}]  # favorite wins
    raise AssertionError(path)


def _fake_stats(asof_date):
    return [StatRow(sport="nba", asof_date=asof_date, team=team, group=_GROUP[f],
                    field=f, value=float(v))
            for team, fields in _STATS.items() for f, v in fields.items()]


class FakeDB:
    """In-memory hosted-DB stand-in — the union of verbs the loop calls."""

    def __init__(self):
        self.snapshots = []      # SnapshotRow
        self.stats = []          # dict rows
        self.survivors = {}      # game_id -> row
        self.alerted = set()
        self.vetoed = []         # dict rows, with synthetic ids
        self.bets = {}           # id -> row
        self._veto_seq = 0

    # stats
    def upsert_stats(self, rows):
        self.stats.extend(rows)

    def get_stats(self, sport, asof_date):
        return [r for r in self.stats if r["asof_date"] == asof_date]

    # snapshots
    def append_snapshots(self, rows):
        self.snapshots.extend(rows)

    def get_snapshots(self, sport, game_date, game_id):
        return [s for s in self.snapshots if s.game_id == game_id]

    # survivors
    def is_alerted(self, sport, game_id):
        return game_id in self.alerted

    def upsert_survivor(self, row):
        self.survivors[row["game_id"]] = row

    def mark_alerted(self, sport, game_id):
        self.alerted.add(game_id)

    # vetoed
    def insert_vetoed(self, rows):
        for r in rows:
            self._veto_seq += 1
            self.vetoed.append({**r, "id": self._veto_seq})

    def get_vetoed(self, sport, game_date):
        return [v for v in self.vetoed if v["game_date"] == game_date]

    def settle_vetoed(self, vid, favwin):
        for v in self.vetoed:
            if v["id"] == vid:
                v["favwin_actual"] = favwin

    # bets
    def insert_bet(self, row):        # what the static page's POST does
        self.bets[row["id"]] = {**row, "result": None, "net_pnl": None}

    def get_unsettled_bets(self, sport, game_date):
        return [b for b in self.bets.values()
                if b["game_date"] == game_date and b["result"] is None]

    def settle_bet(self, bet_id, result, net_pnl):
        self.bets[bet_id].update(result=result, net_pnl=net_pnl)

    def get_settled_bets(self, sport, game_date):
        return [b for b in self.bets.values()
                if b["game_date"] == game_date and b["result"] is not None]


def test_stage0_paper_loop(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(nba, "_odds_api_get", _fake_odds_api)
    monkeypatch.setattr(nba, "_pull_nba_stats", lambda asof: _fake_stats(asof))
    monkeypatch.setattr(daily_stats, "db", fake)
    monkeypatch.setattr(settle, "db", fake)

    sport = NBA()

    # 1) daily stats (as of D-1 = game_date - 1).
    ds = daily_stats.run(sport, asof_date="2026-01-09")
    assert ds["rows"] == len(_STATS) * 7
    assert len(fake.stats) == ds["rows"]

    # 2) tick at entry time -> survivor + exactly one PAPER alert.
    alerts = []
    s = tick.run_tick(sport, now=NOW, db=fake, stage="paper",
                      alert_fn=lambda cand, stage: alerts.append((cand, stage)))
    assert s["snapshots"] > 0
    assert s["survivors"] == 1 and s["alerted"] == 1 and s["vetoed"] == 0
    cand, stage = alerts[0]
    assert stage == "paper"
    assert cand.favorite == FAV and cand.dog == DOG and cand.entry_ml == -180
    assert cand.liquidity == 500
    assert "evt1" in fake.survivors

    # idempotent: a second tick in-window does not re-alert.
    s2 = tick.run_tick(sport, now=NOW, db=fake, stage="paper",
                       alert_fn=lambda c, st: alerts.append((c, st)))
    assert s2["alerted"] == 0 and len(alerts) == 1

    # 3) the static page logs a $0 paper decision.
    fake.insert_bet({"id": "bet-1", "sport": "nba", "game_date": GAME_DATE,
                     "favorite": cand.favorite, "dog": cand.dog,
                     "entry_ml": cand.entry_ml, "liquidity": cand.liquidity,
                     "stake_chosen": 0.0, "placed": False})

    # 4) settle: favorite won; paper stake -> $0 P&L, graded 'win'.
    scores = {GAME_DATE: {frozenset((FAV, DOG)): {FAV: 112.0, DOG: 101.0}}}
    out = settle.run(sport, GAME_DATE, scores_by_date=scores)
    assert out["bets_settled"] == 1
    graded = fake.bets["bet-1"]
    assert graded["result"] == "win" and graded["net_pnl"] == 0.0

    # 5) morning summary composes cleanly (no bets *placed* at paper stage).
    msg = summary.build_summary(GAME_DATE, "paper",
                                fake.get_settled_bets("nba", GAME_DATE),
                                fake.get_vetoed("nba", GAME_DATE))
    assert msg.subject == "[PAPER] 2026-01-10 — no bets placed"
