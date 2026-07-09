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


def build_alert(cand, cfg: AlertConfig) -> Composed:
    """Compose the alert message for one surviving candidate. Pure."""
    stake = recommended_stake(cfg.stage, liquidity=cand.liquidity,
                              stage2_target=cfg.stage2_target)
    win = to_win(stake, cand.entry_ml)
    tag = "PAPER" if is_paper(cfg.stage) else cfg.stage.upper()
    tip = cand.game.commence_time.strftime("%Y-%m-%d %H:%M UTC")
    liq = f"${cand.liquidity:,.0f}" if cand.liquidity is not None else "n/a"

    subject = f"[{tag}] {cand.favorite} {_fmt_ml(cand.entry_ml)} vs {cand.dog}"
    lines = [
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
        f"<h2>Survivor — {cand.sport.upper()} "
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


def send_alert(cand, cfg: AlertConfig, *, sender=_email.send) -> None:
    """Compose and send. `sender` is injected for tests."""
    msg = build_alert(cand, cfg)
    sender(cfg.email_from, cfg.email_to, msg.subject,
           html=msg.html, text=msg.text)
