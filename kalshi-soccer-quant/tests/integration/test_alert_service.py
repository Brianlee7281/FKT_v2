"""Tests for Step 5.3: Alert Service.

Verifies alert routing, formatting, rate limiting, auto-alert rules,
and integration with MatchScheduler.

Reference: implementation_roadmap.md -> Step 5.3
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.alerts.main import (
    ALERT_ROUTING,
    AlertChannel,
    AlertMessage,
    AlertService,
    AlertSeverity,
)
from src.common.config import SystemConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config():
    return SystemConfig(
        trading_mode="paper",
        initial_bankroll=10000.0,
        active_markets=["over_25"],
        target_leagues=["1204", "1399"],
        fee_rate=0.07,
        K_frac=0.25,
        z=1.645,
        theta_entry=0.02,
        f_order_cap=0.03,
        f_match_cap=0.05,
        f_total_cap=0.20,
        live_score_poll_interval=0.01,
        goalserve_api_key="test_key",
        slack_webhook="",
        telegram_bot_token="",
        telegram_chat_id="",
    )


@pytest.fixture
def config_with_slack():
    return SystemConfig(
        trading_mode="paper",
        initial_bankroll=10000.0,
        active_markets=["over_25"],
        target_leagues=["1204"],
        fee_rate=0.07,
        K_frac=0.25,
        z=1.645,
        theta_entry=0.02,
        f_order_cap=0.03,
        f_match_cap=0.05,
        f_total_cap=0.20,
        live_score_poll_interval=0.01,
        goalserve_api_key="test_key",
        slack_webhook="https://hooks.slack.com/services/TEST/WEBHOOK",
        telegram_bot_token="",
        telegram_chat_id="",
    )


@pytest.fixture
def config_with_all():
    return SystemConfig(
        trading_mode="paper",
        initial_bankroll=10000.0,
        active_markets=["over_25"],
        target_leagues=["1204"],
        fee_rate=0.07,
        K_frac=0.25,
        z=1.645,
        theta_entry=0.02,
        f_order_cap=0.03,
        f_match_cap=0.05,
        f_total_cap=0.20,
        live_score_poll_interval=0.01,
        goalserve_api_key="test_key",
        slack_webhook="https://hooks.slack.com/services/TEST/WEBHOOK",
        telegram_bot_token="123456:ABC-DEF",
        telegram_chat_id="-1001234567890",
    )


class FakeHttpResponse:
    """Minimal httpx.Response substitute."""
    status_code = 200
    def raise_for_status(self):
        pass


class FakeHttpClient:
    """Records HTTP calls instead of sending them."""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.is_closed = False

    async def post(self, url: str, json: dict = None, **kwargs) -> FakeHttpResponse:
        self.posts.append((url, json))
        return FakeHttpResponse()

    async def aclose(self):
        self.is_closed = True


# ---------------------------------------------------------------------------
# AlertMessage formatting
# ---------------------------------------------------------------------------

class TestAlertMessage:
    """Test alert message formatting."""

    def test_to_dict(self):
        alert = AlertMessage(AlertSeverity.CRITICAL, "Test Title", "Test body", "m1")
        d = alert.to_dict()
        assert d["severity"] == "critical"
        assert d["title"] == "Test Title"
        assert d["body"] == "Test body"
        assert d["match_id"] == "m1"
        assert "timestamp" in d

    def test_format_slack(self):
        alert = AlertMessage(AlertSeverity.WARNING, "Drawdown Alert", "DD at 12%", "ARS-CHE")
        payload = alert.format_slack()
        assert "blocks" in payload
        blocks = payload["blocks"]
        assert len(blocks) == 3  # header, body, context
        assert "WARNING" in blocks[0]["text"]["text"]
        assert "ARS-CHE" in blocks[0]["text"]["text"]
        assert "DD at 12%" in blocks[1]["text"]["text"]

    def test_format_slack_no_body(self):
        alert = AlertMessage(AlertSeverity.INFO, "Engine spawned", "")
        payload = alert.format_slack()
        # No body block when body is empty
        assert len(payload["blocks"]) == 2

    def test_format_telegram(self):
        alert = AlertMessage(AlertSeverity.CRITICAL, "WS Down", "Reconnecting", "m1")
        text = alert.format_telegram()
        assert "<b>[CRITICAL] WS Down</b>" in text
        assert "<code>m1</code>" in text
        assert "Reconnecting" in text

    def test_format_telegram_no_match_id(self):
        alert = AlertMessage(AlertSeverity.INFO, "Daily Summary", "4 matches")
        text = alert.format_telegram()
        assert "<b>[INFO] Daily Summary</b>" in text
        assert "<code>" not in text


# ---------------------------------------------------------------------------
# Alert Routing
# ---------------------------------------------------------------------------

class TestAlertRouting:
    """Test severity → channel routing."""

    def test_info_routes_to_slack_only(self):
        channels = ALERT_ROUTING[AlertSeverity.INFO]
        assert channels == [AlertChannel.SLACK]

    def test_warning_routes_to_slack_only(self):
        channels = ALERT_ROUTING[AlertSeverity.WARNING]
        assert channels == [AlertChannel.SLACK]

    def test_critical_routes_to_slack_and_telegram(self):
        channels = ALERT_ROUTING[AlertSeverity.CRITICAL]
        assert AlertChannel.SLACK in channels
        assert AlertChannel.TELEGRAM in channels

    def test_success_routes_to_slack_only(self):
        channels = ALERT_ROUTING[AlertSeverity.SUCCESS]
        assert channels == [AlertChannel.SLACK]


# ---------------------------------------------------------------------------
# AlertService.send()
# ---------------------------------------------------------------------------

class TestAlertServiceSend:
    """Test send() dispatching."""

    @pytest.mark.asyncio
    async def test_send_no_channels_configured(self, config):
        """send() returns False when no channels are configured."""
        service = AlertService(config)
        result = await service.send(AlertSeverity.INFO, "Test")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_slack_only(self, config_with_slack):
        """INFO alert goes to Slack only."""
        http = FakeHttpClient()
        service = AlertService(config_with_slack, http_client=http)

        result = await service.send(AlertSeverity.INFO, "Engine spawned", "Match m1", match_id="m1")

        assert result is True
        assert len(http.posts) == 1
        url, payload = http.posts[0]
        assert "slack.com" in url
        assert "blocks" in payload

    @pytest.mark.asyncio
    async def test_send_critical_both_channels(self, config_with_all):
        """CRITICAL alert goes to Slack + Telegram."""
        http = FakeHttpClient()
        service = AlertService(config_with_all, http_client=http)

        result = await service.send(AlertSeverity.CRITICAL, "Drawdown exceeded", "DD at 15%")

        assert result is True
        assert len(http.posts) == 2
        urls = [p[0] for p in http.posts]
        assert any("slack.com" in u for u in urls)
        assert any("telegram.org" in u for u in urls)

    @pytest.mark.asyncio
    async def test_send_string_severity(self, config_with_slack):
        """send() accepts string severity."""
        http = FakeHttpClient()
        service = AlertService(config_with_slack, http_client=http)

        result = await service.send("warning", "Test warning")
        assert result is True
        assert len(http.posts) == 1

    @pytest.mark.asyncio
    async def test_send_records_history(self, config_with_slack):
        """Sent alerts are recorded in history."""
        http = FakeHttpClient()
        service = AlertService(config_with_slack, http_client=http)

        await service.send(AlertSeverity.INFO, "Alert 1")
        await service.send(AlertSeverity.WARNING, "Alert 2")

        history = service.get_recent_alerts()
        assert len(history) == 2
        assert history[0]["title"] == "Alert 1"
        assert history[1]["title"] == "Alert 2"

    @pytest.mark.asyncio
    async def test_send_handles_http_error(self, config_with_slack):
        """send() handles HTTP errors gracefully."""
        class FailingHttpClient:
            is_closed = False
            async def post(self, url, json=None, **kwargs):
                raise ConnectionError("Network error")
            async def aclose(self):
                pass

        service = AlertService(config_with_slack, http_client=FailingHttpClient())
        result = await service.send(AlertSeverity.INFO, "Test")

        # Should not raise, returns False
        assert result is False


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    """Test alert rate limiting."""

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_excess(self, config_with_slack):
        """Exceeding rate limit returns False."""
        http = FakeHttpClient()
        service = AlertService(config_with_slack, http_client=http)

        # CRITICAL limit is 5/minute
        for i in range(5):
            result = await service.send(AlertSeverity.CRITICAL, f"Alert {i}")
            assert result is True

        # 6th should be rate limited
        result = await service.send(AlertSeverity.CRITICAL, "Alert 6")
        assert result is False

        # Only 5 HTTP calls made
        assert len(http.posts) == 5

    @pytest.mark.asyncio
    async def test_different_severities_independent(self, config_with_slack):
        """Rate limits are independent per severity."""
        http = FakeHttpClient()
        service = AlertService(config_with_slack, http_client=http)

        # Fill up CRITICAL (limit 5)
        for i in range(5):
            await service.send(AlertSeverity.CRITICAL, f"Critical {i}")

        # INFO should still work (limit 30)
        result = await service.send(AlertSeverity.INFO, "Info alert")
        assert result is True


# ---------------------------------------------------------------------------
# Auto-alert rules (state snapshot checks)
# ---------------------------------------------------------------------------

class TestAutoAlertRules:
    """Test _check_state_alerts auto-alert conditions."""

    @pytest.mark.asyncio
    async def test_drawdown_alert(self, config_with_slack):
        """Drawdown > 10% triggers CRITICAL alert."""
        http = FakeHttpClient()
        service = AlertService(config_with_slack, http_client=http)

        state = {
            "match_id": "ARS-CHE",
            "drawdown_pct": 12.5,
        }
        await service._check_state_alerts(state)

        assert len(http.posts) == 1
        url, payload = http.posts[0]
        # Check the alert content
        blocks_text = json.dumps(payload)
        assert "Drawdown" in blocks_text

    @pytest.mark.asyncio
    async def test_no_drawdown_alert_below_threshold(self, config_with_slack):
        """Drawdown <= 10% does not trigger alert."""
        http = FakeHttpClient()
        service = AlertService(config_with_slack, http_client=http)

        state = {"match_id": "m1", "drawdown_pct": 8.0}
        await service._check_state_alerts(state)

        assert len(http.posts) == 0

    @pytest.mark.asyncio
    async def test_preliminary_var_alert(self, config_with_slack):
        """PRELIMINARY > 30s triggers WARNING."""
        http = FakeHttpClient()
        service = AlertService(config_with_slack, http_client=http)

        state = {
            "match_id": "LIV-MCI",
            "event_state": "PRELIMINARY",
            "preliminary_start": time.time() - 45,  # 45 seconds ago
        }
        await service._check_state_alerts(state)

        assert len(http.posts) == 1
        blocks_text = json.dumps(http.posts[0][1])
        assert "PRELIMINARY" in blocks_text

    @pytest.mark.asyncio
    async def test_preliminary_under_30s_no_alert(self, config_with_slack):
        """PRELIMINARY < 30s does not trigger alert."""
        http = FakeHttpClient()
        service = AlertService(config_with_slack, http_client=http)

        state = {
            "match_id": "m1",
            "event_state": "PRELIMINARY",
            "preliminary_start": time.time() - 10,  # only 10 seconds
        }
        await service._check_state_alerts(state)

        assert len(http.posts) == 0

    @pytest.mark.asyncio
    async def test_live_odds_ws_down_alert(self, config_with_slack):
        """Live odds WS down triggers CRITICAL."""
        http = FakeHttpClient()
        service = AlertService(config_with_slack, http_client=http)

        state = {
            "match_id": "BAR-RMA",
            "live_odds_healthy": False,
        }
        await service._check_state_alerts(state)

        assert len(http.posts) == 1
        blocks_text = json.dumps(http.posts[0][1])
        assert "Live Odds" in blocks_text

    @pytest.mark.asyncio
    async def test_live_score_fails_5x_alert(self, config_with_slack):
        """Live score polling failed 5x triggers CRITICAL."""
        http = FakeHttpClient()
        service = AlertService(config_with_slack, http_client=http)

        state = {
            "match_id": "ARS-CHE",
            "live_score_consecutive_failures": 5,
        }
        await service._check_state_alerts(state)

        assert len(http.posts) == 1
        blocks_text = json.dumps(http.posts[0][1])
        assert "Live Score" in blocks_text

    @pytest.mark.asyncio
    async def test_live_score_fails_under_5_no_alert(self, config_with_slack):
        """Live score < 5 failures does not trigger alert."""
        http = FakeHttpClient()
        service = AlertService(config_with_slack, http_client=http)

        state = {
            "match_id": "m1",
            "live_score_consecutive_failures": 3,
        }
        await service._check_state_alerts(state)

        assert len(http.posts) == 0

    @pytest.mark.asyncio
    async def test_healthy_state_no_alerts(self, config_with_slack):
        """Normal state doesn't trigger any alerts."""
        http = FakeHttpClient()
        service = AlertService(config_with_slack, http_client=http)

        state = {
            "match_id": "m1",
            "drawdown_pct": 2.0,
            "event_state": "CONFIRMED",
            "live_odds_healthy": True,
        }
        await service._check_state_alerts(state)

        assert len(http.posts) == 0


# ---------------------------------------------------------------------------
# Channel configuration detection
# ---------------------------------------------------------------------------

class TestChannelConfiguration:
    """Test channels_configured property."""

    def test_no_channels(self, config):
        service = AlertService(config)
        channels = service.channels_configured
        assert channels["slack"] is False
        assert channels["telegram"] is False

    def test_slack_only(self, config_with_slack):
        service = AlertService(config_with_slack)
        channels = service.channels_configured
        assert channels["slack"] is True
        assert channels["telegram"] is False

    def test_all_channels(self, config_with_all):
        service = AlertService(config_with_all)
        channels = service.channels_configured
        assert channels["slack"] is True
        assert channels["telegram"] is True


# ---------------------------------------------------------------------------
# Telegram formatting
# ---------------------------------------------------------------------------

class TestTelegramSend:
    """Test Telegram-specific sending."""

    @pytest.mark.asyncio
    async def test_telegram_payload(self, config_with_all):
        """Telegram sends correct payload with chat_id and parse_mode."""
        http = FakeHttpClient()
        service = AlertService(config_with_all, http_client=http)

        await service.send(AlertSeverity.CRITICAL, "Test", "Body text")

        # Find the Telegram call
        telegram_calls = [(u, p) for u, p in http.posts if "telegram.org" in u]
        assert len(telegram_calls) == 1
        url, payload = telegram_calls[0]

        assert "123456:ABC-DEF" in url
        assert payload["chat_id"] == "-1001234567890"
        assert payload["parse_mode"] == "HTML"
        assert "<b>[CRITICAL] Test</b>" in payload["text"]


# ---------------------------------------------------------------------------
# Redis pub/sub handling
# ---------------------------------------------------------------------------

class TestRedisHandler:
    """Test _handle_alert from Redis channel."""

    @pytest.mark.asyncio
    async def test_handle_alert_from_redis(self, config_with_slack):
        """_handle_alert processes Redis alert messages."""
        http = FakeHttpClient()
        service = AlertService(config_with_slack, http_client=http)

        data = {
            "severity": "warning",
            "title": "Engine HOLD",
            "body": "Manual review needed",
            "match_id": "m1",
        }
        await service._handle_alert(data)

        assert len(http.posts) == 1
        blocks_text = json.dumps(http.posts[0][1])
        assert "Engine HOLD" in blocks_text


# ---------------------------------------------------------------------------
# Service lifecycle
# ---------------------------------------------------------------------------

class TestServiceLifecycle:
    """Test start/stop lifecycle."""

    @pytest.mark.asyncio
    async def test_start_without_redis(self, config):
        """start() returns immediately without Redis (direct-call mode)."""
        service = AlertService(config)
        await service.start()  # should not hang

    @pytest.mark.asyncio
    async def test_stop_sets_shutdown(self, config):
        """stop() signals shutdown."""
        service = AlertService(config)
        await service.stop()
        assert service._shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_close_releases_http(self, config):
        """close() releases HTTP client if owned."""
        http = FakeHttpClient()
        service = AlertService(config, http_client=http)
        # Externally injected — should NOT close
        await service.close()
        assert not http.is_closed

    @pytest.mark.asyncio
    async def test_close_releases_owned_http(self, config):
        """close() releases HTTP client if service created it."""
        service = AlertService(config)
        service._http = FakeHttpClient()
        service._owns_http = True
        await service.close()
        assert service._http.is_closed


# ---------------------------------------------------------------------------
# History management
# ---------------------------------------------------------------------------

class TestAlertHistory:
    """Test alert history ring buffer."""

    @pytest.mark.asyncio
    async def test_history_limit(self, config_with_slack):
        """History is capped at _max_history."""
        http = FakeHttpClient()
        service = AlertService(config_with_slack, http_client=http)
        service._max_history = 5

        for i in range(10):
            await service.send(AlertSeverity.INFO, f"Alert {i}")

        history = service.get_recent_alerts()
        assert len(history) == 5
        # Should have the last 5
        assert history[0]["title"] == "Alert 5"
        assert history[4]["title"] == "Alert 9"

    @pytest.mark.asyncio
    async def test_get_recent_alerts_with_limit(self, config_with_slack):
        """get_recent_alerts respects limit parameter."""
        http = FakeHttpClient()
        service = AlertService(config_with_slack, http_client=http)

        for i in range(5):
            await service.send(AlertSeverity.INFO, f"Alert {i}")

        history = service.get_recent_alerts(limit=2)
        assert len(history) == 2
        assert history[0]["title"] == "Alert 3"
        assert history[1]["title"] == "Alert 4"


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------

class TestSchedulerIntegration:
    """Verify AlertService is wired into MatchScheduler."""

    def test_scheduler_has_alerter(self, config):
        from src.scheduler.main import MatchScheduler

        class FakeGoalserveClient:
            async def get_fixtures(self, *a, **kw):
                return []

        scheduler = MatchScheduler(config, goalserve_client=FakeGoalserveClient())
        assert hasattr(scheduler, "alerter")
        assert isinstance(scheduler.alerter, AlertService)

    def test_scheduler_accepts_injected_alerter(self, config):
        from src.scheduler.main import MatchScheduler

        class FakeGoalserveClient:
            async def get_fixtures(self, *a, **kw):
                return []

        custom_alerter = AlertService(config)
        scheduler = MatchScheduler(
            config,
            goalserve_client=FakeGoalserveClient(),
            alerter=custom_alerter,
        )
        assert scheduler.alerter is custom_alerter
