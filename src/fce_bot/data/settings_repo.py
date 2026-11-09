"""Repository for managing dynamic bot settings and payment configuration."""

import logging
from typing import Optional, Dict, Any
from fce_bot.config import settings
from fce_bot.data.db import get_persistent_db

logger = logging.getLogger(__name__)


class SettingsRepository:
    """Provides persistent key-value configuration storage backed by bot_settings table."""

    def __init__(self, db_path=None):
        self.db_path = db_path

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Fetch raw string setting value from persistent storage."""
        try:
            with get_persistent_db(self.db_path) as conn:
                cur = conn.cursor()
                row = cur.execute(
                    "SELECT value FROM bot_settings WHERE key = ?",
                    (key,)
                ).fetchone()
                if row:
                    return str(row["value"])
        except Exception as e:
            logger.warning("Error reading setting '%s': %s", key, e)
        return default

    def set(self, key: str, value: Any) -> bool:
        """Store or update a setting value."""
        val_str = str(value)
        try:
            with get_persistent_db(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO bot_settings (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = CURRENT_TIMESTAMP
                """, (key, val_str))
                return True
        except Exception as e:
            logger.error("Error writing setting '%s': %s", key, e)
            return False

    def get_sub_price_eur(self) -> float:
        """Get monthly subscription price in EUR (default 1.00 EUR)."""
        raw = self.get("sub_price_eur")
        if raw is not None:
            try:
                return round(float(raw), 2)
            except ValueError:
                pass
        return float(settings.AI_SUBSCRIPTION_PRICE_EUR)

    def set_sub_price_eur(self, price: float) -> bool:
        """Update subscription price in EUR."""
        return self.set("sub_price_eur", f"{max(0.0, float(price)):.2f}")

    def get_sub_stars(self) -> int:
        """Get monthly subscription price in Telegram Stars (default 50 Stars)."""
        raw = self.get("sub_stars")
        if raw is not None:
            try:
                return max(1, int(raw))
            except ValueError:
                pass
        return int(settings.AI_SUBSCRIPTION_STARS)

    def set_sub_stars(self, stars: int) -> bool:
        """Update subscription price in Telegram Stars (e.g. 1 Star for testing, 50 Stars for production)."""
        return self.set("sub_stars", max(1, int(stars)))

    def get_price_display(self) -> str:
        """Return formatted price string for UI (strictly Telegram Stars)."""
        return f"{self.get_sub_stars()} ⭐"

    def get_sub_days(self) -> int:
        """Get subscription duration in days (default 30 days)."""
        raw = self.get("sub_days")
        if raw is not None:
            try:
                return max(1, int(raw))
            except ValueError:
                pass
        return int(settings.AI_SUBSCRIPTION_DAYS)

    def set_sub_days(self, days: int) -> bool:
        """Update subscription duration in days."""
        return self.set("sub_days", max(1, int(days)))

    def get_trial_days(self) -> int:
        """Get one-time free trial duration in days (default 3 days)."""
        raw = self.get("trial_days")
        if raw is not None:
            try:
                return max(1, int(raw))
            except ValueError:
                pass
        return int(settings.AI_TRIAL_DAYS)

    def set_trial_days(self, days: int) -> bool:
        """Update free trial duration in days."""
        return self.set("trial_days", max(1, int(days)))

    def is_trial_enabled(self) -> bool:
        """Check if one-time free trial is active and available to users."""
        raw = self.get("trial_enabled")
        if raw is not None:
            return raw in ("1", "true", "True", "yes")
        return bool(settings.AI_TRIAL_ENABLED)

    def set_trial_enabled(self, enabled: bool) -> bool:
        """Enable or disable one-time trial availability."""
        return self.set("trial_enabled", "1" if enabled else "0")

    def get_currency(self) -> str:
        """Get billing currency: strictly 'XTR' (Telegram Stars)."""
        return "XTR"

    def set_currency(self, currency: str) -> bool:
        """Set billing currency. Only 'XTR' (Telegram Stars) is permitted."""
        curr = currency.strip().upper()
        if curr != "XTR":
            logger.warning("Attempted to set unsupported currency '%s'. Only 'XTR' is allowed.", currency)
            return False
        return self.set("currency", "XTR")

    def get_payment_provider_token(self) -> str:
        """Get payment provider token for Telegram Bot Payments API."""
        raw = self.get("payment_provider_token")
        if raw:
            return raw.strip()
        return (settings.PAYMENT_PROVIDER_TOKEN or "").strip()

    def set_payment_provider_token(self, token: str) -> bool:
        """Set payment provider token (e.g. Stripe token from BotFather)."""
        return self.set("payment_provider_token", token.strip())

    def get_admin_ai_test_mode(self, admin_id: int | str) -> bool:
        """Check if an administrator has temporarily disabled AI VIP to test buying or free trial."""
        raw = self.get(f"admin_ai_test:{admin_id}")
        if raw is not None:
            return raw in ("1", "true", "True", "yes")
        return False

    def set_admin_ai_test_mode(self, admin_id: int | str, enabled: bool) -> bool:
        """Toggle test buying mode for an administrator account."""
        return self.set(f"admin_ai_test:{admin_id}", "1" if enabled else "0")

    def get_all_ai_settings(self) -> Dict[str, Any]:
        """Return dict summary of all current AI subscription and trial settings."""
        return {
            "sub_price_eur": self.get_sub_price_eur(),
            "sub_stars": self.get_sub_stars(),
            "price_display": self.get_price_display(),
            "sub_days": self.get_sub_days(),
            "trial_days": self.get_trial_days(),
            "trial_enabled": self.is_trial_enabled(),
            "currency": "XTR",
            "has_provider_token": False,
            "provider_type": "Telegram Stars (⭐ XTR - Zero Config)",
        }
