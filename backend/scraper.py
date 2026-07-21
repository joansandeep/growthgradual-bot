"""
Growing Gradual Python Scraper Backend

Fetch strategy per feed (tried in order, stops at first success):
  0. httpx + BeautifulSoup  — fast static fetch, no browser, ~2s per site
  1. RSS                    — for sites with known RSS URLs (instant, structured)
  2. Selenium               — for JS-heavy sites that need a real browser

Playwright is kept in requirements but NOT used at runtime — it got 0 articles
on every Render deployment and wasted 5-10s per site before Selenium fallback.

Selenium is wrapped with asyncio.wait_for(timeout=45s) so a hanging Chrome
process (e.g. Financial Express renderer crash) can never block the queue.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# ─── ET cookie injection ───────────────────────────────────────────────────────
# Paste your browser cookies for economictimes.indiatimes.com into ET_COOKIES in
# your .env file as a single semicolon-separated Cookie header string, e.g.:
#   ET_COOKIES="deviceid=abc123; __gads=..."
ET_COOKIES: str = os.environ.get("ET_COOKIES", "")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Growing Gradual] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("Growing Gradual")

# ─── Config ───────────────────────────────────────────────────────────────────
CACHE_PATH        = Path(os.getenv("CACHE_PATH", str(Path(__file__).parent / "cache.json")))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL", "21600"))   # 6 hours
MAX_ITEMS         = 12

# Per-feed timeouts
HTTPX_TIMEOUT     = 12   # seconds — fast static fetch
RSS_TIMEOUT       = 10   # seconds — RSS feeds are tiny
SEL_TIMEOUT       = 45   # seconds — hard asyncio kill on Selenium (prevents 9-min hangs)
SEL_PAGE_LOAD     = 30   # seconds — Selenium driver.set_page_load_timeout

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.155 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.155 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.155 Safari/537.36",
]

# ─── Models ───────────────────────────────────────────────────────────────────
@dataclass
class Article:
    id:         str
    title:      str
    source:     str
    url:        str
    time:       str
    time_ms:    int
    tag:        str
    category:   str
    summary:    str
    source_url: str = ""

@dataclass
class CacheFile:
    fetched_at: int
    articles:   list[Article] = field(default_factory=list)
    sources:    list[str]     = field(default_factory=list)


# ─── Feed registry ────────────────────────────────────────────────────────────
# strategy:
#   "httpx"    — static HTML fetch (fastest, no browser, works for most Indian sites)
#   "rss"      — RSS/Atom feed URL in rss_url field
#   "selenium" — full browser (JS-heavy sites only)
#   "httpx|selenium" — try httpx first, fall back to selenium if < 3 articles
#
# sel_wait: seconds to sleep after page load in Selenium (default 3)

FEEDS: list[dict] = [

    # ══════════════════════════════════════════════════════════════════════════
    # ALL MARKETS
    # ══════════════════════════════════════════════════════════════════════════
    dict(
        url="https://www.moneycontrol.com/news/business/markets/",
        source="Moneycontrol", category="all", tag="Markets",
        strategy="httpx|selenium",
        selectors=[
            "li.clearfix a", "li[id^=newslist] a",
            "h2.article_title a", "h2 a", "h3 a",
        ],
    ),
    dict(
        # ET serves article list in SSR HTML — httpx works great
        url="https://economictimes.indiatimes.com/markets",
        source="Economic Times", category="all", tag="Markets",
        strategy="httpx|selenium",
        selectors=[".eachStory h3 a", ".eachStory h2 a", "article h3 a", "h3 a"],
        cookies=ET_COOKIES,
    ),
    dict(
        # Livemint times out in Selenium — use RSS instead
        rss_url="https://www.livemint.com/rss/markets",
        url="https://www.livemint.com/market",
        source="Livemint", category="all", tag="Markets",
        strategy="rss",
        selectors=["h2.headline a", "h3.headline a", ".listingNew li h2 a", "h2 a"],
    ),
    dict(
        url="https://www.ndtvprofit.com/latest",
        source="NDTV Profit", category="all", tag="Markets",
        strategy="httpx|selenium",
        selectors=[
            ".story-card__headline a", "h2.story__headline a",
            "a.story-card__link", "h2 a", "h3 a",
        ],
        sel_wait=3,
    ),
    dict(
        url="https://www.thehindubusinessline.com/markets/",
        source="Hindu BusinessLine", category="all", tag="Markets",
        strategy="httpx|selenium",
        selectors=["h2.title a", "h3.title a", ".element h3 a", ".element h2 a"],
        sel_wait=4,
    ),
    dict(
        url="https://www.zeebiz.com/markets",
        source="Zee Business", category="all", tag="Markets",
        strategy="httpx|selenium",
        selectors=[".col_left h3 a", ".article_wrap h3 a", "h3 a", "h2 a"],
        sel_wait=3,
    ),
    dict(
        url="https://www.cnbctv18.com/market/",
        source="CNBC TV18", category="all", tag="Markets",
        strategy="httpx|selenium",
        selectors=["article h3 a", ".story-card h3 a", "h3 a", "h2 a"],
        sel_wait=3,
    ),
    dict(
        # Financial Express hangs Selenium for 9 min — RSS only, never browser
        rss_url="https://www.financialexpress.com/feed/",
        url="https://www.financialexpress.com/market/",
        source="Financial Express", category="all", tag="Markets",
        strategy="rss",
        selectors=[".list-wrap h2 a", ".pcArticle h2 a", "h2 a"],
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # STOCKS
    # ══════════════════════════════════════════════════════════════════════════
    dict(
        url="https://economictimes.indiatimes.com/markets/stocks",
        source="Economic Times", category="stocks", tag="Stocks",
        strategy="httpx|selenium",
        selectors=[".eachStory h3 a", ".eachStory h2 a", "article h3 a", "h3 a"],
        cookies=ET_COOKIES,
    ),
    dict(
        rss_url="https://www.livemint.com/rss/markets",
        url="https://www.livemint.com/market/stock-market-news",
        source="Livemint", category="stocks", tag="Stocks",
        strategy="rss",
        selectors=["h2.headline a", "h3.headline a", "h2 a"],
    ),
    dict(
        url="https://www.thehindubusinessline.com/markets/stock-markets/",
        source="Hindu BusinessLine", category="stocks", tag="Stocks",
        strategy="httpx|selenium",
        selectors=["h2.title a", "h3.title a", ".element h3 a", ".element h2 a"],
        sel_wait=4,
    ),
    dict(
        url="https://www.moneycontrol.com/news/business/stocks/",
        source="Moneycontrol", category="stocks", tag="Stocks",
        strategy="httpx|selenium",
        selectors=["li.clearfix a", ".news_list li a", "h2 a"],
    ),
    dict(
        url="https://www.business-standard.com/markets/capital-market-news",
        source="Business Standard", category="stocks", tag="Stocks",
        strategy="httpx|selenium",
        selectors=[".cardlist li a", ".listing-txt h2 a", "h2 a"],
        sel_wait=3,
    ),
    dict(
        url="https://www.equitymaster.com/share-market-today/",
        source="Equitymaster", category="stocks", tag="Analysis",
        strategy="httpx|selenium",
        selectors=[".article-list h2 a", ".title a", "h2 a", "h3 a"],
    ),
    dict(
        url="https://groww.in/blog",
        source="Groww", category="stocks", tag="Markets",
        strategy="httpx|selenium",
        selectors=["h3 a", "h2 a", ".blogCard a"],
        sel_wait=4,
    ),
    dict(
        url="https://blog.finology.in/",
        source="Finology", category="stocks", tag="Analysis",
        strategy="httpx|selenium",
        selectors=["h2 a", "h3 a", ".post-title a", ".entry-title a"],
    ),
    dict(
        # Reuters serves articles in initial HTML
        url="https://www.reuters.com/markets/",
        source="Reuters", category="all", tag="Markets",
        strategy="httpx|selenium",
        selectors=["a[data-testid='Heading']", "article a", "h3 a", "h2 a"],
        sel_wait=4,
    ),
    dict(
        rss_url="https://finance.yahoo.com/rss/topstories",
        url="https://finance.yahoo.com/news/",
        source="Yahoo Finance", category="all", tag="Markets",
        strategy="rss",
        selectors=["h3 a", "h2 a"],
    ),
    dict(
        url="https://www.investing.com/news/stock-market-news",
        source="Investing.com", category="stocks", tag="Markets",
        strategy="httpx|selenium",
        selectors=["a[data-test='article-title-link']", "article a", "h3 a", "h2 a"],
        sel_wait=5,
    ),
    dict(
        url="https://tradingeconomics.com/news",
        source="Trading Economics", category="finance", tag="Economy",
        strategy="httpx|selenium",
        selectors=[".article-title a", "h2 a", "h3 a"],
        sel_wait=4,
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # BANKING
    # ══════════════════════════════════════════════════════════════════════════
    dict(
        url="https://economictimes.indiatimes.com/industry/banking/finance",
        source="Economic Times", category="banks", tag="Banking",
        strategy="httpx|selenium",
        selectors=[".eachStory h3 a", ".eachStory h2 a", "article h3 a", "h3 a"],
        cookies=ET_COOKIES,
    ),
    dict(
        rss_url="https://www.livemint.com/rss/industry",
        url="https://www.livemint.com/industry/banking",
        source="Livemint", category="banks", tag="Banking",
        strategy="rss",
        selectors=["h2.headline a", "h3.headline a", "h2 a"],
    ),
    dict(
        url="https://www.thehindubusinessline.com/money-and-banking/",
        source="Hindu BusinessLine", category="banks", tag="Banking",
        strategy="httpx|selenium",
        selectors=["h2.title a", "h3.title a", ".element h3 a"],
        sel_wait=4,
    ),
    dict(
        url="https://www.moneycontrol.com/news/business/banks/",
        source="Moneycontrol", category="banks", tag="Banking",
        strategy="httpx|selenium",
        selectors=["li.clearfix a", ".news_list li a", "h2 a"],
    ),
    dict(
        url="https://www.business-standard.com/finance/banking",
        source="Business Standard", category="banks", tag="Banking",
        strategy="httpx|selenium",
        selectors=[".cardlist li a", ".listing-txt h2 a", "h2 a"],
        sel_wait=3,
    ),
    dict(
        url="https://www.cnbctv18.com/banking/",
        source="CNBC TV18", category="banks", tag="Banking",
        strategy="httpx|selenium",
        selectors=["article h3 a", ".story-card h3 a", "h3 a", "h2 a"],
        sel_wait=3,
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # MUTUAL FUNDS
    # ══════════════════════════════════════════════════════════════════════════
    dict(
        url="https://www.moneycontrol.com/mutualfundindia/news/",
        source="Moneycontrol", category="mutual_funds", tag="Mutual Funds",
        strategy="httpx|selenium",
        selectors=["li.clearfix a", ".news_list li a", "h2 a"],
    ),
    dict(
        url="https://economictimes.indiatimes.com/mf/analysis",
        source="Economic Times", category="mutual_funds", tag="Mutual Funds",
        strategy="httpx|selenium",
        selectors=[".eachStory h3 a", ".eachStory h2 a", "article h3 a", "h3 a"],
        cookies=ET_COOKIES,
    ),
    dict(
        rss_url="https://www.livemint.com/rss/MutualFund",
        url="https://www.livemint.com/mutual-fund",
        source="Livemint", category="mutual_funds", tag="Mutual Funds",
        strategy="rss",
        selectors=["h2.headline a", "h3.headline a", "h2 a"],
    ),
    dict(
        url="https://www.valueresearchonline.com/stories/",
        source="Value Research", category="mutual_funds", tag="Mutual Funds",
        strategy="httpx|selenium",
        selectors=[".story-card h3 a", ".article-card h3 a", "h3 a", "h2 a"],
    ),
    dict(
        url="https://cafemutual.com/news/industry",
        source="Cafemutual", category="mutual_funds", tag="Mutual Funds",
        strategy="httpx|selenium",
        selectors=[".news-item h3 a", ".article-list h3 a", "h3 a", "h2 a"],
        sel_wait=3,
    ),
    # ══════════════════════════════════════════════════════════════════════════
    # FINANCE / ECONOMY
    # ══════════════════════════════════════════════════════════════════════════
    dict(
        rss_url="https://www.livemint.com/rss/money",
        url="https://www.livemint.com/economy",
        source="Livemint", category="finance", tag="Economy",
        strategy="rss",
        selectors=["h2.headline a", "h3.headline a", "h2 a"],
    ),
    dict(
        url="https://economictimes.indiatimes.com/news/economy",
        source="Economic Times", category="finance", tag="Economy",
        strategy="httpx|selenium",
        selectors=[".eachStory h3 a", ".eachStory h2 a", "article h3 a", "h3 a"],
        cookies=ET_COOKIES,
    ),
    dict(
        url="https://www.thehindubusinessline.com/economy/",
        source="Hindu BusinessLine", category="finance", tag="Economy",
        strategy="httpx|selenium",
        selectors=["h2.title a", "h3.title a", ".element h3 a"],
        sel_wait=4,
    ),
    dict(
        url="https://www.moneycontrol.com/news/economy/",
        source="Moneycontrol", category="finance", tag="Economy",
        strategy="httpx|selenium",
        selectors=["li.clearfix a", ".news_list li a", "h2 a"],
    ),
    dict(
        url="https://www.business-standard.com/economy-policy",
        source="Business Standard", category="finance", tag="Economy",
        strategy="httpx|selenium",
        selectors=[".cardlist li a", ".listing-txt h2 a", "h2 a"],
        sel_wait=3,
    ),
    dict(
        # Financial Express — RSS only, browser always hangs
        rss_url="https://www.financialexpress.com/feed/",
        url="https://www.financialexpress.com/economy/",
        source="Financial Express", category="finance", tag="Economy",
        strategy="rss",
        selectors=[".list-wrap h2 a", ".pcArticle h2 a", "h2 a"],
    ),
]


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _strip_html(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()

def _de(s: str) -> str:
    for e, c in [
        ("&amp;","&"),("&lt;","<"),("&gt;",">"),("&quot;",'"'),("&#39;","'"),
        ("&nbsp;"," "),("&#8216;","\u2018"),("&#8217;","\u2019"),
        ("&#8220;","\u201c"),("&#8221;","\u201d"),("&#8230;","\u2026"),
        ("&rsquo;","\u2019"),("&ldquo;","\u201c"),("&rdquo;","\u201d"),
        ("&hellip;","\u2026"),("&mdash;","\u2014"),("&#8211;","\u2013"),
    ]:
        s = s.replace(e, c)
    return s

def _make_id(source: str, title: str) -> str:
    return source.replace(" ","_") + "::" + hashlib.md5(f"{source}::{title}".encode()).hexdigest()[:8]

def _random_ua() -> str:
    return random.choice(UA_POOL)

_SKIP = {
    "subscribe","log in","sign in","sign up","register","cookie policy",
    "privacy policy","terms of use","terms & conditions","advertise with us",
    "contact us","about us","download app","follow us on","newsletter",
    "read more","load more","see all","view all","click here",
    "read full story","more stories","latest news","whatsapp","facebook",
    "twitter","instagram","youtube","telegram","home","markets","stocks",
    "banking","economy","finance","mutual funds","breaking news",
    "live updates","watch live","back to top","hindi","english",
}

def _is_headline(text: str) -> bool:
    if len(text) < 30 or " " not in text:
        return False
    tl = text.lower().strip()
    if tl in _SKIP:
        return False
    if any(tl.startswith(s + " ") or tl == s for s in _SKIP):
        return False
    if re.match(r"^https?://", tl):
        return False
    if not re.search(r"[a-zA-Z]{4,}", text):
        return False
    return True

def _resolve_url(href: str, base_url: str) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/") and base_url:
        p = urlparse(base_url)
        return f"{p.scheme}://{p.netloc}{href}"
    if href.startswith("http"):
        return href
    return ""

def _ft(ds: str) -> tuple[str, int]:
    """Parse a date string → (human label, epoch_ms)."""
    if not ds:
        return "Recently", int(time.time() * 1000) - 60000
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(ds)
        ms = int(d.timestamp() * 1000)
    except Exception:
        try:
            from datetime import datetime
            d = datetime.fromisoformat(ds.replace("Z", "+00:00"))
            ms = int(d.timestamp() * 1000)
        except Exception:
            return "Recently", int(time.time() * 1000) - 60000
    diff = int(time.time() * 1000) - ms
    mins = diff // 60000
    if mins < 1:   return "Just now", ms
    if mins < 60:  return f"{mins}m ago", ms
    hrs = mins // 60
    if hrs < 24:   return f"{hrs}h ago", ms
    return f"{hrs // 24}d ago", ms


# ─── HTML parser (shared by httpx + selenium) ─────────────────────────────────
def _parse_html(
    html: str, source: str, category: str, tag: str,
    selectors: list[str], base_url: str = "",
) -> list[Article]:
    now_ms = int(time.time() * 1000)
    seen: set[str] = set()
    articles: list[Article] = []

    def _add(title: str, href: str, summary: str = "") -> bool:
        title = _de(title).strip()[:180]
        if not _is_headline(title):
            return False
        key = re.sub(r"[^a-z0-9]", "", title[:60].lower())
        if key in seen:
            return False
        seen.add(key)
        url = _resolve_url(href, base_url)
        if not url:
            return False
        clean_summary = _de(summary).strip()[:400] if summary else ""
        if clean_summary and (clean_summary.startswith("http") or len(clean_summary) < 20):
            clean_summary = ""
        articles.append(Article(
            id=_make_id(source, title), title=title, source=source,
            url=url, time="Recently", time_ms=now_ms,
            tag=tag, category=category, summary=clean_summary,
        ))
        return len(articles) >= MAX_ITEMS

    def _sibling_summary(el) -> str:  # type: ignore
        try:
            container = el.find_parent(["li", "article", "div", "section"])
            if container is None:
                return ""
            for p in container.find_all("p", recursive=False):
                txt = p.get_text(separator=" ").strip()
                if txt and not txt.startswith("http") and len(txt) >= 20:
                    return txt
        except Exception:
            pass
        return ""

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")

        for sel in selectors:
            try:
                for el in soup.select(sel):
                    if el.name == "a":
                        has_heading = bool(el.find(["h2", "h3"]))
                        broad_sel = not any(
                            t in sel for t in ("h2", "h3", "headline", "title", "story", "article")
                        )
                        if broad_sel and not has_heading:
                            continue
                        t = el.get_text(separator=" ").strip()
                        h = el.get("href", "")
                        summ = _sibling_summary(el)
                    else:
                        a = el.find("a")
                        if not a:
                            continue
                        t = a.get_text(separator=" ").strip()
                        h = a.get("href", "")
                        summ = _sibling_summary(el)
                    if _add(t, h, summ):
                        break
                if len(articles) >= MAX_ITEMS:
                    break
            except Exception:
                continue

        if not articles:
            for a in soup.find_all("a", href=True):
                if _add(a.get_text(separator=" ").strip(), a["href"], _sibling_summary(a)):
                    break

    except ImportError:
        for m in re.finditer(r'<a\b[^>]+href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>', html, re.I):
            if _add(_strip_html(m.group(2)), m.group(1)):
                break

    return articles


# ─── Strategy 0: httpx static fetch ──────────────────────────────────────────
async def _httpx_fetch(feed: dict) -> list[Article]:
    url = feed["url"]
    source = feed["source"]
    try:
        import httpx
        headers = {
            "User-Agent": _random_ua(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "no-cache",
        }
        if feed.get("cookies"):
            headers["Cookie"] = feed["cookies"]
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=HTTPX_TIMEOUT,
            verify=False,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            arts = _parse_html(
                resp.text, source, feed["category"], feed["tag"],
                feed.get("selectors", []), url,
            )
            if arts:
                log.info("httpx scraped %d articles from %s", len(arts), source)
            return arts
    except Exception as e:
        log.debug("httpx failed (%s): %s", source, e)
        return []


# ─── Strategy 1: RSS fetch ────────────────────────────────────────────────────
def _parse_rss(xml: str, feed: dict) -> list[Article]:
    """Parse RSS/Atom XML → Article list."""
    items = re.findall(r"<item[\s\S]*?</item>|<entry[\s\S]*?</entry>", xml, re.I)
    articles: list[Article] = []
    seen: set[str] = set()

    def xt(block: str, tag: str) -> str:
        c = re.search(rf"<{tag}[^>]*><!\[CDATA\[([\s\S]*?)\]\]>", block, re.I)
        if c: return c.group(1).strip()
        p = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", block, re.I)
        return p.group(1).strip() if p else ""

    source = feed["source"]
    for block in items[:15]:
        raw_title = _de(xt(block, "title"))
        title = re.sub(r"<[^>]*>", "", raw_title).strip()[:180]
        if not _is_headline(title):
            continue
        key = re.sub(r"[^a-z0-9]", "", title[:60].lower())
        if key in seen:
            continue
        seen.add(key)

        link = xt(block, "link")
        if not link:
            # Atom: <link href="..." />
            lm = re.search(r"<link[^>]+href=[\"']([^\"']+)[\"']", block, re.I)
            link = lm.group(1) if lm else ""
        url = link.strip() if link.startswith("http") else ""
        if not url:
            continue

        summary = _de(re.sub(r"<[^>]+>", " ", xt(block, "description") or xt(block, "summary"))).strip()[:400]
        if len(summary) < 20:
            summary = ""

        pub = xt(block, "pubDate") or xt(block, "published") or xt(block, "updated")
        time_label, time_ms = _ft(pub)

        articles.append(Article(
            id=_make_id(source, title), title=title, source=source,
            url=url, time=time_label, time_ms=time_ms,
            tag=feed["tag"], category=feed["category"], summary=summary,
        ))
        if len(articles) >= MAX_ITEMS:
            break

    return articles

async def _rss_fetch(feed: dict) -> list[Article]:
    rss_url = feed.get("rss_url", "")
    if not rss_url:
        return []
    source = feed["source"]
    try:
        import httpx
        async with httpx.AsyncClient(
            headers={"User-Agent": _random_ua(), "Accept": "application/rss+xml,application/xml,text/xml,*/*"},
            follow_redirects=True,
            timeout=RSS_TIMEOUT,
            verify=False,
        ) as client:
            resp = await client.get(rss_url)
            resp.raise_for_status()
            arts = _parse_rss(resp.text, feed)
            if arts:
                log.info("RSS scraped %d articles from %s", len(arts), source)
            return arts
    except Exception as e:
        log.debug("RSS failed (%s): %s", source, e)
        return []


# ─── Strategy 2: Selenium (with hard asyncio timeout) ────────────────────────
_sel_available = False

def _check_selenium() -> bool:
    global _sel_available
    try:
        import undetected_chromedriver as uc  # type: ignore
        opts = uc.ChromeOptions()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        d = uc.Chrome(options=opts, version_main=None)
        d.quit()
        _sel_available = True
        log.info("undetected-chromedriver ready")
        return True
    except Exception:
        pass
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        d = webdriver.Chrome(options=opts)
        d.quit()
        _sel_available = True
        log.info("Selenium ChromeDriver ready")
        return True
    except Exception as e:
        log.warning("Selenium unavailable: %s", e)
        return False

def _sel_fetch_sync(feed: dict) -> list[Article]:
    """Run in executor — blocking Selenium call."""
    url       = feed["url"]
    source    = feed["source"]
    selectors = feed.get("selectors", [])
    wait_secs = feed.get("sel_wait", 3)
    ua = _random_ua()
    driver = None

    try:
        import undetected_chromedriver as uc  # type: ignore
        opts = uc.ChromeOptions()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument(f"--user-agent={ua}")
        driver = uc.Chrome(options=opts, version_main=None)
    except Exception:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument(f"--user-agent={ua}")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        driver = webdriver.Chrome(options=opts)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        })

    try:
        driver.set_page_load_timeout(SEL_PAGE_LOAD)
        driver.get(url)
        time.sleep(wait_secs)
        html = driver.page_source
        return _parse_html(html, source, feed["category"], feed["tag"], selectors, url)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

async def _sel_fetch(feed: dict) -> list[Article]:
    """Selenium fetch wrapped in a hard asyncio timeout — prevents 9-min hangs."""
    if not _sel_available:
        return []
    source = feed["source"]
    try:
        loop = asyncio.get_event_loop()
        # Hard kill: if executor doesn't return in SEL_TIMEOUT seconds → cancel
        arts = await asyncio.wait_for(
            loop.run_in_executor(None, _sel_fetch_sync, feed),
            timeout=SEL_TIMEOUT,
        )
        log.info("Selenium scraped %d articles from %s", len(arts), source)
        return arts
    except asyncio.TimeoutError:
        log.warning("Selenium TIMEOUT (%ds) for %s — skipping", SEL_TIMEOUT, source)
        return []
    except Exception as e:
        log.warning("Selenium failed (%s): %s", source, e)
        return []


# ─── Dispatcher ───────────────────────────────────────────────────────────────
async def _fetch_feed(feed: dict) -> tuple[list[Article], str]:
    source   = feed["source"]
    strategy = feed.get("strategy", "httpx|selenium")

    if strategy == "rss":
        # RSS only — fast, structured, no browser needed
        arts = await _rss_fetch(feed)
        if not arts:
            # RSS failed — try httpx as fallback on the regular URL
            log.debug("RSS failed for %s, trying httpx fallback", source)
            arts = await _httpx_fetch(feed)
        return arts, source

    if strategy == "httpx":
        return await _httpx_fetch(feed), source

    if strategy == "selenium":
        return await _sel_fetch(feed), source

    # strategy == "httpx|selenium" (default)
    # Step 1: fast httpx attempt
    arts = await _httpx_fetch(feed)
    if len(arts) >= 3:
        return arts, source

    # Step 2: httpx got < 3 articles (JS-rendered, bot block, etc.) → Selenium
    if arts:
        log.info("httpx got only %d for %s — trying Selenium", len(arts), source)
    else:
        log.info("httpx got 0 for %s — trying Selenium", source)
    arts = await _sel_fetch(feed)
    return arts, source


# ─── Sequential runner ────────────────────────────────────────────────────────
async def _run_feeds(feeds: list[dict], all_arts: list, sources: list) -> None:
    total = len(feeds)
    for i, feed in enumerate(feeds, 1):
        log.info("[%d/%d] Starting: %s (strategy=%s)", i, total, feed["source"], feed.get("strategy","httpx|selenium"))
        try:
            arts, src = await _fetch_feed(feed)
        except Exception as e:
            log.warning("[%d/%d] ERROR %s: %s", i, total, feed["source"], e)
            arts, src = [], feed["source"]

        if arts:
            all_arts.extend(arts)
            if src not in sources:
                sources.append(src)
            log.info("[%d/%d] DONE %s — %d articles", i, total, src, len(arts))
            _write_cache(CacheFile(
                fetched_at=int(time.time() * 1000),
                articles=list(all_arts),
                sources=list(sources),
            ))
            log.info("[%d/%d] Cache updated (%d total so far)", i, total, len(all_arts))
        else:
            log.info("[%d/%d] SKIP %s — no data", i, total, feed["source"])

        if i < total:
            # Small delay between sites — shorter since httpx is fast
            strategy = feed.get("strategy", "httpx|selenium")
            delay = random.uniform(0.5, 1.5) if strategy in ("rss", "httpx") else random.uniform(2.0, 4.0)
            log.info("[%d/%d] Waiting %.1fs before next site...", i, total, delay)
            await asyncio.sleep(delay)


# ─── Cache ────────────────────────────────────────────────────────────────────
def _read_cache(ignore_ttl: bool = False) -> Optional[CacheFile]:
    try:
        data = json.loads(CACHE_PATH.read_text())
        age = int(time.time() * 1000) - data["fetched_at"]
        if ignore_ttl or age < CACHE_TTL_SECONDS * 1000:
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
            "articles": [asdict(a) for a in c.articles],
            "sources": c.sources,
        }))
    except Exception:
        pass


# ─── Main scraper ─────────────────────────────────────────────────────────────
async def scrape_all() -> CacheFile:
    all_arts: list[Article] = []
    sources:  list[str]     = []

    log.info("=== Checking Selenium ===")
    await asyncio.get_event_loop().run_in_executor(None, _check_selenium)

    log.info("=== Scraping %d feeds ===", len(FEEDS))
    await _run_feeds(FEEDS, all_arts, sources)
    log.info("Raw: %d articles from %d sources", len(all_arts), len(sources))

    seen: set[str] = set()
    unique: list[Article] = []
    for a in all_arts:
        k = re.sub(r"[^a-z0-9]", "", a.title[:60].lower())
        if k not in seen:
            seen.add(k)
            unique.append(a)

    unique.sort(key=lambda a: a.time_ms, reverse=True)
    log.info("Deduplicated: %d articles", len(unique))

    cache = CacheFile(fetched_at=int(time.time() * 1000), articles=unique, sources=sources)
    _write_cache(cache)
    return cache


# ─── FastAPI ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Growing Gradual backend ready")
    log.info("Strategies: httpx (primary) → RSS (structured) → Selenium (JS fallback, 45s timeout)")
    yield

app = FastAPI(title="Growing Gradual Scraper", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

def _resp(cache: CacheFile, from_cache: bool) -> dict:
    return {
        "articles":  [asdict(a) for a in cache.articles],
        "total":     len(cache.articles),
        "sources":   cache.sources,
        "fromCache": from_cache,
        "fetchedAt": datetime.fromtimestamp(cache.fetched_at / 1000, tz=timezone.utc).isoformat(),
    }

@app.get("/api/scrape")
async def get_articles():
    cached = _read_cache(ignore_ttl=True)
    if cached:
        log.info("Serving %d articles from cache", len(cached.articles))
        return _resp(cached, True)
    log.info("No cache — running first-time scrape...")
    return _resp(await scrape_all(), False)

@app.post("/api/scrape")
async def force_refresh():
    return _resp(await scrape_all(), False)

@app.get("/health")
def health():
    return {
        "status": "ok",
        "feeds": len(FEEDS),
        "selenium_ready": _sel_available,
        "cache_exists": CACHE_PATH.exists(),
        "cache_path": str(CACHE_PATH),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("scraper:app", host="0.0.0.0", port=8000, reload=False)
