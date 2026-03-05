"""Tests for Step 4.1: KalshiClient — Auth signing, response parsing.

Tests the HMAC-SHA256 signing logic and response dataclass construction
without making real HTTP calls.

Reference: implementation_roadmap.md -> Step 4.1
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from src.kalshi.client import KalshiClient, OrderResponse, Position


# ---------------------------------------------------------------------------
# Auth Signing Tests
# ---------------------------------------------------------------------------

class TestAuthSigning:
    """Verify HMAC-SHA256 signing produces correct headers."""

    def test_signature_format(self):
        """Sign request produces all required headers."""
        client = KalshiClient(
            api_key="test-key",
            api_secret="test-secret",
        )
        headers = client._sign_request(
            "GET", "/trade-api/v2/portfolio/balance", 1700000000000
        )

        assert headers["KALSHI-ACCESS-KEY"] == "test-key"
        assert "KALSHI-ACCESS-SIGNATURE" in headers
        assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
        assert headers["Content-Type"] == "application/json"

    def test_signature_deterministic(self):
        """Same inputs produce same signature."""
        client = KalshiClient(api_key="k", api_secret="s")
        h1 = client._sign_request("POST", "/path", 123)
        h2 = client._sign_request("POST", "/path", 123)
        assert h1["KALSHI-ACCESS-SIGNATURE"] == h2["KALSHI-ACCESS-SIGNATURE"]

    def test_signature_changes_with_method(self):
        """Different methods produce different signatures."""
        client = KalshiClient(api_key="k", api_secret="s")
        h_get = client._sign_request("GET", "/path", 123)
        h_post = client._sign_request("POST", "/path", 123)
        assert (
            h_get["KALSHI-ACCESS-SIGNATURE"]
            != h_post["KALSHI-ACCESS-SIGNATURE"]
        )

    def test_signature_changes_with_path(self):
        """Different paths produce different signatures."""
        client = KalshiClient(api_key="k", api_secret="s")
        h1 = client._sign_request("GET", "/path/a", 123)
        h2 = client._sign_request("GET", "/path/b", 123)
        assert (
            h1["KALSHI-ACCESS-SIGNATURE"] != h2["KALSHI-ACCESS-SIGNATURE"]
        )

    def test_signature_matches_manual_hmac(self):
        """Signature matches hand-computed HMAC-SHA256."""
        client = KalshiClient(api_key="k", api_secret="mysecret")
        ts = 1700000000000
        method = "GET"
        path = "/trade-api/v2/portfolio/balance"

        headers = client._sign_request(method, path, ts)

        # Manual computation
        message = f"{ts}{method}{path}"
        expected = hmac.new(
            b"mysecret", message.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        assert headers["KALSHI-ACCESS-SIGNATURE"] == expected


# ---------------------------------------------------------------------------
# Dataclass Construction Tests
# ---------------------------------------------------------------------------

class TestOrderResponse:
    """Verify OrderResponse dataclass fields."""

    def test_fields(self):
        resp = OrderResponse(
            order_id="ord-123",
            status="resting",
            side="yes",
            action="buy",
            count=10,
            filled_count=5,
            price=57,
            ticker="SOCCER-MCI-ARS-O25",
            raw={"order_id": "ord-123"},
        )
        assert resp.order_id == "ord-123"
        assert resp.filled_count == 5
        assert resp.ticker == "SOCCER-MCI-ARS-O25"


class TestPosition:
    """Verify Position dataclass fields."""

    def test_fields(self):
        pos = Position(
            ticker="SOCCER-MCI-ARS-O25",
            market_exposure=5000,
            rest_count=2,
            total_traded=15,
            realized_pnl=300,
            raw={},
        )
        assert pos.ticker == "SOCCER-MCI-ARS-O25"
        assert pos.market_exposure == 5000
        assert pos.realized_pnl == 300
