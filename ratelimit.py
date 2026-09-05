"""Shared outbound rate limiting.

One token bucket per fetch run, shared by every worker in it. The distinction
matters: the original scraper slept between requests inside each worker, so
the only way to slow a run down was to make every individual request slower.
A bucket caps the *aggregate* rate instead, which lets concurrency and
politeness be set independently.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """Thread-safe token bucket capping the aggregate request rate.

    Tokens accrue continuously at ``rate`` per second up to ``burst``. A worker
    takes one before each request and blocks if the bucket is empty, so N
    workers share one budget rather than each holding their own.
    """

    def __init__(self, rate: float, burst: int) -> None:
        self._rate = max(rate, 0.1)
        self._burst = max(burst, 1)
        self._tokens = float(self._burst)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def take(self, tokens: float = 1.0) -> None:
        """Block until ``tokens`` are available, then spend them."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._burst, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return

                deficit = tokens - self._tokens
                wait = deficit / self._rate

            # Slept outside the lock so other workers can keep refilling.
            time.sleep(min(wait, 1.0))
