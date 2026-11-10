"""User repository for tracking interactions and subscriptions (GDPR Compliant)."""

import logging
from typing import List, Optional, Any, Tuple
from datetime import datetime, timedelta, timezone
from circumorari_bot.config import settings
from circumorari_bot.data.db import get_persistent_db as get_db

logger = logging.getLogger(__name__)


def _parse_utc_dt(val: Any) -> Optional[datetime]:
    """Parse a datetime object or timestamp string into timezone-aware UTC datetime."""
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)
    s = str(val).strip()
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s.split("+")[0].split("Z")[0], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


class UsersRepository:
    """Manages bot user interactions and broadcast subscriptions with GDPR compliance."""

    def __init__(self, db_path=None):
        self.db_path = db_path

    @staticmethod
    def _is_valid_chat_id(chat_id: int | str) -> bool:
        """Ensure chat_id is a valid numeric Telegram ID (never a mock string or exceed 64-bit int)."""
        if isinstance(chat_id, bool) or chat_id is None:
            return False
        if isinstance(chat_id, int):
            return True
        if isinstance(chat_id, str):
            s = chat_id.strip()
            return bool(s) and (s.isdigit() or (s.startswith("-") and s[1:].isdigit()))
        return False

    def register_or_update(
        self,
        chat_id: int | str,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        is_subscribed: bool = False
    ) -> None:
        """Register a user chat interaction or update activity timestamp.
        
        New users are saved with notifications disabled (is_subscribed=0) and AI disabled (ai_allowed=0)
        by default, while preserving any existing subscription/AI permissions on subsequent messages.
        """
        if not self._is_valid_chat_id(chat_id):
            return
        clean_user = str(username) if isinstance(username, str) else None
        clean_first = str(first_name) if isinstance(first_name, str) else None
        try:
            with get_db(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO users (chat_id, username, first_name, last_active, is_subscribed, ai_allowed)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, 0)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        username = coalesce(excluded.username, users.username),
                        first_name = coalesce(excluded.first_name, users.first_name),
                        last_active = CURRENT_TIMESTAMP
                """, (str(chat_id).strip(), clean_user, clean_first, 1 if is_subscribed else 0))
        except Exception as e:
            logger.warning("Could not persist user %s: %s", chat_id, e)

    def is_user_subscribed(self, chat_id: int | str) -> bool:
        """Check if user has explicitly subscribed to service announcements."""
        try:
            with get_db(self.db_path) as conn:
                cur = conn.cursor()
                row = cur.execute(
                    "SELECT is_subscribed FROM users WHERE chat_id = ?",
                    (str(chat_id),)
                ).fetchone()
                return bool(row and row["is_subscribed"] == 1)
        except Exception as e:
            logger.warning("Error checking subscription for %s: %s", chat_id, e)
            return False

    def subscribe_user(
        self,
        chat_id: int | str,
        username: Optional[str] = None,
        first_name: Optional[str] = None
    ) -> bool:
        """Explicit opt-in: register user for news and service broadcasts."""
        if not self._is_valid_chat_id(chat_id):
            return False
        clean_user = str(username) if isinstance(username, str) else None
        clean_first = str(first_name) if isinstance(first_name, str) else None
        try:
            with get_db(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO users (chat_id, username, first_name, last_active, is_subscribed, ai_allowed)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, 1, 0)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        is_subscribed = 1,
                        username = coalesce(excluded.username, users.username),
                        first_name = coalesce(excluded.first_name, users.first_name),
                        last_active = CURRENT_TIMESTAMP
                """, (str(chat_id).strip(), clean_user, clean_first))
                return True
        except Exception as e:
            logger.error("Error subscribing user %s: %s", chat_id, e)
            return False

    def unsubscribe_user(self, chat_id: int | str) -> bool:
        """Explicit opt-out: stop receiving news and service broadcasts."""
        try:
            with get_db(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE users SET is_subscribed = 0 WHERE chat_id = ?",
                    (str(chat_id),)
                )
                return True
        except Exception as e:
            logger.error("Error unsubscribing user %s: %s", chat_id, e)
            return False

    def delete_user(self, chat_id: int | str) -> bool:
        """Permanently delete user data (GDPR Art. 17 - Right to Erasure)."""
        try:
            with get_db(self.db_path) as conn:
                cur = conn.cursor()
                res = cur.execute("DELETE FROM users WHERE chat_id = ?", (str(chat_id),))
                return res.rowcount > 0
        except Exception as e:
            logger.error("Error deleting user %s: %s", chat_id, e)
            return False

    def set_subscription(self, chat_id: int | str, is_subscribed: bool) -> bool:
        """Toggle user news broadcast subscription state (creating record if needed)."""
        if not self._is_valid_chat_id(chat_id):
            return False
        try:
            with get_db(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO users (chat_id, is_subscribed, ai_allowed, last_active)
                    VALUES (?, ?, 0, CURRENT_TIMESTAMP)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        is_subscribed = ?
                """, (str(chat_id).strip(), 1 if is_subscribed else 0, 1 if is_subscribed else 0))
                return True
        except Exception as e:
            logger.error("Error setting subscription for user %s: %s", chat_id, e)
            return False

    def get_subscribed_chat_ids(self) -> List[str]:
        """Return list of active subscribed chat IDs."""
        try:
            with get_db(self.db_path) as conn:
                cur = conn.cursor()
                rows = cur.execute("""
                    SELECT chat_id FROM users
                    WHERE is_subscribed = 1
                """).fetchall()
                return [row["chat_id"] for row in rows]
        except Exception as e:
            logger.warning("Error fetching subscribed users: %s", e)
            return []

    def is_ai_allowed(self, chat_id: int | str) -> bool:
        """Check if user has active permission to use the AI assistant.
        
        Configured admins in settings.admin_user_ids are authorized by default unless they have
        explicitly enabled admin test buying mode to test customer payment flows.
        For normal users, access is granted if ai_allowed == 1 AND (ai_expires_at is NULL or in future).
        If ai_expires_at <= now, auto-deactivates the user and returns False.
        """
        try:
            cid_int = int(chat_id)
            if cid_int in settings.admin_user_ids:
                from circumorari_bot.data.settings_repo import SettingsRepository
                if not SettingsRepository(self.db_path).get_admin_ai_test_mode(cid_int):
                    return True
        except (ValueError, TypeError):
            pass

        try:
            with get_db(self.db_path) as conn:
                cur = conn.cursor()
                row = cur.execute(
                    "SELECT ai_allowed, ai_expires_at FROM users WHERE chat_id = ?",
                    (str(chat_id),)
                ).fetchone()
                if not row or row["ai_allowed"] != 1:
                    return False

                exp_raw = row["ai_expires_at"]
                if exp_raw is None:
                    # Permanent grant
                    return True

                exp_dt = _parse_utc_dt(exp_raw)
                if exp_dt is None:
                    return True

                now_utc = datetime.now(timezone.utc)
                if exp_dt > now_utc:
                    return True

                # Expired! Auto-deactivate user
                logger.info("Auto-deactivating expired AI access for user %s (expired at %s)", chat_id, exp_raw)
                conn.execute(
                    "UPDATE users SET ai_allowed = 0, last_active = CURRENT_TIMESTAMP WHERE chat_id = ?",
                    (str(chat_id),)
                )
                return False
        except Exception as e:
            logger.warning("Error checking AI allowance for %s: %s", chat_id, e)
            return False

    def get_ai_status(self, chat_id: int | str) -> dict:
        """Return detailed AI subscription and trial status for a user."""
        cid_str = str(chat_id).strip()
        is_admin_user = False
        try:
            if int(cid_str) in settings.admin_user_ids:
                is_admin_user = True
        except (ValueError, TypeError):
            pass

        from circumorari_bot.data.settings_repo import SettingsRepository
        admin_test_mode = is_admin_user and SettingsRepository(self.db_path).get_admin_ai_test_mode(cid_str)

        user = self.get_user(cid_str)
        if not user:
            is_vip = is_admin_user and not admin_test_mode
            return {
                "chat_id": cid_str,
                "is_admin": is_vip,
                "admin_test_mode": admin_test_mode,
                "ai_allowed": is_vip,
                "is_permanent": is_vip,
                "ai_expires_at": None,
                "days_remaining": None,
                "is_expired": False,
                "trial_used": False,
            }

        ai_allowed = bool(user.get("ai_allowed"))
        trial_used = bool(user.get("trial_used"))
        exp_raw = user.get("ai_expires_at")
        exp_dt = _parse_utc_dt(exp_raw)

        is_expired = False
        days_remaining = None
        is_permanent = False

        if is_admin_user and not admin_test_mode:
            is_permanent = True
            ai_allowed = True
        elif ai_allowed:
            if exp_dt is None:
                is_permanent = True
            else:
                now_utc = datetime.now(timezone.utc)
                if exp_dt <= now_utc:
                    is_expired = True
                    ai_allowed = False
                    # Auto-deactivate in DB
                    self.set_ai_allowed(cid_str, False)
                else:
                    diff = exp_dt - now_utc
                    days_remaining = max(0, diff.days + (1 if diff.seconds > 0 else 0))
        elif exp_dt is not None:
            now_utc = datetime.now(timezone.utc)
            if exp_dt <= now_utc:
                is_expired = True

        return {
            "chat_id": cid_str,
            "is_admin": is_admin_user and not admin_test_mode,
            "admin_test_mode": admin_test_mode,
            "ai_allowed": ai_allowed if admin_test_mode else (is_admin_user or ai_allowed),
            "is_permanent": is_permanent,
            "ai_expires_at": exp_dt.strftime("%Y-%m-%d %H:%M:%S") if exp_dt else None,
            "days_remaining": days_remaining,
            "is_expired": is_expired,
            "trial_used": trial_used,
        }

    def set_ai_allowed(self, chat_id: int | str, allowed: bool) -> bool:
        """Enable or revoke AI usage permission for a chat ID."""
        if not self._is_valid_chat_id(chat_id):
            return False
        cid_str = str(chat_id).strip()

        # If admin is disabling AI for themselves, enable test mode so revocation takes effect
        try:
            cid_int = int(cid_str)
            if cid_int in settings.admin_user_ids:
                from circumorari_bot.data.settings_repo import SettingsRepository
                SettingsRepository(self.db_path).set_admin_ai_test_mode(cid_int, not allowed)
        except (ValueError, TypeError):
            pass

        try:
            with get_db(self.db_path) as conn:
                cur = conn.cursor()
                if not allowed:
                    cur.execute("""
                        INSERT INTO users (chat_id, ai_allowed, is_subscribed, last_active)
                        VALUES (?, 0, 0, CURRENT_TIMESTAMP)
                        ON CONFLICT(chat_id) DO UPDATE SET
                            ai_allowed = 0,
                            ai_expires_at = NULL,
                            last_active = CURRENT_TIMESTAMP
                    """, (cid_str,))
                else:
                    cur.execute("""
                        INSERT INTO users (chat_id, ai_allowed, is_subscribed, last_active)
                        VALUES (?, 1, 0, CURRENT_TIMESTAMP)
                        ON CONFLICT(chat_id) DO UPDATE SET
                            ai_allowed = 1,
                            last_active = CURRENT_TIMESTAMP
                    """, (cid_str,))
                return True
        except Exception as e:
            logger.error("Error setting AI allowance for user %s: %s", chat_id, e)
            return False

    def set_ai_permanent(self, chat_id: int | str) -> bool:
        """Grant permanent/lifetime AI access (ai_allowed=1, ai_expires_at=NULL)."""
        if not self._is_valid_chat_id(chat_id):
            return False
        cid_str = str(chat_id).strip()

        # If admin, ensure test mode is disabled so permanent VIP status is restored
        try:
            cid_int = int(cid_str)
            if cid_int in settings.admin_user_ids:
                from circumorari_bot.data.settings_repo import SettingsRepository
                SettingsRepository(self.db_path).set_admin_ai_test_mode(cid_int, False)
        except (ValueError, TypeError):
            pass

        try:
            with get_db(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO users (chat_id, ai_allowed, ai_expires_at, is_subscribed, last_active)
                    VALUES (?, 1, NULL, 0, CURRENT_TIMESTAMP)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        ai_allowed = 1,
                        ai_expires_at = NULL,
                        last_active = CURRENT_TIMESTAMP
                """, (cid_str,))
                return True
        except Exception as e:
            logger.error("Error setting permanent AI for user %s: %s", chat_id, e)
            return False

    def activate_trial(self, chat_id: int | str, days: int = 3) -> Tuple[bool, str]:
        """Activate the one-time free AI trial for a user.
        
        Returns (True, expires_at_str) on success, or (False, reason) if trial was already used.
        """
        if not self._is_valid_chat_id(chat_id):
            return False, "invalid_chat_id"
        cid_str = str(chat_id).strip()
        user = self.get_user(cid_str)
        if user and user.get("trial_used"):
            return False, "already_used"

        now_utc = datetime.now(timezone.utc)
        expires_at = now_utc + timedelta(days=max(1, days))
        exp_str = expires_at.strftime("%Y-%m-%d %H:%M:%S")

        try:
            with get_db(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO users (chat_id, ai_allowed, ai_expires_at, trial_used, is_subscribed, last_active)
                    VALUES (?, 1, ?, 1, 0, CURRENT_TIMESTAMP)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        ai_allowed = 1,
                        ai_expires_at = excluded.ai_expires_at,
                        trial_used = 1,
                        last_active = CURRENT_TIMESTAMP
                """, (cid_str, exp_str))
                return True, exp_str
        except Exception as e:
            logger.error("Error activating trial for user %s: %s", chat_id, e)
            return False, str(e)

    def extend_ai_subscription(self, chat_id: int | str, days: int = 30) -> Tuple[bool, str]:
        """Add subscription days for a user, extending from current expiration if active, or from now.
        
        The month is strictly rolling calendar days (default 30) from activation or remaining time.
        """
        if not self._is_valid_chat_id(chat_id):
            return False, "invalid_chat_id"
        cid_str = str(chat_id).strip()
        user = self.get_user(cid_str)
        now_utc = datetime.now(timezone.utc)

        base_dt = now_utc
        if user and user.get("ai_allowed") and user.get("ai_expires_at"):
            curr_exp = _parse_utc_dt(user.get("ai_expires_at"))
            if curr_exp and curr_exp > now_utc:
                base_dt = curr_exp

        new_expires_at = base_dt + timedelta(days=max(1, days))
        exp_str = new_expires_at.strftime("%Y-%m-%d %H:%M:%S")

        try:
            with get_db(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO users (chat_id, ai_allowed, ai_expires_at, is_subscribed, last_active)
                    VALUES (?, 1, ?, 0, CURRENT_TIMESTAMP)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        ai_allowed = 1,
                        ai_expires_at = excluded.ai_expires_at,
                        last_active = CURRENT_TIMESTAMP
                """, (cid_str, exp_str))
                return True, exp_str
        except Exception as e:
            logger.error("Error extending AI subscription for user %s: %s", chat_id, e)
            return False, str(e)

    def reset_trial(self, chat_id: int | str) -> bool:
        """Reset trial status for user so they can try trial again if admin permits."""
        if not self._is_valid_chat_id(chat_id):
            return False
        cid_str = str(chat_id).strip()
        try:
            with get_db(self.db_path) as conn:
                conn.execute("UPDATE users SET trial_used = 0 WHERE chat_id = ?", (cid_str,))
                return True
        except Exception as e:
            logger.error("Error resetting trial for user %s: %s", chat_id, e)
            return False

    def deactivate_expired_users(self) -> int:
        """Auto-deactivate users whose AI subscription/trial has passed expiration."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        try:
            with get_db(self.db_path) as conn:
                cur = conn.cursor()
                res = cur.execute("""
                    UPDATE users
                    SET ai_allowed = 0, last_active = CURRENT_TIMESTAMP
                    WHERE ai_allowed = 1
                      AND ai_expires_at IS NOT NULL
                      AND ai_expires_at <= ?
                """, (now_str,))
                count = res.rowcount
                if count > 0:
                    logger.info("Auto-deactivated %d expired AI users", count)
                return count
        except Exception as e:
            logger.warning("Error running auto-deactivation of expired AI users: %s", e)
            return 0

    def get_ai_allowed_chat_ids(self) -> List[str]:
        """Return list of chat IDs authorized to use AI."""
        try:
            with get_db(self.db_path) as conn:
                cur = conn.cursor()
                rows = cur.execute("""
                    SELECT chat_id FROM users
                    WHERE ai_allowed = 1
                """).fetchall()
                return [row["chat_id"] for row in rows]
        except Exception as e:
            logger.warning("Error fetching AI allowed users: %s", e)
            return []

    def get_user(self, identifier: int | str) -> Optional[dict]:
        """Find a single user by chat_id or username (case-insensitive, with or without @)."""
        if identifier is None:
            return None
        ident_str = str(identifier).strip()
        if not ident_str:
            return None
        clean_uname = ident_str.lstrip("@")
        try:
            with get_db(self.db_path) as conn:
                cur = conn.cursor()
                row = cur.execute("""
                    SELECT chat_id, username, first_name, last_active, is_subscribed, ai_allowed, ai_expires_at, trial_used
                    FROM users
                    WHERE chat_id = ?
                       OR (username IS NOT NULL AND LOWER(username) = LOWER(?))
                    LIMIT 1
                """, (ident_str, clean_uname)).fetchone()
                return dict(row) if row else None
        except Exception as e:
            logger.warning("Error fetching user %s: %s", identifier, e)
            return None

    def search_users(self, query: str = "", limit: int = 50) -> List[dict]:
        """Search users by username, first_name, or chat_id (or list all if query is empty)."""
        q = (query or "").strip().lstrip("@")
        try:
            with get_db(self.db_path) as conn:
                cur = conn.cursor()
                if not q:
                    rows = cur.execute("""
                        SELECT chat_id, username, first_name, last_active, is_subscribed, ai_allowed, ai_expires_at, trial_used
                        FROM users
                        ORDER BY last_active DESC
                        LIMIT ?
                    """, (limit,)).fetchall()
                else:
                    like_q = f"%{q}%"
                    rows = cur.execute("""
                        SELECT chat_id, username, first_name, last_active, is_subscribed, ai_allowed, ai_expires_at, trial_used
                        FROM users
                        WHERE chat_id LIKE ?
                           OR (username IS NOT NULL AND LOWER(username) LIKE LOWER(?))
                           OR (first_name IS NOT NULL AND LOWER(first_name) LIKE LOWER(?))
                        ORDER BY last_active DESC
                        LIMIT ?
                    """, (like_q, like_q, like_q, limit)).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("Error searching users with query '%s': %s", query, e)
            return []

    def get_all_users(self, filter_mode: str = "all", limit: int = 100) -> List[dict]:
        """Return list of user dicts filtered by 'all', 'sub', or 'ai'."""
        try:
            with get_db(self.db_path) as conn:
                cur = conn.cursor()
                if filter_mode == "sub":
                    sql = """
                        SELECT chat_id, username, first_name, last_active, is_subscribed, ai_allowed, ai_expires_at, trial_used
                        FROM users WHERE is_subscribed = 1
                        ORDER BY last_active DESC LIMIT ?
                    """
                elif filter_mode == "ai":
                    sql = """
                        SELECT chat_id, username, first_name, last_active, is_subscribed, ai_allowed, ai_expires_at, trial_used
                        FROM users WHERE ai_allowed = 1
                        ORDER BY last_active DESC LIMIT ?
                    """
                else:
                    sql = """
                        SELECT chat_id, username, first_name, last_active, is_subscribed, ai_allowed, ai_expires_at, trial_used
                        FROM users
                        ORDER BY last_active DESC LIMIT ?
                    """
                rows = cur.execute(sql, (limit,)).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("Error listing users (mode=%s): %s", filter_mode, e)
            return []

    def resolve_chat_id(self, identifier: int | str) -> Optional[str]:
        """Resolve a username (e.g. @mario or mario) or chat_id to a target chat_id string."""
        if identifier is None:
            return None
        ident_str = str(identifier).strip()
        if not ident_str:
            return None
        user = self.get_user(ident_str)
        if user:
            return str(user["chat_id"])
        # If numeric chat_id not yet in DB, allow direct ID targeting
        if ident_str.lstrip("-").isdigit():
            return ident_str
        return None

    def get_stats(self) -> dict:
        """Return user count statistics including AI subscriptions and trials."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        try:
            with get_db(self.db_path) as conn:
                cur = conn.cursor()
                total = cur.execute("SELECT count(*) as cnt FROM users").fetchone()["cnt"]
                subscribed = cur.execute("SELECT count(*) as cnt FROM users WHERE is_subscribed = 1").fetchone()["cnt"]
                ai_allowed = cur.execute("SELECT count(*) as cnt FROM users WHERE ai_allowed = 1").fetchone()["cnt"]
                ai_active = cur.execute("""
                    SELECT count(*) as cnt FROM users
                    WHERE ai_allowed = 1
                      AND (ai_expires_at IS NULL OR ai_expires_at > ?)
                """, (now_str,)).fetchone()["cnt"]
                trials_used = cur.execute("SELECT count(*) as cnt FROM users WHERE trial_used = 1").fetchone()["cnt"]
                ai_expired = cur.execute("""
                    SELECT count(*) as cnt FROM users
                    WHERE ai_expires_at IS NOT NULL AND ai_expires_at <= ?
                """, (now_str,)).fetchone()["cnt"]
                cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
                active_30d = cur.execute("SELECT count(*) as cnt FROM users WHERE last_active >= ?", (cutoff,)).fetchone()["cnt"]
                return {
                    "total_users": total,
                    "subscribed_users": subscribed,
                    "ai_allowed_users": ai_allowed,
                    "ai_active_users": ai_active,
                    "ai_trial_users": trials_used,
                    "ai_expired_users": ai_expired,
                    "active_last_30_days": active_30d
                }
        except Exception as e:
            logger.warning("Error reading user stats: %s", e)
            return {
                "total_users": 0, "subscribed_users": 0, "ai_allowed_users": 0,
                "ai_active_users": 0, "ai_trial_users": 0, "ai_expired_users": 0,
                "active_last_30_days": 0
            }

    def prune_inactive(self, days: int = 60) -> int:
        """Clean up old users not seen in N days while preserving subscribed and unexpired AI users."""
        try:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            with get_db(self.db_path) as conn:
                cur = conn.cursor()
                res = cur.execute("""
                    DELETE FROM users
                    WHERE last_active < ?
                      AND is_subscribed = 0
                      AND (
                          ai_allowed = 0
                          OR (ai_expires_at IS NOT NULL AND ai_expires_at <= ?)
                      )
                """, (cutoff, now_str))
                return res.rowcount
        except Exception as e:
            logger.warning("Error pruning inactive users: %s", e)
            return 0
