"""Runs a sport's veto layers over candidates (Design B §4).

A candidate is vetoed if ANY layer fires. `evaluate_all` returns every layer
that fired (not short-circuited) so the audit trail (`vetoed` table) can record
the full reason string, exactly as the backtest did.
"""

from __future__ import annotations

from sports.base import Candidate, VetoContext, VetoLayer


def evaluate_all(layers: list[VetoLayer], cand: Candidate, ctx: VetoContext) -> list[str]:
    """Return the ordered list of layer names that fired for this candidate."""
    return [layer.name for layer in layers if layer.evaluate(cand, ctx).fired]


def is_vetoed(layers: list[VetoLayer], cand: Candidate, ctx: VetoContext) -> bool:
    """A candidate survives only if no layer fires."""
    return any(layer.evaluate(cand, ctx).fired for layer in layers)
