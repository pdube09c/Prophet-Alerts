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

import sys
from dataclasses import dataclass
from typing import Optional

from engine import email as _email
from engine import push as _push
from engine.stake import is_paper, recommended_stake, to_win


@dataclass(frozen=True)
class AlertConfig:
    """Non-secret alert settings from config/settings.toml."""
    stage: str
    email_from: str
    email_to: str
    page_url: str
    stage2_target: float = 250.0
    # Reference stake ladder, validated + supplied by the config loader (see
    # engine/config.py). Empty here so no stake numbers are hard-coded in this
    # module; build_alert renders the ladder section only when it's populated.
    stake_ladder: tuple = ()
    # ntfy.sh push topic, supplied by the config loader (NTFY_TOPIC / settings).
    # Empty -> the best-effort push is skipped; the email still sends.
    ntfy_topic: str = ""


@dataclass(frozen=True)
class Composed:
    subject: str
    text: str
    html: str
    # Short ntfy push, composed from the same stake/win as the email so the two
    # channels never disagree. Empty on a message composed without push fields.
    push_title: str = ""
    push_body: str = ""


def _fmt_ml(ml: int) -> str:
    return f"+{ml}" if ml > 0 else str(ml)


def _ladder_rows(stake_ladder, liquidity, ml):
    """Reference ladder rows: (target, stake, to_win, capped) for each target.
    `stake` is the target bound by posted liquidity when known (same cap rule as
    stage-2 sizing); `capped` flags rows the liquidity ceiling actually bound."""
    rows = []
    for target in stake_ladder:
        if liquidity is not None and liquidity > 0 and target > liquidity:
            stake, capped = float(liquidity), True
        else:
            stake, capped = float(target), False
        rows.append((target, stake, to_win(stake, ml), capped))
    return rows


def _ladder_text(rows, liq: str) -> list[str]:
    """Plain-text ladder — clearly labeled reference, secondary to the
    recommendation above it. No styling to lean on, so a header + a 'cap' tag
    column carry the meaning."""
    out = ["Reference ladder (informational — NOT the recommendation):",
           f"  {'Target':<9}{'Stake':<14}{'To win':<13}"]
    for target, stake, win, capped in rows:
        out.append(f"  {'$' + format(target, ',.0f'):<9}"
                   f"{'$' + format(stake, ',.2f'):<14}"
                   f"{'$' + format(win, ',.2f'):<13}"
                   f"{'cap' if capped else ''}")
    if any(r[3] for r in rows):
        out.append(f"  cap = stake limited by posted liquidity ({liq})")
    return out


def _ladder_html(rows, liq: str) -> str:
    """Plain, muted reference table — deliberately smaller/greyer than the
    recommended-stake callout so it reads as secondary context."""
    head = ('<tr style="color:#888;font-size:12px;text-align:left">'
            '<th style="padding:2px 14px 2px 0">Target</th>'
            '<th style="padding:2px 14px 2px 0;text-align:right">Stake</th>'
            '<th style="padding:2px 14px 2px 0;text-align:right">To win</th>'
            '<th style="padding:2px 0"></th></tr>')
    body = ""
    for target, stake, win, capped in rows:
        tag = ('<span style="color:#b06000;font-size:11px;font-weight:600">'
               'cap</span>') if capped else ""
        body += ('<tr>'
                 f'<td style="padding:2px 14px 2px 0">${target:,.0f}</td>'
                 f'<td style="padding:2px 14px 2px 0;text-align:right">'
                 f'${stake:,.2f}</td>'
                 f'<td style="padding:2px 14px 2px 0;text-align:right">'
                 f'${win:,.2f}</td>'
                 f'<td style="padding:2px 0">{tag}</td></tr>')
    note = (f'<p style="font-size:11px;color:#999;margin:4px 0 0">'
            f'cap = stake limited by posted liquidity ({liq}).</p>'
            if any(r[3] for r in rows) else "")
    return (
        '<h3 style="font-size:12px;color:#888;font-weight:600;'
        'text-transform:uppercase;letter-spacing:.05em;margin:18px 0 4px">'
        'Reference ladder '
        '<span style="font-weight:400;text-transform:none;letter-spacing:0">'
        '(informational — not the recommendation)</span></h3>'
        '<table style="border-collapse:collapse;color:#555;font-size:13px">'
        f'{head}{body}</table>{note}')


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

    rows = _ladder_rows(cfg.stake_ladder, cand.liquidity, cand.entry_ml)

    # The recommendation is what the reader must see first — boxed and set off
    # by blank lines so it is unmistakable in plain text. The ladder sits below.
    rec_block = [
        "==================================================",
        f"  >> RECOMMENDED STAKE ({tag}): ${stake:,.2f}",
        f"     to win ${win:,.2f}",
        "==================================================",
    ]
    ladder_block = ["", *_ladder_text(rows, liq)] if rows else []
    lines = banner_txt + [
        f"SURVIVOR — {cand.sport.upper()} ({tag})",
        "",
        f"Favorite : {cand.favorite}  ({_fmt_ml(cand.entry_ml)} ML on ProphetX)",
        f"Underdog : {cand.dog}",
        f"Tip      : {tip}",
        f"Liquidity: {liq}",
        "",
        *rec_block,
        *ladder_block,
        "",
        "This app does not place bets. Log your decision here:",
        cfg.page_url or "(selection page URL not configured)",
    ]
    text = "\n".join(lines)

    # HTML: a large green callout for the recommendation, dominant over the
    # smaller/greyer reference-ladder table beneath it.
    rec_html = (
        '<div style="margin:16px 0;padding:14px 18px;border:2px solid #0a7d00;'
        'border-radius:8px;background:#f2fff0">'
        '<div style="font-size:12px;color:#555;font-weight:600;'
        f'text-transform:uppercase;letter-spacing:.06em">Recommended stake — {tag}</div>'
        '<div style="font-size:28px;line-height:1.15;font-weight:800;'
        f'color:#0a3d00;margin-top:2px">${stake:,.2f}</div>'
        '<div style="font-size:15px;color:#333;margin-top:2px">'
        f'&rarr; to win ${win:,.2f}</div></div>'
    )
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
        f"</table>"
        + rec_html
        + (_ladder_html(rows, liq) if rows else "")
        + f"<p><i>This app does not place bets.</i></p>"
        + (f'<p><a href="{cfg.page_url}">Log your decision</a></p>'
           if cfg.page_url else "")
    )

    # ntfy push — one short line reusing the SAME stake/win as the email above,
    # so the attention-getter can never disagree with the detail channel. The
    # title stays ASCII (it rides an HTTP header); the arrow lives in the body.
    push_title = f"Prophet-Alerts: {cand.sport.upper()} survivor"
    push_body = (
        f"Survivor: {cand.favorite} {_fmt_ml(cand.entry_ml)} over {cand.dog}"
        f" | Stake ({tag}): ${stake:,.2f} → win ${win:,.2f}"
        f" | details in email")
    if marker:
        push_title = f"[{marker}] {push_title}"
        push_body = f"{marker} — {push_body}"

    return Composed(subject=subject, text=text, html=html,
                    push_title=push_title, push_body=push_body)


def _fire_push(cfg: AlertConfig, msg: Composed, pusher) -> None:
    """Fire the ntfy push, best-effort. Skipped when no topic is configured.
    ANY failure (network error, ntfy down, bad topic) is caught, logged, and
    swallowed so the push can NEVER block or crash the alert — by the time we
    are here the email has already been sent, and it is the source of truth."""
    if not cfg.ntfy_topic:
        return
    try:
        pusher(cfg.ntfy_topic, msg.push_title, msg.push_body)
    except Exception as exc:  # noqa: BLE001 — best-effort channel, never propagate
        print(f"[ntfy] push failed (email already sent): {exc!r}",
              file=sys.stderr)


def send_alert(cand, cfg: AlertConfig, *, sender=_email.send,
               pusher=_push.send, marker: Optional[str] = None) -> None:
    """Compose and send the alert. Email is the source of truth and is sent
    FIRST; the ntfy push is best-effort and fired AFTER (see _fire_push), so an
    ntfy problem can never affect email delivery. `sender`/`pusher` are injected
    for tests; `marker` tags a synthetic smoke-test message (see build_alert)."""
    msg = build_alert(cand, cfg, marker=marker)
    sender(cfg.email_from, cfg.email_to, msg.subject,
           html=msg.html, text=msg.text)
    _fire_push(cfg, msg, pusher)


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
