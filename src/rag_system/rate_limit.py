"""Lightweight in-process rate limiting for sensitive endpoints.

Provides a sliding-window limiter and a FastAPI dependency factory used to
throttle abuse-prone routes (login, registration, token refresh). The limiter
is per-process and in-memory: it adds meaningful protection for a single
instance without an external dependency, but it does NOT coordinate across
replicas. For a multi-instance deployment, front the API with a shared limiter
(e.g. a reverse proxy or Redis-backed limiter) as well.

Keying favours the client IP. Behind a trusted proxy the left-most
``X-Forwarded-For`` entry is used; otherwise the socket peer address. Because
``X-Forwarded-For`` can be spoofed by a direct client, deploy this behind a
proxy that overwrites the header for untrusted hops.
"""

from __future__ import annotations

import time
from threading import Lock

from fastapi import HTTPException, Request, status

from rag_system.observability import get_logger, metrics

logger = get_logger(__name__)

__all__ = ["SlidingWindowRateLimiter", "rate_limit"]


class SlidingWindowRateLimiter:
    """Allow at most *limit* events per *window_seconds* for a given key.

    Uses a sliding window of event timestamps per key. Thread-safe; empty
    buckets are pruned as keys go idle so memory tracks the active key set.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        if limit < 1:
            raise ValueError("rate limit must be at least 1")
        self._limit = limit
        self._window = float(window_seconds)
        self._hits: dict[str, list[float]] = {}
        self._lock = Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """Record an attempt for *key*.

        Returns ``(allowed, retry_after_seconds)``. When the limit is exceeded
        the attempt is not recorded and ``retry_after_seconds`` indicates how
        long until the oldest in-window event ages out.
        """
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                bucket = []
                self._hits[key] = bucket

            # Drop events that have aged out of the window.
            drop = 0
            for ts in bucket:
                if ts <= cutoff:
                    drop += 1
                else:
                    break
            if drop:
                del bucket[:drop]

            if len(bucket) >= self._limit:
                retry_after = max(1, int(self._window - (now - bucket[0]) + 0.999))
                return False, retry_after

            bucket.append(now)
            if not bucket:  # pragma: no cover - defensive
                self._hits.pop(key, None)
            return True, 0


def _client_key(request: Request) -> str:
    """Best-effort client identifier for rate-limit bucketing."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # Left-most entry is the original client per the XFF convention.
        return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


def rate_limit(limiter: SlidingWindowRateLimiter, *, scope: str):
    """Build a FastAPI dependency enforcing *limiter* for a named *scope*.

    The bucket key combines the scope and the client identifier, so different
    endpoints throttle independently. Exceeding the limit raises HTTP 429 with a
    ``Retry-After`` header.
    """

    def dependency(request: Request) -> None:
        key = f"{scope}:{_client_key(request)}"
        allowed, retry_after = limiter.check(key)
        if not allowed:
            metrics.increment("rag_rate_limited_total", {"scope": scope})
            logger.warning(
                "Rate limit exceeded for %s", scope, extra={"path": request.url.path}
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please slow down and try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )

    return dependency
