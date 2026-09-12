"""College-football plug-in (the second sport).

Mirrors sports/nba.py in shape and discipline: pure, unit-testable predicates
over (favorite, dog, point-in-time team stats, this game's odds snapshots), with
the IO adapters kept at the bottom. Everything downstream of a surviving
candidate — the stake ladder, three-stage sizing, alert compose/send, the
bets-logging round trip, the selection page — is the sport-agnostic engine and
is reused UNCHANGED.

Strategy: bet the favorite moneyline on ProphetX when all of these hold.

  Universe
    - FBS vs FBS. Membership comes from CFBD /teams/fbs?year=YYYY at RUNTIME,
      never a hardcoded list, so reclassifiers (North Dakota State, Sacramento
      State in 2026) are picked up and departures drop automatically. A
      hardcoded list caused silent drops last season.
    - Favorite ML in [-300, -100] (nothing heavier than -300).
    - Week 3+. Week 5+ is the validated range, but alerts fire from Week 3 with
      an explicit data-maturity flag rather than being gated off (see
      `maturity`): the alert tells the truth about its own data.

  Three conditions — the favorite must pass ALL THREE
    1. stuff        — VETO if the dog has a top-quartile defensive stuff rate.
    2. retail-extend— VETO if >=2 of the 4 retail books each show a sustained
                      >=1pt move toward the favorite from that book's opening
                      line, in the window from listing to T-48h before kickoff,
                      AND Pinnacle shows no >=1pt move toward the favorite over
                      the same window.
    3. dog-SR       — VETO unless the dog's offensive success rate EXCEEDS the
                      favorite's.

Stats are CFBD as-of, garbage-time-EXCLUDED, through the PRIOR week
(endWeek = N-1 for a week-N game). `assert_no_lookahead` enforces that; season-
final or unbounded stats are never used.

Name resolution is exact-match only, from the generated crosswalk
(sports/data/cfb_crosswalk.json, see tools/gen_cfb_crosswalk.py). There is no
fuzzy matching: an unresolvable name raises UnmappedTeamError and surfaces
loudly rather than silently dropping a game.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from .base import (
    Candidate, Game, Result, SnapshotRow, StatRow, VetoContext, VetoLayer, VetoResult,
)

ET = ZoneInfo("America/New_York")

# --- strategy constants ------------------------------------------------------

PROPHETX = "prophetx"
RETAIL_BOOKS = ("williamhill_us", "betmgm", "fanduel", "draftkings")
PINNACLE = "pinnacle"
NON_PROPHETX_BOOKS = (PINNACLE,) + RETAIL_BOOKS

# Favorite ML band, inclusive. ML >= -300 -> exclude favorites heavier than -300.
PRICE_BAND = (-300, -100)

# ProphetX charges 2% commission on NET WINNINGS (confirmed via their help
# centre). Third-party sources quoting 1% are stale — do not "correct" this down.
PAYOUT_HAIRCUT = 0.98

# Alerts fire from this week; the strategy is validated from RELIABLE_WEEK.
ALERT_FROM_WEEK = 3
RELIABLE_WEEK = 5
# Below this many games of as-of data the stats conditions are noise-regime.
NOISE_REGIME_MAX_GAMES = 4

# Retail-extend veto.
RETAIL_EXTEND_CUTOFF_HOURS = 48   # window ends T-48h before THIS game's kickoff
RETAIL_EXTEND_MIN_BOOKS = 2       # >=2 of the 4 retail books must have moved
MOVE_POINTS = 1.0                 # >=1pt displacement toward the favorite
SUSTAIN_SNAPSHOTS = 2             # held across 2 consecutive hourly snapshots

# Stuff veto: "top quartile" == at or above the 75th percentile of the as-of FBS
# population.
STUFF_QUANTILE = 0.75

# CFBD stat fields, stored tall in the `stats` table under this group.
STAT_GROUP = "advanced"
F_STUFF = "STUFF_RATE_DEF"      # defense.stuffRate
F_SR = "SUCCESS_RATE_OFF"       # offense.successRate
F_GAMES = "GAMES"               # completed games behind the as-of stats
F_ASOF_WEEK = "ASOF_WEEK"       # the endWeek the row was pulled through

# CFBD's per-endpoint sentinel aggregate row. It is NOT a team and must never
# enter a quartile population.
CFBD_SENTINEL_TEAMS = frozenset({"nationalaverages"})


class UnmappedTeamError(RuntimeError):
    """A team name that the exact-match crosswalk cannot resolve.

    Raised, never swallowed: an unmapped FBS team is a data-quality failure that
    must be seen and fixed in the crosswalk, not a game that quietly disappears.
    """


# --- crosswalk (generated; exact match only) ---------------------------------

_CROSSWALK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "data", "cfb_crosswalk.json")
_crosswalk_cache: Optional[dict] = None


def crosswalk() -> dict:
    """The generated crosswalk payload (cached).

    Source of truth is odds-backtest-verification/src/{ncaaf,cfbd}-crosswalk.ts;
    this JSON is compiled from it by tools/gen_cfb_crosswalk.py and kept honest
    by tests/test_cfb.py.
    """
    global _crosswalk_cache
    if _crosswalk_cache is None:
        with open(_CROSSWALK_PATH, encoding="utf-8") as fh:
            _crosswalk_cache = json.load(fh)
    return _crosswalk_cache


def cfbd_to_canonical(name: str) -> str:
    """CFBD school name -> the canonical (Odds API) team string. Exact match."""
    table = crosswalk()["cfbd_to_canonical"]
    try:
        return table[name]
    except KeyError:
        raise UnmappedTeamError(
            f"CFBD team {name!r} is not in the CFBD->canonical crosswalk. "
            f"Exact match only — no fuzzy fallback. Add it to "
            f"odds-backtest-verification/src/cfbd-crosswalk.ts and re-run "
            f"tools/gen_cfb_crosswalk.py.") from None


def assert_known_odds_team(name: str) -> str:
    """Validate an Odds API team string against the crosswalk. Exact match."""
    if name not in crosswalk()["conference"]:
        raise UnmappedTeamError(
            f"Odds API team {name!r} is not in the NCAAF crosswalk. Exact match "
            f"only — no fuzzy fallback. Add it to "
            f"odds-backtest-verification/src/ncaaf-crosswalk.ts and re-run "
            f"tools/gen_cfb_crosswalk.py.")
    return name


# Board names seen this process that the crosswalk could not resolve. The tick
# prints each one once; the validator reports them as a hard failure.
UNRESOLVED_BOARD_NAMES: set = set()


def note_unresolved(name: str, reason: str) -> None:
    """Record + announce an unresolvable board name, once per process.

    Deliberately loud-but-not-fatal AT THE BOARD LEVEL. The NCAAF board lists
    FBS-vs-FCS games we do not bet, and FCS rosters churn every season, so
    raising here would let one unknown FCS opponent suppress alerts for every
    other game on the board — a far worse failure than the one it guards against.

    The hard errors live where they belong instead:
      - sports.cfb.CFB.fbs() RAISES if a CFBD *FBS* school cannot be mapped, so
        an unmapped FBS team can never silently shrink the universe. That is the
        authoritative membership check.
      - tools/validate_cfb.py FAILS on any unresolvable board name, and is the
        pre-flight gate to run before going live and after any crosswalk change.
    """
    if name in UNRESOLVED_BOARD_NAMES:
        return
    UNRESOLVED_BOARD_NAMES.add(name)
    print(f"[cfb] UNRESOLVED TEAM NAME {name!r} — {reason}. This game is being "
          f"skipped. If it is an FBS team, add it to the crosswalk and re-run "
          f"tools/gen_cfb_crosswalk.py; run `python -m tools.validate_cfb` for "
          f"the full list.", file=sys.stderr)


def find_unmapped(names: Iterable[str], *, kind: str = "odds") -> list[str]:
    """Names the crosswalk cannot resolve — for the pre-flight validator.

    Reports ALL of them at once (a one-at-a-time hard error would make fixing a
    board with several new names a slow loop).
    """
    table = (crosswalk()["conference"] if kind == "odds"
             else crosswalk()["cfbd_to_canonical"])
    return sorted({n for n in names if n not in table})


# --- CFBD calendar / week arithmetic (pure over a calendar payload) ----------
# The season's week boundaries come from CFBD /calendar?year=YYYY rather than
# hardcoded dates, so a shifted Week 0 or a 15-week season needs no code change.

def _cal_dt(entry: dict, *keys: str) -> datetime:
    """Read the first present datetime field. CFBD has renamed these across API
    versions (startDate/endDate vs firstGameStart/lastGameStart), so accept
    either and fail loudly rather than guessing a boundary."""
    for k in keys:
        v = entry.get(k)
        if v:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    raise RuntimeError(
        f"CFBD calendar entry has none of {keys!r}; cannot determine week "
        f"boundaries. Entry: {entry!r}")


def week_bounds(calendar: list[dict]) -> dict:
    """week number -> (start, end) as tz-aware UTC datetimes. Regular season."""
    out: dict = {}
    for e in calendar:
        if str(e.get("seasonType", "regular")) != "regular":
            continue
        wk = int(e["week"])
        out[wk] = (_cal_dt(e, "startDate", "firstGameStart"),
                   _cal_dt(e, "endDate", "lastGameStart"))
    if not out:
        raise RuntimeError("CFBD calendar contained no regular-season weeks.")
    return out


def week_of(kickoff: datetime, calendar: list[dict]) -> int:
    """The season week containing `kickoff`.

    Falls back to the nearest earlier week when a kickoff sits in a gap between
    published boundaries (CFBD's ranges do not always abut exactly).
    """
    bounds = week_bounds(calendar)
    for wk, (start, end) in sorted(bounds.items()):
        if start <= kickoff <= end:
            return wk
    earlier = [wk for wk, (start, _) in bounds.items() if start <= kickoff]
    if earlier:
        return max(earlier)
    return min(bounds)


def asof_week_end(week: int, calendar: list[dict]) -> Optional[date]:
    """Calendar date ending the as-of week for a week-`week` game: the end of
    week N-1. None when there is no prior week (week 1 / week 0)."""
    bounds = week_bounds(calendar)
    prior = week - 1
    if prior not in bounds:
        return None
    return bounds[prior][1].date()


def latest_completed_week(as_of: datetime, calendar: list[dict]) -> Optional[int]:
    """The last regular-season week fully ended at `as_of` — the endWeek a stats
    pull run at that moment may legitimately use."""
    bounds = week_bounds(calendar)
    done = [wk for wk, (_, end) in bounds.items() if end <= as_of]
    return max(done) if done else None


def assert_no_lookahead(game_week: int, endweek: int) -> None:
    """Hard guard on the ARITHMETIC: stats for a week-N game must be through
    week N-1 at the latest. Season-final or unbounded stats would leak the
    game's own result (and every later one) into the decision that precedes it.

    Raises — never warns, and no caller catches it.

    NOTE this alone is weak: `stats_asof_key` derives endWeek as `wk - 1` by
    construction, so passing that back here can never trip. The guard that
    actually bites on real data is `assert_stats_are_asof` below, which checks
    the endWeek the loaded rows were PULLED through. Both are kept: this one
    documents and enforces the rule for any caller computing endWeek
    independently (tools/validate_cfb.py does exactly that).
    """
    if endweek >= game_week:
        raise ValueError(
            f"LOOKAHEAD: week-{game_week} game would be evaluated on stats "
            f"through endWeek={endweek}. Stats must be as-of endWeek="
            f"{game_week - 1} or earlier.")


def assert_stats_are_asof(game_week: int, team_stats: dict) -> None:
    """Hard guard on the DATA: every loaded stat row must have been pulled
    through a week strictly EARLIER than the game's.

    This is the one that can actually fire. `pull_stats` stamps each row with the
    endWeek it was pulled through (F_ASOF_WEEK), so this catches the failures the
    arithmetic check cannot see: a mis-filed as-of key, a backfill run with the
    wrong endWeek, a season-final pull written over the weekly rows, or a
    calendar shift that silently moved a week boundary.

    It checks EVERY team in the view, not just the two playing. A single
    contaminated row would shift the stuff-rate quartile threshold and therefore
    change the verdict on games that row is not even part of.

    Raises ValueError. Nothing catches it: a lookahead-contaminated evaluation
    must stop the tick, not quietly emit an alert built on a leaked result.
    """
    offenders = sorted(
        (team, int(v[F_ASOF_WEEK]))
        for team, v in team_stats.items()
        if F_ASOF_WEEK in v and int(v[F_ASOF_WEEK]) >= game_week
    )
    if offenders:
        shown = ", ".join(f"{t} (endWeek={w})" for t, w in offenders[:5])
        more = f" and {len(offenders) - 5} more" if len(offenders) > 5 else ""
        raise ValueError(
            f"LOOKAHEAD: {len(offenders)} stat row(s) for a week-{game_week} "
            f"game were pulled through week {game_week} or later: {shown}{more}. "
            f"Stats must be as-of endWeek={game_week - 1} or earlier. Refusing "
            f"to evaluate on leaked data.")


# --- data-maturity flag (pure) ----------------------------------------------
# Required on EVERY CFB alert. Alerts from Week 3 are fired, not suppressed —
# but they must never overstate themselves, so each one carries its own as-of
# week, how many games of data sit behind the stats, whether the stats
# conditions are in the noise regime, and whether the retail-extend window
# actually closed. Annotation only; it never gates.

def stats_confidence(games: Optional[int], game_week: int) -> str:
    if games is None:
        return "unknown (no games count)"
    if games < NOISE_REGIME_MAX_GAMES or game_week < RELIABLE_WEEK:
        return "noise-regime"
    return "reliable"


def retail_extend_status(snapshots: list, kickoff: datetime,
                         now: datetime) -> tuple[str, str]:
    """('complete'|'partial', why). Complete only when the T-48h window has
    actually closed AND we accumulated enough of it to judge a sustained move."""
    cutoff = kickoff - timedelta(hours=RETAIL_EXTEND_CUTOFF_HOURS)
    in_window = sorted({s.taken_at for s in snapshots if s.taken_at <= cutoff})
    if now < cutoff:
        return "partial", "T-48h window still open at fire time"
    if len(in_window) < SUSTAIN_SNAPSHOTS:
        return "partial", (f"only {len(in_window)} snapshot(s) accumulated "
                           f"before T-48h")
    return "complete", f"{len(in_window)} snapshots through T-48h"


def maturity(cand_week: int, team_stats: dict, favorite: str, dog: str,
             snapshots: list, kickoff: datetime, now: datetime) -> tuple:
    """The (label, value) annotation pairs carried on every CFB candidate."""
    asof_week = cand_week - 1
    counts = [team_stats.get(t, {}).get(F_GAMES) for t in (favorite, dog)]
    known = [int(c) for c in counts if c is not None]
    games = min(known) if known else None
    games_txt = (f"{games} game{'s' if games != 1 else ''} of data"
                 if games is not None else "games of data unknown")
    status, why = retail_extend_status(snapshots, kickoff, now)
    return (
        ("Data maturity", f"as-of through Week {asof_week}: {games_txt}"),
        ("Stats confidence",
         f"{stats_confidence(games, cand_week)} "
         f"(validated from Week {RELIABLE_WEEK}+)"),
        ("Retail-extend", f"{status} — {why}"),
    )


# --- quartile helpers over the as-of FBS population --------------------------

def _population(team_stats: dict, field: str) -> list[float]:
    """Every FBS team's value for `field`. The stats pull already restricts rows
    to the runtime FBS membership and drops CFBD's nationalAverages sentinel, so
    this view IS the population; the sentinel guard below is belt-and-braces."""
    return [v[field] for t, v in team_stats.items()
            if field in v and t.strip().lower() not in CFBD_SENTINEL_TEAMS]


def quantile(values: list[float], q: float) -> float:
    """The q-quantile by linear interpolation. statistics.quantiles is unusable
    here: it needs n>=2 and only yields fixed cut points."""
    if not values:
        raise ValueError("empty population")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _stat(team_stats: dict, team: str, field: str) -> Optional[float]:
    v = team_stats.get(team, {}).get(field)
    return None if v is None else float(v)


# --- the three veto predicates (pure, unit-tested) ---------------------------

def veto_stuff(cand: Candidate, ctx: VetoContext) -> VetoResult:
    """VETO if the underdog's defensive stuff rate is top-quartile across the
    as-of FBS population. A dog that stuffs runs blows up the favorite's
    short-yardage and clock game — the favorite-ML edge does not survive it.

    Missing stats do NOT fire the veto: a missing value is an absence of
    evidence, and the maturity flag already tells the reader the data is thin.
    """
    pop = _population(ctx.team_stats, F_STUFF)
    dog_stuff = _stat(ctx.team_stats, cand.dog, F_STUFF)
    if dog_stuff is None or not pop:
        return VetoResult(False, "stuff")
    return VetoResult(dog_stuff >= quantile(pop, STUFF_QUANTILE), "stuff")


def _book_series(snapshots: list, book: str, cutoff: datetime) -> list[float]:
    """One book's home-team spread over time, up to and including `cutoff`."""
    by_time = {s.taken_at: s.home_point for s in snapshots
               if s.book == book and s.home_point is not None
               and s.taken_at <= cutoff}
    return [by_time[t] for t in sorted(by_time)]


def _displacement_toward_favorite(series: list[float],
                                  fav_is_home: bool) -> list[float]:
    """Per-snapshot displacement from this book's OPENING line, in points,
    positive when the line has moved toward the favorite.

    fav_point = home_point if the favorite is home else -home_point, i.e. the
    (negative) number the favorite lays. Moving toward the favorite means laying
    MORE, so fav_point falls and `first - v` rises.
    """
    if not series:
        return []
    sign = 1.0 if fav_is_home else -1.0
    fav = [sign * h for h in series]
    first = fav[0]
    return [first - v for v in fav]


def _sustained_move_toward_favorite(series: list[float], fav_is_home: bool) -> bool:
    """>=1pt toward the favorite off this book's opener, held across
    SUSTAIN_SNAPSHOTS consecutive snapshots (not a one-poll blip that reverts)."""
    disp = _displacement_toward_favorite(series, fav_is_home)
    streak = 0
    for d in disp[1:]:
        if d >= MOVE_POINTS:
            streak += 1
            if streak >= SUSTAIN_SNAPSHOTS:
                return True
        else:
            streak = 0
    return False


def _any_move_toward_favorite(series: list[float], fav_is_home: bool) -> bool:
    """>=1pt toward the favorite at ANY snapshot in the window.

    Deliberately NOT sustained-qualified: the spec defines "sustained" for the
    retail books only, and states the Pinnacle leg as a plain >=1pt move. Read
    literally, which is also the conservative direction for this veto — the
    looser Pinnacle test makes it EASIER for Pinnacle to confirm the move, and a
    confirmed move suppresses the veto rather than firing it.
    """
    return any(d >= MOVE_POINTS
               for d in _displacement_toward_favorite(series, fav_is_home)[1:])


def veto_retail_extend(cand: Candidate, ctx: VetoContext) -> VetoResult:
    """VETO on retail steam the sharp book does not confirm.

    Window: from the game's listing to T-48h before THIS game's kickoff. It is
    kickoff-relative, so a Saturday noon game and a Wednesday-night MACtion game
    are handled by identical logic with no special-casing.

    Fires when >=RETAIL_EXTEND_MIN_BOOKS of the 4 retail books each show a
    sustained >=1pt move toward the favorite off their OWN opener, AND Pinnacle
    shows no >=1pt move toward the favorite over the same window. Retail
    extending a favorite that Pinnacle leaves alone is public money, not
    information, and the price is stale by kickoff.
    """
    fav_is_home = cand.favorite == cand.game.home
    cutoff = cand.game.commence_time - timedelta(hours=RETAIL_EXTEND_CUTOFF_HOURS)

    moved = sum(
        1 for bk in RETAIL_BOOKS
        if _sustained_move_toward_favorite(
            _book_series(ctx.snapshots, bk, cutoff), fav_is_home))
    pinny_moved = _any_move_toward_favorite(
        _book_series(ctx.snapshots, PINNACLE, cutoff), fav_is_home)

    fired = moved >= RETAIL_EXTEND_MIN_BOOKS and not pinny_moved
    return VetoResult(fired, "retail-extend")


def veto_dog_sr(cand: Candidate, ctx: VetoContext) -> VetoResult:
    """VETO unless the underdog's offensive success rate EXCEEDS the favorite's.

    The edge lives in favorites opposed by dogs who move the chains but cannot
    finish; a dog who is simply worse down-to-down is priced right. Strict > —
    equal success rates do not clear the filter.

    Missing stats do NOT fire (see veto_stuff).
    """
    dog_sr = _stat(ctx.team_stats, cand.dog, F_SR)
    fav_sr = _stat(ctx.team_stats, cand.favorite, F_SR)
    if dog_sr is None or fav_sr is None:
        return VetoResult(False, "dog-SR")
    return VetoResult(not (dog_sr > fav_sr), "dog-SR")


# --- team-stat view builder --------------------------------------------------

def team_stats_view(stat_rows) -> dict:
    """Tall StatRows -> team_name -> {field: value}, sentinel row dropped."""
    view: dict = {}
    for r in stat_rows:
        if r.team.strip().lower() in CFBD_SENTINEL_TEAMS:
            continue
        view.setdefault(r.team, {})[r.field] = r.value
    return view


# --- candidate construction (pure, unit-tested) ------------------------------

def build_candidate(game: Game, snapshots: list, team_stats: dict, *,
                    fbs: set, game_week: int,
                    now: Optional[datetime] = None) -> Optional[Candidate]:
    """A favorite-ML candidate at entry time, or None if a universe filter fails.

    Universe: FBS vs FBS (runtime membership), week >= ALERT_FROM_WEEK, and a
    ProphetX favorite ML inside PRICE_BAND. The favorite is the negative-
    consensus side across the non-ProphetX books at the EARLIEST snapshot, so
    the designation is fixed at listing and cannot flip on late movement.

    None here means "not a candidate" (a real, expected outcome). It never means
    "name did not resolve" — that raises UnmappedTeamError.
    """
    if not snapshots:
        return None

    # Universe: FBS vs FBS. Unresolvable names are announced (never dropped
    # quietly) and the game is skipped — see note_unresolved for why this is a
    # log rather than a raise at the board level.
    for team in (game.home, game.away):
        if team not in crosswalk()["conference"]:
            note_unresolved(team, "not in the NCAAF crosswalk (exact match only)")
            return None
    if game.home not in fbs or game.away not in fbs:
        return None
    if game_week < ALERT_FROM_WEEK:
        return None

    # The loaded stats must predate this game. Checked here, on the rows that
    # will actually feed the vetoes, rather than only on the arithmetic.
    assert_stats_are_asof(game_week, team_stats)

    times = sorted({s.taken_at for s in snapshots})
    first, last = times[0], times[-1]
    # The evaluation instant is the latest snapshot — the tick stamps its rows
    # with its own `now`, so `last` IS this tick's clock. Anchoring the maturity
    # flag to it (rather than to a fresh wall-clock read) keeps the annotation
    # describing exactly the data it was computed from.
    now = now or last

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

    px = next((s for s in snapshots if s.taken_at == last and s.book == PROPHETX),
              None)
    if px is None:
        return None
    if favorite == game.home:
        entry_ml, liquidity = px.home_ml, px.home_limit
    else:
        entry_ml, liquidity = px.away_ml, px.away_limit
    if entry_ml is None:
        return None

    lo, hi = PRICE_BAND
    if not (lo <= entry_ml <= hi):
        return None

    return Candidate(
        sport="cfb", game=game, favorite=favorite, dog=dog,
        entry_ml=int(entry_ml), liquidity=liquidity, entry_time_actual=last,
        annotations=maturity(game_week, team_stats, favorite, dog, snapshots,
                             game.commence_time, now),
    )


# --- the Sport implementation ------------------------------------------------

class CFB:
    """College-football sport plug-in.

    Two jobs ride the existing tick, exactly as the NBA plug-in does:

      accumulate — every tick, all week. One board-wide /odds pull (~2 credits)
                   writes every listed game's per-book spread snapshot to
                   Supabase. This IS the movement trajectory the retail-extend
                   veto later reads; there is no separate collector.
      fire       — per game, keyed off kickoff. When a game reaches
                   T-`entry_offset_minutes`, the engine rebuilds the candidate
                   from the accumulated trajectory + CFBD as-of stats, runs the
                   three conditions, and hands a survivor to the existing alert
                   path untouched.

    Both fall out of the engine's self-healing rolling tick; nothing here
    re-implements it.
    """

    key = "cfb"
    # Fire at T-24h (configurable via settings [cfb] entry_offset_minutes). A day
    # out the T-48h retail-extend window has closed, so the veto reads a full
    # trajectory and the maturity flag normally reports "complete"; "partial"
    # then means a genuinely short window (listed inside T-48h, or accumulation
    # started late) rather than a routine early fire.
    entry_offset_minutes = 24 * 60
    # Games run from noon to past midnight ET across Saturdays and weeknights,
    # and the accumulate job wants every tick regardless, so the window is open.
    active_window_hours = (0, 24)

    def __init__(self, *, entry_offset_minutes: Optional[int] = None,
                 year: Optional[int] = None):
        if entry_offset_minutes is not None:
            self.entry_offset_minutes = int(entry_offset_minutes)
        self._year = year
        self._odds_cache: Optional[list] = None
        self._fbs_cache: Optional[set] = None
        self._calendar_cache: Optional[list] = None

    # --- season plumbing -----------------------------------------------------

    def year(self) -> int:
        """Season year. A January bowl/playoff date belongs to the prior season."""
        if self._year is not None:
            return self._year
        today = datetime.now(ET).date()
        return today.year if today.month >= 7 else today.year - 1

    def calendar(self) -> list:
        if self._calendar_cache is None:
            self._calendar_cache = _cfbd_get("calendar", year=self.year())
        return self._calendar_cache

    def fbs(self) -> set:
        """Runtime FBS membership, as canonical (Odds API) names.

        From CFBD /teams/fbs?year=YYYY on every run — never hardcoded. Each CFBD
        school must resolve through the crosswalk; one that does not raises
        UnmappedTeamError rather than silently shrinking the universe.
        """
        if self._fbs_cache is None:
            teams = _cfbd_get("teams/fbs", year=self.year())
            self._fbs_cache = {cfbd_to_canonical(t["school"]) for t in teams}
        return self._fbs_cache

    # --- Sport contract ------------------------------------------------------

    def todays_games(self, date) -> list[Game]:
        """Every listed NCAAF game on the board (not just today's).

        Accumulation runs all week and firing is T-24h, so the tick must see the
        whole board. Shares ONE cached /odds payload with pull_odds_snapshot so a
        tick costs ~2 credits, not 4.
        """
        return _games_from_events(self._board())

    def pull_odds_snapshot(self, taken_at=None) -> list[SnapshotRow]:
        taken_at = (taken_at or datetime.now(timezone.utc)).replace(microsecond=0)
        return _snapshots_from_odds(self._board(), taken_at)

    def _board(self) -> list:
        if self._odds_cache is None:
            self._odds_cache = _odds_api_get("odds", markets="spreads,h2h")
        return self._odds_cache

    def pull_stats(self, asof_date) -> list[StatRow]:
        """CFBD as-of team stats through the latest COMPLETED week at `asof_date`.

        Garbage time excluded, endWeek-bounded (never season-final), restricted
        to runtime FBS membership, and keyed in the `stats` table by the calendar
        date that week ended — the same key `stats_asof_key` derives for a game
        in the following week.
        """
        asof = _as_utc(asof_date)
        # The as-of date names a day; the week ending that evening counts.
        asof_end = asof.replace(hour=23, minute=59, second=59)

        cal = self.calendar()
        endweek = latest_completed_week(asof_end, cal)
        if endweek is None:
            return []
        key = week_bounds(cal)[endweek][1].date().isoformat()

        rows = _cfbd_get("stats/season/advanced", year=self.year(),
                         endWeek=endweek, excludeGarbageTime="true")
        games = _cfbd_games_played(self.year(), endweek)
        fbs = self.fbs()

        out: list[StatRow] = []
        for r in rows:
            name = r.get("team")
            if name is None or str(name).strip().lower() in CFBD_SENTINEL_TEAMS:
                continue
            canonical = cfbd_to_canonical(name)
            if canonical not in fbs:
                continue
            values = {
                F_STUFF: _dig(r, "defense", "stuffRate"),
                F_SR: _dig(r, "offense", "successRate"),
                F_GAMES: games.get(canonical),
                F_ASOF_WEEK: float(endweek),
            }
            for fieldname, value in values.items():
                if value is None:
                    continue
                out.append(StatRow(sport="cfb", asof_date=key, team=canonical,
                                   group=STAT_GROUP, field=fieldname,
                                   value=float(value)))
        return out

    def stats_asof_key(self, game: Game) -> str:
        """Stats for a week-N game are as of the END of week N-1."""
        wk = week_of(game.commence_time, self.calendar())
        assert_no_lookahead(wk, wk - 1)
        end = asof_week_end(wk, self.calendar())
        if end is None:
            # No prior week: no legitimate as-of stats exist. Return a key that
            # cannot match a row rather than silently falling back to a later one.
            return f"{self.year()}-01-01"
        return end.isoformat()

    stats_view = staticmethod(team_stats_view)

    def build_candidates(self, game, snapshots, stats) -> Optional[Candidate]:
        view = stats if isinstance(stats, dict) else team_stats_view(stats)
        wk = week_of(game.commence_time, self.calendar())
        assert_no_lookahead(wk, wk - 1)
        return build_candidate(game, snapshots, view, fbs=self.fbs(), game_week=wk)

    def veto_layers(self) -> list[VetoLayer]:
        # Ordered; the engine vetoes if any fires and records every one that did.
        return [
            VetoLayer("stuff", veto_stuff),
            VetoLayer("retail-extend", veto_retail_extend),
            VetoLayer("dog-SR", veto_dog_sr),
        ]

    # Lookback for the /scores fetch. Wider than NBA: a Saturday slate is still
    # being graded on Sunday and Monday, and weeknight games stretch the window
    # further, so 3 days keeps every kickoff in range.
    scores_days_from = 3

    def fetch_scores(self, *, days_from: int = 3) -> dict:
        """Completed final scores from The Odds API /scores (ncaaf)."""
        from engine.settle import scores_from_events
        return scores_from_events(
            _odds_api_get("scores", daysFrom=days_from), _et_date)

    def settle(self, bet, final_score) -> Result:
        """The favorite wins outright if it scores more. Net of 2% commission."""
        win = final_score[bet.favorite] > final_score[bet.dog]
        net = (PAYOUT_HAIRCUT * bet.stake_chosen * 100.0 / abs(bet.entry_ml)
               if win else -bet.stake_chosen)
        return Result(win=win, net_pnl=round(net, 2))


# --- Odds API + CFBD plumbing ------------------------------------------------

_ODDS_BASE = "https://api.the-odds-api.com/v4/sports/americanfootball_ncaaf"
_BOOKMAKERS = "pinnacle,prophetx,williamhill_us,betmgm,fanduel,draftkings"
_CFBD_BASE = "https://api.collegefootballdata.com"


def _as_utc(value) -> datetime:
    """A date / 'YYYY-MM-DD' / datetime -> a tz-aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        dt = datetime.fromisoformat(str(value))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _odds_api_get(path: str, **extra) -> list:
    """The Odds API. ProphetX's app now DISPLAYS percentages (CFTC mandate) but
    the API still returns American odds for oddsFormat=american — confirmed
    live, so no conversion is applied here."""
    import requests
    key = os.environ.get("ODDSAPIKEY")
    if not key:
        raise RuntimeError("ODDSAPIKEY must be set in the environment.")
    params = {"apiKey": key, "oddsFormat": "american",
              "bookmakers": _BOOKMAKERS, "includeBetLimits": "true", **extra}
    resp = requests.get(f"{_ODDS_BASE}/{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _cfbd_get(path: str, **params) -> list:
    import requests
    key = os.environ.get("CFBD_API_KEY")
    if not key:
        raise RuntimeError("CFBD_API_KEY must be set in the environment.")
    resp = requests.get(f"{_CFBD_BASE}/{path}", params=params,
                        headers={"Authorization": f"Bearer {key}",
                                 "Accept": "application/json"}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _dig(row: dict, *path):
    cur = row
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _cfbd_games_played(year: int, endweek: int) -> dict:
    """Canonical team name -> completed games through `endweek`.

    Feeds the maturity flag's "N games of data". Counted from the schedule
    rather than assumed equal to the week number, so byes are handled. Returns
    {} if the schedule cannot be read — the flag then says the count is unknown
    instead of asserting a number it cannot support.
    """
    try:
        games = _cfbd_get("games", year=year, seasonType="regular")
    except Exception as exc:  # noqa: BLE001 — annotation input, never fatal
        print(f"[cfb] games-played count unavailable ({exc!r}); maturity flag "
              f"will report it as unknown.")
        return {}
    counts: dict = {}
    for g in games:
        if not g.get("completed"):
            continue
        week = g.get("week")
        if week is None or int(week) > endweek:
            continue
        for k in ("homeTeam", "home_team", "awayTeam", "away_team"):
            name = g.get(k)
            if not name:
                continue
            try:
                canonical = cfbd_to_canonical(name)
            except UnmappedTeamError:
                continue  # FCS opponents are expected and uninteresting here
            counts[canonical] = counts.get(canonical, 0) + 1
    return counts


def _et_date(commence_iso: str) -> str:
    dt = datetime.fromisoformat(commence_iso.replace("Z", "+00:00"))
    return dt.astimezone(ET).date().isoformat()


def _games_from_events(events: list) -> list[Game]:
    games = []
    for e in events:
        commence = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
        games.append(Game(sport="cfb", game_id=e["id"],
                          game_date=_et_date(e["commence_time"]),
                          commence_time=commence,
                          home=e["home_team"], away=e["away_team"]))
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
            rows.append(SnapshotRow(sport="cfb", game_date=game_date,
                                    game_id=e["id"], taken_at=taken_at,
                                    book=bk["key"], home_point=hp,
                                    home_ml=home_ml, away_ml=away_ml,
                                    home_limit=home_lim, away_limit=away_lim))
    return rows
