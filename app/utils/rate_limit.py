import time
from collections import defaultdict, deque


class RateLimiter:
    """Sliding-window limiter kept in memory, keyed by client IP.

    Analyses cost money (LLM tokens), so a public demo needs some protection.
    For several instances this would move to Redis.
    """

    def __init__(self, max_requests: int, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> float | None:
        """Record a hit. Returns None if allowed, otherwise seconds until the next free slot."""
        if self.max_requests <= 0:
            return None

        now = time.monotonic()
        hits = self._hits[key]
        while hits and hits[0] <= now - self.window:
            hits.popleft()

        if len(hits) >= self.max_requests:
            return round(hits[0] + self.window - now, 1)

        hits.append(now)
        if len(self._hits) > 10_000:  # don't let the dict grow forever
            self._hits = defaultdict(deque, {k: v for k, v in self._hits.items() if v})
        return None
