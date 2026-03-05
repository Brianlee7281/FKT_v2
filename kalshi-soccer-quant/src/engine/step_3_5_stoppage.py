"""Step 3.5: Real-Time Stoppage-Time Handling.

Adjusts T_exp in real time using dual-source cross-validation
(Live Odds WebSocket <1s + Live Score REST 3-8s).

Three phases:
  A. Regular time (minute ≤ 45 in 1H, ≤ 90 in 2H): T = T_exp
  B. First-half stoppage (minute > 45, period = 1H): T unchanged,
     first-half end determined by halftime event
  C. Second-half stoppage (minute > 90, period = 2H): T = minute + rolling_horizon

Reference: phase3.md → Step 3.5
"""

from __future__ import annotations

from src.common.logging import get_logger

log = get_logger(__name__)

# Default rolling horizon for second-half stoppage (minutes)
DEFAULT_ROLLING_HORIZON = 1.5

# Minute divergence threshold for cross-validation warning
MINUTE_MISMATCH_THRESHOLD = 2.0


class StoppageTimeManager:
    """Manages real-time T_exp adjustment using dual-source minute data."""

    def __init__(
        self,
        T_exp: float,
        rolling_horizon: float = DEFAULT_ROLLING_HORIZON,
    ):
        self.T_exp = T_exp
        self.rolling_horizon = rolling_horizon
        self.first_half_stoppage = False
        self.second_half_stoppage = False
        self._lo_minute: float | None = None  # Live Odds minute
        self._ls_minute: float | None = None  # Live Score minute

    def update_from_live_odds(self, minute: float, period: str) -> float:
        """Update from Live Odds WebSocket — faster updates (<1s).

        Args:
            minute: Current match minute (e.g., 93.0 for 90+3).
            period: Current period string (e.g., "1st Half", "2nd Half").

        Returns:
            Updated T value.
        """
        self._lo_minute = minute
        return self._compute_T(minute, period)

    def update_from_live_score(self, minute: float, period: str) -> float:
        """Update from Live Score REST — authoritative updates (3-8s).

        Args:
            minute: Current match minute.
            period: Current period string.

        Returns:
            Updated T value.
        """
        self._ls_minute = minute

        # Cross-validation: warn if sources diverge by 2+ minutes
        if (self._lo_minute is not None
                and abs(self._lo_minute - minute) > MINUTE_MISMATCH_THRESHOLD):
            log.warning(
                "minute_mismatch",
                live_odds=self._lo_minute,
                live_score=minute,
                delta=abs(self._lo_minute - minute),
            )

        return self._compute_T(minute, period)

    def _compute_T(self, minute: float, period: str) -> float:
        """Compute effective T based on current minute and period.

        Phase A: Regular time → T_exp unchanged.
        Phase B: First-half stoppage → T_exp unchanged
                 (first-half end is determined by halftime event).
        Phase C: Second-half stoppage → T = minute + rolling_horizon.
        """
        # Phase B: first-half stoppage
        if period in ("1st Half", "1st") and minute > 45:
            if not self.first_half_stoppage:
                self.first_half_stoppage = True
                log.info("first_half_stoppage_entered", minute=minute)
            return self.T_exp

        # Phase C: second-half stoppage
        if period in ("2nd Half", "2nd") and minute > 90:
            if not self.second_half_stoppage:
                self.second_half_stoppage = True
                log.info("second_half_stoppage_entered", minute=minute)
            T_rolling = minute + self.rolling_horizon
            if T_rolling > self.T_exp:
                self.T_exp = T_rolling
            return self.T_exp

        # Phase A: regular time
        return self.T_exp

    @property
    def current_T(self) -> float:
        """Current effective T value."""
        return self.T_exp

    def reset(self, T_exp: float) -> None:
        """Reset for a new match."""
        self.T_exp = T_exp
        self.first_half_stoppage = False
        self.second_half_stoppage = False
        self._lo_minute = None
        self._ls_minute = None
