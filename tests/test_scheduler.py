"""dlmgr/scheduler.py — token-bucket throttler + priority queue sorting.

Untested until the 2026-11-06 review; the throttler's refill/blocking math
is exactly the kind of code that rots silently (rate 0, capacity floors,
multi-thread sharing)."""
from __future__ import annotations

import threading
import time

from dlmgr.job import DownloadJob
from dlmgr.scheduler import PriorityScheduler, TokenBucketThrottler


def test_throttler_zero_rate_is_unlimited():
    t = TokenBucketThrottler(rate_bps=0)
    start = time.monotonic()
    t.acquire(100 * 1024 * 1024)  # must return immediately
    assert time.monotonic() - start < 0.5


def test_throttler_limits_average_rate():
    # 200 KiB/s with a 400 KiB bucket: drain it, then a 200 KiB acquire
    # needs ~1 s of refill.
    t = TokenBucketThrottler(rate_bps=200 * 1024, capacity=400 * 1024)
    t.acquire(400 * 1024)  # drains the full bucket
    start = time.monotonic()
    t.acquire(200 * 1024)
    elapsed = time.monotonic() - start
    assert 0.5 <= elapsed <= 3.0, elapsed


def test_throttler_oversized_request_does_not_deadlock():
    # Regression 2026-11-06: acquire(n) with n > capacity used to spin
    # forever — the refill caps at capacity, so `tokens >= n` never fired.
    # Oversized requests are now granted once the bucket is full.
    t = TokenBucketThrottler(rate_bps=200 * 1024, capacity=50 * 1024)
    start = time.monotonic()
    t.acquire(400 * 1024)  # 8x the bucket capacity
    assert time.monotonic() - start < 2.0
    # The oversized grant drains the bucket: a small follow-up must wait.
    start = time.monotonic()
    t.acquire(50 * 1024)
    elapsed = time.monotonic() - start
    assert 0.1 <= elapsed <= 2.0, elapsed


def test_throttler_set_rate_zero_unblocks_immediately():
    t = TokenBucketThrottler(rate_bps=50 * 1024, capacity=10 * 1024)
    t.acquire(20 * 1024)  # drain the bucket past its burst
    t.set_rate(0)
    start = time.monotonic()
    t.acquire(10 * 1024 * 1024)
    assert time.monotonic() - start < 0.5


def test_throttler_shared_across_threads_never_raises():
    t = TokenBucketThrottler(rate_bps=2 * 1024 * 1024, capacity=512 * 1024)
    errors = []

    def worker():
        try:
            t.acquire(256 * 1024)
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)
    assert not any(th.is_alive() for th in threads)
    assert not errors


def test_priority_scheduler_sorts_priority_then_age():
    def job(priority: int, created_at: float) -> DownloadJob:
        j = DownloadJob(filename="f", save_path="f")
        j.priority = priority
        j.created_at = created_at
        return j

    j1 = job(5, 100.0)
    j2 = job(9, 200.0)  # highest priority wins even though newest
    j3 = job(5, 50.0)   # same priority: older first
    out = PriorityScheduler().sort_queue([j1, j2, j3])
    assert out == [j2, j3, j1]
