"""Daily point-in-time stats pull (Design B §12.6).

Runs once a day (before the slate) from .github/workflows/daily-stats.yml. Pulls
each team's stats as of D-1 (no lookahead) via the sport adapter and upserts them
into the `stats` table, so the tick can evaluate candidates without touching
nba_api on every poll.

`asof_date` defaults to yesterday (ET) but can be overridden (backfill / tests).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from engine import db
from sports.nba import ET, NBA


def _yesterday_et() -> str:
    return (datetime.now(ET).date() - timedelta(days=1)).isoformat()


def run(sport=None, asof_date: str | None = None) -> dict:
    """Pull one sport's point-in-time stats as of `asof_date` and upsert them.

    Sport-agnostic: each plug-in decides what "as of" means for it (NBA: the
    prior day; CFB: through the prior completed week) and returns tall StatRows
    keyed accordingly. This function just persists whatever it is handed.
    """
    sport = sport or NBA()
    asof = asof_date or _yesterday_et()
    rows = sport.pull_stats(asof)
    db.upsert_stats([
        {"sport": r.sport, "asof_date": r.asof_date, "team": r.team,
         "group": r.group, "field": r.field, "value": r.value}
        for r in rows
    ])
    return {"sport": sport.key, "asof_date": asof, "rows": len(rows)}


if __name__ == "__main__":
    import argparse
    import json

    from engine.tick import build_sport

    parser = argparse.ArgumentParser(prog="python -m engine.daily_stats")
    parser.add_argument("--sport", default="nba",
                        help="which sport's stats to pull (nba|cfb)")
    parser.add_argument("--asof-date", dest="asof_date", default=None,
                        help="as-of date YYYY-MM-DD; default = yesterday ET")
    args = parser.parse_args()

    print(json.dumps(run(build_sport(args.sport), asof_date=args.asof_date),
                     indent=2))
