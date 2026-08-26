"""Bandwidth throttling and priority scheduling for the download engine.

The TokenBucketThrottler implements a classic token-bucket rate limiter
that can be applied globally and per-job. The engine's monitor loop
calls ``acquire(bytes)`` before each chunk write to enforce the limit.

Priority scheduling is handled by the engine's ``_try_start_jobs()``
which picks queued jobs in priority order. This module provides the
``PriorityScheduler`` helper that sorts the job queue."""
from __future__ import annotations

import threading
import time
from typing import Dict, List


class TokenBucketThrottler:
    """Thread-safe token-bucket rate limiter.

    A rate of 0 means unlimited (no throttling). Tokens are replenished
    at ``rate`` bytes per second. ``acquire(n)`` blocks until n tokens
    are available."""

    def __init__(self, rate_bps: int = 0, capacity: int = 0) -> None:
        self._rate = rate_bps
        self._capacity = capacity or rate_bps or (10 * 1024 * 1024)
        self._tokens = float(self._capacity)
        self._last_refill = time.time()
        self._lock = threading.Lock()

    @property
    def rate(self) -> int:
        return self._rate

    def set_rate(self, rate_bps: int) -> None:
        """Update the rate. 0 = unlimited."""
        with self._lock:
            self._rate = rate_bps
            if rate_bps > 0:
                self._capacity = max(self._capacity, rate_bps)
            self._tokens = float(self._capacity)

    def acquire(self, n: int) -> None:
        """Block until n tokens are available. Returns immediately if unlimited."""
        if self._rate <= 0:
            return
        while True:
            with self._lock:
                now = time.time()
                elapsed = now - self._last_refill
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._last_refill = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                # Calculate wait time for enough tokens.
                deficit = n - self._tokens
                wait = deficit / self._rate
            time.sleep(min(wait, 0.5))


class PriorityScheduler:
    """Sorts queued jobs by priority (higher = first) then creation time.

    Used by the engine to decide which jobs to start next when a slot
    becomes available."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def sort_queue(self, jobs: List) -> List:
        """Return jobs sorted by priority (desc) then created_at (asc)."""
        with self._lock:
            return sorted(jobs, key=lambda j: (-j.priority, j.created_at))
