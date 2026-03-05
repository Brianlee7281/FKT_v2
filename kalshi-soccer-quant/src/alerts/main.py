"""Step 5.3: Alert Service — Push Notifications for Trading Events.

Subscribes to Redis events → routes alerts to Slack/Telegram based on severity.

Alert Categories (from dashboard_design.md):
  INFO     → Slack only      (position entry, daily summary)
  WARNING  → Slack only      (position exit loss, PRELIMINARY >30s)
  CRITICAL → Slack + Telegram (drawdown >10%, WS failure, health risk)

Can also be called directly by MatchScheduler/MatchEngine without Redis.

Reference: docs/blueprint.md → Process N+2: ALERT_SERVICE,
           docs/dashboard_design.md → Alert Categories
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from enum import Enum

import httpx

from src.common.config import SystemConfig
from src.common.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AlertSeverity(Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertChannel(Enum):
    SLACK = "slack"
    TELEGRAM = "telegram"


# Severity → channels routing table
ALERT_ROUTING: dict[AlertSeverity, list[AlertChannel]] = {
    AlertSeverity.INFO: [AlertChannel.SLACK],
    AlertSeverity.SUCCESS: [AlertChannel.SLACK],
    AlertSeverity.WARNING: [AlertChannel.SLACK],
    AlertSeverity.CRITICAL: [AlertChannel.SLACK, AlertChannel.TELEGRAM],
}

# Slack emoji per severity
_SLACK_EMOJI: dict[AlertSeverity, str] = {
    AlertSeverity.INFO: ":information_source:",
    AlertSeverity.SUCCESS: ":white_check_mark:",
    AlertSeverity.WARNING: ":warning:",
    AlertSeverity.CRITICAL: ":rotating_light:",
}


# ---------------------------------------------------------------------------
# Alert Message
# ---------------------------------------------------------------------------

class AlertMessage:
    """Structured alert ready for dispatch."""

    __slots__ = ("severity", "title", "body", "match_id", "timestamp")

    def __init__(
        self,
        severity: AlertSeverity,
        title: str,
        body: str,
        match_id: str | None = None,
    ):
        self.severity = severity
        self.title = title
        self.body = body
        self.match_id = match_id
        self.timestamp = datetime.now(timezone.utc)

    def to_dict(self) -> dict:
        return {
            "severity": self.severity.value,
            "title": self.title,
            "body": self.body,
            "match_id": self.match_id,
            "timestamp": self.timestamp.isoformat(),
        }

    def format_slack(self) -> dict:
        """Format as Slack webhook payload (Block Kit)."""
        emoji = _SLACK_EMOJI.get(self.severity, "")
        header = f"{emoji} *[{self.severity.value.upper()}]* {self.title}"
        if self.match_id:
            header += f"  `{self.match_id}`"

        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": header},
            },
        ]

        if self.body:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": self.body},
            })

        blocks.append({
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"_{self.timestamp.strftime('%H:%M:%S UTC')}_"},
            ],
        })

        return {"blocks": blocks}

    def format_telegram(self) -> str:
        """Format as Telegram HTML message."""
        sev = self.severity.value.upper()
        parts = [f"<b>[{sev}] {self.title}</b>"]
        if self.match_id:
            parts[0] += f"  <code>{self.match_id}</code>"
        if self.body:
            parts.append(self.body)
        parts.append(f"<i>{self.timestamp.strftime('%H:%M:%S UTC')}</i>")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Alert Service
# ---------------------------------------------------------------------------

class AlertService:
    """Subscribe to Redis events → send to Slack/Telegram.

    Can be used in two modes:
      1. Standalone: call start() to subscribe to Redis pub/sub.
      2. Direct: call send() from MatchScheduler/MatchEngine.
    """

    def __init__(
        self,
        config: SystemConfig,
        *,
        redis_client=None,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.config = config
        self._redis = redis_client
        self._http = http_client
        self._owns_http = http_client is None

        # Webhook URLs
        self._slack_webhook = config.slack_webhook
        self._telegram_bot_token = config.telegram_bot_token
        self._telegram_chat_id = config.telegram_chat_id

        # Shutdown coordination
        self._shutdown_event = asyncio.Event()

        # Alert history (ring buffer for dedup/rate limiting)
        self._recent_alerts: list[AlertMessage] = []
        self._max_history = 100

        # Rate limiting: max alerts per minute per severity
        self._rate_limits: dict[AlertSeverity, int] = {
            AlertSeverity.INFO: 30,
            AlertSeverity.SUCCESS: 30,
            AlertSeverity.WARNING: 10,
            AlertSeverity.CRITICAL: 5,
        }
        self._rate_counters: dict[AlertSeverity, list[float]] = {
            s: [] for s in AlertSeverity
        }

    async def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        return self._http

    async def close(self) -> None:
        """Release HTTP client if we own it."""
        if self._owns_http and self._http and not self._http.is_closed:
            await self._http.aclose()

    # ------------------------------------------------------------------
    # Standalone mode: Redis pub/sub listener
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to Redis channels and process alerts.

        Subscribes to:
          - "alerts" channel: direct alert messages
          - "match:*:state" pattern: state snapshots for auto-alert rules
        """
        if self._redis is None:
            log.warning("alert_service_no_redis", msg="No Redis client, running in direct-call mode only")
            return

        log.info("alert_service_starting")

        pubsub = self._redis.pubsub()
        await pubsub.subscribe("alerts")
        await pubsub.psubscribe("match:*:state")

        try:
            while not self._shutdown_event.is_set():
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message is None:
                    continue

                try:
                    data = json.loads(message["data"])
                except (json.JSONDecodeError, TypeError, KeyError):
                    continue

                channel = message.get("channel", "")
                msg_type = message.get("type", "")

                if channel == "alerts":
                    await self._handle_alert(data)
                elif msg_type == "pmessage":
                    await self._check_state_alerts(data)

        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe()
            await pubsub.punsubscribe()
            log.info("alert_service_stopped")

    async def stop(self) -> None:
        """Signal shutdown."""
        self._shutdown_event.set()
        await self.close()

    # ------------------------------------------------------------------
    # Direct API: send alerts programmatically
    # ------------------------------------------------------------------

    async def send(
        self,
        severity: AlertSeverity | str,
        title: str,
        body: str = "",
        match_id: str | None = None,
    ) -> bool:
        """Send an alert to configured channels.

        Args:
            severity: Alert severity (enum or string like "CRITICAL").
            title: Short alert title.
            body: Detailed message body.
            match_id: Optional match identifier.

        Returns:
            True if at least one channel received the alert.
        """
        if isinstance(severity, str):
            severity = AlertSeverity(severity.lower())

        alert = AlertMessage(severity, title, body, match_id)

        # Rate limiting
        if not self._check_rate_limit(severity):
            log.warning(
                "alert_rate_limited",
                severity=severity.value,
                title=title,
            )
            return False

        # Track history
        self._recent_alerts.append(alert)
        if len(self._recent_alerts) > self._max_history:
            self._recent_alerts = self._recent_alerts[-self._max_history:]

        # Route to channels
        channels = ALERT_ROUTING.get(severity, [AlertChannel.SLACK])
        sent_any = False

        for channel in channels:
            try:
                if channel == AlertChannel.SLACK:
                    sent = await self._send_slack(alert)
                elif channel == AlertChannel.TELEGRAM:
                    sent = await self._send_telegram(alert)
                else:
                    sent = False

                if sent:
                    sent_any = True
            except Exception as e:
                log.error(
                    "alert_send_error",
                    channel=channel.value,
                    error=str(e),
                )

        log.info(
            "alert_dispatched",
            severity=severity.value,
            title=title,
            match_id=match_id,
            sent=sent_any,
        )

        return sent_any

    # ------------------------------------------------------------------
    # Auto-alert rules (from state snapshots)
    # ------------------------------------------------------------------

    async def _handle_alert(self, data: dict) -> None:
        """Handle a direct alert message from the 'alerts' Redis channel."""
        severity = data.get("severity", "info")
        title = data.get("title", "Alert")
        body = data.get("body", "")
        match_id = data.get("match_id")

        await self.send(severity, title, body, match_id)

    async def _check_state_alerts(self, state: dict) -> None:
        """Check auto-alert conditions from engine state snapshots.

        Rules (from blueprint + dashboard_design):
          1. Drawdown > 10% → CRITICAL
          2. PRELIMINARY > 30s → WARNING (possible VAR)
          3. Live odds WS down → CRITICAL
          4. Live score fails 5x → CRITICAL
        """
        match_id = state.get("match_id", "")

        # Rule 1: Drawdown check
        drawdown_pct = state.get("drawdown_pct", 0)
        if drawdown_pct > 10:
            await self.send(
                AlertSeverity.CRITICAL,
                "Max Drawdown Exceeded",
                f"Drawdown {drawdown_pct:.1f}%. Review required.",
                match_id=match_id,
            )

        # Rule 2: PRELIMINARY for over 30 seconds (possible VAR)
        if state.get("event_state") == "PRELIMINARY":
            preliminary_start = state.get("preliminary_start", 0)
            if preliminary_start and time.time() - preliminary_start > 30:
                await self.send(
                    AlertSeverity.WARNING,
                    "PRELIMINARY >30s",
                    f"Possible VAR review for {match_id}.",
                    match_id=match_id,
                )

        # Rule 3: Live odds WebSocket down
        if state.get("live_odds_healthy") is False:
            await self.send(
                AlertSeverity.CRITICAL,
                "Live Odds WS Down",
                f"Live Odds WebSocket disconnected for {match_id}. Fallback to 2-layer mode.",
                match_id=match_id,
            )

        # Rule 4: Live score polling failed 5+ times consecutively
        live_score_failures = state.get("live_score_consecutive_failures", 0)
        if live_score_failures >= 5:
            await self.send(
                AlertSeverity.CRITICAL,
                "Live Score Polling Failed",
                f"Live Score polling failed {live_score_failures}x. Match {match_id} frozen.",
                match_id=match_id,
            )

    # ------------------------------------------------------------------
    # Channel senders
    # ------------------------------------------------------------------

    async def _send_slack(self, alert: AlertMessage) -> bool:
        """Post alert to Slack via incoming webhook.

        Returns True if successfully sent.
        """
        if not self._slack_webhook:
            log.debug("slack_not_configured")
            return False

        client = await self._ensure_http()
        payload = alert.format_slack()

        response = await client.post(self._slack_webhook, json=payload)
        response.raise_for_status()

        return True

    async def _send_telegram(self, alert: AlertMessage) -> bool:
        """Send alert to Telegram via Bot API.

        Returns True if successfully sent.
        """
        if not self._telegram_bot_token or not self._telegram_chat_id:
            log.debug("telegram_not_configured")
            return False

        client = await self._ensure_http()
        url = f"https://api.telegram.org/bot{self._telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self._telegram_chat_id,
            "text": alert.format_telegram(),
            "parse_mode": "HTML",
        }

        response = await client.post(url, json=payload)
        response.raise_for_status()

        return True

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _check_rate_limit(self, severity: AlertSeverity) -> bool:
        """Check if we're within rate limits for this severity.

        Uses a sliding window of 60 seconds.
        """
        now = time.monotonic()
        window = 60.0

        # Prune old entries
        timestamps = self._rate_counters[severity]
        self._rate_counters[severity] = [
            t for t in timestamps if now - t < window
        ]

        limit = self._rate_limits.get(severity, 30)
        if len(self._rate_counters[severity]) >= limit:
            return False

        self._rate_counters[severity].append(now)
        return True

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_recent_alerts(self, limit: int = 20) -> list[dict]:
        """Return recent alert history."""
        return [a.to_dict() for a in self._recent_alerts[-limit:]]

    @property
    def channels_configured(self) -> dict[str, bool]:
        """Return which channels are configured."""
        return {
            "slack": bool(self._slack_webhook),
            "telegram": bool(self._telegram_bot_token and self._telegram_chat_id),
        }
