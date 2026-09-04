"""
Market News Digest
===================
Builds a "today's market news" digest: the latest headlines, each paired
with a real photo where one can be found — never AI-generated or generic
stock imagery.

Two-tier source strategy:

  1. Tavily (news topic, multi-key fan-out) — used whenever at least one
     TAVILY_API_KEY is configured. Tavily's own image search supplies
     candidate photos alongside the article results.

  2. Direct-scrape fallback — reused straight from scraper.py, this
     project's original news scraper (httpx -> RSS -> Selenium per site,
     Selenium simply no-ops when unavailable). Runs whenever Tavily has no
     keys configured, or comes back with too few results to make a digest
     (rate-limited, network trouble, etc). Restricted to the feeds tagged
     "Markets" and run concurrently (scraper.py's own _run_feeds() is
     sequential/throttled by design for its standalone caching service,
     which is too slow for an interactive report request). Images for this
     path come straight off each article's own page (og:image /
     twitter:image meta tags), so a photo is always genuinely tied to the
     headline it's shown under.

Both tiers return the same normalized article shape so callers don't need
to know which one actually ran.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger("market_news")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Matched against the <head> only (first 20KB of the response) — cheap, and
# avoids downloading/scanning full article bodies just to find one tag.
_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_IMAGE_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']',
    re.IGNORECASE,
)


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


async def _extract_og_image(client: httpx.AsyncClient, url: str) -> str | None:
    """Best-effort og:image/twitter:image fetch for one article page. Never raises."""
    try:
        resp = await client.get(url, headers={"User-Agent": _UA}, timeout=8, follow_redirects=True)
        if not resp.is_success:
            return None
        head = resp.text[:20000]
        m = _OG_IMAGE_RE.search(head) or _OG_IMAGE_RE_ALT.search(head)
        if m:
            img = m.group(1).strip()
            if img.startswith("http"):
                return img
    except Exception as exc:
        log.debug("og:image fetch failed for %s: %s: %s", url, type(exc).__name__, exc)
    return None


async def _images_for_articles(urls: list[str]) -> dict[str, str]:
    """Fetch og:image for each article URL concurrently. Best-effort — a URL
    with no discoverable image is simply absent from the returned map."""
    if not urls:
        return {}
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[_extract_og_image(client, u) for u in urls], return_exceptions=True,
        )
    return {u: img for u, img in zip(urls, results) if isinstance(img, str) and img}


async def _tavily_tier(query: str, max_articles: int) -> tuple[list[dict], list[dict]]:
    """Try Tavily first. Returns (articles, tavily_image_candidates) — either
    may be empty, which the caller treats as "fall back to direct scrape"."""
    from utils.keys import get_tavily_keys
    if not get_tavily_keys():
        log.info("Market news: no Tavily keys configured — skipping straight to direct scrape")
        return [], []

    from routes.chat import tavily_search
    images_out: list[dict] = []
    try:
        results = await tavily_search(query, max_results=max_articles, images_out=images_out)
    except Exception as exc:
        log.warning("Market news: Tavily search failed: %s: %s", type(exc).__name__, exc)
        return [], []

    articles = [
        {
            "title": (r.get("title") or "").strip(),
            "source": _domain(r.get("url", "")),
            "url": r.get("url", ""),
            "summary": (r.get("snippet") or "")[:300].strip(),
            "published": r.get("published") or "",
        }
        for r in results
        if r.get("title") and r.get("url")
    ]
    return articles[:max_articles], images_out


async def _scraper_tier(max_articles: int) -> list[dict]:
    """Fallback: fetch articles directly by URL using this project's own
    scraper (scraper.py) instead of Tavily — restricted to Markets-tagged
    feeds and run concurrently for interactive-request latency."""
    try:
        from scraper import FEEDS, _fetch_feed
    except Exception as exc:
        log.warning("Market news: direct-scrape fallback unavailable: %s: %s", type(exc).__name__, exc)
        return []

    market_feeds = [f for f in FEEDS if f.get("tag") == "Markets"][:6]
    if not market_feeds:
        return []

    async def _safe_fetch(feed: dict):
        try:
            return await asyncio.wait_for(_fetch_feed(feed), timeout=20)
        except Exception as exc:
            log.debug("Market news: fallback feed %s failed: %s", feed.get("source"), exc)
            return [], feed.get("source", "")

    t0 = time.perf_counter()
    results = await asyncio.gather(*[_safe_fetch(f) for f in market_feeds])
    log.info("Market news: direct scrape of %d Markets feeds took %.0fms",
              len(market_feeds), (time.perf_counter() - t0) * 1000)

    all_arts = [a for arts, _src in results for a in arts]

    # Same de-dup rule as scraper.py's own scrape_all(): normalized-title
    # match, newest first.
    seen: set[str] = set()
    unique: list[dict] = []
    for a in sorted(all_arts, key=lambda a: a.time_ms, reverse=True):
        key = re.sub(r"[^a-z0-9]", "", a.title[:60].lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append({
            "title": a.title,
            "source": a.source,
            "url": a.url,
            "summary": a.summary,
            "published": a.time,
        })
    return unique[:max_articles]


async def get_todays_market_news(
    query: str = "Indian stock market news today", max_articles: int = 8,
) -> dict:
    """
    Returns:
        {"provider": "tavily" | "rss_fallback",
         "articles": [{"title","source","url","summary","published","image_url"}]}

    Never raises — an empty articles list is a valid, handled outcome for
    the caller (routes/report.py falls through to the normal LLM pipeline
    when this comes back empty).
    """
    t0 = time.perf_counter()
    provider = "tavily"
    articles, tavily_images = await _tavily_tier(query, max_articles)

    # Too few results isn't necessarily a Tavily failure, but a digest of 1-2
    # headlines isn't useful either way — treat it the same as unavailable
    # and get a fuller picture from the direct scrape instead.
    if len(articles) < 3:
        provider = "rss_fallback"
        articles = await _scraper_tier(max_articles)

    if provider == "tavily" and articles:
        # Best-effort spread of Tavily's own image candidates across the
        # articles first (cheap, no extra requests); anything still missing
        # a photo gets a direct og:image lookup on its own article page.
        image_map: dict[str, str] = {}
        for a, img in zip(articles, tavily_images):
            url = img.get("url", "")
            if url:
                image_map[a["url"]] = url
        still_needed = [a["url"] for a in articles if a["url"] not in image_map]
        if still_needed:
            image_map.update(await _images_for_articles(still_needed))
    else:
        image_map = await _images_for_articles([a["url"] for a in articles])

    for a in articles:
        a["image_url"] = image_map.get(a["url"], "")

    elapsed = int((time.perf_counter() - t0) * 1000)
    log.info(
        "Market news digest: provider=%s articles=%d (with images: %d) in %dms",
        provider, len(articles), sum(1 for a in articles if a["image_url"]), elapsed,
    )
    return {"provider": provider, "articles": articles}
