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
    import json
    print(json.dumps(run(), indent=2))
