"""Unit tests for the stake table + alert composer (Design B §12.4).

Pure — no email is sent. `send_alert`'s wiring is checked with an injected fake
sender.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from engine.alert import AlertConfig, build_alert, send_alert
from engine.config import DEFAULT_STAKE_LADDER, _validate_stake_ladder
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


# --- reference stake ladder --------------------------------------------------

def _ladder_cfg(stage="stage1"):
    return AlertConfig(stage=stage, email_from="a@x", email_to="b@y",
                       page_url="https://page", stake_ladder=DEFAULT_STAKE_LADDER)


def test_ladder_omitted_when_unconfigured():
    # Default AlertConfig has an empty ladder -> no ladder section at all.
    cfg = AlertConfig(stage="paper", email_from="a@x", email_to="b@y",
                      page_url="https://page")
    msg = build_alert(_cand(), cfg)
    assert "Reference ladder" not in msg.text
    assert "Reference ladder" not in msg.html


def test_recommendation_precedes_and_is_distinct_from_ladder():
    # Hierarchy must survive in BOTH text and html: the recommendation comes
    # first and is not part of the ladder.
    msg = build_alert(_cand(liq=1234.0), _ladder_cfg())
    assert msg.text.index("RECOMMENDED STAKE") < msg.text.index("Reference ladder")
    assert (msg.html.index("Recommended stake")
            < msg.html.index("Reference ladder"))
    assert "not the recommendation" in msg.html


def test_ladder_caps_stake_at_liquidity_and_tags_row():
    # liquidity 1234 -> targets above it are capped to 1234 and flagged.
    msg = build_alert(_cand(ml=-150, liq=1234.0), _ladder_cfg())
    assert "$1,000.00" in msg.text          # 1000 target uncapped
    assert "$1,234.00" in msg.text          # 2000+ targets capped to liquidity
    assert "cap" in msg.text
    # to-win uses the same 0.98 ML haircut: 1000 @ -150 -> 653.33
    assert "$653.33" in msg.text


def test_ladder_uncapped_when_no_liquidity():
    msg = build_alert(_cand(ml=-150, liq=None), _ladder_cfg())
    assert "$5,000.00" in msg.text          # full target shown, no cap
    assert "cap =" not in msg.text          # no cap footnote


@pytest.mark.parametrize("bad", [
    "100,500",          # string, not a list
    [],                 # empty
    [100, -5],          # negative
    [500, 100],         # not ascending
    [100, 100],         # not strictly ascending
    [100, "x"],         # non-number entry
    [True, 100],        # bool is not an allowed number
])
def test_validate_stake_ladder_rejects_malformed(bad):
    with pytest.raises(ValueError):
        _validate_stake_ladder(bad)


def test_validate_stake_ladder_accepts_and_normalizes():
    assert _validate_stake_ladder([100, 500, 1000]) == (100.0, 500.0, 1000.0)
    assert _validate_stake_ladder(list(DEFAULT_STAKE_LADDER)) == DEFAULT_STAKE_LADDER
