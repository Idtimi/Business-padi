"""Rate limiting and external API call resilience with exponential backoff."""

import asyncio
import logging
import time
from collections import defaultdict

import httpx

from app.config import settings

log = logging.getLogger(__name__)


class RateLimiter:
    """Sliding-window per-user rate limiter."""

    def __init__(self, default_limit: int = 30, window_seconds: int = 60):
        self.default_limit = default_limit
        self.window_seconds = window_seconds
        self.history: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, user_ref: str, limit: int | None = None) -> tuple[bool, int]:
        """Checks if a user is within their rate limit.

        Returns (is_allowed, remaining_quota).
        """
        now = time.time()
        max_requests = limit or self.default_limit
        cutoff = now - self.window_seconds

        # Prune older entries
        timestamps = [ts for ts in self.history[user_ref] if ts > cutoff]
        self.history[user_ref] = timestamps

        if len(timestamps) >= max_requests:
            return False, 0

        self.history[user_ref].append(now)
        remaining = max_requests - len(self.history[user_ref])
        return True, remaining

    def reset(self, user_ref: str | None = None):
        """Clears tracking for testing."""
        if user_ref:
            self.history.pop(user_ref, None)
        else:
            self.history.clear()


limiter = RateLimiter(default_limit=settings.rate_limit_per_minute)


RETRY_EXCEPTIONS = (httpx.RequestError, httpx.HTTPStatusError, ConnectionError, TimeoutError, OSError)


async def call_with_retry(
    coro_fn,
    *args,
    max_retries: int = 3,
    base_delay: float = 0.5,
    backoff_factor: float = 2.0,
    retry_exceptions: tuple = RETRY_EXCEPTIONS,
    **kwargs,
):
    """Executes an async function with exponential backoff on network / 5xx errors."""
    attempt = 0
    delay = base_delay

    while True:
        try:
            return await coro_fn(*args, **kwargs)
        except retry_exceptions as e:
            attempt += 1
            # If it's a 4xx error (client error), do not retry
            if isinstance(e, httpx.HTTPStatusError) and 400 <= e.response.status_code < 500:
                raise e

            if attempt >= max_retries:
                log.error("Exceeded max retries (%d) for %s: %s", max_retries, getattr(coro_fn, "__name__", str(coro_fn)), str(e))
                raise e

            log.warning(
                "External call %s failed (attempt %d/%d): %s. Retrying in %.2fs...",
                getattr(coro_fn, "__name__", str(coro_fn)),
                attempt,
                max_retries,
                str(e),
                delay,
            )
            await asyncio.sleep(delay)
            delay *= backoff_factor
