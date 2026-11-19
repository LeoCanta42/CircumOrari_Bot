"""Repository for managing dynamic bot settings and payment configuration."""

import logging
from typing import Optional, Dict, Any, List
from circumorari_bot.config import settings
from circumorari_bot.data.db import get_persistent_db

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

    def delete(self, key: str) -> bool:
        """Remove a setting key from persistent storage."""
        try:
            with get_persistent_db(self.db_path) as conn:
                conn.execute("DELETE FROM bot_settings WHERE key = ?", (key,))
                return True
        except Exception as e:
            logger.error("Error deleting setting '%s': %s", key, e)
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
        if not enabled:
            return self.delete(f"admin_ai_test:{admin_id}")
        return self.set(f"admin_ai_test:{admin_id}", "1")

    def get_ai_rate_limit_requests(self) -> int:
        """Get max AI requests allowed per user in the sliding fair-use window (default 3)."""
        raw = self.get("ai_rate_limit_requests")
        if raw is not None:
            try:
                return max(1, int(raw))
            except (ValueError, TypeError):
                pass
        return max(1, int(settings.AI_RATE_LIMIT_PER_MINUTE))

    def set_ai_rate_limit_requests(self, max_requests: int) -> bool:
        """Persist max AI requests allowed per user in the sliding fair-use window."""
        return self.set("ai_rate_limit_requests", max(1, int(max_requests)))

    def get_ai_rate_limit_window_minutes(self) -> int:
        """Get AI fair-use sliding window duration in minutes (default 10)."""
        raw = self.get("ai_rate_limit_window_minutes")
        if raw is not None:
            try:
                return max(1, int(raw))
            except (ValueError, TypeError):
                pass
        return max(1, int(settings.AI_RATE_LIMIT_WINDOW_MINUTES))

    def set_ai_rate_limit_window_minutes(self, minutes: int) -> bool:
        """Persist AI fair-use sliding window duration in minutes."""
        return self.set("ai_rate_limit_window_minutes", max(1, int(minutes)))

    @staticmethod
    def _is_valid_chat_id(chat_id: int | str) -> bool:
        """Validate numeric Telegram chat_id (digits, optional leading '-')."""
        if isinstance(chat_id, bool) or chat_id is None:
            return False
        if isinstance(chat_id, int):
            return True
        if isinstance(chat_id, str):
            s = chat_id.strip()
            return bool(s) and (s.isdigit() or (s.startswith("-") and s[1:].isdigit()))
        return False

    def is_maintenance_mode(self) -> bool:
        """Check whether bot maintenance mode is active (default False)."""
        raw = self.get("maintenance_mode")
        if raw is not None:
            return raw in ("1", "true", "True", "yes")
        return False

    def set_maintenance_mode(self, enabled: bool) -> bool:
        """Enable or disable bot maintenance mode."""
        return self.set("maintenance_mode", "1" if enabled else "0")

    def add_maintenance_waitlist_user(self, chat_id: int | str) -> bool:
        """Record a user who contacted the bot during maintenance for back-online notification."""
        if not self._is_valid_chat_id(chat_id):
            return False
        cid_str = str(chat_id).strip()
        try:
            with get_persistent_db(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO maintenance_waitlist (chat_id, contacted_at)
                    VALUES (?, CURRENT_TIMESTAMP)
                    ON CONFLICT(chat_id) DO UPDATE SET contacted_at = CURRENT_TIMESTAMP
                """, (cid_str,))
                return True
        except Exception as e:
            logger.error("Error adding user %s to maintenance waitlist: %s", chat_id, e)
            return False

    def get_maintenance_waitlist_users(self) -> List[str]:
        """Return ordered list of non-banned chat IDs waiting for maintenance end notification."""
        try:
            with get_persistent_db(self.db_path) as conn:
                cur = conn.cursor()
                rows = cur.execute("""
                    SELECT m.chat_id
                    FROM maintenance_waitlist m
                    LEFT JOIN users u ON m.chat_id = u.chat_id
                    WHERE COALESCE(u.is_banned, 0) = 0
                    ORDER BY m.contacted_at ASC
                """).fetchall()
                return [str(row["chat_id"]) for row in rows]
        except Exception as e:
            logger.warning("Error reading maintenance waitlist: %s", e)
            return []

    def count_maintenance_waitlist_users(self) -> int:
        """Return count of non-banned users waiting for maintenance end notification."""
        try:
            with get_persistent_db(self.db_path) as conn:
                cur = conn.cursor()
                row = cur.execute("""
                    SELECT COUNT(*) as cnt
                    FROM maintenance_waitlist m
                    LEFT JOIN users u ON m.chat_id = u.chat_id
                    WHERE COALESCE(u.is_banned, 0) = 0
                """).fetchone()
                return int(row["cnt"]) if row else 0
        except Exception as e:
            logger.warning("Error counting maintenance waitlist: %s", e)
            return 0

    def clear_maintenance_waitlist(self) -> int:
        """Clear all entries from maintenance_waitlist and return removed row count."""
        try:
            with get_persistent_db(self.db_path) as conn:
                cur = conn.cursor()
                res = cur.execute("DELETE FROM maintenance_waitlist")
                return int(res.rowcount or 0)
        except Exception as e:
            logger.error("Error clearing maintenance waitlist: %s", e)
            return 0

    def get_all_ai_settings(self) -> Dict[str, Any]:
        """Return dict summary of all current AI subscription and trial settings."""
        return {
            "sub_price_eur": self.get_sub_price_eur(),
            "sub_stars": self.get_sub_stars(),
            "price_display": self.get_price_display(),
            "sub_days": self.get_sub_days(),
            "trial_days": self.get_trial_days(),
            "trial_enabled": self.is_trial_enabled(),
            "ai_rate_limit_requests": self.get_ai_rate_limit_requests(),
            "ai_rate_limit_window_minutes": self.get_ai_rate_limit_window_minutes(),
            "currency": "XTR",
            "has_provider_token": False,
            "provider_type": "Telegram Stars (⭐ XTR - Zero Config)",
        }
