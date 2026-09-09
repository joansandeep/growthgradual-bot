"""Bounded Selenium fallback for JavaScript-heavy public pages.

The normal research path should stay HTTP-first. Selenium is only a fallback
when static HTTP retrieval returns little useful text or a known bot/challenge
page. All calls are bounded so a browser failure cannot stall report/search
processing.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Optional

SELENIUM_ENABLED = os.getenv("ENABLE_SELENIUM_FETCH", "1").strip().lower() not in {"0", "false", "no", "off"}
SELENIUM_TIMEOUT = float(os.getenv("SELENIUM_FETCH_TIMEOUT", "12"))
SELENIUM_PAGE_LOAD = float(os.getenv("SELENIUM_PAGE_LOAD_TIMEOUT", "10"))


def _chrome_binary() -> str | None:
    configured = os.getenv("CHROME_BIN", "").strip()
    if configured:
        return configured
    for p in ("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"):
        if os.path.exists(p):
            return p
    return None


def _extract_text(html: str, max_chars: int) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<noscript[\s\S]*?</noscript>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()[:max_chars]


def _fetch_sync(url: str, max_chars: int) -> str:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--window-size=1280,1800")
    options.page_load_strategy = "eager"
    binary = _chrome_binary()
    if binary:
        options.binary_location = binary

    driver = webdriver.Chrome(options=options)
    try:
        driver.set_page_load_timeout(SELENIUM_PAGE_LOAD)
        driver.get(url)
        # Give lightweight JS frameworks a chance to paint, but stay bounded.
        try:
            driver.implicitly_wait(1)
        except Exception:
            pass
        body_text = driver.execute_script("return document.body ? document.body.innerText : '';") or ""
        if body_text.strip():
            return re.sub(r"\n{3,}", "\n\n", body_text).strip()[:max_chars]
        return _extract_text(driver.page_source or "", max_chars)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


async def fetch_js_page(url: str, max_chars: int = 6000) -> str:
    if not SELENIUM_ENABLED:
        return ""
    try:
        text = await asyncio.wait_for(asyncio.to_thread(_fetch_sync, url, max_chars), timeout=SELENIUM_TIMEOUT)
        return (text or "")[:max_chars]
    except Exception:
        return ""
