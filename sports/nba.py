"""NBA plug-in (Design B §5) — build first.

Ports the validated backtest veto logic exactly. The four veto layers are pure
functions over (favorite, dog, team-stat view, this game's odds snapshots) so
the identical code runs in the live tick and in the historical unit tests that
reproduce the backtest's keep/veto decisions.

The four layers (point-in-time as of D-1, median split across all 30 teams):
  1. book      — sustained >=1 pt move toward the dog, held 2+ consecutive
                 snapshots, on the retail median OR Pinnacle.
  2. grinder   — dog bottom-half PACE AND bottom-half PCT_FGA_3PT AND
                 top-half DREB_PCT.
  3. pace/TOV  — dog below-median TM_TOV_PCT AND favorite above-median PACE.
  4. FT/def    — dog top-half PCT_PTS_FT AND dog DEF_RATING below median.

Any layer fires -> veto. Transition and three-point are deliberately excluded
(both were tested and falsified).
"""

from __future__ import annotations

import os
import statistics
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .base import (
    Candidate, Game, Result, SnapshotRow, StatRow, VetoContext, VetoLayer, VetoResult,
)

ET = ZoneInfo("America/New_York")

# Fields each layer reads, by nba_api group. team_stats views are flattened to
# field -> value (these field names are unique across the groups used).
FIELDS_BY_GROUP = {
    # GP is not a veto input; it's carried for the >=6 games-played gate.
    "advanced": ["PACE", "DREB_PCT", "TM_TOV_PCT", "DEF_RATING", "GP"],
    "scoring": ["PCT_FGA_3PT", "PCT_PTS_FT"],
}
ALL_FIELDS = [f for fs in FIELDS_BY_GROUP.values() for f in fs]

# Candidate construction (Design B §5).
PROPHETX = "prophetx"
NON_PROPHETX_BOOKS = ("pinnacle", "williamhill_us", "betmgm", "fanduel", "draftkings")
PRICE_BAND = (-350, -100)       # favorite ML band, inclusive
MIN_GAMES_PLAYED = 6            # GP gate, both teams
PAYOUT_HAIRCUT = 0.98          # to-win = 0.98 * stake * 100/|ml| (exchange fee)

# nba_api and The Odds API disagree on a few franchise names. Canonicalize team
# names to the Odds-API form (used for candidates/odds) so stat lookups match.
TEAM_NAME_ALIASES = {
    "LA Clippers": "Los Angeles Clippers",
}


def canonical_team(name: str) -> str:
    return TEAM_NAME_ALIASES.get(name, name)

# Book layer books.
RETAIL_BOOKS = ("williamhill_us", "betmgm", "fanduel", "draftkings")
PINNACLE = "pinnacle"

# Book-move detection. PROVISIONAL (91.1%, floored + documented). Live alerts
# run this on UNROUNDED Odds API data at full fidelity; backtest parity is not a
# goal (the 44 unreproducible ground-truth vetoes are logged in
# docs/book_veto_audit.md and may reflect an original quirk not worth copying).
#
# The rule (confirmed spec): for each game take the
# chronological consensus home-team spread, computed two ways -- (a) the median
# across the four retail books, (b) Pinnacle alone. Measure displacement from
# the first snapshot in the "toward the dog" direction (home favorite -> home
# line moving up / less negative; away favorite -> home line moving down). Veto
# if EITHER series shows a displacement >= 1.0 toward the dog sustained across 2+
# consecutive snapshots (not a one-snapshot blip that reverts).
#
# This is verified as the best possible directional reproduction of the backtest
# (91.1% of candidates). The direction is decisively correct: toward-dog 91.1%
# vs favorite-only 38%, bidirectional 62%, unsigned home-line 62-67%, wider
# snapshot windows worse. The ~9% residual is 44 games the backtest tagged
# `book` that have NO >=1 toward-dog move in the pre-entry retail/Pinnacle series
# (24 have zero toward-dog movement; several move toward the favorite) -- not
# reproducible by any directional rule from the on-disk 22-snapshot grid, and
# likely reflects a wider opening-line anchor / extra signal in the original book
# code. See tests/test_veto_layers for the full reconciliation.
BOOK_MOVE_POINTS = 1.0          # displacement toward the dog, in points
BOOK_SUSTAIN_SNAPSHOTS = 2      # held this many consecutive snapshots
BOOK_MIN_AGREEMENT = 0.90       # reproduction floor asserted by the unit test


# --- median-split helpers ----------------------------------------------------
# "median split across all 30 teams": compute the median of the field over the
# slate's 30 teams; a team is below/above median by STRICT comparison. Bottom-
# half == below median; top-half == above median.
#
# Strict (not <=/>=) is verified correct against the backtest: the only stat-layer
# disagreements are teams whose *rounded* stat value lands exactly on the median.
# The backtest used full-precision nba values, where those teams fall just off the
# median and strict comparison reproduces its decision. See tests/test_veto_layers.

def _median(team_stats: dict, field: str) -> float:
    return statistics.median(t[field] for t in team_stats.values())


def below_median(team_stats: dict, team: str, field: str) -> bool:
    return team_stats[team][field] < _median(team_stats, field)


def above_median(team_stats: dict, team: str, field: str) -> bool:
    return team_stats[team][field] > _median(team_stats, field)


# --- the four veto-layer predicates ------------------------------------------

def veto_grinder(cand: Candidate, ctx: VetoContext) -> VetoResult:
    ts, dog = ctx.team_stats, cand.dog
    fired = (
        below_median(ts, dog, "PACE")
        and below_median(ts, dog, "PCT_FGA_3PT")
        and above_median(ts, dog, "DREB_PCT")
    )
    return VetoResult(fired, "grinder")


def veto_pace_tov(cand: Candidate, ctx: VetoContext) -> VetoResult:
    ts = ctx.team_stats
    fired = (
        below_median(ts, cand.dog, "TM_TOV_PCT")
        and above_median(ts, cand.favorite, "PACE")
    )
    return VetoResult(fired, "pace/TOV")


def veto_ft_def(cand: Candidate, ctx: VetoContext) -> VetoResult:
    ts, dog = ctx.team_stats, cand.dog
    fired = (
        above_median(ts, dog, "PCT_PTS_FT")
        and below_median(ts, dog, "DEF_RATING")
    )
    return VetoResult(fired, "FT/def")


def _book_trajectory(snapshots: list, books: tuple, home: str) -> list[float]:
    """Home-team spread over time for a set of books, median across the books
    at each snapshot time. Returns one value per snapshot time, time-ordered.
    """
    by_time: dict = {}
    for s in snapshots:
        if s.book in books and s.home_point is not None:
            by_time.setdefault(s.taken_at, []).append(s.home_point)
    out = []
    for t in sorted(by_time):
        out.append(statistics.median(by_time[t]))
    return out


def _sustained_move_toward_dog(traj: list[float], fav_is_home: bool) -> bool:
    """True if the favorite's spread displaces >=1 pt toward the dog from the
    first snapshot and holds for 2+ consecutive snapshots.

    fav_point = home_point if favorite is home else -home_point (negative: the
    favorite lays points). Moving "toward the dog" == fav_point rising (laying
    fewer). Displacement from the first snapshot >= BOOK_MOVE_POINTS, sustained.
    """
    if len(traj) < BOOK_SUSTAIN_SNAPSHOTS:
        return False
    sign = 1.0 if fav_is_home else -1.0
    fav = [sign * h for h in traj]
    first = fav[0]
    streak = 0
    for v in fav[1:]:
        if v - first >= BOOK_MOVE_POINTS:
            streak += 1
            if streak >= BOOK_SUSTAIN_SNAPSHOTS:
                return True
        else:
            streak = 0
    return False


def veto_book(cand: Candidate, ctx: VetoContext) -> VetoResult:
    home = cand.game.home
    fav_is_home = cand.favorite == home
    retail = _book_trajectory(ctx.snapshots, RETAIL_BOOKS, home)
    pinny = _book_trajectory(ctx.snapshots, (PINNACLE,), home)
    fired = (
        _sustained_move_toward_dog(retail, fav_is_home)
        or _sustained_move_toward_dog(pinny, fav_is_home)
    )
    return VetoResult(fired, "book")


# --- team-stat view builder --------------------------------------------------

def team_stats_view(stat_rows) -> dict:
    """Assemble a flat team_name -> {field: value} view from tall StatRows."""
    view: dict = {}
    for r in stat_rows:
        if r.field in ALL_FIELDS:
            view.setdefault(canonical_team(r.team), {})[r.field] = r.value
    return view


# --- candidate construction (pure, unit-tested) ------------------------------

def build_candidate(game: Game, snapshots: list, team_stats: dict) -> Optional[Candidate]:
    """Favorite-ML candidate at entry time, or None.

    Favorite = negative-consensus side (median home spread across NON-ProphetX
    books at the earliest snapshot). Entry ML = ProphetX favorite ML at the
    latest (eval-time) snapshot. Gates: price band -350..-100, both teams >=6 GP.
    Liquidity = ProphetX bet limit on the favorite side. Returns None if any gate
    fails or the needed prices are missing.
    """
    if not snapshots:
        return None
    times = sorted({s.taken_at for s in snapshots})
    first, last = times[0], times[-1]

    # 1) consensus favorite from the earliest snapshot's non-ProphetX spreads.
    first_points = [s.home_point for s in snapshots
                    if s.taken_at == first and s.book in NON_PROPHETX_BOOKS
                    and s.home_point is not None]
    if not first_points:
        return None
    consensus_home = statistics.median(first_points)
    if consensus_home < 0:
        favorite, dog = game.home, game.away
    elif consensus_home > 0:
        favorite, dog = game.away, game.home
    else:
        return None  # pick'em, no favorite

    # 2) ProphetX price + liquidity at the eval-time (latest) snapshot.
    px = next((s for s in snapshots if s.taken_at == last and s.book == PROPHETX), None)
    if px is None:
        return None
    if favorite == game.home:
        entry_ml, liquidity = px.home_ml, px.home_limit
    else:
        entry_ml, liquidity = px.away_ml, px.away_limit
    if entry_ml is None:
        return None

    # 3) gates: price band + games played.
    lo, hi = PRICE_BAND
    if not (lo <= entry_ml <= hi):
        return None
    for team in (favorite, dog):
        gp = team_stats.get(team, {}).get("GP")
        if gp is None or gp < MIN_GAMES_PLAYED:
            return None

    return Candidate(sport="nba", game=game, favorite=favorite, dog=dog,
                     entry_ml=int(entry_ml), liquidity=liquidity,
                     entry_time_actual=last)


# --- the Sport implementation ------------------------------------------------

class NBA:
    """NBA sport plug-in. Data adapters (Odds API, nba_api) are wired in a later
    build step; the veto layers below are complete and unit-tested against the
    backtest's keep/veto decisions.
    """

    key = "nba"
    entry_offset_minutes = 115          # 1:55 before tip
    active_window_hours = (14, 24)      # ET hours games approach tip (bound runner minutes)

    def veto_layers(self) -> list[VetoLayer]:
        # Ordered; the engine vetoes if any fires.
        return [
            VetoLayer("book", veto_book),
            VetoLayer("grinder", veto_grinder),
            VetoLayer("pace/TOV", veto_pace_tov),
            VetoLayer("FT/def", veto_ft_def),
        ]

    def build_candidates(self, game, snapshots, stats) -> Optional[Candidate]:
        view = stats if isinstance(stats, dict) else team_stats_view(stats)
        return build_candidate(game, snapshots, view)

    # --- data adapters -------------------------------------------------------

    def todays_games(self, date) -> list[Game]:
        return _games_from_events(_odds_api_get("events"))

    def pull_odds_snapshot(self) -> list[SnapshotRow]:
        """Current odds for today's slate -> raw per-book SnapshotRows."""
        taken_at = datetime.now(timezone.utc).replace(microsecond=0)
        return _snapshots_from_odds(_odds_api_get("odds", markets="spreads,h2h"),
                                    taken_at)

    def pull_stats(self, asof_date) -> list[StatRow]:
        """Point-in-time team stats as of `asof_date` -> tall StatRows.

        Uses nba_api LeagueDashTeamStats (Advanced + Scoring) with a DateTo
        cutoff so the pull is as-of the date with no lookahead. nba_api is
        imported lazily so this module imports without it (tests don't need it).
        """
        return _pull_nba_stats(str(asof_date))

    def settle(self, bet, final_score) -> Result:
        """favorite wins if its final score is higher; P&L per the stake table."""
        fav_pts = final_score[bet.favorite]
        dog_pts = final_score[bet.dog]
        win = fav_pts > dog_pts
        if win:
            net = PAYOUT_HAIRCUT * bet.stake_chosen * 100.0 / abs(bet.entry_ml)
        else:
            net = -bet.stake_chosen
        return Result(win=win, net_pnl=round(net, 2))


# --- Odds API + nba_api plumbing ---------------------------------------------

_ODDS_BASE = "https://api.the-odds-api.com/v4/sports/basketball_nba"
_BOOKMAKERS = "pinnacle,prophetx,williamhill_us,betmgm,fanduel,draftkings"


def _odds_api_get(path: str, **extra) -> list:
    import requests
    key = os.environ.get("ODDSAPIKEY")
    if not key:
        raise RuntimeError("ODDSAPIKEY must be set in the environment.")
    params = {"apiKey": key, "oddsFormat": "american",
              "bookmakers": _BOOKMAKERS, "includeBetLimits": "true", **extra}
    resp = requests.get(f"{_ODDS_BASE}/{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _et_date(commence_iso: str) -> str:
    dt = datetime.fromisoformat(commence_iso.replace("Z", "+00:00"))
    return dt.astimezone(ET).date().isoformat()


def _games_from_events(events: list) -> list[Game]:
    games = []
    for e in events:
        commence = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
        games.append(Game(sport="nba", game_id=e["id"],
                          game_date=_et_date(e["commence_time"]),
                          commence_time=commence, home=e["home_team"], away=e["away_team"]))
    return games


def _snapshots_from_odds(events: list, taken_at: datetime) -> list[SnapshotRow]:
    rows: list[SnapshotRow] = []
    for e in events:
        home, away = e["home_team"], e["away_team"]
        game_date = _et_date(e["commence_time"])
        for bk in e.get("bookmakers", []):
            hp = home_ml = away_ml = home_lim = away_lim = None
            for m in bk.get("markets", []):
                for o in m.get("outcomes", []):
                    is_home = o["name"] == home
                    if m["key"] == "spreads" and is_home:
                        hp = o.get("point")
                    elif m["key"] == "h2h":
                        if is_home:
                            home_ml, home_lim = o.get("price"), o.get("bet_limit")
                        elif o["name"] == away:
                            away_ml, away_lim = o.get("price"), o.get("bet_limit")
            rows.append(SnapshotRow(sport="nba", game_date=game_date, game_id=e["id"],
                                    taken_at=taken_at, book=bk["key"], home_point=hp,
                                    home_ml=home_ml, away_ml=away_ml,
                                    home_limit=home_lim, away_limit=away_lim))
    return rows


def _season_for(asof_date: str) -> str:
    """NBA season string, e.g. '2025-26' for a date in the 2025-26 season."""
    y, m, _ = (int(x) for x in asof_date.split("-"))
    start = y if m >= 7 else y - 1
    return f"{start}-{str(start + 1)[-2:]}"


def _pull_nba_stats(asof_date: str) -> list[StatRow]:
    from nba_api.stats.endpoints import leaguedashteamstats
    season = _season_for(asof_date)
    common = dict(season=season, season_type_all_star="Regular Season",
                  per_mode_detailed="PerGame", date_to_nullable=asof_date)
    want = {"advanced": ["PACE", "DREB_PCT", "TM_TOV_PCT", "DEF_RATING", "GP"],
            "scoring": ["PCT_FGA_3PT", "PCT_PTS_FT"]}
    measure = {"advanced": "Advanced", "scoring": "Scoring"}
    out: list[StatRow] = []
    for group, fields in want.items():
        res = leaguedashteamstats.LeagueDashTeamStats(
            measure_type_detailed_defense=measure[group], **common)
        data = res.get_normalized_dict()["LeagueDashTeamStats"]
        for team in data:
            name = canonical_team(team["TEAM_NAME"])
            for f in fields:
                out.append(StatRow(sport="nba", asof_date=asof_date, team=name,
                                   group=group, field=f, value=float(team[f])))
    return out
