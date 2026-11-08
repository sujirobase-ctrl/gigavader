"""Thread-safe token-bucket rate limiter for capping aggregate download speed.

Ported from Lysagxra/BunkrDownloader (GPL-3.0) and made host-agnostic so it caps
bandwidth for *every* download in the run, regardless of which host served the
file. A single RateLimiter instance is shared across every download thread (all
chunks of all files), so --rate-limit caps total process bandwidth rather than a
per-connection or per-file limit that would be multiplied by --connections.

Design note: tokens are allowed to go negative (debt model) instead of a spin-wait
loop. A single call may request more bytes than the bucket burst capacity (e.g. one
large HTTP read) — the resulting sleep repays that debt at the configured rate.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """Caps aggregate throughput across every thread sharing this instance."""

    def __init__(self, rate_bytes_per_sec: float | None) -> None:
        """Initialize the limiter.

        ``rate_bytes_per_sec``: max aggregate bytes/sec across all callers.
        ``None`` or a non-positive value disables throttling (consume is a no-op).
        """
        self.rate = (
            rate_bytes_per_sec if rate_bytes_per_sec and rate_bytes_per_sec > 0
            else None
        )
        self._tokens = float(self.rate or 0)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    @property
    def is_limited(self) -> bool:
        """Return True if a rate cap is active."""
        return self.rate is not None

    def consume(self, n_bytes: int, cancel_event: threading.Event | None = None) -> None:
        """Block the calling thread until ``n_bytes`` of bandwidth budget is free.

        Safe to call concurrently from many threads; each call only blocks the
        thread that called it. Other threads keep spending/accumulating tokens
        independently while one is sleeping.
        """
        if self.rate is None or n_bytes <= 0:
            return

        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._last_refill = now
            self._tokens = min(self.rate, self._tokens + elapsed * self.rate)
            self._tokens -= n_bytes
            deficit = -self._tokens

        if deficit > 0:
            delay = deficit / self.rate
            if cancel_event is None:
                time.sleep(delay)
            else:
                cancel_event.wait(delay)
