"""Configuration management for FCE Bot using pydantic-settings."""

import os
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from dotenv import load_dotenv
from pydantic import Field, AliasChoices
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env", override=True)
load_dotenv(BASE_DIR / ".env.local", override=True)

# Non-libpq query parameters added by Prisma / Vercel integrations that must be stripped for psycopg
_UNSUPPORTED_PG_QUERY_PARAMS = {
    "pgbouncer",
    "connection_limit",
    "pool_timeout",
    "schema",
    "supa",
    "statement_cache_size",
    "prepared_statements",
}


def sanitize_postgres_url(raw_url: Optional[str]) -> Optional[str]:
    """Normalize a Neon / Vercel Postgres connection URL for psycopg v3 compatibility."""
    if not raw_url:
        return None
    cleaned = raw_url.strip().strip('"').strip("'")
    if not cleaned:
        return None
    if cleaned.startswith("postgres://"):
        cleaned = "postgresql://" + cleaned[len("postgres://"):]
    if not cleaned.startswith(("postgresql://", "postgresql+")):
        return None
    if cleaned.startswith("postgresql+psycopg://"):
        cleaned = "postgresql://" + cleaned[len("postgresql+psycopg://"):]

    parts = urlsplit(cleaned)
    query_items = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _UNSUPPORTED_PG_QUERY_PARAMS
    ]
    param_names = {k.lower() for k, _ in query_items}
    host = (parts.hostname or "").lower()
    if host and host not in ("localhost", "127.0.0.1", "::1") and "sslmode" not in param_names:
        query_items.append(("sslmode", "require"))

    new_query = urlencode(query_items)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(BASE_DIR / ".env"), str(BASE_DIR / ".env.local")),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Project metadata
    PROJECT_NAME: str = "FCE Circumetnea Bot"
    DEBUG: bool = False
    BASE_DIR: Path = BASE_DIR

    # Telegram configuration
    TELEGRAM_BOT_TOKEN: str = Field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""),
        description="Telegram Bot API Token"
    )
    NEWS_BOT_TOKEN: Optional[str] = Field(
        default_factory=lambda: os.getenv("NEWS_BOT_TOKEN"),
        description="Optional separate token for news announcements"
    )
    TELEGRAM_CHANNEL_ID: Optional[str] = Field(
        default_factory=lambda: os.getenv("TELEGRAM_CHANNEL_ID"),
        description="Telegram channel ID / @handle for news broadcasts"
    )
    ADMIN_USER_IDS_RAW: str = Field(
        default_factory=lambda: os.getenv("ADMIN_USER_IDS", ""),
        validation_alias=AliasChoices("ADMIN_USER_IDS", "ADMIN_USER_IDS_RAW"),
        description="Comma-separated Telegram user IDs of administrators"
    )

    # Database configuration
    DATABASE_PATH: Path = BASE_DIR / "fce_lines.db"
    USERS_DATABASE_PATH: Path = BASE_DIR / "users.db"
    DATABASE_URL: Optional[str] = Field(
        default_factory=lambda: (
            os.getenv("POSTGRES_URL")
            or os.getenv("DATABASE_URL")
            or os.getenv("POSTGRES_URL_NON_POOLING")
            or os.getenv("NEON_DATABASE_URL")
        ),
        validation_alias=AliasChoices(
            "POSTGRES_URL",
            "DATABASE_URL",
            "POSTGRES_URL_NON_POOLING",
            "NEON_DATABASE_URL",
        ),
        description="Neon / Vercel PostgreSQL URL for permanent serverless persistence",
    )

    # Circumetnea data endpoints
    CIRCUMETNEA_BASE_URL: str = "https://www.circumetnea.it"
    CIRCUMETNEA_LINES_URL: str = "https://www.circumetnea.it/le-nostre-linee/"
    CIRCUMETNEA_GTFS_URL: str = (
        "https://www.circumetnea.it/download/general-transit-feed-specification-fce-01-02-2025-28-02-2028/?wpdmdl=17798"
    )
    CIRCUMETNEA_RSS_URL: str = "https://www.circumetnea.it/category/news/feed/"

    # AI Trip Assistant (Exclusively Google Gemini)
    GEMINI_API_KEY: Optional[str] = Field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY") or os.getenv("AI_API_KEY"),
        validation_alias=AliasChoices("GEMINI_API_KEY", "AI_API_KEY")
    )
    GEMINI_MODEL: str = "gemini-3.5-flash-lite"

    # AI Subscription & Payments Configuration
    PAYMENT_PROVIDER_TOKEN: Optional[str] = Field(
        default_factory=lambda: os.getenv("PAYMENT_PROVIDER_TOKEN", ""),
        description="Telegram Payment Provider Token from @BotFather (e.g. Stripe test/live) or empty for Telegram Stars"
    )
    AI_SUBSCRIPTION_PRICE_EUR: float = Field(
        default=1.00,
        description="Monthly AI subscription cost in EUR"
    )
    AI_SUBSCRIPTION_STARS: int = Field(
        default=50,
        description="Monthly AI subscription cost in Telegram Stars (XTR)"
    )
    AI_SUBSCRIPTION_DAYS: int = Field(
        default=30,
        description="Duration of monthly AI access in calendar days"
    )
    AI_TRIAL_DAYS: int = Field(
        default=3,
        description="Duration of one-time free AI trial in calendar days"
    )
    AI_TRIAL_ENABLED: bool = Field(
        default=True,
        description="Whether the one-time free trial can be activated by new users"
    )
    AI_CURRENCY: str = Field(
        default="XTR",
        description="Billing currency: strictly Telegram Stars (XTR)"
    )

    # Vercel & Webhook configuration
    WEBHOOK_URL: Optional[str] = None  # e.g. https://my-fce-bot.vercel.app/api/webhook
    WEBHOOK_SECRET: Optional[str] = None
    CRON_SECRET: Optional[str] = None

    @property
    def admin_user_ids(self) -> List[int]:
        """Parsed integer list of admin IDs."""
        ids: List[int] = []
        if not self.ADMIN_USER_IDS_RAW:
            return ids
        for item in self.ADMIN_USER_IDS_RAW.replace(";", ",").split(","):
            cleaned = item.strip()
            if cleaned.isdigit() or (cleaned.startswith("-") and cleaned[1:].isdigit()):
                ids.append(int(cleaned))
        return ids

    @property
    def postgres_url(self) -> Optional[str]:
        """Sanitized PostgreSQL connection URL (checks settings and live environment variables)."""
        raw = (
            self.DATABASE_URL
            or os.getenv("POSTGRES_URL")
            or os.getenv("DATABASE_URL")
            or os.getenv("POSTGRES_URL_NON_POOLING")
            or os.getenv("NEON_DATABASE_URL")
        )
        return sanitize_postgres_url(raw)

    @property
    def effective_news_token(self) -> str:
        """Returns NEWS_BOT_TOKEN or falls back to TELEGRAM_BOT_TOKEN."""
        return self.NEWS_BOT_TOKEN or self.TELEGRAM_BOT_TOKEN


settings = Settings()
