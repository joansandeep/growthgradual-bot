"""
Supabase JWT -> user_id resolution.

Validates the bearer token via Supabase's Auth REST API (GET /auth/v1/user).
No JWT secret needed — works out of the box with the SUPABASE_URL /
SUPABASE_ANON_KEY that are already configured for data access. Results are
cached briefly per-token so a chat stream making several backend calls
doesn't re-validate on every one.
"""
import hashlib
import logging
import os
import time
from typing import Optional

import httpx
from fastapi.requests import Request

log = logging.getLogger("auth")

_SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
_SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

_CACHE_TTL_SECONDS = 300  # 5 min
_cache: dict[str, tuple[str, float]] = {}  # token_hash -> (user_id, expires_at)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _extract_token(request: Request) -> Optional[str]:
    auth_header = request.headers.get("authorization")
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    return token or None


async def get_user_id(request: Request) -> Optional[str]:
    """
    Returns the authenticated Supabase user's UUID, or None if the request
    has no token, the token is invalid/expired, or Supabase isn't
    configured. Never raises — an unauthenticated request should degrade to
    anonymous behavior, not fail the whole call.
    """
    token = _extract_token(request)
    if not token:
        return None

    key = _token_hash(token)
    cached = _cache.get(key)
    now = time.time()
    if cached and cached[1] > now:
        return cached[0]

    if not _SUPABASE_URL or not _SUPABASE_ANON_KEY:
        return None

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(
                f"{_SUPABASE_URL}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": _SUPABASE_ANON_KEY,
                },
            )
        if r.status_code != 200:
            log.debug("Supabase token validation returned %s", r.status_code)
            return None
        user_id = r.json().get("id")
        if not user_id:
            return None

        _cache[key] = (user_id, now + _CACHE_TTL_SECONDS)
        if len(_cache) > 2000:  # opportunistic cleanup, avoid unbounded growth
            for k, (_, exp) in list(_cache.items()):
                if exp <= now:
                    _cache.pop(k, None)
        return user_id
    except Exception as exc:
        log.debug("Supabase token validation failed (non-critical): %s", exc)
        return None
