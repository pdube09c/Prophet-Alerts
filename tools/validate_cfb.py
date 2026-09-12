"""Pre-flight validation for the CFB path. Run this BEFORE any live CFB alert.

Checks the three things that can silently poison a live alert, and fails loudly
on each. Read-only: it pulls from CFBD, the Odds API and Supabase but writes
nothing, so it is safe to run any time.

  1. crosswalk  — every FBS team from CFBD /teams/fbs and every team name on the
                  live NCAAF board resolves by EXACT match. Zero unmapped FBS
                  teams is the bar; anything unmapped is listed in full.
  2. as-of join — the CFBD as-of stats for the current week join to the board's
                  teams, and the endWeek used is strictly < the game week
                  (no lookahead).
  3. trajectory — the retail-extend veto reads the accumulated Supabase
                  trajectory correctly. Prints, per accumulated game, which
                  retail books show a sustained >=1pt move toward the favorite
                  inside the T-48h window, whether Pinnacle confirms, and the
                  resulting verdict — so a game with a known >=2-book /
                  Pinnacle-flat move can be checked by eye.

Usage:
    python -m tools.validate_cfb                 # all checks
    python -m tools.validate_cfb --skip-db       # no Supabase (checks 1-2)

Exit code is non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from sports import cfb
from sports.base import VetoContext


def _hdr(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def check_crosswalk(sport: cfb.CFB) -> list[str]:
    """Every CFBD FBS school and every live board name must resolve exactly."""
    _hdr("1. CROSSWALK — exact-match resolution, zero unmapped FBS teams")
    failures: list[str] = []

    teams = cfb._cfbd_get("teams/fbs", year=sport.year())
    schools = [t["school"] for t in teams]
    unmapped_cfbd = cfb.find_unmapped(schools, kind="cfbd")
    print(f"CFBD /teams/fbs?year={sport.year()}: {len(schools)} FBS teams")
    if unmapped_cfbd:
        failures.append(f"{len(unmapped_cfbd)} CFBD FBS team(s) unmapped")
        print(f"  FAIL — unmapped CFBD names ({len(unmapped_cfbd)}):")
        for n in unmapped_cfbd:
            print(f"    - {n!r}")
        print("  Fix: add to odds-backtest-verification/src/cfbd-crosswalk.ts, "
              "then re-run tools/gen_cfb_crosswalk.py")
    else:
        print("  OK — every FBS school resolves to a canonical name")

    board = sport._board()
    names = sorted({n for e in board for n in (e["home_team"], e["away_team"])})
    unmapped_odds = cfb.find_unmapped(names, kind="odds")
    print(f"\nLive NCAAF board: {len(board)} games, {len(names)} distinct names")
    if unmapped_odds:
        failures.append(f"{len(unmapped_odds)} board team(s) unmapped")
        print(f"  FAIL — unmapped Odds API names ({len(unmapped_odds)}):")
        for n in unmapped_odds:
            print(f"    - {n!r}")
        print("  Fix: add to odds-backtest-verification/src/ncaaf-crosswalk.ts, "
              "then re-run tools/gen_cfb_crosswalk.py")
    else:
        print("  OK — every name on the board resolves")

    fbs = sport.fbs()
    fbs_games = [e for e in board
                 if e["home_team"] in fbs and e["away_team"] in fbs]
    print(f"\nFBS vs FBS on the board: {len(fbs_games)} of {len(board)} games")
    return failures


def check_asof_join(sport: cfb.CFB) -> list[str]:
    """As-of stats must exist for the current week and must not look ahead."""
    _hdr("2. AS-OF JOIN — CFBD endWeek=N-1, no lookahead")
    failures: list[str] = []
    now = datetime.now(timezone.utc)
    cal = sport.calendar()

    week_now = cfb.week_of(now, cal)
    endweek = cfb.latest_completed_week(now, cal)
    print(f"season {sport.year()} · current week {week_now} · "
          f"latest completed week {endweek}")

    if endweek is None:
        print("  SKIP — no completed week yet; as-of stats do not exist.")
        return failures

    try:
        cfb.assert_no_lookahead(week_now, endweek)
        print(f"  OK — endWeek={endweek} < game week {week_now} (no lookahead)")
    except ValueError as exc:
        failures.append("lookahead guard failed")
        print(f"  FAIL — {exc}")

    rows = sport.pull_stats(now.date().isoformat())
    teams = {r.team for r in rows}
    key = {r.asof_date for r in rows}
    print(f"\nCFBD /stats/season/advanced endWeek={endweek} "
          f"excludeGarbageTime=true")
    print(f"  {len(rows)} stat rows · {len(teams)} teams · asof key(s) {sorted(key)}")

    if any(t.strip().lower() in cfb.CFBD_SENTINEL_TEAMS for t in teams):
        failures.append("nationalAverages sentinel present in stat rows")
        print("  FAIL — the nationalAverages sentinel row was NOT excluded")
    else:
        print("  OK — nationalAverages sentinel excluded")

    view = cfb.team_stats_view(rows)
    missing_stuff = sorted(t for t in teams if cfb.F_STUFF not in view.get(t, {}))
    missing_sr = sorted(t for t in teams if cfb.F_SR not in view.get(t, {}))
    print(f"  teams missing {cfb.F_STUFF}: {len(missing_stuff)}"
          f"{' ' + str(missing_stuff[:5]) if missing_stuff else ''}")
    print(f"  teams missing {cfb.F_SR}: {len(missing_sr)}"
          f"{' ' + str(missing_sr[:5]) if missing_sr else ''}")

    pop = cfb._population(view, cfb.F_STUFF)
    if pop:
        q = cfb.quantile(pop, cfb.STUFF_QUANTILE)
        print(f"  stuff-rate top-quartile threshold (n={len(pop)}): {q:.4f}")
    else:
        failures.append("no stuff-rate population — the stuff veto cannot run")
        print("  FAIL — empty stuff-rate population")

    # The board's FBS games must actually find their teams in the as-of view.
    fbs = sport.fbs()
    board_teams = {n for e in sport._board()
                   for n in (e["home_team"], e["away_team"]) if n in fbs}
    absent = sorted(board_teams - set(view))
    print(f"\nBoard FBS teams present in the as-of stats: "
          f"{len(board_teams) - len(absent)}/{len(board_teams)}")
    if absent:
        print(f"  WARN — {len(absent)} board FBS team(s) have no as-of stat row:")
        for n in absent[:10]:
            print(f"    - {n}")
        print("  (expected early in the season for a team that has not played; "
              "the maturity flag reports this and the vetoes abstain.)")
    return failures


def check_trajectory(sport: cfb.CFB) -> list[str]:
    """The retail-extend veto must read the accumulated Supabase trajectory."""
    _hdr("3. TRAJECTORY — retail-extend veto over accumulated snapshots")
    failures: list[str] = []
    from engine import db

    fbs = sport.fbs()
    games = [g for g in sport.todays_games(None)
             if g.home in fbs and g.away in fbs]
    games.sort(key=lambda g: g.commence_time)
    if not games:
        print("  SKIP — no FBS vs FBS games on the board.")
        return failures

    now = datetime.now(timezone.utc)
    examined = with_traj = 0
    for game in games[:40]:
        snaps = db.get_snapshots("cfb", game.game_date, game.game_id)
        if not snaps:
            continue
        examined += 1
        cutoff = game.commence_time - timedelta(
            hours=cfb.RETAIL_EXTEND_CUTOFF_HOURS)
        in_window = sorted({s.taken_at for s in snaps if s.taken_at <= cutoff})
        if not in_window:
            continue
        with_traj += 1

        try:
            wk = cfb.week_of(game.commence_time, sport.calendar())
            cand = sport.build_candidates(game, snaps, {})
        except cfb.UnmappedTeamError as exc:
            failures.append(f"unmapped team on {game.game_id}")
            print(f"  FAIL — {exc}")
            continue
        if cand is None:
            continue

        fav_is_home = cand.favorite == game.home
        per_book = {
            bk: cfb._sustained_move_toward_favorite(
                cfb._book_series(snaps, bk, cutoff), fav_is_home)
            for bk in cfb.RETAIL_BOOKS
        }
        pinny = cfb._any_move_toward_favorite(
            cfb._book_series(snaps, cfb.PINNACLE, cutoff), fav_is_home)
        verdict = cfb.veto_retail_extend(
            cand, VetoContext(team_stats={}, snapshots=snaps))
        status, why = cfb.retail_extend_status(snaps, game.commence_time, now)
        moved = [b for b, m in per_book.items() if m]

        print(f"\n  {cand.favorite} {cand.entry_ml} vs {cand.dog}  "
              f"(week {wk}, kickoff {game.commence_time:%Y-%m-%d %H:%M}Z)")
        print(f"    snapshots in T-48h window : {len(in_window)}  [{status}: {why}]")
        print(f"    retail books moved        : {len(moved)}/4 "
              f"{moved if moved else ''}")
        print(f"    Pinnacle confirms move    : {pinny}")
        print(f"    -> retail-extend veto     : "
              f"{'FIRES' if verdict.fired else 'passes'}")

    print(f"\n  {examined} game(s) with accumulated snapshots; "
          f"{with_traj} with a T-48h window to read.")
    if examined == 0:
        print("  WARN — no snapshots accumulated yet. Let the CFB tick run for "
              "a few hours, then re-run this check.")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.validate_cfb")
    ap.add_argument("--skip-db", action="store_true",
                    help="skip the Supabase trajectory check (checks 1-2 only)")
    ap.add_argument("--year", type=int, default=None, help="override season year")
    args = ap.parse_args()

    sport = cfb.CFB(year=args.year)
    failures: list[str] = []
    failures += check_crosswalk(sport)
    failures += check_asof_join(sport)
    if not args.skip_db:
        failures += check_trajectory(sport)

    _hdr("RESULT")
    if failures:
        print(f"FAILED — {len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        print("\nDo NOT enable live CFB alerts until these are clear.")
        return 1
    print("All checks passed. The CFB path is safe to run live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
