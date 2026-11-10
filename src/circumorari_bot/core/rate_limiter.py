"""In-memory thread-safe sliding-window rate limiter for AI queries."""

import time
import math
import threading
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


class AIRateLimiter:
    """Thread-safe sliding-window rate limiter per user."""

    def __init__(self) -> None:
        self._history: Dict[int, List[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def check_rate_limit(
        self,
        user_id: int,
        max_requests: int = 3,
        window_seconds: int = 60,
        current_time: Optional[float] = None,
    ) -> Tuple[bool, int]:
        """Check if request is allowed for user_id under the sliding window.

        Args:
            user_id: Telegram chat/user identifier.
            max_requests: Maximum requests allowed in the time window.
            window_seconds: Duration of the sliding window in seconds.
            current_time: Optional timestamp override (useful for deterministic testing).

        Returns:
            Tuple[bool, int]: (is_allowed, retry_after_seconds).
            If allowed: returns (True, 0).
            If rate limited: returns (False, retry_after) with positive integer seconds.
        """
        now = time.time() if current_time is None else current_time
        cutoff = now - window_seconds

        with self._lock:
            timestamps = self._history[user_id]
            # Discard timestamps that fell outside the sliding window
            valid_timestamps = [ts for ts in timestamps if ts > cutoff]
            self._history[user_id] = valid_timestamps

            if len(valid_timestamps) < max_requests:
                valid_timestamps.append(now)
                return True, 0

            # Calculate retry_after from the oldest request within the active window
            oldest = valid_timestamps[0]
            retry_after = max(1, math.ceil(oldest + window_seconds - now))
            return False, retry_after

    def reset(self, user_id: Optional[int] = None) -> None:
        """Reset history for a single user or all users (testing / admin override)."""
        with self._lock:
            if user_id is not None:
                self._history.pop(user_id, None)
            else:
                self._history.clear()


# Global singleton instance
ai_rate_limiter = AIRateLimiter()
