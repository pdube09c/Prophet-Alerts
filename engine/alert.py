"""Compose + send a survivor alert (Design B §12.4).

An alert is a factual heads-up on a candidate that survived every veto layer:
who the favorite is, the live price and liquidity, the tip time, the recommended
stake for the current stage, and a link to the selection page where the user
logs whether (and how much) they actually bet. The app NEVER places the bet —
this message is the whole of its outward action (rule #1).

`build_alert` is pure (composes subject/text/html from a candidate + config) so
it is unit-tested without sending anything; `send_alert` wires it to email.send.
It is the `alert_fn` the tick injects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from engine import email as _email
from engine.stake import is_paper, recommended_stake, to_win


@dataclass(frozen=True)
class AlertConfig:
    """Non-secret alert settings from config/settings.toml."""
    stage: str
    email_from: str
    email_to: str
    page_url: str
    stage2_target: float = 250.0


@dataclass(frozen=True)
class Composed:
    subject: str
    text: str
    html: str


def _fmt_ml(ml: int) -> str:
    return f"+{ml}" if ml > 0 else str(ml)


def build_alert(cand, cfg: AlertConfig, *, marker: Optional[str] = None) -> Composed:
    """Compose the alert message for one surviving candidate. Pure.

    `marker` (e.g. "SMOKE TEST") prefixes the subject and adds a banner to the
    body so a synthetic message can never be mistaken for a real selection;
    marker=None (the default) composes a normal alert unchanged.
    """
    stake = recommended_stake(cfg.stage, liquidity=cand.liquidity,
                              stage2_target=cfg.stage2_target)
    win = to_win(stake, cand.entry_ml)
    tag = "PAPER" if is_paper(cfg.stage) else cfg.stage.upper()
    tip = cand.game.commence_time.strftime("%Y-%m-%d %H:%M UTC")
    liq = f"${cand.liquidity:,.0f}" if cand.liquidity is not None else "n/a"

    subject = f"[{tag}] {cand.favorite} {_fmt_ml(cand.entry_ml)} vs {cand.dog}"
    banner_txt: list[str] = []
    banner_html = ""
    if marker:
        subject = f"[{marker}] {subject}"
        banner_txt = [f"*** {marker} — synthetic candidate, NOT a real "
                      f"selection. Do not bet. ***", ""]
        banner_html = (f'<p style="color:#b00000"><b>{marker}</b> — synthetic '
                       f"candidate, NOT a real selection. Do not bet.</p>")

    lines = banner_txt + [
        f"SURVIVOR — {cand.sport.upper()} ({tag})",
        "",
        f"Favorite : {cand.favorite}  ({_fmt_ml(cand.entry_ml)} ML on ProphetX)",
        f"Underdog : {cand.dog}",
        f"Tip      : {tip}",
        f"Liquidity: {liq}",
        "",
        f"Recommended stake: ${stake:,.2f}  ->  to win ${win:,.2f}",
        "",
        "This app does not place bets. Log your decision here:",
        cfg.page_url or "(selection page URL not configured)",
    ]
    text = "\n".join(lines)
    html = (
        banner_html
        + f"<h2>Survivor — {cand.sport.upper()} "
        f"<small>({tag})</small></h2>"
        f"<table>"
        f"<tr><td><b>Favorite</b></td><td>{cand.favorite} "
        f"({_fmt_ml(cand.entry_ml)} ML on ProphetX)</td></tr>"
        f"<tr><td><b>Underdog</b></td><td>{cand.dog}</td></tr>"
        f"<tr><td><b>Tip</b></td><td>{tip}</td></tr>"
        f"<tr><td><b>Liquidity</b></td><td>{liq}</td></tr>"
        f"<tr><td><b>Recommended stake</b></td>"
        f"<td>${stake:,.2f} &rarr; to win ${win:,.2f}</td></tr>"
        f"</table>"
        f"<p><i>This app does not place bets.</i></p>"
        + (f'<p><a href="{cfg.page_url}">Log your decision</a></p>'
           if cfg.page_url else "")
    )
    return Composed(subject=subject, text=text, html=html)


def send_alert(cand, cfg: AlertConfig, *, sender=_email.send,
               marker: Optional[str] = None) -> None:
    """Compose and send. `sender` is injected for tests; `marker` tags a
    synthetic smoke-test message (see build_alert)."""
    msg = build_alert(cand, cfg, marker=marker)
    sender(cfg.email_from, cfg.email_to, msg.subject,
           html=msg.html, text=msg.text)


# --- smoke test --------------------------------------------------------------
# `python -m engine.alert --smoke [--stage STAGE]` forces one sample alert
# through the REAL SendGrid path using live config, so the whole compose ->
# send -> inbox chain can be verified on demand, independent of the schedule
# and the DB. --stage overrides ONLY the stake calc for that one run (in
# memory) so stage1/stage2 sizing can be seen without touching the real STAGE.

def _smoke_candidate():
    """A synthetic, obviously-fake survivor. Built in memory — this path never
    reads from or writes to Supabase, isolating the email path from the DB."""
    from datetime import datetime, timezone
    from sports.base import Candidate, Game

    # Fixed, plainly-not-real tip time + fake team names so it can never be
    # confused with a live selection on a real slate.
    tip = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    game = Game(sport="nba", game_id="SMOKE-TEST:TST@TST",
                game_date="2026-01-01", commence_time=tip,
                home="Test Favorite", away="Test Underdog")
    return Candidate(sport="nba", game=game, favorite="Test Favorite",
                     dog="Test Underdog", entry_ml=-150, liquidity=1234.0,
                     entry_time_actual=tip)


_SMOKE_STAGES = ("paper", "stage1", "stage2")


def _smoke(stage_override: Optional[str] = None) -> None:
    """Send one [SMOKE TEST] alert via SendGrid using the live alert config
    (STAGE/EMAIL_FROM/EMAIL_TO/... from the env or settings.toml).

    `stage_override` swaps ONLY the stage used for the stake calc in this run,
    via an in-memory dataclasses.replace on the config — it is never persisted,
    writes no DB, and does not change the real STAGE Variable/env. Defaults to
    the live config's stage when None.
    """
    from dataclasses import replace
    from engine.config import alert_config  # local import: avoids import cycle

    cfg = alert_config()
    if stage_override is not None:
        norm = stage_override.strip().lower()
        if norm not in _SMOKE_STAGES:
            raise SystemExit(
                f"--stage must be one of {'|'.join(_SMOKE_STAGES)}; "
                f"got {stage_override!r}")
        cfg = replace(cfg, stage=norm)  # in-memory only; real STAGE untouched

    send_alert(_smoke_candidate(), cfg, marker="SMOKE TEST")
    print(f"[SMOKE TEST] alert sent to {cfg.email_to!r} "
          f"(stage={cfg.stage!r}, from={cfg.email_from!r})")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m engine.alert",
        description="Force a synthetic [SMOKE TEST] alert through SendGrid.")
    parser.add_argument("--smoke", action="store_true",
                        help="send one synthetic alert via the real email path")
    parser.add_argument("--stage", metavar="STAGE", default=None,
                        help="override the stake-calc stage for this run only "
                             "(paper|stage1|stage2); defaults to live config")
    args = parser.parse_args()

    if not args.smoke:
        parser.error("nothing to do — pass --smoke")
    _smoke(stage_override=args.stage)
