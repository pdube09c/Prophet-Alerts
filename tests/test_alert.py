"""Unit tests for the stake table + alert composer (Design B §12.4).

Pure — no email is sent. `send_alert`'s wiring is checked with an injected fake
sender.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from engine.alert import AlertConfig, build_alert, send_alert
from engine.stake import recommended_stake, to_win
from sports.base import Candidate, Game

UTC = timezone.utc


# --- stake table -------------------------------------------------------------

def test_paper_recommends_zero():
    assert recommended_stake("paper") == 0.0
    assert recommended_stake("paper", liquidity=9999) == 0.0


def test_stage1_is_flat_100_regardless_of_liquidity():
    assert recommended_stake("stage1") == 100.0
    assert recommended_stake("stage1", liquidity=10) == 100.0


def test_stage2_is_capped_by_liquidity():
    assert recommended_stake("stage2", stage2_target=250) == 250.0
    assert recommended_stake("stage2", liquidity=120, stage2_target=250) == 120.0
    assert recommended_stake("stage2", liquidity=500, stage2_target=250) == 250.0


def test_unknown_stage_raises():
    with pytest.raises(ValueError):
        recommended_stake("stage9")


def test_to_win_haircut_and_zero_stake():
    # -200 favorite: base win = 100*100/200 = 50, haircut 0.98 -> 49.00
    assert to_win(100, -200) == 49.0
    assert to_win(0, -150) == 0.0


# --- alert composition -------------------------------------------------------

def _cand(ml=-150, liq=500.0):
    game = Game(sport="nba", game_id="2026-01-10:MIA@BOS", game_date="2026-01-10",
                commence_time=datetime(2026, 1, 10, 23, 40, tzinfo=UTC),
                home="Boston Celtics", away="Miami Heat")
    return Candidate(sport="nba", game=game, favorite="Boston Celtics",
                     dog="Miami Heat", entry_ml=ml, liquidity=liq,
                     entry_time_actual=game.commence_time)


def test_paper_alert_labels_and_zero_stake():
    cfg = AlertConfig(stage="paper", email_from="a@x", email_to="b@y",
                      page_url="https://page")
    msg = build_alert(_cand(), cfg)
    assert msg.subject.startswith("[PAPER]")
    assert "Boston Celtics" in msg.subject and "-150" in msg.subject
    assert "$0.00" in msg.text                     # recommended stake
    assert "does not place bets" in msg.text
    assert "https://page" in msg.text


def test_stage1_alert_shows_stake_and_to_win():
    cfg = AlertConfig(stage="stage1", email_from="a@x", email_to="b@y",
                      page_url="https://page")
    msg = build_alert(_cand(ml=-200), cfg)
    assert msg.subject.startswith("[STAGE1]")
    assert "$100.00" in msg.text
    assert "$49.00" in msg.text                    # to-win at -200 with haircut


def test_send_alert_passes_composed_message_to_sender():
    cfg = AlertConfig(stage="paper", email_from="from@x", email_to="to@y",
                      page_url="https://page")
    captured = {}

    def fake_sender(sender, to, subject, *, html, text):
        captured.update(sender=sender, to=to, subject=subject, html=html, text=text)

    send_alert(_cand(), cfg, sender=fake_sender)
    assert captured["sender"] == "from@x"
    assert captured["to"] == "to@y"
    assert captured["subject"].startswith("[PAPER]")
    assert "<h2>" in captured["html"]
