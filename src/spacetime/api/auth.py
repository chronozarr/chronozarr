"""API key authentication and tier-based access control.

Key format: tr_{tier_code}_{identifier}
Tier codes: dev, exp (explorer), bld (builder), pro

In dev mode (no key provided), requests default to dev tier with full access.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Key prefix constants
_PREFIX_DEV = "tr_dev_"
_PREFIX_EXPLORER = "tr_exp_"
_PREFIX_BUILDER = "tr_bld_"
_PREFIX_PRO = "tr_pro_"

# Built-in local development key
LOCAL_DEV_KEY = _PREFIX_DEV + "local"

# Tier definitions: rate limits are requests per minute
TIERS = {
    "dev": {"rate_limit": None, "monthly_quota": None},
    "explorer": {"rate_limit": 100, "monthly_quota": 50_000},
    "builder": {"rate_limit": 500, "monthly_quota": 500_000},
    "pro": {"rate_limit": 2000, "monthly_quota": 5_000_000},
}

# Prefix → tier mapping
_PREFIX_MAP = {
    _PREFIX_DEV: "dev",
    _PREFIX_EXPLORER: "explorer",
    _PREFIX_BUILDER: "builder",
    _PREFIX_PRO: "pro",
}

# Simple in-memory rate limiter: key → list of request timestamps
_rate_windows: dict[str, list[float]] = defaultdict(list)
_WINDOW_SECONDS = 60.0


def resolve_tier(key: str) -> str | None:
    """Resolve an API key to its tier. Returns None if invalid."""
    for prefix, tier in _PREFIX_MAP.items():
        if key.startswith(prefix) and len(key) > len(prefix):
            return tier
    return None


def check_rate_limit(key: str, tier: str) -> bool:
    """Check if a key is within its rate limit. Returns True if allowed."""
    limit = TIERS[tier]["rate_limit"]
    if limit is None:
        return True

    now = time.monotonic()
    window = _rate_windows[key]

    # Prune old entries
    cutoff = now - _WINDOW_SECONDS
    _rate_windows[key] = [t for t in window if t > cutoff]

    if len(_rate_windows[key]) >= limit:
        return False

    _rate_windows[key].append(now)
    return True


class AuthMiddleware(BaseHTTPMiddleware):
    """Extract API key, resolve tier, enforce rate limits.

    Attaches request.state.tier and request.state.api_key.
    Non-API routes (/, /docs, /static) skip auth entirely.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Skip auth for non-API routes
        if not path.startswith("/v1/"):
            return await call_next(request)

        # Extract key from multiple sources
        key = _extract_key(request)

        if not key:
            # Dev mode: no key = dev tier (local development)
            request.state.tier = "dev"
            request.state.api_key = LOCAL_DEV_KEY
            return await call_next(request)

        tier = resolve_tier(key)
        if tier is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid API key. Get one at https://tileripper.dev",
            )

        if not check_rate_limit(key, tier):
            limit = TIERS[tier]["rate_limit"]
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded ({limit} req/min for {tier} tier)",
            )

        request.state.tier = tier
        request.state.api_key = key
        return await call_next(request)


def _extract_key(request: Request) -> str:
    """Pull API key from Authorization header, X-API-Key header, or query param."""
    # Bearer token
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()

    # Custom header
    key = request.headers.get("x-api-key", "")
    if key:
        return key

    # Query param (convenience for browser/curl testing)
    return request.query_params.get("key", "")
