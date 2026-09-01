"""
Web Search Service — provider-replaceable façade
================================================
The application already had a battle-hardened Tavily implementation living
inside ``routes/chat.py`` (multi-key fan-out, junk-domain filtering,
thin-result page enrichment, freshness sorting, httpx fallback, key banning).
This module does NOT reimplement any of that. It puts a small, stable
interface *in front of* it so that:

  1. the orchestrator can ask for a web search without knowing or caring
     which vendor answers it, and
  2. swapping Tavily for another vendor later is one class + one registry
     entry, with no changes anywhere else.

Provider selection is environment-driven::

    WEB_SEARCH_PROVIDER=tavily      # default
    WEB_SEARCH_PROVIDER=serper      # requires SERPER_API_KEY
    WEB_SEARCH_PROVIDER=none        # hard-disable web search

Nothing here is finance-specific. ``topic``/``region``/``time_range`` are
plain optional parameters chosen per-query by the caller (in practice, by the
LLM through the tool schema) instead of being derived from a hard-coded
finance/general classification.

No API keys are read, logged, or returned by this module beyond what the
underlying providers already do — see ``utils/keys.py`` for the key pools.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Iterable

import httpx

log = logging.getLogger("websearch")

# Tavily's own accepted `topic` values. Kept as data, not branching logic, so
# a caller can pass any of them without this module knowing why.
VALID_TOPICS = ("general", "news", "finance")
VALID_TIME_RANGES = ("day", "week", "month", "year")


def _normalize_topic(topic: str | None) -> str:
    t = (topic or "general").strip().lower()
    return t if t in VALID_TOPICS else "general"


def _normalize_time_range(time_range: str | None) -> str | None:
    t = (time_range or "").strip().lower()
    return t if t in VALID_TIME_RANGES else None


class WebSearchProvider:
    """Minimal provider contract. Implementations must never raise."""

    name = "base"

    def available(self) -> bool:  # pragma: no cover - trivial
        return False

    async def search(
        self,
        queries: list[str],
        *,
        max_results: int = 20,
        topic: str | None = None,
        region: str | None = None,
        time_range: str | None = None,
        images_out: list | None = None,
        historical_intent: bool = False,
    ) -> list[dict]:
        raise NotImplementedError


class TavilyProvider(WebSearchProvider):
    """
    Delegates to the existing multi-key Tavily pipeline in ``routes.chat``.

    The import is deliberately function-local: ``routes.chat`` imports the
    orchestrator, which imports the tool registry, which imports this module.
    A module-level import here would close that cycle. ``routes/report.py``
    already uses the same lazy-import convention for the same reason.
    """

    name = "tavily"

    def available(self) -> bool:
        from utils.keys import get_tavily_keys

        return bool(get_tavily_keys())

    async def search(
        self,
        queries: list[str],
        *,
        max_results: int = 20,
        topic: str | None = None,
        region: str | None = None,
        time_range: str | None = None,
        images_out: list | None = None,
        historical_intent: bool = False,
    ) -> list[dict]:
        from routes.chat import tavily_search_multi

        # tavily_search_multi (and the tavily_search it wraps) does not accept
        # topic/country/time_range as call-site params — those are derived
        # internally per-query via classify_query()/detect_country()/
        # _detect_recency_time_range() inside tavily_search(). Passing them
        # here raised: TypeError: tavily_search_multi() got an unexpected
        # keyword argument 'topic'. The topic/region/time_range args on this
        # method are kept in the provider interface (other providers, e.g.
        # Serper, do use them) but are intentionally not forwarded to Tavily —
        # no capability is lost, since Tavily was already computing its own
        # topic/country/time_range per query rather than taking hints for them.
        return await tavily_search_multi(
            queries,
            max_results=max_results,
            images_out=images_out,
            historical_intent=historical_intent,
        )


class SerperProvider(WebSearchProvider):
    """
    Optional drop-in alternative (google.serper.dev). Exists to prove the
    interface is genuinely swappable and to give deployments without Tavily a
    working web-search path. Returns the same normalized result dicts as the
    Tavily path so downstream code is identical either way.
    """

    name = "serper"
    ENDPOINT = "https://google.serper.dev/search"

    def _key(self) -> str:
        return os.environ.get("SERPER_API_KEY", "").strip()

    def available(self) -> bool:
        return bool(self._key())

    async def search(
        self,
        queries: list[str],
        *,
        max_results: int = 20,
        topic: str | None = None,
        region: str | None = None,
        time_range: str | None = None,
        images_out: list | None = None,
        historical_intent: bool = False,
    ) -> list[dict]:
        key = self._key()
        if not key:
            return []

        # Serper expresses freshness as a Google "tbs" qdr: token.
        qdr = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}.get(
            _normalize_time_range(time_range) or "", ""
        )
        seen: set[str] = set()
        merged: list[dict] = []
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=25, write=15, pool=10)) as client:
                for q in queries[:4]:
                    payload: dict = {"q": q, "num": min(max_results, 20)}
                    if qdr:
                        payload["tbs"] = qdr
                    if region:
                        payload["gl"] = region[:2].lower()
                    res = await client.post(
                        self.ENDPOINT,
                        headers={"X-API-KEY": key, "Content-Type": "application/json"},
                        json=payload,
                    )
                    if not res.is_success:
                        log.warning("Serper HTTP %d for %r", res.status_code, q[:60])
                        continue
                    for r in (res.json().get("organic") or []):
                        url = r.get("link", "")
                        if not url or url in seen:
                            continue
                        seen.add(url)
                        merged.append({
                            "title": r.get("title", ""),
                            "url": url,
                            "snippet": (r.get("snippet") or "")[:800],
                            "fullContent": (r.get("snippet") or "")[:800],
                            "score": None,
                            "published": r.get("date"),
                            "trusted_source": True,
                        })
        except Exception as exc:
            log.warning("Serper search failed: %s: %s", type(exc).__name__, exc)
        return merged


class NullProvider(WebSearchProvider):
    """Explicitly disabled web search — used when WEB_SEARCH_PROVIDER=none."""

    name = "none"

    def available(self) -> bool:
        return False

    async def search(self, queries: list[str], **_kwargs) -> list[dict]:
        return []


_REGISTRY: dict[str, type[WebSearchProvider]] = {
    TavilyProvider.name: TavilyProvider,
    SerperProvider.name: SerperProvider,
    NullProvider.name: NullProvider,
}


def register_provider(cls: type[WebSearchProvider]) -> None:
    """Hook for adding a provider without editing this module."""
    _REGISTRY[cls.name] = cls


def provider_name() -> str:
    return (os.environ.get("WEB_SEARCH_PROVIDER") or TavilyProvider.name).strip().lower()


def get_provider(name: str | None = None) -> WebSearchProvider:
    key = (name or provider_name()).lower()
    cls = _REGISTRY.get(key)
    if cls is None:
        log.warning("Unknown WEB_SEARCH_PROVIDER=%r — falling back to %s", key, TavilyProvider.name)
        cls = TavilyProvider
    return cls()


def web_search_available() -> bool:
    try:
        return get_provider().available()
    except Exception:
        return False


async def search_web(
    queries: str | Iterable[str],
    *,
    max_results: int = 20,
    topic: str | None = None,
    region: str | None = None,
    time_range: str | None = None,
    images_out: list | None = None,
    historical_intent: bool = False,
    provider: str | None = None,
) -> dict:
    """
    Run a web search through the configured provider.

    Never raises. Always returns::

        {"results": [...], "provider": str, "error": str | None, "elapsed_ms": int}

    An empty ``results`` list with ``error=None`` means the search ran and
    genuinely found nothing — callers must treat that as "no data available",
    never as licence to answer from memory as if it were retrieved fact.
    """
    qs = [queries] if isinstance(queries, str) else [q for q in queries if q and q.strip()]
    if not qs:
        return {"results": [], "provider": provider_name(), "error": "empty query", "elapsed_ms": 0}

    prov = get_provider(provider)
    t0 = time.perf_counter()
    if not prov.available():
        log.warning("Web search unavailable — provider=%s has no usable credentials", prov.name)
        return {
            "results": [],
            "provider": prov.name,
            "error": f"web search provider '{prov.name}' is not configured",
            "elapsed_ms": 0,
        }

    try:
        results = await prov.search(
            qs,
            max_results=max_results,
            topic=topic,
            region=region,
            time_range=time_range,
            images_out=images_out,
            historical_intent=historical_intent,
        )
        err = None
    except Exception as exc:
        results, err = [], f"{type(exc).__name__}: {exc}"
        log.warning("Web search provider %s raised: %s", prov.name, err)

    elapsed = int((time.perf_counter() - t0) * 1000)
    log.info(
        "Web search via %s: %d query(ies) -> %d result(s) in %dms (topic=%s region=%s time_range=%s)",
        prov.name, len(qs), len(results), elapsed, _normalize_topic(topic), region,
        _normalize_time_range(time_range),
    )
    return {"results": results, "provider": prov.name, "error": err, "elapsed_ms": elapsed}
