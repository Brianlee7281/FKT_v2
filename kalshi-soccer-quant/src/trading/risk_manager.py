"""Risk Manager — 3-Layer Position Limits.

Enforces three nested risk caps to prevent ruin:
  Layer 1: Single order <= f_order_cap (3%) of bankroll
  Layer 2: Per-match exposure <= f_match_cap (5%) of bankroll
  Layer 3: Total portfolio <= f_total_cap (20%) of bankroll

Reference: phase4.md -> Step 4.3 (3-Layer Risk Limits)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.common.logging import get_logger

log = get_logger(__name__)

# Default risk parameters (from config.py)
DEFAULT_F_ORDER_CAP = 0.03  # 3%
DEFAULT_F_MATCH_CAP = 0.05  # 5%
DEFAULT_F_TOTAL_CAP = 0.20  # 20%


@dataclass
class RiskManager:
    """3-layer risk limit enforcement.

    Tracks per-match and total portfolio exposure, and clamps
    proposed order amounts to respect all three layers.

    Attributes:
        f_order_cap: Max fraction per single order.
        f_match_cap: Max fraction per match.
        f_total_cap: Max fraction for total portfolio.
        match_exposures: Current exposure per match_id (dollars).
    """

    f_order_cap: float = DEFAULT_F_ORDER_CAP
    f_match_cap: float = DEFAULT_F_MATCH_CAP
    f_total_cap: float = DEFAULT_F_TOTAL_CAP
    match_exposures: dict[str, float] = field(default_factory=dict)

    def get_match_exposure(self, match_id: str) -> float:
        """Get current exposure for a match."""
        return self.match_exposures.get(match_id, 0.0)

    def get_total_exposure(self) -> float:
        """Get total portfolio exposure across all matches."""
        return sum(self.match_exposures.values())

    def record_exposure(self, match_id: str, amount: float) -> None:
        """Record additional exposure for a match.

        Args:
            match_id: Match identifier.
            amount: Dollar amount of new exposure.
        """
        current = self.match_exposures.get(match_id, 0.0)
        self.match_exposures[match_id] = current + amount

    def remove_exposure(self, match_id: str, amount: float) -> None:
        """Remove exposure when a position is closed.

        Args:
            match_id: Match identifier.
            amount: Dollar amount to remove.
        """
        current = self.match_exposures.get(match_id, 0.0)
        self.match_exposures[match_id] = max(0.0, current - amount)
        if self.match_exposures[match_id] <= 0:
            del self.match_exposures[match_id]

    def apply_risk_limits(
        self, f_invest: float, match_id: str, bankroll: float
    ) -> float:
        """Apply 3-layer risk limits to proposed investment.

        Args:
            f_invest: Proposed investment fraction from Kelly.
            match_id: Match identifier for Layer 2 check.
            bankroll: Current bankroll in dollars.

        Returns:
            Clamped dollar amount that respects all three layers.
        """
        if bankroll <= 0 or f_invest <= 0:
            return 0.0

        amount = f_invest * bankroll

        # Layer 1: single order cap
        order_cap = bankroll * self.f_order_cap
        amount = min(amount, order_cap)

        # Layer 2: per-match cap
        current_match = self.get_match_exposure(match_id)
        remaining_match = bankroll * self.f_match_cap - current_match
        amount = min(amount, max(0.0, remaining_match))

        # Layer 3: total portfolio cap
        total_exposure = self.get_total_exposure()
        remaining_total = bankroll * self.f_total_cap - total_exposure
        amount = min(amount, max(0.0, remaining_total))

        proposed = f_invest * bankroll
        if amount < proposed * 0.01:  # effectively zero after clamping
            log.debug(
                "risk_limit_blocked",
                match_id=match_id,
                proposed=proposed,
                clamped=amount,
            )

        return max(0.0, amount)

    def reset(self) -> None:
        """Clear all tracked exposures (e.g., end of day)."""
        self.match_exposures.clear()
