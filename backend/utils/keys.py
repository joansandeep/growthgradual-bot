"""
Key-pool management — mirrors the TypeScript helpers in the old Next.js routes.
"""
import os
import time
from threading import Lock

_lock = Lock()
_rate_limited_until: dict[str, float] = {}
_usage_count: dict[str, int] = {}


def _load_pool(env_var: str) -> list[str]:
    raw = os.environ.get(env_var, "")
    return [k.strip() for k in raw.split(",") if k.strip()]


def get_groq_keys()   -> list[str]: return _load_pool("GROQ_API_KEYS")
def get_tavily_keys() -> list[str]: return _load_pool("TAVILY_API_KEY")
def get_gemini_keys() -> list[str]: return _load_pool("GEMINI_API_KEY")


def pick_key(pool: list[str], label: str) -> str:
    if not pool:
        raise ValueError(f"No {label} keys configured")
    now = time.time()
    with _lock:
        available = [k for k in pool if _rate_limited_until.get(k, 0) < now]
        candidates = available if available else pool
        candidates.sort(key=lambda k: _usage_count.get(k, 0))
        key = candidates[0]
        _usage_count[key] = _usage_count.get(key, 0) + 1
    return key


def mark_rate_limited(key: str, ms: int = 60_000) -> None:
    with _lock:
        _rate_limited_until[key] = time.time() + ms / 1000


def is_rate_limited(key: str) -> bool:
    return _rate_limited_until.get(key, 0) > time.time()


def round_robin(pool: list[str]) -> list[str]:
    """Return pool sorted by least-used first (excluding currently RL'd keys)."""
    now = time.time()
    available = [k for k in pool if _rate_limited_until.get(k, 0) < now]
    result = sorted(available, key=lambda k: _usage_count.get(k, 0))
    # append RL'd keys at the end as last resort
    rl = [k for k in pool if k not in result]
    return result + rl
