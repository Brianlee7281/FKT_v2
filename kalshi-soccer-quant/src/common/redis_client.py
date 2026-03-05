"""Async Redis Pub/Sub wrapper."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import redis.asyncio as aioredis

from src.common.logging import get_logger

log = get_logger(__name__)


class RedisClient:
    """Thin async wrapper around redis-py for pub/sub and basic ops."""

    def __init__(self, url: str = "redis://localhost:6379/0"):
        self._url = url
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        self._redis = aioredis.from_url(
            self._url,
            decode_responses=True,
            max_connections=20,
        )
        await self._redis.ping()
        log.info("redis_connected", url=self._url)

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            log.info("redis_closed")

    @property
    def redis(self) -> aioredis.Redis:
        if self._redis is None:
            raise RuntimeError("RedisClient not connected. Call connect() first.")
        return self._redis

    # ── Pub/Sub ──

    async def publish(self, channel: str, data: str | dict) -> int:
        """Publish a message to a channel.

        Returns the number of subscribers that received the message.
        """
        if isinstance(data, dict):
            data = json.dumps(data)
        return await self.redis.publish(channel, data)

    async def subscribe(self, *channels: str) -> aioredis.client.PubSub:
        """Subscribe to one or more channels. Returns the PubSub object."""
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(*channels)
        return pubsub

    async def psubscribe(self, *patterns: str) -> aioredis.client.PubSub:
        """Pattern-subscribe. Returns the PubSub object."""
        pubsub = self.redis.pubsub()
        await pubsub.psubscribe(*patterns)
        return pubsub

    async def listen(self, pubsub: aioredis.client.PubSub) -> AsyncIterator[dict]:
        """Yield messages from a PubSub subscription."""
        async for message in pubsub.listen():
            if message["type"] in ("message", "pmessage"):
                yield message

    # ── Basic key/value ops ──

    async def get(self, key: str) -> str | None:
        return await self.redis.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        await self.redis.set(key, value, ex=ex)

    async def delete(self, key: str) -> None:
        await self.redis.delete(key)

    # ── Health check ──

    async def ping(self) -> bool:
        try:
            return await self.redis.ping()
        except Exception:
            return False
