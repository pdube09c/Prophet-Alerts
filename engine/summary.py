"""Morning results summary (Design B §12.6).

Composes and emails the prior day's outcome: the record and P&L on the bets you
logged, plus the veto audit (how the games we vetoed actually turned out — the
running check on whether the veto layers earn their keep). Runs after settle.py
in .github/workflows/morning-summary.yml.

`build_summary` is pure (settled rows -> subject/text/html) so it's unit-tested
without sending; `run` fetches from the DB and sends via email.send.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine import config, db
from engine import email as _email
from engine.stake import is_paper


@dataclass(frozen=True)
class Composed:
    subject: str
    text: str
    html: str


def _money(v: float) -> str:
    return ("-$" if v < 0 else "$") + f"{abs(v):,.2f}"


def build_summary(date: str, stage: str, settled_bets: list[dict],
                  vetoed: list[dict]) -> Composed:
    """Compose the summary from graded bets + settled vetoed rows. Pure."""
    placed = [b for b in settled_bets if b.get("placed")]
    wins = sum(1 for b in placed if b["result"] == "win")
    losses = sum(1 for b in placed if b["result"] == "loss")
    pnl = sum((b.get("net_pnl") or 0.0) for b in placed)

    # Veto audit: a vetoed favorite that then WON is a veto that cost us; one
    # that LOST is a veto that saved us. Only count settled vetoed rows.
    settled_vetoes = [v for v in vetoed if v.get("favwin_actual") is not None]
    veto_saved = sum(1 for v in settled_vetoes if not v["favwin_actual"])
    veto_cost = sum(1 for v in settled_vetoes if v["favwin_actual"])

    tag = "PAPER" if is_paper(stage) else stage.upper()
    subject = (f"[{tag}] {date} — {wins}-{losses}, {_money(pnl)}"
               if placed else f"[{tag}] {date} — no bets placed")

    lines = [f"Morning summary — {date} ({tag})", ""]
    if placed:
        lines.append(f"Record: {wins}-{losses}   Net P&L: {_money(pnl)}")
        lines.append("")
        for b in placed:
            lines.append(
                f"  {b['result'].upper():4}  {b['favorite']} {b['entry_ml']}  "
                f"(${b['stake_chosen']:,.0f}) -> {_money(b.get('net_pnl') or 0.0)}")
    else:
        lines.append("No bets were placed.")
    lines += ["", f"Veto audit: {len(settled_vetoes)} vetoed favorites settled — "
              f"{veto_saved} lost (veto saved us), {veto_cost} won (veto cost us)."]
    text = "\n".join(lines)

    bet_rows = "".join(
        f"<tr><td>{b['result'].upper()}</td><td>{b['favorite']} {b['entry_ml']}</td>"
        f"<td>${b['stake_chosen']:,.0f}</td><td>{_money(b.get('net_pnl') or 0.0)}</td></tr>"
        for b in placed)
    html = (
        f"<h2>Morning summary — {date} <small>({tag})</small></h2>"
        + (f"<p><b>Record:</b> {wins}-{losses} &nbsp; "
           f"<b>Net P&amp;L:</b> {_money(pnl)}</p>"
           f"<table><tr><th>Result</th><th>Favorite</th><th>Stake</th>"
           f"<th>P&amp;L</th></tr>{bet_rows}</table>"
           if placed else "<p>No bets were placed.</p>")
        + f"<p><b>Veto audit:</b> {len(settled_vetoes)} settled — "
          f"{veto_saved} lost (saved), {veto_cost} won (cost).</p>")
    return Composed(subject=subject, text=text, html=html)


def run(sport_key: str = "nba", game_date: str | None = None, *, sender=None) -> dict:
    from engine.settle import _yesterday_et
    date = game_date or _yesterday_et()
    cfg = config.alert_config()
    settled = db.get_settled_bets(sport_key, date)
    vetoed = db.get_vetoed(sport_key, date)
    msg = build_summary(date, cfg.stage, settled, vetoed)
    send = sender or _email.send
    send(cfg.email_from, cfg.email_to, msg.subject, html=msg.html, text=msg.text)
    return {"game_date": date, "bets": len(settled), "vetoed": len(vetoed)}


if __name__ == "__main__":
    import json
    print(json.dumps(run(), indent=2))
