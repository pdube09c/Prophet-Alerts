"""The Sport plug-in contract and the small shared dataclasses (Design B §3).

The engine orchestrates without knowing the sport. Every sport implements the
abstract `Sport` interface; the engine speaks only in the shared dataclasses
below. NBA ships first (`sports/nba.py`); college football plugs in later
(`sports/cfb.py`) with zero engine rework.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional


# --- Shared dataclasses ------------------------------------------------------

@dataclass(frozen=True)
class Game:
    """A single fixture on a slate."""
    sport: str
    game_id: str
    game_date: str          # ET calendar date, 'YYYY-MM-DD'
    commence_time: datetime  # tip, timezone-aware (UTC)
    home: str
    away: str


@dataclass(frozen=True)
class SnapshotRow:
    """One book's odds for one game at one poll time -> a row appended to the DB.

    Raw per-book data (spread + h2h + limits). The favorite/dog and entry ML are
    derived later by `build_candidates`; the book veto reads `home_point`.
    """
    sport: str
    game_date: str
    game_id: str
    taken_at: datetime
    book: str
    home_point: Optional[float] = None   # spread from the home team's perspective
    home_ml: Optional[int] = None        # home moneyline (American)
    away_ml: Optional[int] = None        # away moneyline (American)
    home_limit: Optional[float] = None   # bet limit on home (liquidity)
    away_limit: Optional[float] = None   # bet limit on away (liquidity)


@dataclass(frozen=True)
class StatRow:
    """One point-in-time team-stat value -> a row in the DB (written once daily).

    Tall format matches the §7 `stats` schema: (sport, asof_date, team, group,
    field, value). The veto layers read a per-team view assembled from these.
    """
    sport: str
    asof_date: str
    team: str        # full team name, e.g. 'Boston Celtics'
    group: str       # 'advanced' | 'scoring' | ...
    field: str       # e.g. 'PACE'
    value: float


@dataclass(frozen=True)
class Candidate:
    """A favorite-ML candidate at entry time, before veto evaluation."""
    sport: str
    game: Game
    favorite: str            # full team name
    dog: str                 # full team name
    entry_ml: int            # ProphetX favorite moneyline at eval snapshot
    liquidity: Optional[float]
    entry_time_actual: datetime
    # Optional (label, value) pairs the alert renders verbatim beneath the
    # header — a sport may annotate a candidate with context the reader must
    # see. CFB uses it for the required data-maturity flag; NBA leaves it empty,
    # and the renderer skips the block entirely when it is.
    annotations: tuple = ()


@dataclass(frozen=True)
class VetoResult:
    """One veto layer's verdict on a candidate."""
    fired: bool
    reason: str              # short layer id, e.g. 'book', 'grinder', 'pace/TOV', 'FT/def'


@dataclass(frozen=True)
class VetoLayer:
    """An ordered veto layer: a named predicate over a candidate + its context.

    `evaluate(candidate, ctx)` returns a VetoResult. `ctx` is a sport-specific
    bag of everything the layer needs (team-stat view, this game's snapshots).
    """
    name: str
    evaluate: Callable[[Candidate, "VetoContext"], VetoResult]


@dataclass
class VetoContext:
    """Everything the veto layers need, assembled by the engine per candidate."""
    team_stats: dict                     # team_name -> {group: {field: value}}
    snapshots: list = field(default_factory=list)  # this game's SnapshotRow list, time-ordered


@dataclass(frozen=True)
class Bet:
    """A logged selection (from the static page)."""
    id: str
    sport: str
    game_date: str
    favorite: str
    dog: str
    entry_ml: int
    liquidity: Optional[float]
    stake_chosen: float
    entry_time_actual: Optional[datetime]
    placed: bool


@dataclass(frozen=True)
class Result:
    """A graded bet."""
    win: bool
    net_pnl: float


# --- The plug-in contract ----------------------------------------------------

class Sport(ABC):
    key: str                      # 'nba', 'cfb'
    entry_offset_minutes: int     # alert this long before tip (NBA: 115 = 1:55)
    active_window_hours: tuple    # local ET hours to run the tick densely

    @abstractmethod
    def todays_games(self, date) -> list[Game]:
        """Games with commence_time, home, away."""

    @abstractmethod
    def pull_odds_snapshot(self, taken_at: Optional[datetime] = None
                           ) -> list[SnapshotRow]:
        """Fetch current odds for today's slate -> rows to append to the DB.

        `taken_at` stamps every returned row. The tick passes its own `now` so
        one tick's rows share a single timestamp — the veto layers group
        snapshots BY that timestamp to build per-poll trajectories, so a tick
        whose rows straddled two stamps would fragment the series. Defaults to
        the current UTC time when called outside a tick.
        """

    @abstractmethod
    def pull_stats(self, asof_date) -> list[StatRow]:
        """Point-in-time team stats as of D-1 -> rows for the DB (once daily)."""

    @abstractmethod
    def build_candidates(self, game: Game, snapshots, stats) -> Optional[Candidate]:
        """Favorite-ML candidate at entry time, or None if no candidate."""

    @abstractmethod
    def veto_layers(self) -> list[VetoLayer]:
        """Ordered veto layers; any layer firing vetoes the candidate."""

    @abstractmethod
    def settle(self, bet: Bet, final_score) -> Result:
        """Grade a logged bet: win/loss + realized P&L."""

    # --- point-in-time stats addressing (overridable; NBA-shaped defaults) ---
    # The tick looks stats up by an `asof` key and flattens the tall rows into a
    # per-team view. Both were NBA assumptions baked into engine/tick.py; they
    # are hooks now so a sport whose stats are addressed differently (CFB: as-of
    # through the PRIOR WEEK, not the prior day) plugs in without engine rework.

    def stats_asof_key(self, game: Game) -> str:
        """The `stats.asof_date` key holding this game's point-in-time stats.

        Default: D-1 relative to the game's ET date (NBA). CFB overrides this
        with the end date of the prior week (endWeek = N-1).
        """
        from datetime import date, timedelta
        d = date.fromisoformat(game.game_date) - timedelta(days=1)
        return d.isoformat()

    def stats_view(self, stat_rows) -> dict:
        """Flatten tall stat rows into team_name -> {field: value}."""
        view: dict = {}
        for r in stat_rows:
            view.setdefault(r.team, {})[r.field] = r.value
        return view
