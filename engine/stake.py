"""Stake table + payout math (Design B §12.4).

Pure functions — no IO — so they're unit-tested directly and reused by both the
alert composer and the settlement math.

The staged rollout (§11) sets how much the app *recommends* (it never places
the bet — rule #1). The user always decides the actual stake on the selection
page:

  paper   (Stage 0)  -> $0  recommended; alerts labeled PAPER.
  stage1             -> flat $100.
  stage2             -> a configured scaled target (settings [nba] stake_target),
                        capped by the book's posted liquidity when known.

to-win uses the exchange payout haircut: 0.98 * stake * 100/|ml| for a favorite
priced at a negative American moneyline.
"""

from __future__ import annotations

from typing import Optional

PAYOUT_HAIRCUT = 0.98          # exchange fee on winnings (matches sports.nba)
STAGE1_FLAT = 100.0
DEFAULT_STAGE2_TARGET = 250.0


def to_win(stake: float, ml: int) -> float:
    """Profit if the favorite wins, net of the exchange haircut. ml is the
    (negative) American moneyline the favorite is laid at."""
    if stake <= 0:
        return 0.0
    return round(PAYOUT_HAIRCUT * stake * 100.0 / abs(ml), 2)


def recommended_stake(stage: str, *, liquidity: Optional[float] = None,
                      stage2_target: float = DEFAULT_STAGE2_TARGET) -> float:
    """Recommended stake for the current rollout stage.

    Stage 2 is capped by posted liquidity when the book reports a limit; paper
    and stage 1 are fixed regardless of liquidity.
    """
    if stage == "paper":
        return 0.0
    if stage == "stage1":
        return STAGE1_FLAT
    if stage == "stage2":
        if liquidity is not None and liquidity > 0:
            return round(min(stage2_target, liquidity), 2)
        return stage2_target
    raise ValueError(f"unknown stage: {stage!r}")


def is_paper(stage: str) -> bool:
    return stage == "paper"
