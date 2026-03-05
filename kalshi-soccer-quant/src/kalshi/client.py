"""Kalshi REST API Client — Orders, Positions, Balance.

Provides authenticated HTTP access to Kalshi's trading API v2
for order submission/cancellation, position lookup, and balance queries.

Reference: phase4.md -> Step 4.1 (Kalshi API)
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

import httpx

from src.common.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT = 10.0


@dataclass
class OrderResponse:
    """Response from order submission."""

    order_id: str
    status: str  # "resting", "executed", "canceled", etc.
    side: str  # "yes", "no"
    action: str  # "buy", "sell"
    count: int  # contracts requested
    filled_count: int  # contracts filled
    price: int  # price in cents
    ticker: str
    raw: dict


@dataclass
class Position:
    """A single position on a market."""

    ticker: str
    market_exposure: int  # cents at risk
    rest_count: int  # resting order count
    total_traded: int  # total contracts traded
    realized_pnl: int  # cents
    raw: dict


class KalshiClient:
    """Authenticated REST client for Kalshi Trading API v2.

    Supports:
      - Order submission (limit orders)
      - Order cancellation
      - Position lookup
      - Balance query
      - Order book snapshot (REST fallback)

    Authentication uses Kalshi's HMAC-SHA256 signing scheme.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = "https://trading-api.kalshi.com/trade-api/v2",
    ):
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(DEFAULT_TIMEOUT),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # -------------------------------------------------------------------
    # Authentication
    # -------------------------------------------------------------------

    def _sign_request(
        self, method: str, path: str, timestamp_ms: int
    ) -> dict[str, str]:
        """Generate Kalshi HMAC-SHA256 auth headers.

        Args:
            method: HTTP method (GET, POST, DELETE).
            path: API path (e.g., "/trade-api/v2/portfolio/balance").
            timestamp_ms: Unix timestamp in milliseconds.

        Returns:
            Dict of auth headers.
        """
        message = f"{timestamp_ms}{method}{path}"
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return {
            "KALSHI-ACCESS-KEY": self._api_key,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict:
        """Make an authenticated request to Kalshi API.

        Args:
            method: HTTP method.
            path: Relative API path (e.g., "/portfolio/balance").
            json_body: Request body for POST/PUT.
            params: Query parameters for GET.

        Returns:
            Parsed JSON response.
        """
        client = await self._ensure_client()
        full_path = f"/trade-api/v2{path}"
        url = f"{self._base_url}{path}"
        timestamp_ms = int(time.time() * 1000)
        headers = self._sign_request(method.upper(), full_path, timestamp_ms)

        response = await client.request(
            method=method.upper(),
            url=url,
            headers=headers,
            json=json_body,
            params=params,
        )
        response.raise_for_status()
        return response.json()

    # -------------------------------------------------------------------
    # Balance
    # -------------------------------------------------------------------

    async def get_balance(self) -> int:
        """Fetch account balance in cents.

        Returns:
            Balance in cents (e.g., 500000 = $5,000).
        """
        data = await self._request("GET", "/portfolio/balance")
        balance = data.get("balance", 0)
        log.debug("kalshi_balance", balance_cents=balance)
        return balance

    # -------------------------------------------------------------------
    # Positions
    # -------------------------------------------------------------------

    async def get_positions(
        self, ticker: str | None = None
    ) -> list[Position]:
        """Fetch open positions.

        Args:
            ticker: Optional market ticker to filter by.

        Returns:
            List of Position objects.
        """
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker

        data = await self._request(
            "GET", "/portfolio/positions", params=params
        )
        positions = []
        for p in data.get("market_positions", []):
            positions.append(
                Position(
                    ticker=p.get("ticker", ""),
                    market_exposure=p.get("market_exposure", 0),
                    rest_count=p.get("resting_orders_count", 0),
                    total_traded=p.get("total_traded", 0),
                    realized_pnl=p.get("realized_pnl", 0),
                    raw=p,
                )
            )
        return positions

    # -------------------------------------------------------------------
    # Orders
    # -------------------------------------------------------------------

    async def submit_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price: int,
        client_order_id: str | None = None,
    ) -> OrderResponse:
        """Submit a limit order.

        Args:
            ticker: Market ticker (e.g., "SOCCER-EPL-MCI-ARS-O25").
            side: "yes" or "no".
            action: "buy" or "sell".
            count: Number of contracts.
            price: Price in cents (1-99).
            client_order_id: Optional idempotency key.

        Returns:
            OrderResponse with fill details.
        """
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": "limit",
            "yes_price": price if side == "yes" else (100 - price),
        }
        if client_order_id:
            body["client_order_id"] = client_order_id

        log.info(
            "kalshi_order_submit",
            ticker=ticker,
            side=side,
            action=action,
            count=count,
            price=price,
        )

        data = await self._request("POST", "/portfolio/orders", json_body=body)
        order = data.get("order", data)
        return OrderResponse(
            order_id=order.get("order_id", ""),
            status=order.get("status", ""),
            side=order.get("side", side),
            action=order.get("action", action),
            count=order.get("count", count),
            filled_count=order.get("filled_count", 0),
            price=order.get("yes_price", price),
            ticker=ticker,
            raw=order,
        )

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel a resting order.

        Args:
            order_id: The order ID to cancel.

        Returns:
            Cancellation response dict.
        """
        log.info("kalshi_order_cancel", order_id=order_id)
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def get_order(self, order_id: str) -> dict:
        """Fetch order details by ID.

        Args:
            order_id: The order ID.

        Returns:
            Order details dict.
        """
        return await self._request("GET", f"/portfolio/orders/{order_id}")

    async def get_orders(
        self,
        ticker: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """Fetch orders with optional filters.

        Args:
            ticker: Filter by market ticker.
            status: Filter by status (e.g., "resting").

        Returns:
            List of order dicts.
        """
        params: dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status

        data = await self._request("GET", "/portfolio/orders", params=params)
        return data.get("orders", [])

    # -------------------------------------------------------------------
    # Order book (REST fallback)
    # -------------------------------------------------------------------

    async def get_orderbook(self, ticker: str) -> dict:
        """Fetch order book snapshot for a market (REST fallback).

        Args:
            ticker: Market ticker.

        Returns:
            Dict with "yes" and "no" depth arrays.
        """
        data = await self._request("GET", f"/markets/{ticker}/orderbook")
        return data.get("orderbook", data)

    # -------------------------------------------------------------------
    # Market info
    # -------------------------------------------------------------------

    async def get_market(self, ticker: str) -> dict:
        """Fetch market metadata.

        Args:
            ticker: Market ticker.

        Returns:
            Market details dict.
        """
        data = await self._request("GET", f"/markets/{ticker}")
        return data.get("market", data)

    async def get_markets(
        self,
        event_ticker: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """Fetch markets with optional filters.

        Args:
            event_ticker: Filter by event.
            status: Filter by status (e.g., "open").

        Returns:
            List of market dicts.
        """
        params: dict[str, Any] = {}
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status

        data = await self._request("GET", "/markets", params=params)
        return data.get("markets", [])
