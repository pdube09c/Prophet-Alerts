"""CFB unit tests — the three conditions, the universe filters, the as-of join,
the maturity flag, and the crosswalk discipline.

Everything here is pure: the veto predicates, candidate construction and week
arithmetic take plain data, so the identical code that runs in the live tick is
exercised directly with no network and no DB.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from sports import cfb
from sports.base import Candidate, Game, SnapshotRow, StatRow, VetoContext

UTC = timezone.utc

# Two real, crosswalk-known FBS names so the exact-match discipline is exercised
# rather than bypassed with invented strings.
FAV = "Ohio State Buckeyes"
DOG = "Michigan Wolverines"
KICK = datetime(2026, 10, 10, 19, 0, tzinfo=UTC)


def game(home=FAV, away=DOG, kickoff=KICK) -> Game:
    return Game(sport="cfb", game_id="G1", game_date=kickoff.date().isoformat(),
                commence_time=kickoff, home=home, away=away)


def snap(book, minutes_before_kick, home_point=None, *, home_ml=None,
         away_ml=None, home_limit=None, away_limit=None, kickoff=KICK):
    return SnapshotRow(
        sport="cfb", game_date=kickoff.date().isoformat(), game_id="G1",
        taken_at=kickoff - timedelta(minutes=minutes_before_kick), book=book,
        home_point=home_point, home_ml=home_ml, away_ml=away_ml,
        home_limit=home_limit, away_limit=away_limit)


def cand(favorite=FAV, dog=DOG, g=None, entry_ml=-150) -> Candidate:
    return Candidate(sport="cfb", game=g or game(), favorite=favorite, dog=dog,
                     entry_ml=entry_ml, liquidity=1000.0,
                     entry_time_actual=KICK - timedelta(hours=24))


def ctx(team_stats=None, snapshots=None) -> VetoContext:
    return VetoContext(team_stats=team_stats or {}, snapshots=snapshots or [])


# --- crosswalk discipline ----------------------------------------------------

class TestCrosswalk:
    def test_exact_match_resolves(self):
        assert cfb.cfbd_to_canonical("Ohio State") == "Ohio State Buckeyes"
        assert cfb.cfbd_to_canonical("Miami") == "Miami Hurricanes"
        assert cfb.cfbd_to_canonical("Miami (OH)") == "Miami (OH) RedHawks"

    def test_unmapped_name_is_a_loud_error_not_a_silent_drop(self):
        with pytest.raises(cfb.UnmappedTeamError) as exc:
            cfb.cfbd_to_canonical("Ohio State Buckeyes")  # canonical, not CFBD
        assert "Exact match only" in str(exc.value)

    def test_no_fuzzy_or_prefix_fallback(self):
        # "Ohio" and "Ohio State" are different schools; a prefix rule would
        # silently conflate them. Both must resolve independently and exactly.
        assert cfb.cfbd_to_canonical("Ohio") == "Ohio Bobcats"
        assert cfb.cfbd_to_canonical("Ohio State") == "Ohio State Buckeyes"
        with pytest.raises(cfb.UnmappedTeamError):
            cfb.cfbd_to_canonical("Ohio St")

    def test_unknown_odds_team_raises(self):
        with pytest.raises(cfb.UnmappedTeamError):
            cfb.assert_known_odds_team("Nowhere Tech Fightin' Nobodies")

    def test_find_unmapped_reports_all_at_once(self):
        bad = cfb.find_unmapped([FAV, "Nope A", "Nope B"], kind="odds")
        assert bad == ["Nope A", "Nope B"]

    def test_2026_reclassifiers_present(self):
        # The whole point of deriving FBS membership at runtime is that these
        # resolve. If the crosswalk loses them, reclassifiers silently vanish.
        assert cfb.cfbd_to_canonical("North Dakota State") == \
            "North Dakota State Bison"
        assert cfb.cfbd_to_canonical("Sacramento State") == \
            "Sacramento State Hornets"


class TestCrosswalkNotStale:
    """The checked-in JSON must still match the .ts source of truth.

    Skipped when the sibling repo is absent (CI), where the checked-in file is
    authoritative; enforced locally, where a divergence can actually be fixed.
    """

    def test_generated_json_matches_typescript_source(self):
        from tools import gen_cfb_crosswalk as gen

        if not os.path.isdir(gen._DEFAULT_SRC):
            pytest.skip(f"crosswalk source repo not present ({gen._DEFAULT_SRC})")
        with open(gen.OUT_PATH, encoding="utf-8") as fh:
            checked_in = fh.read()
        assert checked_in == gen._serialize(gen.build(gen._DEFAULT_SRC)), (
            "sports/data/cfb_crosswalk.json is stale. "
            "Re-run: python -m tools.gen_cfb_crosswalk")

    def test_payload_is_self_consistent(self):
        payload = cfb.crosswalk()
        assert payload["_unmapped_canonical"] == [], (
            "every CFBD mapping must land on a name the odds crosswalk knows")
        assert len(payload["cfbd_to_canonical"]) > 200


# --- week arithmetic + no-lookahead -----------------------------------------

def _calendar(weeks=6, first_start=datetime(2026, 8, 29, tzinfo=UTC)):
    """A synthetic CFBD calendar: `weeks` consecutive Sat-to-Sat regular weeks."""
    out = []
    for i in range(weeks):
        start = first_start + timedelta(days=7 * i)
        out.append({
            "season": 2026, "week": i + 1, "seasonType": "regular",
            "startDate": start.isoformat().replace("+00:00", "Z"),
            "endDate": (start + timedelta(days=6, hours=23)).isoformat()
                       .replace("+00:00", "Z"),
        })
    return out


class TestWeekArithmetic:
    def test_week_of_finds_the_containing_week(self):
        cal = _calendar()
        assert cfb.week_of(datetime(2026, 8, 30, tzinfo=UTC), cal) == 1
        assert cfb.week_of(datetime(2026, 9, 12, tzinfo=UTC), cal) == 3

    def test_week_of_falls_back_to_the_prior_week_in_a_gap(self):
        # CFBD's published ranges do not always abut; a kickoff in a gap must
        # resolve to the week that started before it, never crash.
        cal = [
            {"week": 1, "seasonType": "regular",
             "startDate": "2026-08-29T00:00:00Z", "endDate": "2026-08-31T00:00:00Z"},
            {"week": 2, "seasonType": "regular",
             "startDate": "2026-09-08T00:00:00Z", "endDate": "2026-09-10T00:00:00Z"},
        ]
        assert cfb.week_of(datetime(2026, 9, 4, tzinfo=UTC), cal) == 1

    def test_asof_week_end_is_the_end_of_the_prior_week(self):
        cal = _calendar()
        # Week 3's as-of stats end when week 2 ended.
        assert cfb.asof_week_end(3, cal) == \
            cfb.week_bounds(cal)[2][1].date()

    def test_asof_week_end_is_none_for_week_one(self):
        assert cfb.asof_week_end(1, _calendar()) is None

    def test_latest_completed_week(self):
        cal = _calendar()
        # Mid-week-3: weeks 1 and 2 have ended.
        assert cfb.latest_completed_week(
            datetime(2026, 9, 15, tzinfo=UTC), cal) == 2
        assert cfb.latest_completed_week(
            datetime(2026, 8, 30, tzinfo=UTC), cal) is None

    @pytest.mark.parametrize("game_week,endweek", [(5, 5), (5, 6), (3, 14)])
    def test_lookahead_is_rejected(self, game_week, endweek):
        with pytest.raises(ValueError, match="LOOKAHEAD"):
            cfb.assert_no_lookahead(game_week, endweek)

    def test_asof_prior_week_is_allowed(self):
        cfb.assert_no_lookahead(5, 4)  # must not raise


class TestLookaheadGuardOnRealData:
    """assert_stats_are_asof is the guard that can actually fire: it inspects
    the endWeek the loaded rows were PULLED through, so it catches a mis-filed
    key or a wrong-endWeek backfill that the arithmetic check cannot see."""

    def _view(self, asof_weeks):
        return {f"T{i}": {cfb.F_SR: 0.4, cfb.F_ASOF_WEEK: float(w)}
                for i, w in enumerate(asof_weeks)}

    def test_clean_as_of_stats_pass(self):
        cfb.assert_stats_are_asof(6, self._view([5, 5, 5]))  # must not raise

    def test_same_week_stats_raise(self):
        with pytest.raises(ValueError, match="LOOKAHEAD"):
            cfb.assert_stats_are_asof(6, self._view([5, 6, 5]))

    def test_season_final_stats_raise(self):
        with pytest.raises(ValueError, match="LOOKAHEAD"):
            cfb.assert_stats_are_asof(6, self._view([14, 14, 14]))

    def test_a_single_contaminated_row_raises(self):
        """One bad row shifts the quartile threshold, so it must stop the tick
        even though the two teams playing may both be clean."""
        view = self._view([5] * 40)
        view["T7"][cfb.F_ASOF_WEEK] = 9.0
        with pytest.raises(ValueError, match="T7 .endWeek=9"):
            cfb.assert_stats_are_asof(6, view)

    def test_the_error_names_the_offenders_and_counts_them(self):
        view = self._view([9] * 8)
        with pytest.raises(ValueError) as exc:
            cfb.assert_stats_are_asof(6, view)
        assert "8 stat row(s)" in str(exc.value)
        assert "and 3 more" in str(exc.value)

    def test_rows_without_an_asof_stamp_are_not_silently_trusted(self):
        """A row with no ASOF_WEEK cannot be verified. It does not raise here
        (older rows predate the stamp), but it must not mask a bad sibling."""
        view = {"A": {cfb.F_SR: 0.4}, "B": {cfb.F_SR: 0.4, cfb.F_ASOF_WEEK: 7.0}}
        with pytest.raises(ValueError, match="LOOKAHEAD"):
            cfb.assert_stats_are_asof(6, view)

    def test_build_candidate_refuses_contaminated_stats(self):
        """End to end: the guard runs on the rows that feed the vetoes, so a
        contaminated view stops candidate construction entirely."""
        builder = TestBuildCandidate()
        bad = {FAV: {cfb.F_GAMES: 5, cfb.F_ASOF_WEEK: 6.0},
               DOG: {cfb.F_GAMES: 5, cfb.F_ASOF_WEEK: 6.0}}
        with pytest.raises(ValueError, match="LOOKAHEAD"):
            builder._build(week=6, stats=bad)

    def test_build_candidate_accepts_prior_week_stats(self):
        builder = TestBuildCandidate()
        good = {FAV: {cfb.F_GAMES: 5, cfb.F_ASOF_WEEK: 5.0},
                DOG: {cfb.F_GAMES: 5, cfb.F_ASOF_WEEK: 5.0}}
        assert builder._build(week=6, stats=good) is not None


# --- condition 1: stuff veto -------------------------------------------------

class TestStuffVeto:
    def _pop(self, dog_value):
        """A 9-team stuff-rate population 0.10..0.18 plus the dog at `dog_value`.
        The 75th percentile of 0.10..0.18 (step .01) is 0.16."""
        stats = {f"T{i}": {cfb.F_STUFF: 0.10 + 0.01 * i} for i in range(9)}
        stats[DOG] = {cfb.F_STUFF: dog_value}
        stats[FAV] = {cfb.F_STUFF: 0.11}
        return stats

    def test_fires_on_top_quartile_dog(self):
        assert cfb.veto_stuff(cand(), ctx(self._pop(0.30))).fired

    def test_passes_on_ordinary_dog(self):
        assert not cfb.veto_stuff(cand(), ctx(self._pop(0.05))).fired

    def test_boundary_is_inclusive(self):
        # "Top quartile" includes the threshold itself.
        pop = [0.10 + 0.01 * i for i in range(9)]
        q = cfb.quantile(pop, 0.75)
        stats = {f"T{i}": {cfb.F_STUFF: v} for i, v in enumerate(pop)}
        stats[DOG] = {cfb.F_STUFF: q}
        assert cfb.veto_stuff(cand(), ctx(stats)).fired

    def test_missing_dog_stat_does_not_fire(self):
        # Absence of evidence is not evidence; the maturity flag carries the
        # caveat instead of the veto inventing a verdict.
        assert not cfb.veto_stuff(cand(), ctx({FAV: {cfb.F_STUFF: 0.2}})).fired

    def test_sentinel_row_is_excluded_from_the_threshold(self):
        # A nationalAverages row left in the population would shift the
        # quartile. It must never be counted.
        stats = {f"T{i}": {cfb.F_STUFF: 0.10 + 0.01 * i} for i in range(9)}
        stats[DOG] = {cfb.F_STUFF: 0.155}
        clean = cfb._population(stats, cfb.F_STUFF)
        stats["nationalAverages"] = {cfb.F_STUFF: 99.0}
        assert cfb._population(stats, cfb.F_STUFF) == clean
        assert "nationalAverages" not in cfb.team_stats_view(
            [StatRow("cfb", "2026-09-12", "nationalAverages", "advanced",
                     cfb.F_STUFF, 99.0)])


# --- condition 2: retail-extend veto ----------------------------------------

class TestRetailExtendVeto:
    """Window: listing -> T-48h before kickoff. Fires when >=2 retail books show
    a sustained >=1pt move toward the favorite and Pinnacle does not confirm."""

    def _snaps(self, retail_moves, pinny_move=False, *, home_fav=True,
               kickoff=KICK, after_cutoff_move=False):
        """Build a trajectory. `retail_moves` = how many retail books move.

        Hourly snapshots at T-72h, T-71h, T-70h (all inside the T-48h window).
        A moving book goes from -7 to -8.5 (home favorite laying more) and HOLDS,
        i.e. a sustained >=1pt move toward the favorite.
        """
        sign = 1 if home_fav else -1
        rows = []
        base, moved = sign * -7.0, sign * -8.5
        for hours, use_moved in ((72, False), (71, True), (70, True)):
            for i, bk in enumerate(cfb.RETAIL_BOOKS):
                pt = moved if (use_moved and i < retail_moves) else base
                rows.append(snap(bk, hours * 60, pt, kickoff=kickoff))
            ppt = moved if (use_moved and pinny_move) else base
            rows.append(snap(cfb.PINNACLE, hours * 60, ppt, kickoff=kickoff))
        if after_cutoff_move:
            # A big move INSIDE T-48h (T-2h) — outside the window, must be ignored.
            for bk in cfb.RETAIL_BOOKS:
                rows.append(snap(bk, 120, sign * -20.0, kickoff=kickoff))
            rows.append(snap(cfb.PINNACLE, 120, sign * -20.0, kickoff=kickoff))
        return rows

    def test_fires_on_two_retail_books_with_pinnacle_flat(self):
        assert cfb.veto_retail_extend(
            cand(), ctx(snapshots=self._snaps(2))).fired

    def test_does_not_fire_on_one_retail_book(self):
        assert not cfb.veto_retail_extend(
            cand(), ctx(snapshots=self._snaps(1))).fired

    def test_pinnacle_confirming_suppresses_the_veto(self):
        # Retail steam Pinnacle agrees with is information, not public money.
        assert not cfb.veto_retail_extend(
            cand(), ctx(snapshots=self._snaps(4, pinny_move=True))).fired

    def test_movement_after_the_t48h_cutoff_is_ignored(self):
        # The window ends at T-48h. A huge late move must not create a veto.
        snaps = self._snaps(0, after_cutoff_move=True)
        assert not cfb.veto_retail_extend(cand(), ctx(snapshots=snaps)).fired

    def test_away_favorite_direction_is_handled(self):
        # Away favorite: the home line moving UP means the favorite lays more.
        g = game(home=DOG, away=FAV)
        c = cand(favorite=FAV, dog=DOG, g=g)
        snaps = self._snaps(2, home_fav=False)
        assert cfb.veto_retail_extend(c, ctx(snapshots=snaps)).fired

    def test_unsustained_blip_does_not_fire(self):
        """A one-snapshot spike that reverts is noise, not a sustained move."""
        rows = []
        for hours, pt in ((72, -7.0), (71, -8.5), (70, -7.0), (69, -7.0)):
            for bk in cfb.RETAIL_BOOKS:
                rows.append(snap(bk, hours * 60, pt))
            rows.append(snap(cfb.PINNACLE, hours * 60, -7.0))
        assert not cfb.veto_retail_extend(cand(), ctx(snapshots=rows)).fired

    def test_weeknight_mactiongame_uses_identical_logic(self):
        """The window is kickoff-relative, so a Wednesday night game is handled
        by the same code with no special-casing."""
        wednesday = datetime(2026, 11, 4, 0, 30, tzinfo=UTC)  # Tue 7:30pm ET
        snaps = self._snaps(2, kickoff=wednesday)
        c = cand(g=game(kickoff=wednesday))
        assert cfb.veto_retail_extend(c, ctx(snapshots=snaps)).fired

    def test_each_book_is_measured_against_its_own_opener(self):
        """Books open at different numbers; the move is per-book displacement,
        not displacement from a cross-book consensus."""
        rows = []
        openers = {"williamhill_us": -7.0, "betmgm": -3.0,
                   "fanduel": -10.0, "draftkings": -1.0}
        for hours, delta in ((72, 0.0), (71, -1.5), (70, -1.5)):
            for bk, open_pt in openers.items():
                # Only the first two books move; the others sit on their opener.
                d = delta if bk in ("williamhill_us", "betmgm") else 0.0
                rows.append(snap(bk, hours * 60, open_pt + d))
            rows.append(snap(cfb.PINNACLE, hours * 60, -7.0))
        assert cfb.veto_retail_extend(cand(), ctx(snapshots=rows)).fired


# --- condition 3: dog success-rate filter ------------------------------------

class TestDogSuccessRateVeto:
    def test_passes_when_dog_success_rate_exceeds_favorite(self):
        stats = {DOG: {cfb.F_SR: 0.46}, FAV: {cfb.F_SR: 0.42}}
        assert not cfb.veto_dog_sr(cand(), ctx(stats)).fired

    def test_fires_when_dog_is_worse(self):
        stats = {DOG: {cfb.F_SR: 0.38}, FAV: {cfb.F_SR: 0.45}}
        assert cfb.veto_dog_sr(cand(), ctx(stats)).fired

    def test_equal_rates_fire_strict_greater_than(self):
        stats = {DOG: {cfb.F_SR: 0.42}, FAV: {cfb.F_SR: 0.42}}
        assert cfb.veto_dog_sr(cand(), ctx(stats)).fired

    def test_missing_stat_does_not_fire(self):
        assert not cfb.veto_dog_sr(cand(), ctx({DOG: {cfb.F_SR: 0.4}})).fired


# --- universe filters + candidate construction -------------------------------

class TestBuildCandidate:
    FBS = {FAV, DOG}

    def _snaps(self, *, entry_ml=-150, home_point=-7.0):
        rows = []
        for bk in cfb.NON_PROPHETX_BOOKS:
            rows.append(snap(bk, 72 * 60, home_point))
            rows.append(snap(bk, 24 * 60, home_point))
        rows.append(snap(cfb.PROPHETX, 24 * 60, home_point,
                         home_ml=entry_ml, away_ml=+130, home_limit=2500.0))
        return rows

    def _build(self, snaps=None, week=6, fbs=None, stats=None):
        return cfb.build_candidate(
            game(), snaps if snaps is not None else self._snaps(),
            stats or {}, fbs=fbs if fbs is not None else self.FBS,
            game_week=week, now=KICK - timedelta(hours=24))

    def test_builds_a_candidate_in_band(self):
        c = self._build()
        assert c is not None
        assert (c.favorite, c.dog, c.entry_ml) == (FAV, DOG, -150)
        assert c.sport == "cfb"
        assert c.liquidity == 2500.0

    @pytest.mark.parametrize("ml", [-100, -300, -150])
    def test_price_band_inclusive_ends_are_accepted(self, ml):
        assert self._build(self._snaps(entry_ml=ml)) is not None

    @pytest.mark.parametrize("ml", [-301, -450, +120])
    def test_favorites_heavier_than_minus_300_are_excluded(self, ml):
        assert self._build(self._snaps(entry_ml=ml)) is None

    def test_non_fbs_opponent_is_excluded(self):
        assert self._build(fbs={FAV}) is None

    def test_week_two_does_not_alert(self):
        assert self._build(week=2) is None

    def test_week_three_alerts(self):
        # Weeks 3-4 fire with a maturity flag — they are NOT gated off.
        assert self._build(week=3) is not None

    def test_unmapped_board_name_is_announced_and_skipped(self, capsys):
        """Loud, never silent — but not fatal at the board level: one unknown
        FCS opponent must not suppress alerts for the rest of the board. The
        hard errors live in CFB.fbs() and tools/validate_cfb.py instead."""
        cfb.UNRESOLVED_BOARD_NAMES.discard("Nowhere Tech")
        g = Game(sport="cfb", game_id="G1", game_date="2026-10-10",
                 commence_time=KICK, home="Nowhere Tech", away=DOG)
        assert cfb.build_candidate(g, self._snaps(), {}, fbs=self.FBS,
                                   game_week=6) is None
        assert "UNRESOLVED TEAM NAME" in capsys.readouterr().err
        assert "Nowhere Tech" in cfb.UNRESOLVED_BOARD_NAMES

    def test_favorite_is_fixed_at_the_earliest_snapshot(self):
        """The favorite is the consensus at listing, so it cannot flip on late
        movement between accumulation and the fire."""
        rows = []
        for bk in cfb.NON_PROPHETX_BOOKS:
            rows.append(snap(bk, 72 * 60, -7.0))    # home favored at listing
            rows.append(snap(bk, 24 * 60, +3.0))    # away favored later
        rows.append(snap(cfb.PROPHETX, 24 * 60, +3.0,
                         home_ml=-150, away_ml=+130, home_limit=100.0))
        c = self._build(rows)
        assert c is not None and c.favorite == FAV

    def test_pickem_has_no_favorite(self):
        assert self._build(self._snaps(home_point=0.0)) is None

    def test_no_prophetx_price_means_no_candidate(self):
        rows = [r for r in self._snaps() if r.book != cfb.PROPHETX]
        assert self._build(rows) is None


# --- the data-maturity flag --------------------------------------------------

class TestMaturityFlag:
    def _snaps_through_cutoff(self, n=5):
        return [snap("pinnacle", (72 - i) * 60, -7.0) for i in range(n)]

    def test_every_candidate_carries_the_three_annotations(self):
        """The maturity flag is required on EVERY CFB alert, so it is attached
        at candidate construction rather than by the renderer."""
        builder = TestBuildCandidate()
        c = builder._build(stats={FAV: {cfb.F_GAMES: 5}, DOG: {cfb.F_GAMES: 5}})
        assert c is not None
        assert [label for label, _ in c.annotations] ==             ["Data maturity", "Stats confidence", "Retail-extend"]

    def test_week_three_reports_noise_regime(self):
        ann = dict(cfb.maturity(3, {FAV: {cfb.F_GAMES: 2}, DOG: {cfb.F_GAMES: 2}},
                                FAV, DOG, self._snaps_through_cutoff(), KICK,
                                KICK - timedelta(hours=24)))
        assert ann["Data maturity"] == "as-of through Week 2: 2 games of data"
        assert "noise-regime" in ann["Stats confidence"]

    def test_week_six_with_enough_games_reports_reliable(self):
        ann = dict(cfb.maturity(6, {FAV: {cfb.F_GAMES: 5}, DOG: {cfb.F_GAMES: 5}},
                                FAV, DOG, self._snaps_through_cutoff(), KICK,
                                KICK - timedelta(hours=24)))
        assert "reliable" in ann["Stats confidence"]

    def test_games_count_uses_the_thinner_of_the_two_teams(self):
        ann = dict(cfb.maturity(6, {FAV: {cfb.F_GAMES: 5}, DOG: {cfb.F_GAMES: 3}},
                                FAV, DOG, self._snaps_through_cutoff(), KICK,
                                KICK - timedelta(hours=24)))
        assert "3 games of data" in ann["Data maturity"]
        assert "noise-regime" in ann["Stats confidence"]

    def test_unknown_games_count_is_admitted_not_invented(self):
        ann = dict(cfb.maturity(6, {}, FAV, DOG, self._snaps_through_cutoff(),
                                KICK, KICK - timedelta(hours=24)))
        assert "unknown" in ann["Data maturity"]
        assert "unknown" in ann["Stats confidence"]

    def test_retail_extend_complete_when_window_closed_with_data(self):
        status, _ = cfb.retail_extend_status(
            self._snaps_through_cutoff(5), KICK, KICK - timedelta(hours=24))
        assert status == "complete"

    def test_retail_extend_partial_when_window_still_open(self):
        status, why = cfb.retail_extend_status(
            self._snaps_through_cutoff(5), KICK, KICK - timedelta(hours=60))
        assert status == "partial" and "still open" in why

    def test_retail_extend_partial_when_too_few_snapshots(self):
        status, why = cfb.retail_extend_status(
            self._snaps_through_cutoff(1), KICK, KICK - timedelta(hours=24))
        assert status == "partial" and "1 snapshot" in why


# --- settlement --------------------------------------------------------------

class TestSettle:
    def _bet(self, stake=100.0, ml=-150):
        from sports.base import Bet
        return Bet(id="b1", sport="cfb", game_date="2026-10-10", favorite=FAV,
                   dog=DOG, entry_ml=ml, liquidity=None, stake_chosen=stake,
                   entry_time_actual=None, placed=True)

    def test_win_applies_the_two_percent_commission(self):
        r = CFBNoNetwork().settle(self._bet(), {FAV: 31, DOG: 24})
        assert r.win
        assert r.net_pnl == round(0.98 * 100.0 * 100.0 / 150.0, 2)

    def test_loss_returns_the_stake(self):
        r = CFBNoNetwork().settle(self._bet(), {FAV: 17, DOG: 20})
        assert not r.win and r.net_pnl == -100.0

    def test_commission_is_two_percent_not_one(self):
        assert cfb.PAYOUT_HAIRCUT == 0.98


class CFBNoNetwork(cfb.CFB):
    """CFB with the network stubbed, for tests that only need pure methods."""

    def __init__(self, calendar=None, fbs=None, **kw):
        super().__init__(year=2026, **kw)
        self._calendar_cache = calendar or _calendar(weeks=14)
        self._fbs_cache = fbs or {FAV, DOG}


# --- the Sport plug-in wiring ------------------------------------------------

class TestSportWiring:
    def test_veto_layers_are_the_three_conditions_in_order(self):
        assert [l.name for l in CFBNoNetwork().veto_layers()] == \
            ["stuff", "retail-extend", "dog-SR"]

    def test_stats_asof_key_is_the_end_of_the_prior_week(self):
        sport = CFBNoNetwork()
        cal = sport.calendar()
        kickoff = cfb.week_bounds(cal)[5][0] + timedelta(days=2)
        key = sport.stats_asof_key(game(kickoff=kickoff))
        assert key == cfb.week_bounds(cal)[4][1].date().isoformat()

    def test_default_fire_window_is_t_minus_24h(self):
        assert CFBNoNetwork().entry_offset_minutes == 24 * 60

    def test_fire_window_is_configurable(self):
        assert CFBNoNetwork(entry_offset_minutes=2880).entry_offset_minutes == 2880

    def test_stats_view_drops_the_sentinel(self):
        rows = [StatRow("cfb", "2026-09-12", "nationalAverages", "advanced",
                        cfb.F_SR, 0.42),
                StatRow("cfb", "2026-09-12", FAV, "advanced", cfb.F_SR, 0.45)]
        assert set(CFBNoNetwork().stats_view(rows)) == {FAV}

    def test_build_sport_constructs_cfb(self):
        from engine.tick import build_sport
        assert build_sport("cfb").key == "cfb"
        assert build_sport("nba").key == "nba"

    def test_build_sport_rejects_unknown(self):
        from engine.tick import build_sport
        with pytest.raises(SystemExit):
            build_sport("mlb")


# --- the whole condition set, end to end over one game -----------------------

class TestAllThreeConditions:
    """A favorite is alerted only if it clears all three. Each test flips one
    input and asserts that condition alone decides the outcome."""

    FBS = {FAV, DOG}

    def _stats(self, dog_stuff=0.05, dog_sr=0.46, fav_sr=0.42):
        stats = {f"T{i}": {cfb.F_STUFF: 0.10 + 0.01 * i, cfb.F_SR: 0.40}
                 for i in range(9)}
        stats[DOG] = {cfb.F_STUFF: dog_stuff, cfb.F_SR: dog_sr,
                      cfb.F_GAMES: 5}
        stats[FAV] = {cfb.F_STUFF: 0.11, cfb.F_SR: fav_sr, cfb.F_GAMES: 5}
        return stats

    def _snaps(self, retail_moves=0, pinny_move=False):
        rows = []
        for hours, use_moved in ((72, False), (71, True), (70, True)):
            for i, bk in enumerate(cfb.RETAIL_BOOKS):
                rows.append(snap(bk, hours * 60,
                                 -8.5 if (use_moved and i < retail_moves) else -7.0))
            rows.append(snap(cfb.PINNACLE, hours * 60,
                             -8.5 if (use_moved and pinny_move) else -7.0))
        rows.append(snap(cfb.PROPHETX, 24 * 60, -7.0, home_ml=-150,
                         away_ml=+130, home_limit=2500.0))
        return rows

    def _evaluate(self, stats=None, snaps=None):
        from engine.veto import evaluate_all
        stats = stats or self._stats()
        snaps = snaps if snaps is not None else self._snaps()
        c = cfb.build_candidate(game(), snaps, stats, fbs=self.FBS, game_week=6,
                                now=KICK - timedelta(hours=24))
        assert c is not None, "expected a candidate"
        return c, evaluate_all(CFBNoNetwork().veto_layers(), c,
                               ctx(stats, snaps))

    def test_clean_candidate_survives_all_three(self):
        c, fired = self._evaluate()
        assert fired == []
        assert c.annotations  # the maturity flag rides along

    def test_stuff_alone_vetoes(self):
        _, fired = self._evaluate(stats=self._stats(dog_stuff=0.30))
        assert fired == ["stuff"]

    def test_retail_extend_alone_vetoes(self):
        _, fired = self._evaluate(snaps=self._snaps(retail_moves=2))
        assert fired == ["retail-extend"]

    def test_dog_sr_alone_vetoes(self):
        _, fired = self._evaluate(stats=self._stats(dog_sr=0.30))
        assert fired == ["dog-SR"]

    def test_every_fired_layer_is_recorded_for_the_audit_trail(self):
        _, fired = self._evaluate(stats=self._stats(dog_stuff=0.30, dog_sr=0.30),
                                  snaps=self._snaps(retail_moves=3))
        assert fired == ["stuff", "retail-extend", "dog-SR"]


# --- the alert path is REUSED, not forked ------------------------------------

class TestAlertPathReuse:
    def test_cfb_alert_renders_through_the_shared_composer(self):
        from engine.alert import AlertConfig, build_alert

        cfg = AlertConfig(stage="stage1", email_from="a@b.c", email_to="d@e.f",
                          page_url="https://example/page",
                          stake_ladder=(100.0, 500.0))
        c = Candidate(sport="cfb", game=game(), favorite=FAV, dog=DOG,
                      entry_ml=-150, liquidity=1234.0, entry_time_actual=KICK,
                      annotations=(("Data maturity", "as-of through Week 5: "
                                                     "5 games of data"),))
        msg = build_alert(c, cfg)
        assert "[CFB]" in msg.subject           # sport-tagged subject
        assert "[CFB]" in msg.push_body         # sport-tagged push
        assert "as-of through Week 5" in msg.text   # maturity in the body
        assert "as-of through Week 5" in msg.html
        assert "Reference ladder" in msg.text   # the SHARED ladder, not a fork
        assert "$100.00" in msg.text            # shared three-stage sizing

    def test_nba_alert_is_unchanged_apart_from_the_sport_tag(self):
        from engine.alert import AlertConfig, build_alert

        cfg = AlertConfig(stage="paper", email_from="a@b.c", email_to="d@e.f",
                          page_url="", stake_ladder=())
        c = Candidate(sport="nba", game=Game(
            sport="nba", game_id="N1", game_date="2026-01-01",
            commence_time=KICK, home="Boston Celtics", away="Miami Heat"),
            favorite="Boston Celtics", dog="Miami Heat", entry_ml=-150,
            liquidity=None, entry_time_actual=KICK)
        msg = build_alert(c, cfg)
        assert "[NBA]" in msg.subject
        # No annotations -> no annotation block at all.
        assert "Data maturity" not in msg.text
        assert "<table style=\"border-collapse:collapse;font-size:13px" not in msg.html

    def test_cfb_smoke_candidate_exercises_the_maturity_block(self):
        from engine.alert import _smoke_candidate

        c = _smoke_candidate("cfb")
        assert c.sport == "cfb"
        assert [l for l, _ in c.annotations] == \
            ["Data maturity", "Stats confidence", "Retail-extend"]


# --- the CFBD stats pull (adapter, with the network faked) -------------------

class TestPullStats:
    def _sport(self, monkeypatch, rows, games=None):
        sport = CFBNoNetwork(fbs={FAV, DOG})
        calls = {}

        def fake_get(path, **params):
            calls[path] = params
            if path == "stats/season/advanced":
                return rows
            if path == "games":
                return games or []
            raise AssertionError(f"unexpected CFBD call {path}")

        monkeypatch.setattr(cfb, "_cfbd_get", fake_get)
        return sport, calls

    def _row(self, team, stuff=0.15, sr=0.44):
        return {"team": team, "offense": {"successRate": sr},
                "defense": {"stuffRate": stuff}}

    def test_pull_uses_endweek_and_excludes_garbage_time(self, monkeypatch):
        sport, calls = self._sport(
            monkeypatch, [self._row("Ohio State"), self._row("Michigan")])
        # Mid-week-4 of the synthetic calendar: weeks 1-3 have ended.
        asof = cfb.week_bounds(sport.calendar())[3][1].date().isoformat()
        rows = sport.pull_stats(asof)
        params = calls["stats/season/advanced"]
        assert params["endWeek"] == 3
        assert params["excludeGarbageTime"] == "true"
        assert "week" not in params  # never season-final / unbounded
        assert {r.team for r in rows} == {FAV, DOG}

    def test_rows_are_keyed_by_the_week_end_date(self, monkeypatch):
        sport, _ = self._sport(monkeypatch, [self._row("Ohio State")])
        cal = sport.calendar()
        asof = cfb.week_bounds(cal)[3][1].date().isoformat()
        rows = sport.pull_stats(asof)
        expected = cfb.week_bounds(cal)[3][1].date().isoformat()
        assert {r.asof_date for r in rows} == {expected}
        # ...and that is exactly the key a week-4 game looks up.
        kickoff = cfb.week_bounds(cal)[4][0] + timedelta(days=2)
        assert sport.stats_asof_key(game(kickoff=kickoff)) == expected

    def test_sentinel_row_is_dropped_at_pull_time(self, monkeypatch):
        sport, _ = self._sport(monkeypatch, [
            self._row("Ohio State"), {"team": "nationalAverages",
                                      "offense": {"successRate": 0.42},
                                      "defense": {"stuffRate": 0.15}}])
        asof = cfb.week_bounds(sport.calendar())[3][1].date().isoformat()
        assert all(r.team != "nationalAverages"
                   for r in sport.pull_stats(asof))

    def test_non_fbs_teams_are_excluded_from_the_population(self, monkeypatch):
        sport, _ = self._sport(monkeypatch, [
            self._row("Ohio State"), self._row("Villanova")])  # FCS
        asof = cfb.week_bounds(sport.calendar())[3][1].date().isoformat()
        assert {r.team for r in sport.pull_stats(asof)} == {FAV}

    def test_unmapped_cfbd_team_raises_at_pull_time(self, monkeypatch):
        sport, _ = self._sport(monkeypatch, [self._row("Nowhere Tech")])
        asof = cfb.week_bounds(sport.calendar())[3][1].date().isoformat()
        with pytest.raises(cfb.UnmappedTeamError):
            sport.pull_stats(asof)

    def test_games_played_counts_completed_games_only(self, monkeypatch):
        games = [
            {"week": 1, "completed": True,
             "homeTeam": "Ohio State", "awayTeam": "Michigan"},
            {"week": 2, "completed": True,
             "homeTeam": "Ohio State", "awayTeam": "Michigan"},
            {"week": 3, "completed": False,
             "homeTeam": "Ohio State", "awayTeam": "Michigan"},
            {"week": 9, "completed": True,   # beyond endWeek
             "homeTeam": "Ohio State", "awayTeam": "Michigan"},
        ]
        sport, _ = self._sport(
            monkeypatch, [self._row("Ohio State"), self._row("Michigan")], games)
        asof = cfb.week_bounds(sport.calendar())[3][1].date().isoformat()
        rows = sport.pull_stats(asof)
        counts = {r.team: r.value for r in rows if r.field == cfb.F_GAMES}
        assert counts == {FAV: 2.0, DOG: 2.0}

    def test_no_completed_week_yields_no_rows(self, monkeypatch):
        sport, _ = self._sport(monkeypatch, [self._row("Ohio State")])
        # Before week 1 ends there is no legitimate as-of window.
        assert sport.pull_stats("2026-08-30") == []


# --- the odds adapter --------------------------------------------------------

class TestOddsAdapter:
    EVENT = {
        "id": "abc123", "commence_time": "2026-10-10T19:00:00Z",
        "home_team": FAV, "away_team": DOG,
        "bookmakers": [{
            "key": "prophetx",
            "markets": [
                {"key": "spreads", "outcomes": [
                    {"name": FAV, "point": -7.5}, {"name": DOG, "point": 7.5}]},
                {"key": "h2h", "outcomes": [
                    {"name": FAV, "price": -280, "bet_limit": 900.0},
                    {"name": DOG, "price": 230, "bet_limit": 400.0}]},
            ]}]}

    def test_snapshot_rows_capture_spread_ml_and_limits(self):
        taken = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
        rows = cfb._snapshots_from_odds([self.EVENT], taken)
        assert len(rows) == 1
        r = rows[0]
        assert (r.sport, r.book, r.home_point) == ("cfb", "prophetx", -7.5)
        assert (r.home_ml, r.away_ml) == (-280, 230)
        assert (r.home_limit, r.away_limit) == (900.0, 400.0)

    def test_american_odds_are_stored_unconverted(self):
        # ProphetX's app displays percentages now, but the API still returns
        # American for oddsFormat=american. No conversion belongs here.
        rows = cfb._snapshots_from_odds(
            [self.EVENT], datetime(2026, 10, 8, tzinfo=UTC))
        assert rows[0].home_ml == -280

    def test_games_carry_the_et_calendar_date(self):
        games = cfb._games_from_events([self.EVENT])
        assert len(games) == 1
        assert games[0].sport == "cfb"
        assert games[0].game_date == "2026-10-10"   # 19:00Z = 3pm ET
        assert games[0].game_id == "abc123"

    def test_board_is_pulled_once_per_tick(self, monkeypatch):
        """todays_games + pull_odds_snapshot share ONE /odds payload, so a tick
        costs ~2 credits rather than 4."""
        calls = []

        def fake_get(path, **kw):
            calls.append(path)
            return [self.EVENT]

        monkeypatch.setattr(cfb, "_odds_api_get", fake_get)
        sport = CFBNoNetwork()
        sport.todays_games(None)
        sport.pull_odds_snapshot()
        assert calls == ["odds"]
