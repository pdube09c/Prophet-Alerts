"""Settlement (Design B §12.6).

Grades the prior day's logged bets and records how the vetoed games turned out
(the audit trail that tells us whether the veto layers earned their keep). Runs
each morning from .github/workflows/morning-summary.yml, before the summary
email.

Final scores come from The Odds API /scores endpoint (same ODDSAPIKEY, no extra
credential). Grading + P&L are delegated to the sport (`NBA.settle`) so the math
lives in one place.

`fetch_scores` is the only network touchpoint; it's injected into `run` so the
settlement logic is unit-tested with canned scores.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from engine import db
from sports.base import Bet
from sports.nba import ET, NBA, _odds_api_get, _et_date


def _yesterday_et() -> str:
    return (datetime.now(ET).date() - timedelta(days=1)).isoformat()


def fetch_scores(days_from: int = 2) -> dict:
    """{game_date: {frozenset({teamA, teamB}): {team: points}}} for completed
    games in the recent window, from The Odds API /scores."""
    events = _odds_api_get("scores", daysFrom=days_from)
    out: dict = {}
    for e in events:
        if not e.get("completed") or not e.get("scores"):
            continue
        date = _et_date(e["commence_time"])
        try:
            score = {s["name"]: float(s["score"]) for s in e["scores"]}
        except (TypeError, ValueError):
            continue
        key = frozenset(score.keys())
        out.setdefault(date, {})[key] = score
    return out


def _bet_from_row(r: dict) -> Bet:
    return Bet(id=r["id"], sport=r["sport"], game_date=r["game_date"],
              favorite=r["favorite"], dog=r["dog"], entry_ml=r["entry_ml"],
              liquidity=r.get("liquidity"), stake_chosen=r["stake_chosen"],
              entry_time_actual=None, placed=r.get("placed", False))


def run(sport=None, game_date: str | None = None, *, scores_by_date=None) -> dict:
    """Settle a date's bets + vetoed audit. Returns a small summary dict."""
    sport = sport or NBA()
    date = game_date or _yesterday_et()
    scores = scores_by_date if scores_by_date is not None else fetch_scores()
    day = scores.get(date, {})

    summary = {"sport": sport.key, "game_date": date,
               "bets_settled": 0, "vetoed_settled": 0, "unmatched": 0}

    for row in db.get_unsettled_bets(sport.key, date):
        final = day.get(frozenset((row["favorite"], row["dog"])))
        if final is None:
            summary["unmatched"] += 1
            continue
        res = sport.settle(_bet_from_row(row), final)
        db.settle_bet(row["id"], "win" if res.win else "loss", res.net_pnl)
        summary["bets_settled"] += 1

    for row in db.get_vetoed(sport.key, date):
        if row.get("favwin_actual") is not None:
            continue
        final = day.get(frozenset((row["favorite"], row["dog"])))
        if final is None:
            continue
        favwin = final[row["favorite"]] > final[row["dog"]]
        db.settle_vetoed(row["id"], favwin)
        summary["vetoed_settled"] += 1

    return summary


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2))
