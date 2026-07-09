"""Unit tests for settlement + morning summary (Design B §12.6).

No network, no DB, no email: settle.run is driven with canned scores + a fake DB
module, and build_summary is tested as a pure function.
"""

from __future__ import annotations

import types

from engine import settle, summary
from sports.nba import NBA


class FakeDB:
    def __init__(self, unsettled, vetoed):
        self._unsettled = unsettled
        self._vetoed = vetoed
        self.settled_bets = []      # (id, result, net_pnl)
        self.settled_vetoes = []    # (id, favwin)

    def get_unsettled_bets(self, sport, date):
        return list(self._unsettled)

    def settle_bet(self, bet_id, result, net_pnl):
        self.settled_bets.append((bet_id, result, net_pnl))

    def get_vetoed(self, sport, date):
        return list(self._vetoed)

    def settle_vetoed(self, vid, favwin):
        self.settled_vetoes.append((vid, favwin))


def _bet(bid, fav, dog, ml, stake, placed=True):
    return {"id": bid, "sport": "nba", "game_date": "2026-01-10",
            "favorite": fav, "dog": dog, "entry_ml": ml,
            "liquidity": None, "stake_chosen": stake, "placed": placed}


# --- settlement --------------------------------------------------------------

def test_settle_grades_bets_and_vetoed_audit(monkeypatch):
    fav, dog = "Boston Celtics", "Miami Heat"
    scores = {"2026-01-10": {
        frozenset((fav, dog)): {fav: 110.0, dog: 101.0},          # favorite won
    }}
    bets = [_bet("b1", fav, dog, -200, 100)]                       # placed, fav won
    vetoed = [{"id": 7, "favorite": fav, "dog": dog, "favwin_actual": None}]
    fake = FakeDB(bets, vetoed)
    monkeypatch.setattr(settle, "db", fake)

    out = settle.run(NBA(), "2026-01-10", scores_by_date=scores)
    assert out["bets_settled"] == 1 and out["vetoed_settled"] == 1
    # -200 win: 0.98 * 100 * 100/200 = 49.00
    assert fake.settled_bets == [("b1", "win", 49.0)]
    assert fake.settled_vetoes == [(7, True)]


def test_settle_marks_loss_and_unmatched(monkeypatch):
    fav, dog = "Boston Celtics", "Miami Heat"
    scores = {"2026-01-10": {
        frozenset((fav, dog)): {fav: 99.0, dog: 105.0},           # favorite lost
    }}
    bets = [_bet("b1", fav, dog, -150, 100),
            _bet("b2", "Denver Nuggets", "Phoenix Suns", -120, 50)]  # no score
    fake = FakeDB(bets, [])
    monkeypatch.setattr(settle, "db", fake)

    out = settle.run(NBA(), "2026-01-10", scores_by_date=scores)
    assert out["bets_settled"] == 1 and out["unmatched"] == 1
    assert fake.settled_bets == [("b1", "loss", -100.0)]


# --- summary composition -----------------------------------------------------

def test_build_summary_record_pnl_and_audit():
    settled = [
        {"favorite": "Boston Celtics", "entry_ml": -200, "stake_chosen": 100,
         "result": "win", "net_pnl": 49.0, "placed": True},
        {"favorite": "Denver Nuggets", "entry_ml": -150, "stake_chosen": 100,
         "result": "loss", "net_pnl": -100.0, "placed": True},
        {"favorite": "LA Lakers", "entry_ml": -110, "stake_chosen": 0,
         "result": "win", "net_pnl": 0.0, "placed": False},        # paper, excluded from record
    ]
    vetoed = [
        {"favwin_actual": False}, {"favwin_actual": False},        # saved us
        {"favwin_actual": True},                                    # cost us
        {"favwin_actual": None},                                    # unsettled -> ignored
    ]
    msg = summary.build_summary("2026-01-10", "stage1", settled, vetoed)
    assert msg.subject == "[STAGE1] 2026-01-10 — 1-1, -$51.00"
    assert "Record: 1-1" in msg.text
    assert "Net P&L: -$51.00" in msg.text
    assert "2 lost (veto saved us), 1 won (veto cost us)" in msg.text
    # paper/non-placed row must not appear in the placed record.
    assert "LA Lakers" not in msg.text


def test_build_summary_no_bets():
    msg = summary.build_summary("2026-01-10", "paper", settled_bets=[], vetoed=[])
    assert msg.subject == "[PAPER] 2026-01-10 — no bets placed"
    assert "No bets were placed." in msg.text
