"""Small in-process token-bucket limiter for public capability routes.

This is a per-process defense-in-depth control, not a replacement for an edge
rate limit. It bounds accidental or single-source abuse even in the reference
deployment, while operators can add AWS WAF or another distributed limiter in
front of the ALB for stronger guarantees.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketRateLimiter:
    """Thread-safe per-key token bucket with bounded key cardinality."""

    def __init__(
        self,
        *,
        capacity: int,
        refill_period_seconds: float = 60.0,
        max_keys: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or refill_period_seconds <= 0 or max_keys < 1:
            raise ValueError("rate limiter bounds must be positive")
        self._capacity = float(capacity)
        self._refill_rate = self._capacity / refill_period_seconds
        self._idle_ttl = refill_period_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> int | None:
        """Consume one token, or return seconds until the next is available."""
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                self._prune_idle(now)
                if len(self._buckets) >= self._max_keys:
                    return max(1, math.ceil(1 / self._refill_rate))
                bucket = _Bucket(tokens=self._capacity, updated_at=now)

            elapsed = max(0.0, now - bucket.updated_at)
            available = min(
                self._capacity,
                bucket.tokens + elapsed * self._refill_rate,
            )
            if available < 1.0:
                bucket.tokens = available
                bucket.updated_at = now
                self._buckets[key] = bucket
                return max(1, math.ceil((1.0 - available) / self._refill_rate))

            bucket.tokens = available - 1.0
            bucket.updated_at = now
            self._buckets[key] = bucket
            return None

    def _prune_idle(self, now: float) -> None:
        stale = [
            key
            for key, bucket in self._buckets.items()
            if now - bucket.updated_at >= self._idle_ttl
        ]
        for key in stale:
            self._buckets.pop(key, None)

    def reset(self) -> None:
        """Clear process-local state for deterministic tests."""
        with self._lock:
            self._buckets.clear()
