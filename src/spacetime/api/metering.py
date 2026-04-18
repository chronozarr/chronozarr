"""Lightweight usage metering.

Tracks per-key request counts in memory. Designed to be replaced with a
persistent backend (SQLite, Redis, Postgres) when TileRipper goes to prod.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class KeyUsage:
    """Usage counters for a single API key within a billing period."""

    requests: int = 0
    tile_requests: int = 0
    query_requests: int = 0
    stats_requests: int = 0
    raw_requests: int = 0
    bytes_served: int = 0
    period_start: float = field(default_factory=time.time)


class UsageMeter:
    """In-memory usage tracker. One instance per app lifetime."""

    def __init__(self) -> None:
        self._usage: dict[str, KeyUsage] = defaultdict(KeyUsage)

    def record(self, key: str, endpoint: str, *, bytes_served: int = 0) -> None:
        """Record a request."""
        u = self._usage[key]
        u.requests += 1
        u.bytes_served += bytes_served

        if endpoint == "tile":
            u.tile_requests += 1
        elif endpoint == "query":
            u.query_requests += 1
        elif endpoint == "stats":
            u.stats_requests += 1
        elif endpoint == "raw":
            u.raw_requests += 1

    def get(self, key: str) -> KeyUsage:
        """Get usage for a key."""
        return self._usage[key]

    def reset(self, key: str) -> None:
        """Reset counters for a key (e.g. new billing period)."""
        self._usage[key] = KeyUsage()


# Singleton instance
meter = UsageMeter()
