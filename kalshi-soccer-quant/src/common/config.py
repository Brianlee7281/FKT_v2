"""System configuration loader — YAML + environment variable merge."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


def _env_substitute(value: str) -> str:
    """Replace ${VAR} patterns with environment variable values."""
    if not isinstance(value, str):
        return value
    if "${" in value:
        import re
        def _replace(m: re.Match) -> str:
            return os.environ.get(m.group(1), "")
        return re.sub(r"\$\{(\w+)\}", _replace, value)
    return value


def _deep_substitute(obj):
    """Recursively substitute env vars in a nested dict."""
    if isinstance(obj, dict):
        return {k: _deep_substitute(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_substitute(v) for v in obj]
    if isinstance(obj, str):
        return _env_substitute(obj)
    return obj


def _resolve_redis_url(redis_cfg: dict) -> str:
    """Resolve Redis URL, allowing REDIS_HOST env var to override hostname."""
    url = redis_cfg.get("url", "redis://localhost:6379/0")
    host = os.environ.get("REDIS_HOST")
    if host:
        url = f"redis://{host}:6379/0"
    return url


def _resolve_postgres_url(pg_cfg: dict) -> str:
    """Resolve Postgres URL, allowing POSTGRES_HOST env var to override hostname."""
    url = pg_cfg.get("url", "postgresql://kalshi:kalshi_dev@localhost:5432/kalshi")
    host = os.environ.get("POSTGRES_HOST")
    password = os.environ.get("DB_PASSWORD", "kalshi_dev")
    if host:
        url = f"postgresql://kalshi:{password}@{host}:5432/kalshi"
    return url


@dataclass
class SystemConfig:
    """Loads config/system.yaml and merges with environment variables."""

    # API keys (from env)
    goalserve_api_key: str = ""
    kalshi_api_key: str = ""
    kalshi_api_secret: str = ""

    # Trading mode
    trading_mode: str = "paper"

    # Goalserve
    goalserve_base_url: str = "http://www.goalserve.com/getfeed"
    live_score_poll_interval: int = 3
    live_odds_ws_url: str = "wss://goalserve.com/liveodds"

    # Kalshi
    kalshi_ws_url: str = "wss://trading-api.kalshi.com/trade-api/ws/v2"
    kalshi_rest_url: str = "https://trading-api.kalshi.com/trade-api/v2"

    # Risk
    f_order_cap: float = 0.03
    f_match_cap: float = 0.05
    f_total_cap: float = 0.20
    initial_bankroll: float = 5000.0

    # Trading
    K_frac: float = 0.25
    z: float = 1.645
    theta_entry: float = 0.02
    theta_exit: float = 0.005
    cooldown_seconds: int = 15
    low_confidence_multiplier: float = 0.5
    rapid_entry_enabled: bool = False
    bet365_divergence_auto_exit: bool = False

    # Infrastructure
    redis_url: str = "redis://localhost:6379/0"
    postgres_url: str = "postgresql://kalshi:kalshi_dev@localhost:5432/kalshi"

    # Alerts
    slack_webhook: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Leagues & markets
    target_leagues: list[str] = field(default_factory=lambda: ["1204", "1399"])
    active_markets: list[str] = field(default_factory=lambda: ["over_25", "home_win", "away_win", "btts"])

    # Phase 1 calibration params
    params_dir: str = "output/calibration/production"

    # MC
    mc_N: int = 50000
    mc_executor_workers: int = 4

    # Fee
    fee_rate: float = 0.07

    # Raw config dict
    _raw: dict = field(default_factory=dict, repr=False)

    @staticmethod
    def _load_secret(value: str) -> str:
        """If value is a file path, read and return its contents; otherwise return as-is."""
        if value and os.path.isfile(value):
            return Path(value).read_text().strip()
        return value

    @classmethod
    def load(cls, config_path: str = "config/system.yaml",
             env_file: str | None = ".env") -> SystemConfig:
        """Load config from YAML file and merge with env vars."""
        if env_file and Path(env_file).exists():
            load_dotenv(env_file)

        raw: dict = {}
        config_file = Path(config_path)
        if config_file.exists():
            with open(config_file) as f:
                raw = yaml.safe_load(f) or {}
            raw = _deep_substitute(raw)

        goalserve = raw.get("goalserve", {})
        kalshi = raw.get("kalshi", {})
        risk = raw.get("risk", {})
        trading = raw.get("trading", {})
        redis_cfg = raw.get("redis", {})
        pg_cfg = raw.get("postgres", {})
        alerts = raw.get("alerts", {})
        mc = raw.get("mc", {})

        return cls(
            goalserve_api_key=os.environ.get("GOALSERVE_API_KEY", ""),
            kalshi_api_key=os.environ.get("KALSHI_API_KEY", ""),
            kalshi_api_secret=cls._load_secret(os.environ.get("KALSHI_API_SECRET", "")),

            trading_mode=raw.get("trading_mode", "paper"),

            goalserve_base_url=goalserve.get("base_url", cls.goalserve_base_url),
            live_score_poll_interval=goalserve.get("live_score_poll_interval", 3),
            live_odds_ws_url=goalserve.get("live_odds_ws_url", cls.live_odds_ws_url),

            kalshi_ws_url=kalshi.get("ws_url", cls.kalshi_ws_url),
            kalshi_rest_url=kalshi.get("rest_url", cls.kalshi_rest_url),

            f_order_cap=risk.get("f_order_cap", 0.03),
            f_match_cap=risk.get("f_match_cap", 0.05),
            f_total_cap=risk.get("f_total_cap", 0.20),
            initial_bankroll=risk.get("initial_bankroll", 5000.0),

            K_frac=trading.get("K_frac", 0.25),
            z=trading.get("z", 1.645),
            theta_entry=trading.get("theta_entry", 0.02),
            theta_exit=trading.get("theta_exit", 0.005),
            cooldown_seconds=trading.get("cooldown_seconds", 15),
            low_confidence_multiplier=trading.get("low_confidence_multiplier", 0.5),
            rapid_entry_enabled=trading.get("rapid_entry_enabled", False),
            bet365_divergence_auto_exit=trading.get("bet365_divergence_auto_exit", False),

            redis_url=_resolve_redis_url(redis_cfg),
            postgres_url=_resolve_postgres_url(pg_cfg),

            slack_webhook=alerts.get("slack_webhook", ""),
            telegram_bot_token=alerts.get("telegram_bot_token", ""),
            telegram_chat_id=alerts.get("telegram_chat_id", ""),

            target_leagues=raw.get("target_leagues", ["1204", "1399"]),
            active_markets=raw.get("active_markets", ["over_25", "home_win", "away_win", "btts"]),

            mc_N=mc.get("N", 50000),
            mc_executor_workers=mc.get("executor_workers", 4),

            params_dir=raw.get("params_dir", "output/calibration/production"),

            fee_rate=raw.get("fee_rate", 0.07),

            _raw=raw,
        )
