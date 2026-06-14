"""
Growing Gradual Python Scraper Backend
Three-tier scraping strategy:
  1. httpx  — fast async HTTP for plain RSS/XML feeds
  2. Playwright — headless Chromium for JS-rendered pages
  3. Selenium  — fallback headless Chrome via WebDriver

Usage:
    pip install -r requirements.txt
    playwright install chromium --with-deps
    python scraper.py

API:
    GET  /api/scrape              → fetch articles (cache-first, 5 min TTL)
    POST /api/scrape              → force-refresh all feeds
    GET  /api/scrape/js?url=...   → scrape a single JS-rendered URL (Playwright)
    GET  /api/scrape/selenium?url=... → scrape a URL via Selenium fallback
    GET  /health                  → health + driver status
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Growing Gradual] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("Growing Gradual")

# ─── Config ──────────────────────────────────────────────────────────────────
CACHE_PATH         = Path(os.getenv("CACHE_PATH", "/tmp/Growing Gradual_cache.json"))
CACHE_TTL_SECONDS  = int(os.getenv("CACHE_TTL", "300"))   # 5 minutes
MAX_ITEMS_PER_FEED = 12
HTTP_TIMEOUT       = 10.0   # seconds
BROWSER_TIMEOUT    = 20_000 # ms  (Playwright)
SELENIUM_TIMEOUT   = 20     # seconds


# ─── Models ──────────────────────────────────────────────────────────────────
@dataclass
class Article:
    id:        str
    title:     str
    source:    str
    url:       str
    time:      str
    time_ms:   int
    tag:       str
    category:  str
    summary:   str


@dataclass
class CacheFile:
    fetched_at: int                          # epoch ms
    articles:   list[Article] = field(default_factory=list)
    sources:    list[str]     = field(default_factory=list)


# ─── RSS Feed registry ────────────────────────────────────────────────────────
# strategy: "rss"       → plain httpx RSS fetch
#           "playwright"→ JS-rendered page, scrape with Playwright
#           "selenium"  → JS-rendered page, scrape with Selenium
FEEDS: list[dict] = [
    # ── httpx RSS feeds ───────────────────────────────────────────────────────
    dict(url="https://www.moneycontrol.com/rss/latestnews.xml",
         source="Moneycontrol", category="all", tag="Markets", strategy="rss"),
    dict(url="https://www.moneycontrol.com/rss/marketreports.xml",
         source="Moneycontrol", category="stocks", tag="Markets", strategy="rss"),
    dict(url="https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
         source="Economic Times", category="stocks", tag="Markets", strategy="rss"),
    dict(url="https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
         source="Economic Times", category="stocks", tag="Stocks", strategy="rss"),
    dict(url="https://economictimes.indiatimes.com/industry/banking/finance/rssfeeds/13358259.cms",
         source="Economic Times", category="banks", tag="Banking", strategy="rss"),
    dict(url="https://economictimes.indiatimes.com/mf/rssfeeds/13741497.cms",
         source="Economic Times", category="mutual_funds", tag="Mutual Funds", strategy="rss"),
    dict(url="https://www.livemint.com/rss/markets",
         source="Livemint", category="stocks", tag="Markets", strategy="rss"),
    dict(url="https://www.livemint.com/rss/money",
         source="Livemint", category="finance", tag="Finance", strategy="rss"),
    dict(url="https://feeds.feedburner.com/ndtvprofit-latest",
         source="NDTV Profit", category="all", tag="Markets", strategy="rss"),
    dict(url="https://www.financialexpress.com/market/rss/",
         source="Financial Express", category="stocks", tag="Markets", strategy="rss"),
    dict(url="https://www.business-standard.com/rss/markets-106.rss",
         source="Business Standard", category="stocks", tag="Markets", strategy="rss"),
    dict(url="https://www.business-standard.com/rss/banking-104.rss",
         source="Business Standard", category="banks", tag="Banking", strategy="rss"),
    dict(url="https://www.business-standard.com/rss/mutual-fund-119.rss",
         source="Business Standard", category="mutual_funds", tag="Mutual Funds", strategy="rss"),
    dict(url="https://feeds.reuters.com/reuters/INbusinessNews",
         source="Reuters India", category="finance", tag="Global", strategy="rss"),

    # ── Playwright feeds (JS-rendered pages) ──────────────────────────────────
    # These sites block plain HTTP or need JS to render headlines.
    # Uncomment when Playwright is installed.
    #
    # dict(url="https://www.nseindia.com/market-data/live-equity-market",
    #      source="NSE India", category="stocks", tag="Markets", strategy="playwright"),
    # dict(url="https://groww.in/markets/top-gainers",
    #      source="Groww", category="stocks", tag="Markets", strategy="playwright"),

    # ── Selenium feeds (ultimate fallback) ────────────────────────────────────
    # dict(url="https://ticker.finology.in/",
    #      source="Finology", category="stocks", tag="Markets", strategy="selenium"),
]

RSS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Growing Gradual/2.0; RSS reader)",
    "Accept":     "application/rss+xml, application/xml, text/xml, */*",
}


# ─── RSS / XML helpers ────────────────────────────────────────────────────────
def _xt(xml: str, tag: str) -> str:
    cdata = re.search(rf"<{tag}[^>]*><!\[CDATA\[([\s\S]*?)\]\]>", xml, re.I)
    if cdata:
        return cdata.group(1).strip()
    plain = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", xml, re.I)
    return plain.group(1).strip() if plain else ""

def _attr_link(xml: str) -> str:
    m = re.search(r'<link[^>]+href=["\']([^"\']+)["\']', xml, re.I)
    return m.group(1) if m else ""

def _strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()

def _de(s: str) -> str:
    for e, c in [
        ("&amp;","&"),("&lt;","<"),("&gt;",">"),("&quot;",'"'),("&#39;","'"),
        ("&nbsp;"," "),("&#8216;","\u2018"),("&#8217;","\u2019"),
        ("&#8220;","\u201c"),("&#8221;","\u201d"),("&#8230;","\u2026"),
        ("&rsquo;","\u2019"),("&ldquo;","\u201c"),("&rdquo;","\u201d"),
        ("&hellip;","\u2026"),("&mdash;","\u2014"),
    ]:
        s = s.replace(e, c)
    return s

def _fmt_time(ds: str) -> tuple[str, int]:
    if not ds:
        return "Recently", int(time.time() * 1000) - 60_000
    try:
        dt = parsedate_to_datetime(ds)
    except Exception:
        try:
            dt = datetime.fromisoformat(ds.replace("Z", "+00:00"))
        except Exception:
            return "Recently", int(time.time() * 1000) - 60_000
    ms   = int(dt.timestamp() * 1000)
    diff = time.time() - dt.timestamp()
    if diff < 60:   return "Just now",            ms
    m = int(diff / 60)
    if m < 60:      return f"{m}m ago",            ms
    h = int(m / 60)
    if h < 24:      return f"{h}h ago",            ms
    return              f"{int(h/24)}d ago",        ms

def _make_id(source: str, title: str) -> str:
    raw = f"{source}::{title}"
    return source.replace(" ", "_") + "::" + hashlib.md5(raw.encode()).hexdigest()[:8]

def _parse_rss(xml: str, source: str, category: str, tag: str) -> list[Article]:
    items = re.findall(r"<item[\s\S]*?</item>", xml, re.I)
    out: list[Article] = []
    for block in items[:MAX_ITEMS_PER_FEED]:
        title = _de(_xt(block, "title"))
        if not title or len(title) < 12:
            continue
        raw_link = _xt(block, "link") or _attr_link(block)
        link     = raw_link if raw_link.startswith("http") else "#"
        summary  = _de(_strip_html(_xt(block, "description") or _xt(block, "summary") or ""))[:320]
        raw_date = _xt(block, "pubDate") or _xt(block, "published") or _xt(block, "dc:date") or ""
        t_label, t_ms = _fmt_time(raw_date)
        out.append(Article(
            id=_make_id(source, title), title=title[:160],
            source=source, url=link, time=t_label, time_ms=t_ms,
            tag=tag, category=category, summary=summary,
        ))
    return out


# ─── Cache helpers ────────────────────────────────────────────────────────────
def _read_cache() -> Optional[CacheFile]:
    try:
        data = json.loads(CACHE_PATH.read_text())
        if int(time.time() * 1000) - data["fetched_at"] < CACHE_TTL_SECONDS * 1000:
            return CacheFile(
                fetched_at=data["fetched_at"],
                articles=[Article(**a) for a in data["articles"]],
                sources=data["sources"],
            )
    except Exception:
        pass
    return None

def _write_cache(c: CacheFile) -> None:
    try:
        CACHE_PATH.write_text(json.dumps({
            "fetched_at": c.fetched_at,
            "articles":   [asdict(a) for a in c.articles],
            "sources":    c.sources,
        }))
    except Exception:
        pass


# ─── Tier 1 — httpx RSS fetch ─────────────────────────────────────────────────
async def _fetch_rss(
    client: httpx.AsyncClient,
    url: str, source: str, category: str, tag: str,
) -> tuple[list[Article], bool]:
    try:
        r = await client.get(url, headers=RSS_HEADERS,
                             timeout=HTTP_TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            return [], False
        return _parse_rss(r.text, source, category, tag), True
    except Exception as exc:
        log.warning("RSS error (%s): %s", source, exc)
        return [], False


# ─── Tier 2 — Playwright (async, headless Chromium) ──────────────────────────
_playwright_available = False
_pw_browser = None      # shared browser instance

async def _init_playwright() -> bool:
    """Try to start a shared Playwright Chromium browser."""
    global _playwright_available, _pw_browser
    try:
        from playwright.async_api import async_playwright  # type: ignore
        _pw_ctx = await async_playwright().__aenter__()
        _pw_browser = await _pw_ctx.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-gpu", "--disable-extensions"],
        )
        _playwright_available = True
        log.info("Playwright Chromium ready")
    except Exception as exc:
        log.warning("Playwright not available: %s", exc)
        _playwright_available = False
    return _playwright_available


async def scrape_with_playwright(url: str) -> str:
    """
    Render a URL with Playwright and return the page HTML.
    Waits for network idle so JS-rendered content is included.
    Raises RuntimeError if Playwright is not available.
    """
    if not _playwright_available or _pw_browser is None:
        raise RuntimeError("Playwright is not initialised")

    context = await _pw_browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=BROWSER_TIMEOUT)
        # Extra wait for lazy-loaded content
        await page.wait_for_timeout(1500)
        html = await page.content()
        return html
    finally:
        await context.close()


def _parse_html_articles(
    html: str, source: str, category: str, tag: str,
) -> list[Article]:
    """
    Generic HTML article extractor.
    Looks for <a> tags whose text looks like a headline (≥30 chars, has spaces).
    Works reasonably well for most news pages without site-specific selectors.
    """
    # Find all anchor tags with meaningful text
    links = re.findall(
        r'<a[^>]+href=["\']([^"\'#][^"\']*)["\'][^>]*>([\s\S]*?)</a>',
        html, re.I,
    )
    seen_titles: set[str] = set()
    articles: list[Article] = []

    for href, inner in links:
        title = _de(_strip_html(inner)).strip()
        # Filter: must look like a headline
        if len(title) < 30 or " " not in title:
            continue
        # Skip navigation / boilerplate
        if any(w in title.lower() for w in [
            "subscribe", "log in", "sign up", "cookie", "privacy",
            "advertisement", "follow us", "download app",
        ]):
            continue
        key = re.sub(r"[^a-z0-9]", "", title[:55].lower())
        if key in seen_titles:
            continue
        seen_titles.add(key)

        full_url = href if href.startswith("http") else "#"
        articles.append(Article(
            id=_make_id(source, title),
            title=title[:160],
            source=source,
            url=full_url,
            time="Recently",
            time_ms=int(time.time() * 1000),
            tag=tag,
            category=category,
            summary="",
        ))
        if len(articles) >= MAX_ITEMS_PER_FEED:
            break

    return articles


async def _fetch_playwright(
    url: str, source: str, category: str, tag: str,
) -> tuple[list[Article], bool]:
    """Scrape a JS-rendered page with Playwright."""
    try:
        html = await scrape_with_playwright(url)
        articles = _parse_html_articles(html, source, category, tag)
        log.info("Playwright scraped %d articles from %s", len(articles), source)
        return articles, True
    except Exception as exc:
        log.warning("Playwright scrape failed (%s): %s", source, exc)
        return [], False


# ─── Tier 3 — Selenium (sync, run in executor) ───────────────────────────────
_selenium_available = False

def _check_selenium() -> bool:
    global _selenium_available
    try:
        from selenium import webdriver                            # type: ignore
        from selenium.webdriver.chrome.options import Options    # type: ignore
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        # Quick probe: just instantiate and quit
        d = webdriver.Chrome(options=opts)
        d.quit()
        _selenium_available = True
        log.info("Selenium ChromeDriver ready")
    except Exception as exc:
        log.warning("Selenium not available: %s", exc)
        _selenium_available = False
    return _selenium_available


def _selenium_fetch_sync(url: str) -> str:
    """Blocking Selenium fetch — call via run_in_executor."""
    from selenium import webdriver                                # type: ignore
    from selenium.webdriver.chrome.options import Options        # type: ignore
    from selenium.webdriver.support.ui import WebDriverWait      # type: ignore
    from selenium.webdriver.support import expected_conditions as EC  # type: ignore
    from selenium.webdriver.common.by import By                  # type: ignore

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(options=opts)
    try:
        driver.set_page_load_timeout(SELENIUM_TIMEOUT)
        driver.get(url)
        # Wait until body is present
        WebDriverWait(driver, SELENIUM_TIMEOUT).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        # Extra pause for lazy content
        time.sleep(2)
        return driver.page_source
    finally:
        driver.quit()


async def scrape_with_selenium(url: str) -> str:
    """
    Render a URL with Selenium (runs blocking driver in a thread pool).
    Returns the full page HTML after JS execution.
    Raises RuntimeError if Selenium is not available.
    """
    if not _selenium_available:
        raise RuntimeError("Selenium ChromeDriver is not available")
    loop = asyncio.get_event_loop()
    html = await loop.run_in_executor(None, _selenium_fetch_sync, url)
    return html


async def _fetch_selenium(
    url: str, source: str, category: str, tag: str,
) -> tuple[list[Article], bool]:
    """Scrape a JS-rendered page with Selenium."""
    try:
        html = await scrape_with_selenium(url)
        articles = _parse_html_articles(html, source, category, tag)
        log.info("Selenium scraped %d articles from %s", len(articles), source)
        return articles, True
    except Exception as exc:
        log.warning("Selenium scrape failed (%s): %s", source, exc)
        return [], False


# ─── Dispatcher — routes each feed to the right tier ─────────────────────────
async def _fetch_feed(
    client: httpx.AsyncClient, feed: dict,
) -> tuple[list[Article], bool, str]:
    """
    Returns (articles, success, source_name).
    Strategy waterfall: configured strategy → playwright fallback → selenium fallback.
    """
    strategy = feed.get("strategy", "rss")
    url, source, category, tag = feed["url"], feed["source"], feed["category"], feed["tag"]

    if strategy == "rss":
        articles, ok = await _fetch_rss(client, url, source, category, tag)
        # If RSS fails and browsers are available, try Playwright then Selenium
        if not ok and _playwright_available:
            log.info("RSS failed for %s, trying Playwright…", source)
            articles, ok = await _fetch_playwright(url, source, category, tag)
        if not ok and _selenium_available:
            log.info("Playwright failed for %s, trying Selenium…", source)
            articles, ok = await _fetch_selenium(url, source, category, tag)

    elif strategy == "playwright":
        articles, ok = await _fetch_playwright(url, source, category, tag)
        if not ok and _selenium_available:
            log.info("Playwright failed for %s, trying Selenium…", source)
            articles, ok = await _fetch_selenium(url, source, category, tag)

    elif strategy == "selenium":
        articles, ok = await _fetch_selenium(url, source, category, tag)

    else:
        log.warning("Unknown strategy '%s' for %s", strategy, source)
        articles, ok = [], False

    return articles, ok, source


# ─── Main scraper ─────────────────────────────────────────────────────────────
async def scrape_all() -> CacheFile:
    log.info("Starting scrape of %d feeds…", len(FEEDS))
    async with httpx.AsyncClient() as client:
        tasks    = [_fetch_feed(client, f) for f in FEEDS]
        results  = await asyncio.gather(*tasks)

    all_articles: list[Article] = []
    successful_sources: list[str] = []

    for articles, ok, source in results:
        if ok:
            all_articles.extend(articles)
            if source not in successful_sources:
                successful_sources.append(source)

    log.info("Raw: %d articles from %d sources", len(all_articles), len(successful_sources))

    # Deduplicate by normalised title prefix
    seen: set[str] = set()
    unique: list[Article] = []
    for a in all_articles:
        key = re.sub(r"[^a-z0-9]", "", a.title[:55].lower())
        if key not in seen:
            seen.add(key)
            unique.append(a)

    unique.sort(key=lambda a: a.time_ms, reverse=True)
    log.info("Deduplicated: %d articles", len(unique))

    cache = CacheFile(
        fetched_at=int(time.time() * 1000),
        articles=unique,
        sources=successful_sources,
    )
    _write_cache(cache)
    return cache


# ─── App lifecycle ────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: probe browsers in background (don't block server boot)
    asyncio.create_task(_init_playwright())
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _check_selenium)
    yield
    # Shutdown: close Playwright browser
    if _pw_browser:
        await _pw_browser.close()


# ─── FastAPI app ──────────────────────────────────────────────────────────────
app = FastAPI(title="Growing Gradual Scraper API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _to_response(cache: CacheFile, from_cache: bool) -> dict:
    return {
        "articles":  [asdict(a) for a in cache.articles],
        "total":     len(cache.articles),
        "sources":   cache.sources,
        "fromCache": from_cache,
        "fetchedAt": datetime.fromtimestamp(
            cache.fetched_at / 1000, tz=timezone.utc
        ).isoformat(),
    }


# ── Core feed endpoints ───────────────────────────────────────────────────────
@app.get("/api/scrape")
async def get_articles():
    """Return cached articles if still fresh, otherwise live-scrape all feeds."""
    cached = _read_cache()
    if cached:
        log.info("Serving %d articles from cache", len(cached.articles))
        return _to_response(cached, from_cache=True)
    fresh = await scrape_all()
    return _to_response(fresh, from_cache=False)


@app.post("/api/scrape")
async def force_refresh():
    """Force a full re-scrape of all feeds, ignoring cache."""
    fresh = await scrape_all()
    return _to_response(fresh, from_cache=False)


# ── Single-URL browser endpoints ─────────────────────────────────────────────
@app.get("/api/scrape/js")
async def scrape_js(url: str = Query(..., description="URL of a JS-rendered page")):
    """
    Scrape a single JS-rendered URL using Playwright.
    Returns raw page HTML + extracted articles.

    Example:
        GET /api/scrape/js?url=https://groww.in/markets/top-gainers
    """
    if not _playwright_available:
        raise HTTPException(503, "Playwright is not available. Run: playwright install chromium")
    try:
        html     = await scrape_with_playwright(url)
        articles = _parse_html_articles(html, source="custom", category="all", tag="Markets")
        return {
            "url":           url,
            "articles":      [asdict(a) for a in articles],
            "total":         len(articles),
            "html_length":   len(html),
            "engine":        "playwright",
        }
    except Exception as exc:
        raise HTTPException(500, f"Playwright error: {exc}") from exc


@app.get("/api/scrape/selenium")
async def scrape_selenium_endpoint(
    url: str = Query(..., description="URL of a JS-rendered page"),
):
    """
    Scrape a single JS-rendered URL using Selenium as the fallback engine.
    Useful when Playwright is blocked by a site's anti-bot measures.

    Example:
        GET /api/scrape/selenium?url=https://ticker.finology.in/
    """
    if not _selenium_available:
        raise HTTPException(
            503,
            "Selenium ChromeDriver is not available. "
            "Install: pip install selenium && apt-get install -y chromium-driver",
        )
    try:
        html     = await scrape_with_selenium(url)
        articles = _parse_html_articles(html, source="custom", category="all", tag="Markets")
        return {
            "url":         url,
            "articles":    [asdict(a) for a in articles],
            "total":       len(articles),
            "html_length": len(html),
            "engine":      "selenium",
        }
    except Exception as exc:
        raise HTTPException(500, f"Selenium error: {exc}") from exc


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "status":              "ok",
        "feeds":               len(FEEDS),
        "playwright_ready":    _playwright_available,
        "selenium_ready":      _selenium_available,
        "cache_path":          str(CACHE_PATH),
        "cache_ttl_seconds":   CACHE_TTL_SECONDS,
    }


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
