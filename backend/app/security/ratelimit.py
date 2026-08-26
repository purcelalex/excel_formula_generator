"""
ratelimit.py — a fixed budget of requests per client, per window.

Deliberately in-process and deliberately simple. It is the right size for one
instance of a free tool: it costs nothing, needs no Redis, and stops the
realistic abuse case, which is a script hammering the endpoint rather than a
distributed attack.

Two known limits, stated so they are not discovered in production:
  * With more than one instance behind a load balancer, each instance counts
    separately, so the effective limit multiplies. The same is true of
    Cloudflare Workers, where each isolate keeps its own counters — this stops
    a single script hammering one isolate, which is the realistic abuse case,
    but it is not a global limit. Cloudflare's own rate-limiting rules, applied
    at the edge in front of the Worker, are the answer when you need one.
  * The client IP comes from the connection unless a trusted proxy header is
    configured. Never trust X-Forwarded-For from the open internet — anyone can
    set it, which turns a rate limiter into a rate suggestion.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def check(self, client: str) -> tuple[bool, int]:
        """Return (allowed, seconds_until_retry)."""
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            hits = self._hits[client]
            while hits and hits[0] <= now - self.window_seconds:
                hits.popleft()

            if len(hits) >= self.max_requests:
                retry_after = int(self.window_seconds - (now - hits[0])) + 1
                return False, max(retry_after, 1)

            hits.append(now)
            return True, 0

    def _sweep(self, now: float) -> None:
        """Drop clients that have gone quiet.

        Without this the dictionary grows once per unique IP forever, which is a
        slow memory leak on a public endpoint.
        """
        if now - self._last_sweep < self.window_seconds:
            return
        self._last_sweep = now
        cutoff = now - self.window_seconds
        for client in [c for c, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[client]
