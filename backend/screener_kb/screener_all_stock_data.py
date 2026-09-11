import os
import re
import json
import time
import random
import logging
import csv
import shutil
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
import pandas as pd

from bs4 import BeautifulSoup
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter


# =============================================================================
# CONFIGURATION
# =============================================================================

BASE_URL = "https://www.screener.in"


# =============================================================================
# COMPANY LIMIT
# =============================================================================
#
# 0    = process ALL remaining companies
# 5    = process first 5 remaining companies
# 100  = process first 100 remaining companies
#
# IMPORTANT:
# This applies only to companies NOT already completed.
# =============================================================================

MAX_COMPANIES = 0

# Number of parallel scraper workers. 3 is a safe starting point.
WORKERS = 2


# =============================================================================
# INDUSTRY DISCOVERY
# =============================================================================
#
# None = discover all market/category pages
#
# Example:
# MAX_INDUSTRY_PAGES = 20
#
# =============================================================================

MAX_INDUSTRY_PAGES = None


# =============================================================================
# REQUEST SETTINGS
# =============================================================================

MIN_DELAY = 2.0
MAX_DELAY = 4.0

MAX_RETRIES = 5

REQUEST_TIMEOUT = 30

# Additional delay after rate limiting
RATE_LIMIT_MIN_DELAY = 5
RATE_LIMIT_MAX_DELAY = 15


# =============================================================================
# DATA SETTINGS
# =============================================================================

USE_CONSOLIDATED = True

# Screener chart API can return a very large history.
CHART_DAYS = 10000

# True = save raw company HTML
SAVE_RAW_HTML = False


# =============================================================================
# OUTPUT
# =============================================================================

OUTPUT_DIR = Path(
    r"C:\Users\joan\Documents\scrape_data\screener_all_stocks"
)

COMPANY_DIR = OUTPUT_DIR / "companies"

RAW_DIR = OUTPUT_DIR / "raw_html"


# =============================================================================
# EXCEL FILE DISCOVERY / INTERRUPTED-RUN PROTECTION
# =============================================================================
#
# Company workbooks are written atomically through <name>.tmp.xlsx.
# If Python is stopped while an Excel workbook is being written, that temporary
# file can remain behind and is NOT a real company workbook. Never include it
# in indexing, validation, master rebuilding, or completion detection.
# =============================================================================

def get_company_excel_files():
    """Return only real company workbooks, never interrupted temp files."""
    return sorted(
        p
        for p in COMPANY_DIR.glob("*.xlsx")
        if not p.name.lower().endswith(".tmp.xlsx")
        and not p.name.startswith("~$")
    )


def cleanup_interrupted_excel_temps():
    """Remove orphaned temporary company workbooks from a previous run."""
    removed = 0

    temp_files = set(COMPANY_DIR.glob("*.tmp.xlsx"))
    temp_files.update(COMPANY_DIR.glob("*.TMP.XLSX"))

    for tmp_file in sorted(temp_files):
        try:
            tmp_file.unlink()
            removed += 1
            logger.info("Removed interrupted temporary workbook: %s", tmp_file.name)
        except FileNotFoundError:
            pass
        except PermissionError:
            logger.warning(
                "Could not remove temporary workbook (file may be in use): %s",
                tmp_file,
            )
        except OSError as exc:
            logger.warning(
                "Could not remove temporary workbook %s: %s",
                tmp_file,
                exc,
            )

    if removed:
        logger.info("Cleaned %s orphaned temporary workbook(s).", removed)


MASTER_CSV = OUTPUT_DIR / "master_datapoints.csv"

COMPANY_INDEX_CSV = OUTPUT_DIR / "company_index.csv"

FAILED_CSV = OUTPUT_DIR / "failed_companies.csv"

CHECKPOINT_FILE = OUTPUT_DIR / "checkpoint.json"

DISCOVERY_CACHE_FILE = OUTPUT_DIR / "discovered_companies.json"

# Reuse the saved company universe on later runs instead of rediscovering
# all Screener market/industry pages every time.
# Set to True only when you intentionally want to rebuild the universe.
REFRESH_DISCOVERY = False

# Existing Excel files are considered complete ONLY after validation.
# Incomplete/empty workbooks are automatically put back into the scrape queue.
VALIDATE_EXISTING_WORKBOOKS = True

# Validate the master CSV periodically instead of scanning the entire file after
# every company. The final validation still scans the complete file.
MASTER_VALIDATE_EVERY = 25

# With 3 workers, each worker has its own HTTP session. Shared output files
# are still written by the main thread only.

# Current scraper expects chart history for every company. Existing workbooks
# without Chart_Metrics/chart sheets will be re-scraped.
REQUIRE_CHARTS = True


# =============================================================================
# DIRECTORY CREATION
# =============================================================================

OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

COMPANY_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

if SAVE_RAW_HTML:
    RAW_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )


# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger("screener")

MASTER_APPEND_COUNT = 0


# =============================================================================
# HTTP SESSION
# =============================================================================

_thread_local = threading.local()

def get_session():
    """Return one requests.Session per worker thread."""
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": BASE_URL + "/",
            "Connection": "keep-alive",
        })
        _thread_local.session = s
    return _thread_local.session


# =============================================================================
# HELPERS
# =============================================================================

def clean_text(value):
    """
    Normalize whitespace.
    """

    if value is None:
        return ""

    value = str(value)

    value = value.replace(
        "\xa0",
        " ",
    )

    # Excel/XML does not allow NULL bytes or most C0 control characters.
    # Screener occasionally exposes these characters in scraped text.
    # Remove them here so they cannot poison any downstream Excel cell.
    value = re.sub(
        r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]",
        "",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def safe_filename(value):
    """
    Convert arbitrary text into a Windows-safe filename.
    """

    value = clean_text(
        value
    )

    value = re.sub(
        r'[<>:"/\\|?*]+',
        "_",
        value,
    )

    value = re.sub(
        r"\s+",
        "_",
        value,
    )

    value = value.strip(
        " ._"
    )

    if not value:
        value = "UNKNOWN"

    return value[:150]


def safe_sheet_name(value):
    """
    Excel sheet names:
    - max 31 chars
    - cannot contain []:*?/\
    """

    value = clean_text(
        value
    )

    if not value:
        value = "Sheet"

    value = re.sub(
        r"[\[\]\*\?/:\\]",
        "_",
        value,
    )

    value = value.strip()

    return value[:31]


def absolute_url(url):
    return urljoin(
        BASE_URL,
        url,
    )


def normalize_company_url(url):
    """
    Normalize all Screener company URLs to either:

        /company/TICKER/

    or:

        /company/TICKER/consolidated/

    """

    url = absolute_url(
        url
    )

    parsed = urlparse(
        url
    )

    match = re.search(
        r"/company/([^/]+)",
        parsed.path,
    )

    if not match:
        return url

    ticker = match.group(1).strip()

    if not ticker:
        return url

    if USE_CONSOLIDATED:
        return (
            f"{BASE_URL}/company/"
            f"{ticker}/consolidated/"
        )

    return (
        f"{BASE_URL}/company/"
        f"{ticker}/"
    )


def get_ticker_from_url(url):
    match = re.search(
        r"/company/([^/]+)",
        urlparse(url).path,
    )

    if match:
        return match.group(1)

    return ""


def parse_number(value):
    """
    Convert Screener-style values to numeric values.

    Examples:
        1,234.50 -> 1234.5
        ₹1,234 -> 1234
        (123.5) -> -123.5
        18.4% -> 18.4
    """

    if value is None:
        return None

    value = clean_text(
        value
    )

    if not value:
        return None

    negative = (
        "(" in value
        and ")" in value
    )

    value = (
        value
        .replace(",", "")
        .replace("₹", "")
        .replace("%", "")
        .replace("Rs.", "")
        .replace("Rs", "")
        .replace("Cr.", "")
        .replace("Cr", "")
        .strip()
    )

    match = re.search(
        r"-?\d+(?:\.\d+)?",
        value,
    )

    if not match:
        return None

    try:

        number = float(
            match.group()
        )

        if negative:
            number = -abs(
                number
            )

        return number

    except Exception:
        return None


def sleep_random():
    """
    Random delay between requests.
    """

    time.sleep(
        random.uniform(
            MIN_DELAY,
            MAX_DELAY,
        )
    )


def atomic_replace(
    temporary_file,
    final_file,
):
    """
    Replace destination atomically where possible.
    """

    temporary_file = Path(
        temporary_file
    )

    final_file = Path(
        final_file
    )

    temporary_file.replace(
        final_file
    )


# =============================================================================
# HTTP REQUEST
# =============================================================================

def get_response(
    url,
    params=None,
    is_json=False,
    referer=None,
):
    """
    Robust GET request with:
    - retry
    - 429 handling
    - 5xx handling
    - exponential backoff
    """

    headers = {}

    if referer:
        headers[
            "Referer"
        ] = referer

    if is_json:

        headers[
            "X-Requested-With"
        ] = "XMLHttpRequest"

        headers[
            "Accept"
        ] = (
            "application/json, "
            "text/plain, */*"
        )

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):

        try:

            response = get_session().get(
                url,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )

            logger.info(
                "HTTP %s | %s",
                response.status_code,
                response.url,
            )

            # -------------------------------------------------------------
            # SUCCESS
            # -------------------------------------------------------------

            if response.status_code == 200:
                return response

            # -------------------------------------------------------------
            # RATE LIMIT
            # -------------------------------------------------------------

            if response.status_code == 429:

                retry_after = response.headers.get(
                    "Retry-After"
                )

                if retry_after:

                    try:
                        wait = float(
                            retry_after
                        )

                    except Exception:
                        wait = random.uniform(
                            RATE_LIMIT_MIN_DELAY,
                            RATE_LIMIT_MAX_DELAY,
                        )

                else:

                    wait = min(
                        60,
                        3 * (
                            2 ** (
                                attempt - 1
                            )
                        )
                        + random.uniform(
                            1,
                            4,
                        ),
                    )

                logger.warning(
                    "429 rate limited. "
                    "Waiting %.1fs",
                    wait,
                )

                time.sleep(
                    wait
                )

                continue

            # -------------------------------------------------------------
            # SERVER ERRORS
            # -------------------------------------------------------------

            if response.status_code in (
                500,
                502,
                503,
                504,
            ):

                wait = min(
                    60,
                    (
                        2 ** attempt
                    )
                    + random.uniform(
                        1,
                        3,
                    ),
                )

                logger.warning(
                    "Server error %s. "
                    "Waiting %.1fs",
                    response.status_code,
                    wait,
                )

                time.sleep(
                    wait
                )

                continue

            # -------------------------------------------------------------
            # OTHER HTTP ERROR
            # -------------------------------------------------------------

            response.raise_for_status()

        except requests.RequestException as exc:

            logger.warning(
                "Request failed %s/%s: %s",
                attempt,
                MAX_RETRIES,
                exc,
            )

            if attempt < MAX_RETRIES:

                wait = min(
                    60,
                    (
                        2 ** attempt
                    )
                    + random.uniform(
                        1,
                        3,
                    ),
                )

                time.sleep(
                    wait
                )

    raise RuntimeError(
        f"Request failed after "
        f"{MAX_RETRIES} attempts: "
        f"{url}"
    )


def get_html(
    url,
    referer=None,
):

    response = get_response(
        url,
        referer=referer,
    )

    return response.text


def get_json(
    url,
    params=None,
    referer=None,
):

    response = get_response(
        url,
        params=params,
        is_json=True,
        referer=referer,
    )

    try:
        return response.json()

    except ValueError as exc:

        logger.warning(
            "JSON decode failed: %s",
            exc,
        )

        logger.debug(
            "Response: %s",
            response.text[:1000],
        )

        raise


# =============================================================================
# CHECKPOINT
# =============================================================================

def load_checkpoint():

    if not CHECKPOINT_FILE.exists():

        return {
            "completed": [],
            "failed": [],
        }

    try:

        with open(
            CHECKPOINT_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(
                f
            )

        if not isinstance(
            data,
            dict,
        ):
            raise ValueError(
                "Checkpoint is not a dictionary."
            )

        data.setdefault(
            "completed",
            [],
        )

        data.setdefault(
            "failed",
            [],
        )

        return data

    except Exception as exc:

        logger.warning(
            "Could not load checkpoint: %s",
            exc,
        )

        return {
            "completed": [],
            "failed": [],
        }


def save_checkpoint(
    data
):

    tmp = CHECKPOINT_FILE.with_suffix(
        ".tmp"
    )

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False,
        )

    atomic_replace(
        tmp,
        CHECKPOINT_FILE,
    )


def mark_completed(
    checkpoint,
    company_url,
):
    """
    Mark company completed and remove any previous failed entry.
    """

    completed = set(
        checkpoint.get(
            "completed",
            [],
        )
    )

    completed.add(
        company_url
    )

    checkpoint[
        "completed"
    ] = sorted(
        completed
    )

    checkpoint[
        "failed"
    ] = [
        item
        for item in checkpoint.get(
            "failed",
            [],
        )
        if item.get(
            "url"
        ) != company_url
    ]

    save_checkpoint(
        checkpoint
    )


def mark_failed(
    checkpoint,
    company_url,
    error,
):

    checkpoint.setdefault(
        "failed",
        [],
    )

    checkpoint[
        "failed"
    ] = [
        item
        for item in checkpoint[
            "failed"
        ]
        if item.get(
            "url"
        ) != company_url
    ]

    checkpoint[
        "failed"
    ].append({
        "url": company_url,
        "error": str(error),
        "timestamp": time.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    })

    save_checkpoint(
        checkpoint
    )


# =============================================================================
# TABLE EXTRACTION
# =============================================================================

def extract_table(
    table
):

    if table is None:
        return pd.DataFrame()

    rows = table.find_all(
        "tr"
    )

    if not rows:
        return pd.DataFrame()

    matrix = []

    for tr in rows:

        cells = tr.find_all(
            ["th", "td"]
        )

        if not cells:
            continue

        row = []

        for cell in cells:

            # Screener frequently stores dates
            # in data-date-key.
            date_key = cell.get(
                "data-date-key"
            )

            if date_key:

                value = clean_text(
                    date_key
                )

            else:

                value = clean_text(
                    cell.get_text(
                        " ",
                        strip=True,
                    )
                )

            row.append(
                value
            )

        matrix.append(
            row
        )

    if not matrix:
        return pd.DataFrame()

    width = max(
        len(row)
        for row in matrix
    )

    matrix = [
        row + [""] * (
            width - len(row)
        )
        for row in matrix
    ]

    headers = matrix[0]

    seen = set()

    final_headers = []

    for header in headers:

        header = clean_text(
            header
        )

        if not header:
            header = "Unnamed"

        original = header

        counter = 1

        while header in seen:

            counter += 1

            header = (
                f"{original}_{counter}"
            )

        seen.add(
            header
        )

        final_headers.append(
            header
        )

    df = pd.DataFrame(
        matrix[1:],
        columns=final_headers,
    )

    return df


def section_tables(
    section
):

    output = []

    if section is None:
        return output

    tables = section.find_all(
        "table"
    )

    for i, table in enumerate(
        tables,
        start=1,
    ):

        df = extract_table(
            table
        )

        if df.empty:
            continue

        title = ""

        heading = table.find_previous(
            ["h2", "h3", "h4"]
        )

        if heading:

            title = clean_text(
                heading.get_text(
                    " ",
                    strip=True,
                )
            )

        if not title:

            title = (
                f"Table_{i}"
            )

        output.append(
            (
                title,
                df,
            )
        )

    return output


# =============================================================================
# COMPANY INFORMATION
# =============================================================================

def extract_company_info(
    soup,
    url,
):

    info = {
        "Company URL": url,
        "Company Name": "",
        "Ticker": "",
        "Company ID": "",
        "Warehouse ID": "",
        "Consolidated": "",
        "Current Price": None,
        "Price Change %": None,
        "Price Date": "",
        "Website": "",
    }

    # -------------------------------------------------------------------------
    # COMPANY INFO DATA ATTRIBUTES
    # -------------------------------------------------------------------------

    company_info = soup.select_one(
        "#company-info"
    )

    if company_info:

        info[
            "Company ID"
        ] = company_info.get(
            "data-company-id",
            "",
        )

        info[
            "Warehouse ID"
        ] = company_info.get(
            "data-warehouse-id",
            "",
        )

        info[
            "Consolidated"
        ] = company_info.get(
            "data-consolidated",
            "",
        )

    # -------------------------------------------------------------------------
    # COMPANY NAME
    # -------------------------------------------------------------------------

    h1 = soup.select_one(
        "#top h1"
    )

    if not h1:

        h1 = soup.find(
            "h1"
        )

    if h1:

        info[
            "Company Name"
        ] = clean_text(
            h1.get_text(
                " ",
                strip=True,
            )
        )

    # -------------------------------------------------------------------------
    # TICKER
    # -------------------------------------------------------------------------

    ticker = get_ticker_from_url(
        url
    )

    if ticker:

        info[
            "Ticker"
        ] = ticker

    # -------------------------------------------------------------------------
    # PRICE
    # -------------------------------------------------------------------------

    top = soup.select_one(
        "#top"
    )

    if top:

        price_nodes = top.select(
            ".font-size-18.strong"
        )

        if price_nodes:

            text = clean_text(
                price_nodes[0].get_text(
                    " ",
                    strip=True,
                )
            )

            numbers = re.findall(
                r"-?\d+(?:\.\d+)?",
                text.replace(
                    ",",
                    "",
                ),
            )

            if numbers:

                try:

                    info[
                        "Current Price"
                    ] = float(
                        numbers[0]
                    )

                except Exception:
                    pass

            percent = re.search(
                r"(-?\d+(?:\.\d+)?)\s*%",
                text,
            )

            if percent:

                info[
                    "Price Change %"
                ] = float(
                    percent.group(1)
                )

        # ---------------------------------------------------------------------
        # PRICE DATE
        # ---------------------------------------------------------------------

        date_node = top.select_one(
            ".font-size-11"
        )

        if date_node:

            info[
                "Price Date"
            ] = clean_text(
                date_node.get_text(
                    " ",
                    strip=True,
                )
            )

        # ---------------------------------------------------------------------
        # EXTERNAL WEBSITE
        # ---------------------------------------------------------------------

        for a in top.find_all(
            "a",
            href=True,
        ):

            href = a.get(
                "href",
                "",
            )

            if (
                href.startswith("http")
                and "screener.in" not in href.lower()
                and "bseindia" not in href.lower()
                and "nseindia" not in href.lower()
            ):

                if not info[
                    "Website"
                ]:

                    info[
                        "Website"
                    ] = href

    return info


# =============================================================================
# TOP RATIOS
# =============================================================================

def extract_top_ratios(
    soup
):

    records = []

    container = soup.select_one(
        "#top-ratios"
    )

    if not container:
        return pd.DataFrame()

    for li in container.find_all(
        "li",
        recursive=False,
    ):

        name = li.select_one(
            ".name"
        )

        value = li.select_one(
            ".value"
        )

        name_text = (
            clean_text(
                name.get_text(
                    " ",
                    strip=True,
                )
            )
            if name
            else ""
        )

        value_text = (
            clean_text(
                value.get_text(
                    " ",
                    strip=True,
                )
            )
            if value
            else ""
        )

        if not name_text:
            continue

        records.append({
            "Metric": name_text,
            "Value": value_text,
            "Numeric Value": parse_number(
                value_text
            ),
        })

    return pd.DataFrame(
        records
    )


# =============================================================================
# PROS / CONS
# =============================================================================

def extract_pros_cons(
    soup
):

    section = soup.select_one(
        "section#analysis"
    )

    if not section:
        return pd.DataFrame()

    records = []

    pros = section.select_one(
        ".pros"
    )

    cons = section.select_one(
        ".cons"
    )

    if pros:

        for li in pros.find_all(
            "li"
        ):

            point = clean_text(
                li.get_text(
                    " ",
                    strip=True,
                )
            )

            if point:

                records.append({
                    "Type": "Pros",
                    "Point": point,
                })

    if cons:

        for li in cons.find_all(
            "li"
        ):

            point = clean_text(
                li.get_text(
                    " ",
                    strip=True,
                )
            )

            if point:

                records.append({
                    "Type": "Cons",
                    "Point": point,
                })

    return pd.DataFrame(
        records
    )


# =============================================================================
# PEERS
# =============================================================================

def extract_peers(
    soup,
    info,
    company_url,
):

    warehouse_id = clean_text(
        info.get(
            "Warehouse ID"
        )
    )

    # -------------------------------------------------------------------------
    # API
    # -------------------------------------------------------------------------

    if warehouse_id:

        endpoint = (
            f"{BASE_URL}/api/company/"
            f"{warehouse_id}/peers/"
        )

        try:

            response = get_response(
                endpoint,
                referer=company_url,
            )

            peer_soup = BeautifulSoup(
                response.text,
                "html.parser",
            )

            table = peer_soup.find(
                "table"
            )

            if table:

                df = extract_table(
                    table
                )

                if not df.empty:
                    return df

        except Exception as exc:

            logger.warning(
                "Peer API failed: %s",
                exc,
            )

    # -------------------------------------------------------------------------
    # HTML FALLBACK
    # -------------------------------------------------------------------------

    section = soup.select_one(
        "section#peers"
    )

    if section:

        for table in section.find_all(
            "table"
        ):

            df = extract_table(
                table
            )

            if not df.empty:
                return df

    return pd.DataFrame()


# =============================================================================
# FINANCIAL TABLES
# =============================================================================

FINANCIAL_SECTIONS = {
    "quarters": "Quarterly Results",
    "profit-loss": "Profit & Loss",
    "balance-sheet": "Balance Sheet",
    "cash-flow": "Cash Flow",
    "ratios": "Ratios",
}


def extract_financial_tables(
    soup
):

    sheets = {}

    for section_id, name in FINANCIAL_SECTIONS.items():

        section = soup.select_one(
            f"section#{section_id}"
        )

        if not section:
            continue

        tables = section_tables(
            section
        )

        logger.info(
            "%s: %s table(s)",
            name,
            len(tables),
        )

        for i, (
            title,
            df,
        ) in enumerate(
            tables,
            start=1,
        ):

            if df.empty:
                continue

            if len(tables) == 1:

                sheet_name = name

            else:

                sheet_name = (
                    f"{name}_{i}"
                )

            sheet_name = safe_sheet_name(
                sheet_name
            )

            sheets[
                sheet_name
            ] = df

    return sheets


# =============================================================================
# GROWTH / CAGR
# =============================================================================

def extract_growth_tables(
    soup
):

    records = []

    for table in soup.select(
        "table.ranges-table"
    ):

        heading = table.find(
            "th"
        )

        if not heading:
            continue

        metric = clean_text(
            heading.get_text(
                " ",
                strip=True,
            )
        )

        if not metric:
            continue

        rows = table.find_all(
            "tr"
        )

        for tr in rows[1:]:

            cells = tr.find_all(
                ["td", "th"]
            )

            if len(cells) < 2:
                continue

            period = clean_text(
                cells[0].get_text(
                    " ",
                    strip=True,
                )
            )

            value = clean_text(
                cells[1].get_text(
                    " ",
                    strip=True,
                )
            )

            records.append({
                "Metric": metric,
                "Period": period,
                "Value": value,
                "Numeric Value": parse_number(
                    value
                ),
            })

    return pd.DataFrame(
        records
    )


# =============================================================================
# SHAREHOLDING
# =============================================================================

def extract_shareholding(
    soup
):

    sheets = {}

    section = soup.select_one(
        "section#shareholding"
    )

    if not section:
        return sheets

    containers = [
        (
            "quarterly-shp",
            "Shareholding_Quarterly",
        ),
        (
            "yearly-shp",
            "Shareholding_Yearly",
        ),
    ]

    for container_id, sheet_name in containers:

        container = section.select_one(
            f"#{container_id}"
        )

        if not container:
            continue

        table = container.find(
            "table"
        )

        if not table:
            continue

        df = extract_table(
            table
        )

        if not df.empty:

            sheets[
                sheet_name
            ] = df

    return sheets


# =============================================================================
# DOCUMENTS
# =============================================================================

def extract_documents(
    soup
):

    section = soup.select_one(
        "section#documents"
    )

    if not section:
        return pd.DataFrame()

    records = []

    for a in section.find_all(
        "a",
        href=True,
    ):

        text = clean_text(
            a.get_text(
                " ",
                strip=True,
            )
        )

        href = absolute_url(
            a.get("href")
        )

        if not text:
            continue

        records.append({
            "Document": text,
            "URL": href,
        })

    return pd.DataFrame(
        records
    )


# =============================================================================
# CHART METRIC DISCOVERY
# =============================================================================

def discover_chart_metrics(
    soup
):
    """
    Discover chart queries from multiple possible HTML patterns.

    Screener's chart UI has changed over time, so don't rely exclusively
    on button[name='metrics'].
    """

    metrics = []

    seen = set()

    def add_metric(
        label,
        query,
        source,
    ):

        label = clean_text(
            label
        )

        query = clean_text(
            query
        )

        if not query:
            return

        key = (
            label.lower(),
            query.lower(),
        )

        if key in seen:
            return

        seen.add(
            key
        )

        metrics.append({
            "Chart": (
                label
                or query
            ),
            "Query": query,
            "Source": source,
        })

    # -------------------------------------------------------------------------
    # 1. button[name="metrics"]
    # -------------------------------------------------------------------------

    for button in soup.select(
        "button[name='metrics']"
    ):

        query = clean_text(
            button.get(
                "value",
                "",
            )
        )

        label = clean_text(
            button.get_text(
                " ",
                strip=True,
            )
        )

        add_metric(
            label,
            query,
            "button[name=metrics]",
        )

    # -------------------------------------------------------------------------
    # 2. input[name="metrics"]
    # -------------------------------------------------------------------------

    for node in soup.select(
        "input[name='metrics']"
    ):

        query = clean_text(
            node.get(
                "value",
                "",
            )
        )

        label = clean_text(
            node.get(
                "data-name",
                "",
            )
            or node.get(
                "aria-label",
                "",
            )
        )

        add_metric(
            label,
            query,
            "input[name=metrics]",
        )

    # -------------------------------------------------------------------------
    # 3. data-metric / data-query
    # -------------------------------------------------------------------------

    for node in soup.select(
        "[data-metric]"
    ):

        query = clean_text(
            node.get(
                "data-metric",
                "",
            )
        )

        label = clean_text(
            node.get(
                "data-label",
                "",
            )
            or node.get_text(
                " ",
                strip=True,
            )
        )

        add_metric(
            label,
            query,
            "data-metric",
        )

    # -------------------------------------------------------------------------
    # 4. data-query
    # -------------------------------------------------------------------------

    for node in soup.select(
        "[data-query]"
    ):

        query = clean_text(
            node.get(
                "data-query",
                "",
            )
        )

        label = clean_text(
            node.get(
                "data-label",
                "",
            )
            or node.get_text(
                " ",
                strip=True,
            )
        )

        add_metric(
            label,
            query,
            "data-query",
        )

    # -------------------------------------------------------------------------
    # 5. chart buttons / links
    # -------------------------------------------------------------------------

    for node in soup.select(
        "[data-metric-query]"
    ):

        query = clean_text(
            node.get(
                "data-metric-query",
                "",
            )
        )

        label = clean_text(
            node.get(
                "data-label",
                "",
            )
            or node.get_text(
                " ",
                strip=True,
            )
        )

        add_metric(
            label,
            query,
            "data-metric-query",
        )

    logger.info(
        "Chart metrics discovered: %s",
        len(metrics),
    )

    for metric in metrics:

        logger.info(
            "  Chart=%s | Query=%s | Source=%s",
            metric["Chart"],
            metric["Query"],
            metric["Source"],
        )

    return metrics


# =============================================================================
# CHART API
# =============================================================================

def get_chart_api(
    company_id,
    query,
    days,
    company_url,
):

    endpoint = (
        f"{BASE_URL}/api/company/"
        f"{company_id}/chart/"
    )

    params = {
        "q": query,
        "days": str(days),
    }

    # The chart endpoint follows the company page's consolidated/standalone
    # mode.  Explicitly send it so the chart request cannot silently fall
    # back to the wrong mode for consolidated pages.
    params["consolidated"] = (
        "true" if USE_CONSOLIDATED else "false"
    )

    try:

        return get_json(
            endpoint,
            params=params,
            referer=company_url,
        )

    except Exception as exc:

        logger.warning(
            "Chart API failed | "
            "company_id=%s | query=%s | %s",
            company_id,
            query,
            exc,
        )

        return None


# =============================================================================
# CHART VALUE NORMALIZATION
# =============================================================================

def normalize_chart_date(
    value
):

    if value is None:
        return ""

    # -------------------------------------------------------------------------
    # Unix timestamp
    # -------------------------------------------------------------------------

    if isinstance(
        value,
        (int, float),
    ):

        try:

            # Screener generally uses seconds.
            # Very large values are treated as milliseconds.

            timestamp = float(
                value
            )

            if timestamp > 10_000_000_000:
                timestamp /= 1000

            dt = pd.to_datetime(
                timestamp,
                unit="s",
                errors="coerce",
            )

            if not pd.isna(dt):

                return dt.strftime(
                    "%Y-%m-%d"
                )

        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Existing string date
    # -------------------------------------------------------------------------

    text = clean_text(
        value
    )

    if not text:
        return ""

    # Try ISO / normal date parsing.
    try:

        dt = pd.to_datetime(
            text,
            errors="coerce",
        )

        if not pd.isna(dt):

            return dt.strftime(
                "%Y-%m-%d"
            )

    except Exception:
        pass

    return text


def serialize_chart_value(value):
    """
    Convert chart values into Excel/Pandas-safe scalar values.

    Screener normally returns numbers/strings, but some chart responses can
    contain nested dict/list values. Pandas drop_duplicates() cannot hash
    those objects, so keep them as deterministic JSON strings.
    """

    if isinstance(value, (dict, list, tuple)):

        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except Exception:
            return str(value)

    return value


def extract_chart_point(
    item
):
    """
    Convert different possible chart point formats into:

        date
        value
        extra values

    Supported examples:

        [timestamp, value]

        [date, value]

        {"date": ..., "value": ...}

        {"timestamp": ..., "value": ...}
    """

    if isinstance(
        item,
        dict,
    ):

        date = (
            item.get("date")
            or item.get("timestamp")
            or item.get("time")
            or item.get("x")
        )

        value = (
            item.get("value")
            if "value" in item
            else item.get("y")
        )

        return (
            normalize_chart_date(date),
            serialize_chart_value(value),
            item,
        )

    if isinstance(
        item,
        (list, tuple),
    ):

        if len(item) < 2:
            return (
                "",
                None,
                {},
            )

        date = item[0]

        value = serialize_chart_value(item[1])

        extra = {}

        if len(item) >= 3:

            extra[
                "Extra_1"
            ] = serialize_chart_value(item[2])

        if len(item) >= 4:

            extra[
                "Extra_2"
            ] = serialize_chart_value(item[3])

        return (
            normalize_chart_date(date),
            value,
            extra,
        )

    return (
        "",
        None,
        {},
    )


# =============================================================================
# NORMALIZE CHART DATASET
# =============================================================================

def normalize_chart_dataset(
    dataset,
    chart_name,
    query,
    days,
):
    """
    Convert one Screener chart dataset into a dataframe.

    Screener normally returns:
        {"metric": ..., "label": ..., "values": [[date, value], ...]}

    Some chart families/versions can expose the points under ``data`` or
    ``points``, or return a date->value mapping.  Handle all of those forms
    instead of silently dropping the chart.
    """

    if not isinstance(dataset, dict):
        return pd.DataFrame()

    metric = clean_text(dataset.get("metric", ""))
    label = clean_text(dataset.get("label", ""))

    values = None
    for key in ("values", "data", "points", "series"):
        candidate = dataset.get(key)
        if isinstance(candidate, (list, dict)):
            values = candidate
            break

    if values is None:
        return pd.DataFrame()

    # Normal form: list of points.
    if isinstance(values, dict):
        points = []
        for date_key, raw_value in values.items():
            # A mapping may contain a point object/list as its value.
            if isinstance(raw_value, dict):
                point = dict(raw_value)
                point.setdefault("date", date_key)
                points.append(point)
            elif isinstance(raw_value, (list, tuple)):
                if len(raw_value) >= 2:
                    points.append(raw_value)
                elif len(raw_value) == 1:
                    points.append([date_key, raw_value[0]])
                else:
                    points.append([date_key, None])
            else:
                points.append([date_key, raw_value])
    else:
        points = values

    records = []

    for item in points:
        date, value, extra = extract_chart_point(item)

        if not date:
            # Some dict point formats use a key called x/date that is handled
            # by extract_chart_point; truly malformed points are ignored.
            continue

        record = {
            "Date": date,
            "Metric": metric,
            "Label": label,
            "Value": value,
            "Numeric Value": parse_number(value),
            "Chart": chart_name,
            "Query": query,
            "Days": days,
        }

        if isinstance(extra, dict):
            for key, extra_value in extra.items():
                if key not in record:
                    record[key] = serialize_chart_value(extra_value)

        records.append(record)

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    df = df.drop_duplicates(ignore_index=True)

    if "Date" in df.columns:
        parsed_dates = pd.to_datetime(
            df["Date"],
            errors="coerce",
        )
        if parsed_dates.notna().any():
            df = (
                df.assign(_sort_date=parsed_dates)
                .sort_values("_sort_date")
                .drop(columns=["_sort_date"])
            )

    return df.reset_index(drop=True)


# =============================================================================
# CHART RESPONSE EXTRACTION
# =============================================================================

def extract_chart_datasets(
    data
):
    """
    Find datasets in the API response.

    Normal Screener response:
        {
            "datasets": [...]
        }

    Also handles nested responses where possible.
    """

    if not isinstance(
        data,
        dict,
    ):
        return []

    datasets = data.get(
        "datasets"
    )

    if isinstance(
        datasets,
        list,
    ):

        return datasets

    # -------------------------------------------------------------------------
    # Alternate nested locations
    # -------------------------------------------------------------------------

    for key in (
        "data",
        "chart",
        "result",
        "results",
    ):

        nested = data.get(
            key
        )

        if isinstance(
            nested,
            dict,
        ):

            datasets = nested.get(
                "datasets"
            )

            if isinstance(
                datasets,
                list,
            ):

                return datasets

    return []


# =============================================================================
# ACTUAL CHART DATA
# =============================================================================

def make_chart_placeholder(
    chart_name,
    query,
    reason,
):
    """Create an explicit chart sheet when Screener returns no points.

    A missing optional chart must not make an otherwise valid company fail.
    The placeholder is deliberately labelled so downstream users cannot
    mistake it for real financial observations.
    """
    return pd.DataFrame([{
        "Date": "",
        "Metric": "",
        "Label": "",
        "Value": "",
        "Numeric Value": None,
        "Chart": chart_name,
        "Query": query,
        "Days": CHART_DAYS,
        "Data Source": "Screener chart endpoint",
        "Status": "NO_DATA",
        "Note": clean_text(reason),
    }])


def extract_chart_data(
    soup,
    info,
    company_url,
):

    company_id = clean_text(info.get("Company ID"))

    if not company_id:
        logger.warning("No Company ID; cannot fetch chart API.")
        return {}

    metrics = discover_chart_metrics(soup)

    if not metrics:
        logger.warning("No chart metrics found.")
        return {}

    chart_sheets = {}
    days = CHART_DAYS

    for metric_info in metrics:
        chart_name = metric_info.get("Chart", "")
        query = metric_info.get("Query", "")

        logger.info(
            "Fetching chart | %s | query=%s",
            chart_name,
            query,
        )

        data = get_chart_api(
            company_id,
            query,
            days,
            company_url,
        )

        combined = pd.DataFrame()
        failure_reason = ""

        if not data:
            failure_reason = "Empty/failed chart API response"
        else:
            datasets = extract_chart_datasets(data)

            if not datasets:
                failure_reason = "Chart API returned no datasets"
            else:
                all_records = []

                for dataset in datasets:
                    df = normalize_chart_dataset(
                        dataset,
                        chart_name,
                        query,
                        days,
                    )

                    if df.empty:
                        continue

                    all_records.append(df)

                    logger.info(
                        "  Dataset=%s | rows=%s",
                        dataset.get("metric", ""),
                        len(df),
                    )

                    if "Date" in df.columns and not df.empty:
                        logger.info(
                            "    Date range: %s -> %s",
                            df["Date"].min(),
                            df["Date"].max(),
                        )

                if all_records:
                    combined = pd.concat(
                        all_records,
                        ignore_index=True,
                    )
                    dedupe_columns = [
                        column
                        for column in (
                            "Date", "Metric", "Label", "Value", "Chart", "Query"
                        )
                        if column in combined.columns
                    ]
                    if dedupe_columns:
                        combined = combined.drop_duplicates(
                            subset=dedupe_columns
                        )
                else:
                    failure_reason = "Chart datasets contained no usable points"

        if combined.empty:
            logger.warning(
                "No populated chart data for %s; creating explicit placeholder sheet.",
                chart_name,
            )
            combined = make_chart_placeholder(
                chart_name,
                query,
                failure_reason or "No usable chart points returned",
            )

        sheet_name = safe_sheet_name(
            "Chart_" + safe_filename(chart_name)
        )

        base = sheet_name
        counter = 2
        while sheet_name in chart_sheets:
            suffix = f"_{counter}"
            sheet_name = base[:31 - len(suffix)] + suffix
            counter += 1

        chart_sheets[sheet_name] = combined.reset_index(drop=True)
        sleep_random()

    return chart_sheets


# =============================================================================
# EXCEL FORMATTING
# =============================================================================

def format_workbook(
    output_file
):

    wb = load_workbook(
        output_file
    )

    for ws in wb.worksheets:

        if ws.max_row >= 1:

            ws.freeze_panes = "A2"

        if (
            ws.max_row > 1
            and ws.max_column > 0
        ):

            ws.auto_filter.ref = (
                ws.dimensions
            )

        for column_cells in ws.columns:

            max_length = 0

            for cell in column_cells:

                try:

                    if cell.value is None:
                        continue

                    value = str(
                        cell.value
                    )

                    max_length = max(
                        max_length,
                        len(value),
                    )

                except Exception:
                    pass

            width = min(
                max(
                    10,
                    max_length + 2,
                ),
                45,
            )

            letter = get_column_letter(
                column_cells[0].column
            )

            ws.column_dimensions[
                letter
            ].width = width

    wb.save(
        output_file
    )


# =============================================================================
# SAVE EXCEL
# =============================================================================

# Excel/XML 1.0 does not allow C0 control characters (except TAB/LF/CR),
# surrogate code points, or the C1 control range. Screener occasionally returns
# hidden characters in HTML/API text. If even one reaches lxml/openpyxl, saving
# the entire workbook fails with:
#   ValueError: All strings must be XML compatible...
_EXCEL_ILLEGAL_CHARS_RE = re.compile(
    r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F"
    r"\uD800-\uDFFF]"
)


def sanitize_excel_text(value):
    """Return XML-safe text for Excel while preserving normal Unicode."""
    if value is None:
        return ""

    text = value if isinstance(value, str) else str(value)

    # Remove characters which lxml/openpyxl cannot put in XLSX XML.
    text = _EXCEL_ILLEGAL_CHARS_RE.sub("", text)

    # Be defensive about any surrogate pair/codepoint that survived unusual
    # Unicode handling.
    text = "".join(
        ch for ch in text
        if not 0xD800 <= ord(ch) <= 0xDFFF
    )

    return text


def sanitize_excel_value(value):
    """
    Sanitize ANY value that can reach openpyxl.

    The previous implementation only mapped object-dtype DataFrame columns.
    That is not sufficient because pandas extension dtypes, mixed columns,
    numpy scalars, and values converted during Excel writing can bypass that
    branch. This version recursively converts containers and sanitizes every
    string representation.
    """
    if value is None:
        return ""

    if isinstance(value, str):
        return sanitize_excel_text(value)

    if isinstance(value, dict):
        try:
            return sanitize_excel_text(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
            )
        except Exception:
            return sanitize_excel_text(value)

    if isinstance(value, (list, tuple, set)):
        try:
            return sanitize_excel_text(
                json.dumps(
                    list(value),
                    ensure_ascii=False,
                    default=str,
                )
            )
        except Exception:
            return sanitize_excel_text(value)

    # Bytes can otherwise be handed to XML serialization as a non-string type.
    if isinstance(value, (bytes, bytearray)):
        try:
            return sanitize_excel_text(bytes(value).decode("utf-8", errors="replace"))
        except Exception:
            return sanitize_excel_text(value)

    # Leave normal numeric/date/bool/None values typed so Excel retains them.
    return value


def sanitize_dataframe_for_excel(df):
    """
    Sanitize headers AND EVERY CELL before openpyxl sees the DataFrame.

    This intentionally does not restrict the operation to dtype == object.
    A single illegal Unicode/control character anywhere in any sheet can make
    openpyxl/lxml abort the whole workbook save.
    """
    df = df.copy()

    # Headers are XML text too.
    df.columns = [
        sanitize_excel_text(column)
        for column in df.columns
    ]

    # Map every cell, not just object columns. DataFrame.map is available in
    # current pandas; the fallback supports older pandas releases.
    try:
        df = df.map(sanitize_excel_value)
    except AttributeError:
        df = df.applymap(sanitize_excel_value)

    return df


def find_illegal_excel_cells(df):
    """Return a small diagnostic list of cells containing illegal XML text."""
    findings = []

    for row_pos, row in enumerate(df.itertuples(index=False, name=None), start=2):
        for col_pos, value in enumerate(row, start=1):
            if isinstance(value, str) and _EXCEL_ILLEGAL_CHARS_RE.search(value):
                findings.append(
                    (row_pos, col_pos, repr(value[:120]))
                )
                if len(findings) >= 10:
                    return findings

    return findings


def save_excel(
    sheets,
    output_file
):

    clean_sheets = {}

    for name, df in sheets.items():

        if df is None:
            continue

        if not isinstance(
            df,
            pd.DataFrame,
        ):
            continue

        if df.empty:
            continue

        df = df.copy()

        df = df.dropna(
            how="all"
        )

        df = df.fillna(
            ""
        )

        # Final XLSX/XML safety pass. This catches control characters that
        # may have entered through raw HTML/API data or nested objects.
        df = sanitize_dataframe_for_excel(df)

        df.columns = [
            str(column)
            for column in df.columns
        ]

        name = safe_sheet_name(
            name
        )

        # Avoid duplicate Excel sheet names.
        base = name

        counter = 2

        while name in clean_sheets:

            suffix = (
                f"_{counter}"
            )

            name = (
                base[:31 - len(suffix)]
                + suffix
            )

            counter += 1

        clean_sheets[
            name
        ] = df

    if not clean_sheets:

        raise RuntimeError(
            "No data to save."
        )

    # -------------------------------------------------------------------------
    # Write to temporary file first.
    # -------------------------------------------------------------------------

    output_file = Path(
        output_file
    )

    tmp = output_file.with_suffix(
        ".tmp.xlsx"
    )

    # Keep the external temporary workbook isolated. If Ctrl+C or an exception
    # happens during the openpyxl/lxml write, remove it so a half-written file
    # can never be mistaken for a completed company workbook.
    try:
        with pd.ExcelWriter(
            tmp,
            engine="openpyxl",
        ) as writer:

            for name, df in clean_sheets.items():

                # One final defensive pass immediately before pandas/openpyxl.
                # This is deliberately repeated after all transformations.
                df = sanitize_dataframe_for_excel(df)

                illegal = find_illegal_excel_cells(df)
                if illegal:
                    raise ValueError(
                        f"Illegal Excel/XML characters remain in sheet "
                        f"{name!r}: {illegal[:3]}"
                    )

                df.to_excel(
                    writer,
                    sheet_name=name,
                    index=False,
                )

        format_workbook(
            tmp
        )

        atomic_replace(
            tmp,
            output_file
        )

    except Exception:
        # Never leave a failed company .tmp.xlsx behind.
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception as cleanup_exc:
            logger.warning(
                "Could not remove failed temporary workbook %s: %s",
                tmp,
                cleanup_exc,
            )
        raise


# =============================================================================
# SCRAPED DATA VALIDATION
# =============================================================================

def validate_scraped_sheets(sheets, chart_metrics):
    """Fail the company scrape if required data was not actually extracted."""
    required = {
        "Quarterly Results": lambda n: n == "Quarterly Results",
        "Profit & Loss": lambda n: n == "Profit & Loss" or n.startswith("Profit & Loss_"),
        "Balance Sheet": lambda n: n == "Balance Sheet" or n.startswith("Balance Sheet_"),
        "Cash Flow": lambda n: n == "Cash Flow" or n.startswith("Cash Flow_"),
        "Ratios": lambda n: n == "Ratios" or n.startswith("Ratios_"),
    }

    errors = []

    for label, matcher in required.items():
        matches = [
            (name, df)
            for name, df in sheets.items()
            if matcher(name)
            and isinstance(df, pd.DataFrame)
            and not df.empty
        ]
        if not matches:
            errors.append(f"No populated {label} data extracted")

    if chart_metrics:
        # Chart API calls are especially sensitive to Screener rate limiting.
        # A company must not be discarded just because one optional chart
        # returned an empty response/temporary 429 after all retries.
        # Record the missing chart as a warning; the financial tables remain
        # subject to the hard validation above.
        if "Chart_Metrics" not in sheets or sheets["Chart_Metrics"].empty:
            logger.warning(
                "Chart_Metrics was discovered but could not be populated; "
                "continuing with the company scrape."
            )
        else:
            chart_names = [
                clean_text(x)
                for x in sheets["Chart_Metrics"].get(
                    "Chart", pd.Series(dtype=str)
                ).tolist()
                if clean_text(x)
            ]
            chart_sheet_names = set(sheets.keys())
            for chart_name in sorted(set(chart_names)):
                expected = safe_sheet_name(
                    "Chart_" + safe_filename(chart_name)
                )
                matching = [
                    n for n in chart_sheet_names
                    if n == expected or n.startswith(expected + "_")
                ]
                if not any(
                    isinstance(sheets[n], pd.DataFrame) and not sheets[n].empty
                    for n in matching
                ):
                    logger.warning(
                        "No populated chart data for %s; continuing without "
                        "that chart.",
                        chart_name,
                    )

    if errors:
        raise RuntimeError("; ".join(errors))


# =============================================================================
# COMPANY SCRAPER
# =============================================================================

def scrape_company(
    company_url
):

    html = get_html(
        company_url
    )

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    info = extract_company_info(
        soup,
        company_url,
    )

    ticker = (
        info.get(
            "Ticker"
        )
        or "UNKNOWN"
    )

    company_name = (
        info.get(
            "Company Name"
        )
        or ticker
    )

    logger.info(
        "Company: %s",
        company_name,
    )

    logger.info(
        "Ticker: %s",
        ticker,
    )

    # -------------------------------------------------------------------------
    # Raw HTML
    # -------------------------------------------------------------------------

    if SAVE_RAW_HTML:

        raw_file = (
            RAW_DIR
            / f"{safe_filename(ticker)}.html"
        )

        raw_file.write_text(
            html,
            encoding="utf-8",
        )

    sheets = {}

    # -------------------------------------------------------------------------
    # COMPANY INFO
    # -------------------------------------------------------------------------

    sheets[
        "Company_Info"
    ] = pd.DataFrame(
        [info]
    )

    # -------------------------------------------------------------------------
    # TOP RATIOS
    # -------------------------------------------------------------------------

    top_ratios = extract_top_ratios(
        soup
    )

    if not top_ratios.empty:

        sheets[
            "Top_Ratios"
        ] = top_ratios

    # -------------------------------------------------------------------------
    # PROS / CONS
    # -------------------------------------------------------------------------

    pros_cons = extract_pros_cons(
        soup
    )

    if not pros_cons.empty:

        sheets[
            "Pros_Cons"
        ] = pros_cons

    # -------------------------------------------------------------------------
    # PEERS
    # -------------------------------------------------------------------------

    peers = extract_peers(
        soup,
        info,
        company_url,
    )

    if not peers.empty:

        sheets[
            "Peers"
        ] = peers

    # -------------------------------------------------------------------------
    # FINANCIAL TABLES
    # -------------------------------------------------------------------------

    financial_sheets = (
        extract_financial_tables(
            soup
        )
    )

    sheets.update(
        financial_sheets
    )

    # -------------------------------------------------------------------------
    # GROWTH / CAGR
    # -------------------------------------------------------------------------

    growth = extract_growth_tables(
        soup
    )

    if not growth.empty:

        sheets[
            "Growth_CAGR"
        ] = growth

    # -------------------------------------------------------------------------
    # SHAREHOLDING
    # -------------------------------------------------------------------------

    shareholding = extract_shareholding(
        soup
    )

    sheets.update(
        shareholding
    )

    # -------------------------------------------------------------------------
    # DOCUMENTS
    # -------------------------------------------------------------------------

    documents = extract_documents(
        soup
    )

    if not documents.empty:

        sheets[
            "Documents"
        ] = documents

    # -------------------------------------------------------------------------
    # CHART METRICS
    # -------------------------------------------------------------------------

    chart_metrics = discover_chart_metrics(
        soup
    )

    if chart_metrics:

        sheets[
            "Chart_Metrics"
        ] = pd.DataFrame(
            chart_metrics
        )

    # -------------------------------------------------------------------------
    # CHART HISTORY
    # -------------------------------------------------------------------------

    chart_sheets = extract_chart_data(
        soup,
        info,
        company_url,
    )

    sheets.update(
        chart_sheets
    )

    # -------------------------------------------------------------------------
    # EXCEL
    # -------------------------------------------------------------------------

    filename = (
        f"{safe_filename(ticker)}_"
        f"{safe_filename(company_name)}.xlsx"
    )

    output_file = (
        COMPANY_DIR
        / filename
    )

    # -------------------------------------------------------------------------
    # HARD DATA-INTEGRITY CHECK
    # -------------------------------------------------------------------------
    # Never call a company SUCCESS when the core sections/charts were not
    # actually extracted. Optional sections may legitimately be absent.
    validate_scraped_sheets(
        sheets,
        chart_metrics,
    )

    save_excel(
        sheets,
        output_file,
    )

    # Re-open the file and validate what was actually written to disk.
    valid_file, validation_errors = validate_company_workbook(output_file)
    if not valid_file:
        raise RuntimeError(
            "Saved Excel failed validation: "
            + "; ".join(validation_errors)
        )

    return (
        info,
        sheets,
        output_file,
    )


# =============================================================================
# INDUSTRY DISCOVERY
# =============================================================================

def discover_industry_pages():

    logger.info("")
    logger.info("=" * 80)
    logger.info(
        "DISCOVERING MARKET / INDUSTRY PAGES"
    )
    logger.info("=" * 80)

    found = set()

    queue = [
        f"{BASE_URL}/market/"
    ]

    visited = set()

    while queue:

        url = queue.pop(
            0
        )

        url = url.rstrip(
            "/"
        ) + "/"

        if url in visited:
            continue

        visited.add(
            url
        )

        if len(visited) > 5000:

            logger.warning(
                "Industry discovery safety limit reached."
            )

            break

        try:

            html = get_html(
                url
            )

        except Exception as exc:

            logger.warning(
                "Market page failed: %s",
                exc,
            )

            continue

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        for a in soup.find_all(
            "a",
            href=True,
        ):

            href = clean_text(
                a.get(
                    "href",
                    "",
                )
            )

            if not href.startswith(
                "/market/"
            ):
                continue

            full = absolute_url(
                href
            )

            full = full.rstrip(
                "/"
            ) + "/"

            # Skip root market page.
            if full == (
                f"{BASE_URL}/market/"
            ):
                continue

            if full not in found:

                found.add(
                    full
                )

                if full not in visited:

                    queue.append(
                        full
                    )

        logger.info(
            "Industry pages discovered so far: %s",
            len(found),
        )

        sleep_random()

    result = sorted(
        found
    )

    if MAX_INDUSTRY_PAGES:

        result = result[
            :MAX_INDUSTRY_PAGES
        ]

    logger.info(
        "Industry/category URLs found: %s",
        len(result),
    )

    return result


# =============================================================================
# COMPANY DISCOVERY
# =============================================================================

def extract_company_links(
    soup
):

    companies = set()

    for a in soup.find_all(
        "a",
        href=True,
    ):

        href = clean_text(
            a.get(
                "href",
                "",
            )
        )

        if not href.startswith(
            "/company/"
        ):
            continue

        match = re.match(
            r"^/company/([^/?#]+)",
            href,
        )

        if not match:
            continue

        ticker = match.group(
            1
        )

        if not ticker:
            continue

        companies.add(
            normalize_company_url(
                href
            )
        )

    return companies


def find_next_market_page(
    soup,
    current_url,
):

    current_url = (
        current_url.rstrip("/")
        + "/"
    )

    # -------------------------------------------------------------------------
    # Preferred: explicit Next text
    # -------------------------------------------------------------------------

    for a in soup.find_all(
        "a",
        href=True,
    ):

        href = clean_text(
            a.get(
                "href",
                "",
            )
        )

        text = clean_text(
            a.get_text(
                " ",
                strip=True,
            )
        ).lower()

        if not href.startswith(
            "/market/"
        ):
            continue

        full = absolute_url(
            href
        )

        full = full.rstrip(
            "/"
        ) + "/"

        if full == current_url:
            continue

        if (
            text == "next"
            or text.startswith(
                "next "
            )
            or text == "›"
            or text == ">"
        ):

            return full

    # -------------------------------------------------------------------------
    # Fallback: rel=next
    # -------------------------------------------------------------------------

    next_link = soup.select_one(
        "a[rel='next']"
    )

    if next_link:

        href = next_link.get(
            "href",
            "",
        )

        if href:

            full = absolute_url(
                href
            )

            full = full.rstrip(
                "/"
            ) + "/"

            if full != current_url:

                return full

    # -------------------------------------------------------------------------
    # Fallback: pagination href
    # -------------------------------------------------------------------------

    current_parsed = urlparse(
        current_url
    )

    current_query = parse_qs(
        current_parsed.query
    )

    current_page = 1

    if "page" in current_query:

        try:

            current_page = int(
                current_query[
                    "page"
                ][0]
            )

        except Exception:
            current_page = 1

    candidates = []

    for a in soup.find_all(
        "a",
        href=True,
    ):

        href = clean_text(
            a.get(
                "href",
                "",
            )
        )

        if not href.startswith(
            "/market/"
        ):
            continue

        full = absolute_url(
            href
        )

        parsed = urlparse(
            full
        )

        qs = parse_qs(
            parsed.query
        )

        if "page" not in qs:
            continue

        try:

            page = int(
                qs["page"][0]
            )

        except Exception:
            continue

        if page > current_page:

            candidates.append(
                (
                    page,
                    full,
                )
            )

    if candidates:

        candidates.sort(
            key=lambda x: x[0]
        )

        return candidates[0][1]

    return None


def discover_companies(
    industry_urls
):

    logger.info("")
    logger.info("=" * 80)
    logger.info(
        "DISCOVERING COMPANIES"
    )
    logger.info("=" * 80)

    all_companies = set()

    for index, industry_url in enumerate(
        industry_urls,
        start=1,
    ):

        logger.info("")
        logger.info(
            "Industry %s/%s",
            index,
            len(industry_urls),
        )

        logger.info(
            industry_url
        )

        try:

            current_url = (
                industry_url
            )

            visited_pages = set()

            page_count = 0

            while current_url:

                if current_url in visited_pages:
                    break

                visited_pages.add(
                    current_url
                )

                page_count += 1

                # Safety limit per category.
                if page_count > 10000:

                    logger.warning(
                        "Pagination safety limit reached."
                    )

                    break

                html = get_html(
                    current_url
                )

                soup = BeautifulSoup(
                    html,
                    "html.parser",
                )

                companies = (
                    extract_company_links(
                        soup
                    )
                )

                before = len(
                    all_companies
                )

                all_companies.update(
                    companies
                )

                logger.info(
                    "Page %s | companies found=%s | "
                    "new unique=%s | total=%s",
                    page_count,
                    len(companies),
                    len(all_companies) - before,
                    len(all_companies),
                )

                next_url = (
                    find_next_market_page(
                        soup,
                        current_url,
                    )
                )

                if not next_url:
                    break

                if next_url in visited_pages:
                    break

                current_url = next_url

                sleep_random()

        except Exception as exc:

            logger.warning(
                "Industry failed: %s",
                exc,
            )

        sleep_random()

    result = sorted(
        all_companies
    )

    logger.info(
        "Total unique companies discovered: %s",
        len(result),
    )

    return result


# =============================================================================
# DISCOVERY CACHE
# =============================================================================

def save_discovery_cache(
    company_urls
):

    data = {
        "timestamp": time.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "count": len(
            company_urls
        ),
        "companies": sorted(
            company_urls
        ),
    }

    tmp = DISCOVERY_CACHE_FILE.with_suffix(
        ".tmp"
    )

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False,
        )

    atomic_replace(
        tmp,
        DISCOVERY_CACHE_FILE,
    )


def load_discovery_cache():

    if not DISCOVERY_CACHE_FILE.exists():
        return []

    try:

        with open(
            DISCOVERY_CACHE_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(
                f
            )

        companies = data.get(
            "companies",
            [],
        )

        if isinstance(
            companies,
            list,
        ):

            return sorted(
                set(
                    companies
                )
            )

    except Exception as exc:

        logger.warning(
            "Could not load discovery cache: %s",
            exc,
        )

    return []


# =============================================================================
# FLATTEN COMPANY
# =============================================================================

def is_period_label(value):
    """Return True when a column/value looks like a financial or chart period."""

    text = clean_text(value)

    if not text:
        return False

    if text.upper() in {"TTM", "LTM"}:
        return True

    patterns = [
        r"^\d{2}-\d{2}-\d{4}$",       # 30-06-2023
        r"^\d{2}/\d{2}/\d{4}$",       # 30/06/2023
        r"^\d{4}-\d{2}-\d{2}$",       # 2023-06-30
        r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[ -]\d{2,4}$",
        r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4}$",
        r"^\d{4}$",
    ]

    return any(re.match(pattern, text, re.IGNORECASE) for pattern in patterns)


def infer_period_type(sheet_name, period):
    """Infer a useful period type without changing the source period label."""

    sheet = clean_text(sheet_name).lower()
    period = clean_text(period)

    if "chart" in sheet:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", period):
            return "Daily"
        return "Chart"

    if "quarter" in sheet:
        return "Quarterly"

    if "profit & loss" in sheet or "profit and loss" in sheet:
        return "Annual"

    if "balance sheet" in sheet or "cash flow" in sheet:
        return "Annual"

    if "shareholding" in sheet:
        return "Quarterly"

    if period.upper() in {"TTM", "LTM"}:
        return "TTM"

    if re.match(r"^(Mar)[ -]\d{2,4}$", period, re.IGNORECASE):
        return "Annual"

    if re.match(r"^\d{2}-\d{2}-\d{4}$", period):
        return "Period"

    return "Other"


def flatten_dataframe_to_master_records(info, sheet_name, df):
    """Convert any worksheet into the fixed long-form master schema."""

    if df is None or df.empty:
        return []

    df = df.copy()
    df.columns = [clean_text(c) or "Unnamed" for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated()]

    base = {
        "Company URL": info.get("Company URL", ""),
        "Company Name": info.get("Company Name", ""),
        "Ticker": info.get("Ticker", ""),
        "Company ID": info.get("Company ID", ""),
        "Warehouse ID": info.get("Warehouse ID", ""),
        "Sheet": clean_text(sheet_name),
    }

    records = []
    columns = list(df.columns)
    if not columns:
        return records

    for row_number, (_, row) in enumerate(df.iterrows(), start=1):
        first_column = columns[0]
        first_value = clean_text(row.get(first_column, ""))

        # Chart data has a row-level Date plus fields such as Metric, Label,
        # Value, Numeric Value, Chart, Query and Days. Preserve every field
        # without turning those field names into master columns.
        if first_column.lower() in {"date", "period"} and is_period_label(first_value):
            metric = clean_text(row.get("Metric", ""))
            if not metric:
                metric = clean_text(row.get("Label", ""))
            if not metric:
                metric = clean_text(row.get("Chart", ""))

            chart_value = row.get("Value", "")
            if pd.isna(chart_value):
                chart_value = ""

            chart_value = clean_text(chart_value)

            if chart_value:
                numeric = row.get("Numeric Value", "")
                if pd.isna(numeric):
                    numeric = parse_number(chart_value)

                records.append({
                    **base,
                    "Metric": metric,
                    "Period": first_value,
                    "Period Type": infer_period_type(sheet_name, first_value),
                    "Source Column": "Value",
                    "Value": chart_value,
                    "Numeric Value": numeric,
                    "Row Number": row_number,
                })

            # Preserve additional chart attributes as observations.
            for column in columns[1:]:
                if column in {"Metric", "Label", "Value", "Numeric Value"}:
                    continue

                raw_value = row.get(column, "")
                if pd.isna(raw_value):
                    raw_value = ""
                value = clean_text(raw_value)
                if not value:
                    continue

                records.append({
                    **base,
                    "Metric": metric,
                    "Period": first_value,
                    "Period Type": infer_period_type(sheet_name, first_value),
                    "Source Column": clean_text(column),
                    "Value": value,
                    "Numeric Value": parse_number(value),
                    "Row Number": row_number,
                })

            continue

        # Tables with S.No. + Name use Name as the row/metric label.
        if (
            len(columns) >= 2
            and first_column.lower() in {"s.no.", "s.no", "s no", "serial no", "serial number"}
            and clean_text(columns[1]).lower() in {"name", "particulars", "particular"}
        ):
            label_column = columns[1]
            value_columns = columns[2:]
        else:
            label_column = first_column
            value_columns = columns[1:]

        row_label = clean_text(row.get(label_column, ""))

        for column in value_columns:
            raw_value = row.get(column, "")
            if pd.isna(raw_value):
                raw_value = ""

            value = clean_text(raw_value)
            if not value:
                continue

            period = clean_text(column) if is_period_label(column) else ""
            source_column = "" if period else clean_text(column)

            records.append({
                **base,
                "Metric": row_label,
                "Period": period,
                "Period Type": infer_period_type(sheet_name, period),
                "Source Column": source_column,
                "Value": value,
                "Numeric Value": parse_number(value),
                "Row Number": row_number,
            })

    # Preserve a one-column worksheet.
    if len(columns) == 1:
        for row_number, (_, row) in enumerate(df.iterrows(), start=1):
            value = clean_text(row.iloc[0])
            if value:
                records.append({
                    **base,
                    "Metric": value,
                    "Period": "",
                    "Period Type": "Other",
                    "Source Column": "",
                    "Value": value,
                    "Numeric Value": parse_number(value),
                    "Row Number": row_number,
                })

    return records


def flatten_company(info, sheets):
    """Create normalized long-form master records from scraped company sheets."""

    records = []

    # Company metadata is represented as observations too, so the master
    # schema never needs to expand when a new info field appears.
    company_info_fields = [
        "Company URL", "Company Name", "Ticker", "Company ID",
        "Warehouse ID", "Consolidated", "Current Price",
        "Price Change %", "Price Date", "Website", "BSE", "NSE",
    ]

    for field in company_info_fields:
        value = info.get(field, "")
        if pd.isna(value):
            value = ""
        value = clean_text(value)
        if not value:
            continue

        records.append({
            "Company URL": info.get("Company URL", ""),
            "Company Name": info.get("Company Name", ""),
            "Ticker": info.get("Ticker", ""),
            "Company ID": info.get("Company ID", ""),
            "Warehouse ID": info.get("Warehouse ID", ""),
            "Sheet": "Company_Info",
            "Metric": field,
            "Period": "",
            "Period Type": "Metadata",
            "Source Column": "",
            "Value": value,
            "Numeric Value": parse_number(value),
            "Row Number": 1,
        })

    for sheet_name, df in sheets.items():
        records.extend(
            flatten_dataframe_to_master_records(
                info,
                sheet_name,
                df,
            )
        )

    return records


# =============================================================================
# CSV HEADER
# =============================================================================

def read_csv_header(
    path
):

    path = Path(
        path
    )

    if not path.exists():
        return []

    try:

        with open(
            path,
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as f:

            reader = csv.reader(
                f
            )

            header = next(
                reader,
                None,
            )

            if not header:
                return []

            return [
                str(column)
                for column in header
            ]

    except Exception as exc:

        logger.warning(
            "Could not read master header: %s",
            exc,
        )

        return []


# =============================================================================
# RECORD COLUMNS
# =============================================================================

def get_record_columns(
    records
):

    columns = []

    seen = set()

    for record in records:

        if not isinstance(
            record,
            dict,
        ):
            continue

        for key in record.keys():

            key = str(
                key
            )

            if key not in seen:

                seen.add(
                    key
                )

                columns.append(
                    key
                )

    return columns


# =============================================================================
# MASTER BACKUP
# =============================================================================

def create_master_backup():

    if not MASTER_CSV.exists():
        return None

    timestamp = time.strftime(
        "%Y%m%d_%H%M%S"
    )

    backup = (
        OUTPUT_DIR
        / f"master_datapoints_backup_{timestamp}.csv"
    )

    try:

        shutil.copy2(
            MASTER_CSV,
            backup,
        )

        logger.info(
            "Master backup created: %s",
            backup,
        )

        return backup

    except Exception as exc:

        logger.warning(
            "Could not create master backup: %s",
            exc,
        )

        return None


# =============================================================================
# CSV VALIDATION
# =============================================================================

def validate_csv_width(
    path
):

    path = Path(
        path
    )

    if not path.exists():
        return False

    try:

        with open(
            path,
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as f:

            reader = csv.reader(
                f
            )

            header = next(
                reader,
                None,
            )

            if not header:
                return False

            expected = len(
                header
            )

            for line_number, row in enumerate(
                reader,
                start=2,
            ):

                if len(row) != expected:

                    logger.error(
                        "CSV validation failed at line %s: "
                        "expected %s fields, found %s.",
                        line_number,
                        expected,
                        len(row),
                    )

                    return False

        return True

    except Exception as exc:

        logger.error(
            "CSV validation error: %s",
            exc,
        )

        return False


# =============================================================================
# MASTER CORRUPTION DETECTION
# =============================================================================

def detect_master_corruption():

    if not MASTER_CSV.exists():
        return False

    try:

        with open(
            MASTER_CSV,
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as f:

            reader = csv.reader(
                f
            )

            header = next(
                reader,
                None,
            )

            if not header:
                return True

            expected = len(
                header
            )

            checked = 0

            for row in reader:

                checked += 1

                if len(row) != expected:

                    logger.warning(
                        "Master CSV corruption detected: "
                        "expected %s columns, found %s "
                        "around row %s.",
                        expected,
                        len(row),
                        checked + 1,
                    )

                    return True

                if checked >= 100000:

                    break

        return False

    except Exception as exc:

        logger.warning(
            "Could not validate master CSV: %s",
            exc,
        )

        return True


# =============================================================================
# NORMALIZE MASTER DATAFRAME
# =============================================================================

def normalize_master_dataframe(
    df,
    existing_columns=None,
):

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    df.columns = [
        str(c)
        for c in df.columns
    ]

    # Remove duplicate columns.
    df = df.loc[
        :,
        ~df.columns.duplicated()
    ]

    if existing_columns:

        final_columns = list(
            existing_columns
        )

        for column in df.columns:

            if column not in final_columns:

                final_columns.append(
                    column
                )

        df = df.reindex(
            columns=final_columns
        )

    df = df.fillna(
        ""
    )

    return df


# =============================================================================
# APPEND MASTER SAFELY
# =============================================================================

def append_master(records):
    """Append normalized records using one fixed master schema."""

    if not records:
        return

    master_columns = [
        "Company URL",
        "Company Name",
        "Ticker",
        "Company ID",
        "Warehouse ID",
        "Sheet",
        "Metric",
        "Period",
        "Period Type",
        "Source Column",
        "Value",
        "Numeric Value",
        "Row Number",
    ]

    new_df = pd.DataFrame(records)
    new_df = new_df.reindex(columns=master_columns)
    new_df = new_df.fillna("")

    # Never expand the master schema. Any newly discovered source field is
    # represented by Metric/Period/Source Column/Value instead.
    unexpected = [
        c for c in pd.DataFrame(records).columns
        if c not in master_columns
    ]

    if unexpected:
        logger.warning(
            "Ignoring non-master fields: %s",
            unexpected,
        )

    if not MASTER_CSV.exists():
        tmp = MASTER_CSV.with_suffix(".tmp")
        new_df.to_csv(
            tmp,
            index=False,
            encoding="utf-8-sig",
        )

        if not validate_csv_width(tmp):
            raise RuntimeError("Initial normalized master validation failed.")

        atomic_replace(tmp, MASTER_CSV)
        return

    existing_columns = read_csv_header(MASTER_CSV)

    if existing_columns != master_columns:
        raise RuntimeError(
            "Master CSV has a legacy or unexpected schema. "
            "Run the migration/rebuild step before appending."
        )

    new_df.to_csv(
        MASTER_CSV,
        mode="a",
        header=False,
        index=False,
        encoding="utf-8-sig",
    )

    # Do NOT scan the entire master CSV after every company. That operation
    # becomes progressively slower as the master grows. The complete file is
    # still validated at the end, and periodically during long runs.
    global MASTER_APPEND_COUNT
    MASTER_APPEND_COUNT += 1

    if MASTER_APPEND_COUNT % MASTER_VALIDATE_EVERY == 0:
        if not validate_csv_width(MASTER_CSV):
            raise RuntimeError("Master CSV failed periodic validation.")


# =============================================================================
# COMPANY INFO FROM EXCEL
# =============================================================================

def extract_info_from_excel(
    excel_file
):
    """Read Company_Info safely from a real XLSX workbook.

    Temporary/interrupted files are rejected before pandas/openpyxl is called.
    This prevents the startup crash:
        File is not a zip file
    when Ctrl+C left a .tmp.xlsx file behind.
    """

    excel_file = Path(excel_file)

    if (
        not excel_file.exists()
        or excel_file.name.lower().endswith(".tmp.xlsx")
        or excel_file.name.startswith("~$")
    ):
        return {}

    try:

        df = pd.read_excel(
            excel_file,
            sheet_name="Company_Info",
        )

        if df.empty:
            return {}

        row = df.iloc[
            0
        ].to_dict()

        return {
            str(k): (
                ""
                if pd.isna(v)
                else v
            )
            for k, v in row.items()
        }

    except Exception as exc:

        logger.warning(
            "Could not read Company_Info from %s: %s",
            excel_file,
            exc,
        )

        return {}


# =============================================================================
# REBUILD MASTER FROM EXCEL
# =============================================================================

def flatten_excel_workbook(excel_file):
    """Read an existing company workbook into normalized master records."""

    info = extract_info_from_excel(excel_file)
    if not info:
        return []

    records = []

    try:
        excel = pd.ExcelFile(excel_file)

        for sheet_name in excel.sheet_names:
            try:
                df = pd.read_excel(
                    excel_file,
                    sheet_name=sheet_name,
                )
            except Exception as exc:
                logger.warning(
                    "Could not read sheet %s from %s: %s",
                    sheet_name,
                    excel_file,
                    exc,
                )
                continue

            if df.empty:
                continue

            # Company_Info is a metadata row rather than a normal table.
            if sheet_name == "Company_Info":
                for field, raw_value in df.iloc[0].items():
                    if pd.isna(raw_value):
                        raw_value = ""
                    value = clean_text(raw_value)
                    if not value:
                        continue

                    records.append({
                        "Company URL": info.get("Company URL", ""),
                        "Company Name": info.get("Company Name", ""),
                        "Ticker": info.get("Ticker", ""),
                        "Company ID": info.get("Company ID", ""),
                        "Warehouse ID": info.get("Warehouse ID", ""),
                        "Sheet": "Company_Info",
                        "Metric": clean_text(field),
                        "Period": "",
                        "Period Type": "Metadata",
                        "Source Column": "",
                        "Value": value,
                        "Numeric Value": parse_number(value),
                        "Row Number": 1,
                    })
                continue

            records.extend(
                flatten_dataframe_to_master_records(
                    info,
                    sheet_name,
                    df,
                )
            )

    except Exception as exc:
        logger.warning(
            "Could not rebuild workbook %s: %s",
            excel_file,
            exc,
        )

    return records


def rebuild_master_from_excels():
    """Rebuild the normalized master entirely from saved company workbooks."""

    excel_files = get_company_excel_files()

    if not excel_files:
        logger.info("No existing Excel files to rebuild.")
        return False

    logger.info("")
    logger.info("=" * 80)
    logger.info("REBUILDING NORMALIZED MASTER FROM EXISTING EXCEL FILES")
    logger.info("=" * 80)

    all_records = []

    for index, excel_file in enumerate(excel_files, start=1):
        logger.info(
            "Rebuilding %s/%s: %s",
            index,
            len(excel_files),
            excel_file.name,
        )

        records = flatten_excel_workbook(excel_file)
        logger.info("  Normalized records: %s", len(records))
        all_records.extend(records)

    if not all_records:
        logger.warning("No records reconstructed.")
        return False

    master_columns = [
        "Company URL", "Company Name", "Ticker", "Company ID",
        "Warehouse ID", "Sheet", "Metric", "Period", "Period Type",
        "Source Column", "Value", "Numeric Value", "Row Number",
    ]

    df = pd.DataFrame(all_records).reindex(columns=master_columns).fillna("")

    if MASTER_CSV.exists():
        create_master_backup()

    tmp = MASTER_CSV.with_suffix(".rebuild.tmp")
    df.to_csv(
        tmp,
        index=False,
        encoding="utf-8-sig",
    )

    if not validate_csv_width(tmp):
        raise RuntimeError("Rebuilt normalized master CSV failed validation.")

    atomic_replace(tmp, MASTER_CSV)

    logger.info(
        "Normalized master rebuilt successfully: %s records, %s fixed columns.",
        len(df),
        len(master_columns),
    )

    return True


def master_has_normalized_schema():
    """Check whether the current master uses the new fixed long-form schema."""

    expected = [
        "Company URL", "Company Name", "Ticker", "Company ID",
        "Warehouse ID", "Sheet", "Metric", "Period", "Period Type",
        "Source Column", "Value", "Numeric Value", "Row Number",
    ]

    return read_csv_header(MASTER_CSV) == expected


# =============================================================================
# COMPANY INDEX
# =============================================================================

def rebuild_company_index_from_excels():
    """Rebuild the company index from VALID real XLSX workbooks only.

    This function has an additional defensive filter even though
    get_company_excel_files() already excludes temporary workbooks. This is
    important after Ctrl+C because openpyxl/pandas can leave files such as
    <ticker>_<company>.tmp.xlsx behind while a workbook is being written.
    Those files are not valid XLSX archives and must NEVER be opened here.
    """

    excel_files = [
        Path(p)
        for p in get_company_excel_files()
        if not Path(p).name.lower().endswith(".tmp.xlsx")
        and not Path(p).name.startswith("~$")
    ]

    if not excel_files:
        return

    rows = []

    for excel_file in excel_files:

        info = extract_info_from_excel(
            excel_file
        )

        if not info:
            continue

        rows.append({
            "Company Name": info.get(
                "Company Name",
                "",
            ),
            "Ticker": info.get(
                "Ticker",
                "",
            ),
            "Company ID": info.get(
                "Company ID",
                "",
            ),
            "Warehouse ID": info.get(
                "Warehouse ID",
                "",
            ),
            "Consolidated": info.get(
                "Consolidated",
                "",
            ),
            "Current Price": info.get(
                "Current Price",
                "",
            ),
            "Price Change %": info.get(
                "Price Change %",
                "",
            ),
            "Price Date": info.get(
                "Price Date",
                "",
            ),
            "Company URL": info.get(
                "Company URL",
                "",
            ),
            "Excel File": str(
                excel_file
            ),
        })

    if not rows:
        return

    df = pd.DataFrame(
        rows
    )

    if "Ticker" in df.columns:

        df = df.drop_duplicates(
            subset=["Ticker"],
            keep="last",
        )

    tmp = COMPANY_INDEX_CSV.with_suffix(
        ".tmp"
    )

    df.to_csv(
        tmp,
        index=False,
        encoding="utf-8-sig",
    )

    atomic_replace(
        tmp,
        COMPANY_INDEX_CSV,
    )

    logger.info(
        "Company index rebuilt: %s companies",
        len(df),
    )


def append_company_index(
    info,
    output_file,
):

    row = {
        "Company Name": info.get(
            "Company Name",
            "",
        ),
        "Ticker": info.get(
            "Ticker",
            "",
        ),
        "Company ID": info.get(
            "Company ID",
            "",
        ),
        "Warehouse ID": info.get(
            "Warehouse ID",
            "",
        ),
        "Consolidated": info.get(
            "Consolidated",
            "",
        ),
        "Current Price": info.get(
            "Current Price",
            "",
        ),
        "Price Change %": info.get(
            "Price Change %",
            "",
        ),
        "Price Date": info.get(
            "Price Date",
            "",
        ),
        "Company URL": info.get(
            "Company URL",
            "",
        ),
        "Excel File": str(
            output_file
        ),
    }

    if COMPANY_INDEX_CSV.exists():

        try:

            existing = pd.read_csv(
                COMPANY_INDEX_CSV,
                encoding="utf-8-sig",
            )

        except Exception:

            existing = pd.DataFrame()

    else:

        existing = pd.DataFrame()

    new_row = pd.DataFrame(
        [row]
    )

    combined = pd.concat(
        [
            existing,
            new_row,
        ],
        ignore_index=True,
    )

    if "Ticker" in combined.columns:

        combined = combined.drop_duplicates(
            subset=["Ticker"],
            keep="last",
        )

    tmp = COMPANY_INDEX_CSV.with_suffix(
        ".tmp"
    )

    combined.to_csv(
        tmp,
        index=False,
        encoding="utf-8-sig",
    )

    atomic_replace(
        tmp,
        COMPANY_INDEX_CSV,
    )


# =============================================================================
# FAILED LOG
# =============================================================================

def append_failed(
    url,
    error,
):

    exists = (
        FAILED_CSV.exists()
    )

    pd.DataFrame([
        {
            "Company URL": url,
            "Error": str(error),
            "Timestamp": time.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        }
    ]).to_csv(
        FAILED_CSV,
        mode="a",
        header=not exists,
        index=False,
        encoding="utf-8-sig",
    )


# =============================================================================
# EXISTING WORKBOOK VALIDATION
# =============================================================================

def validate_company_workbook(excel_file, verbose=False):
    """
    Validate an existing company workbook before allowing it to count as
    completed.

    A workbook is valid only when:
      - Company_Info exists and has a real ticker/company name.
      - Each core financial section has at least one populated worksheet:
        Quarterly Results, Profit & Loss, Balance Sheet, Cash Flow, Ratios.
      - If Chart_Metrics exists, every discovered chart has a sheet.
        A sheet may contain an explicit NO_DATA placeholder when Screener
        returned no points for that metric.

    Optional sheets such as Pros_Cons, Peers, Growth_CAGR, Shareholding and
    Documents are NOT required because Screener does not expose them for every
    company.
    """
    excel_file = Path(excel_file)
    reasons = []

    if not excel_file.exists():
        return False, ["File does not exist"]

    wb = None

    try:
        wb = load_workbook(
            excel_file,
            read_only=True,
            data_only=True,
        )

        sheet_names = list(wb.sheetnames)
        sheet_map = {str(x).strip().lower(): x for x in sheet_names}

        if not sheet_names:
            reasons.append("Workbook has no sheets")

        # Company metadata is mandatory.
        if "company_info" not in sheet_map:
            reasons.append("Missing Company_Info")
        else:
            ws = wb[sheet_map["company_info"]]
            rows = list(ws.iter_rows(min_row=1, max_row=3, values_only=True))
            if len(rows) < 2 or not any(
                clean_text(v) for row in rows[1:] for v in row
            ):
                reasons.append("Company_Info has no data row")
            else:
                headers = [clean_text(v) for v in rows[0]]
                values = rows[1]
                info = {
                    headers[i]: values[i] if i < len(values) else ""
                    for i in range(len(headers))
                    if headers[i]
                }
                if not clean_text(info.get("Ticker", "")):
                    reasons.append("Company_Info missing Ticker")
                if not clean_text(info.get("Company Name", "")):
                    reasons.append("Company_Info missing Company Name")

        # Core financial sections. A sheet is populated only if it has at least
        # one data row beyond its header.
        required_prefixes = {
            "Quarterly Results": lambda n: n == "Quarterly Results",
            "Profit & Loss": lambda n: n == "Profit & Loss" or n.startswith("Profit & Loss_"),
            "Balance Sheet": lambda n: n == "Balance Sheet" or n.startswith("Balance Sheet_"),
            "Cash Flow": lambda n: n == "Cash Flow" or n.startswith("Cash Flow_"),
            "Ratios": lambda n: n == "Ratios" or n.startswith("Ratios_"),
        }

        def sheet_has_data(sheet_name):
            ws = wb[sheet_name]
            # Only inspect a small prefix; we only need to know whether real
            # worksheet data exists, not count the whole sheet.
            seen = 0
            for row in ws.iter_rows(min_row=2, max_row=5, values_only=True):
                if any(clean_text(v) for v in row):
                    return True
                seen += 1
            return False

        for label, matcher in required_prefixes.items():
            matches = [n for n in sheet_names if matcher(n)]
            populated = [n for n in matches if sheet_has_data(n)]
            if not populated:
                if not matches:
                    reasons.append(f"Missing {label} sheet")
                else:
                    reasons.append(f"{label} sheet(s) empty")

        # Chart metrics are expected when REQUIRE_CHARTS is enabled. Validate
        # every chart advertised by Chart_Metrics.
        if REQUIRE_CHARTS and "chart_metrics" not in sheet_map:
            reasons.append("Missing Chart_Metrics")
        if "chart_metrics" in sheet_map:
            metrics_ws = wb[sheet_map["chart_metrics"]]
            metric_headers = [clean_text(v) for v in next(
                metrics_ws.iter_rows(min_row=1, max_row=1, values_only=True),
                (),
            )]
            rows = list(metrics_ws.iter_rows(min_row=2, values_only=True))
            chart_names = []
            if "Chart" in metric_headers:
                idx = metric_headers.index("Chart")
                for row in rows:
                    if idx < len(row) and clean_text(row[idx]):
                        chart_names.append(clean_text(row[idx]))

            for chart_name in sorted(set(chart_names)):
                expected = safe_sheet_name("Chart_" + safe_filename(chart_name))
                if expected not in sheet_names:
                    # Handle Excel suffixes created by duplicate chart names.
                    alternatives = [
                        n for n in sheet_names
                        if n.startswith(expected)
                    ]
                    if not alternatives:
                        reasons.append(f"Missing chart sheet: {expected}")
                    elif not any(sheet_has_data(n) for n in alternatives):
                        reasons.append(f"Chart sheet empty: {expected}")
                elif not sheet_has_data(expected):
                    reasons.append(f"Chart sheet empty: {expected}")

    except Exception as exc:
        reasons.append(f"Workbook read error: {exc}")

    finally:
        if wb is not None:
            try:
                wb.close()
            except Exception:
                pass

    valid = not reasons

    if verbose:
        if valid:
            logger.info("VALID workbook: %s", excel_file.name)
        else:
            logger.warning(
                "INCOMPLETE workbook: %s | %s",
                excel_file.name,
                "; ".join(reasons),
            )

    return valid, reasons


def validate_existing_workbooks():
    """Validate all existing company Excel files and return valid URLs."""
    valid_urls = set()
    invalid_rows = []
    files = get_company_excel_files()

    logger.info("")
    logger.info("=" * 80)
    logger.info("VALIDATING EXISTING COMPANY EXCEL FILES")
    logger.info("=" * 80)
    logger.info("Existing Excel files: %s", len(files))

    for index, excel_file in enumerate(files, start=1):
        valid, reasons = validate_company_workbook(excel_file)
        info = extract_info_from_excel(excel_file)
        url = clean_text(info.get("Company URL", ""))
        ticker = clean_text(info.get("Ticker", ""))

        if valid and url:
            valid_urls.add(normalize_company_url(url))
        else:
            invalid_rows.append({
                "Excel File": str(excel_file),
                "Ticker": ticker,
                "Company URL": url,
                "Status": "INCOMPLETE",
                "Reason": "; ".join(reasons) if reasons else "Unknown",
            })

        if index % 100 == 0 or index == len(files):
            logger.info(
                "Workbook validation: %s/%s | valid=%s | incomplete=%s",
                index,
                len(files),
                len(valid_urls),
                len(invalid_rows),
            )

    report = OUTPUT_DIR / "workbook_validation_report.csv"
    if invalid_rows:
        pd.DataFrame(invalid_rows).to_csv(
            report,
            index=False,
            encoding="utf-8-sig",
        )
    elif report.exists():
        report.unlink()

    logger.info(
        "Workbook validation complete | VALID=%s | INCOMPLETE=%s",
        len(valid_urls),
        len(invalid_rows),
    )
    logger.info("Validation report: %s", report)

    return valid_urls, invalid_rows


# =============================================================================
# EXISTING COMPANY DETECTION
# =============================================================================

def get_existing_tickers():

    tickers = set()

    for excel_file in get_company_excel_files():

        info = extract_info_from_excel(
            excel_file
        )

        ticker = clean_text(
            info.get(
                "Ticker",
                "",
            )
        )

        if ticker:

            tickers.add(
                ticker.upper()
            )

    return tickers


def get_existing_company_urls():

    urls = set()

    for excel_file in get_company_excel_files():

        info = extract_info_from_excel(
            excel_file
        )

        url = clean_text(
            info.get(
                "Company URL",
                "",
            )
        )

        if url:

            urls.add(
                normalize_company_url(
                    url
                )
            )

    return urls


# =============================================================================
# FINAL VALIDATION
# =============================================================================

def final_validation():
    logger.info("")
    logger.info("=" * 80)
    logger.info("FINAL VALIDATION")
    logger.info("=" * 80)

    if MASTER_CSV.exists():
        try:
            valid_width = validate_csv_width(MASTER_CSV)
            normalized = master_has_normalized_schema()

            print(
                "✓ Master CSV validation PASSED"
                if valid_width
                else "✗ Master CSV validation FAILED"
            )

            columns = read_csv_header(MASTER_CSV)
            print(f"Master columns: {len(columns)}")
            print(f"Master schema: {'NORMALIZED/LONG' if normalized else 'LEGACY/UNEXPECTED'}")

            if normalized:
                print("Fixed columns:")
                print("  " + ", ".join(columns))

            try:
                file_size = MASTER_CSV.stat().st_size
                print(
                    f"Master size: {file_size / (1024 * 1024):.2f} MB"
                )
            except Exception:
                pass

        except Exception as exc:
            print(f"✗ Validation error: {exc}")
    else:
        print("⚠ Master CSV does not exist.")

    excel_count = len(get_company_excel_files())
    print(f"Excel company files: {excel_count}")


# =============================================================================
# MAIN
# =============================================================================

def scrape_one_worker(company_url):
    """Scrape one company in a worker thread. No shared CSV/checkpoint writes here."""
    try:
        info, sheets, output_file = scrape_company(company_url)

        ticker = clean_text(info.get("Ticker", ""))
        if not ticker:
            raise RuntimeError("Company page returned no ticker.")

        records = flatten_company(info, sheets)
        if not records:
            raise RuntimeError("No records generated for company.")

        return {
            "ok": True,
            "url": company_url,
            "info": info,
            "sheets": sheets,
            "output_file": output_file,
            "records": records,
        }
    except Exception as exc:
        return {
            "ok": False,
            "url": company_url,
            "error": exc,
        }


def main():

    print("=" * 80)

    print(
        "SCREENER ALL-STOCK DATA COLLECTOR"
    )

    print("=" * 80)

    print(
        f"MAX_COMPANIES: {MAX_COMPANIES}"
    )

    if MAX_COMPANIES == 0:

        print(
            "MODE: ALL REMAINING COMPANIES"
        )

    else:

        print(
            f"MODE: FIRST {MAX_COMPANIES} "
            "REMAINING COMPANIES"
        )

    print(
        f"MAX_INDUSTRY_PAGES: "
        f"{MAX_INDUSTRY_PAGES}"
    )

    print(
        f"CHART_DAYS: "
        f"{CHART_DAYS}"
    )

    print(
        f"Consolidated: "
        f"{USE_CONSOLIDATED}"
    )

    print(
        f"Output: "
        f"{OUTPUT_DIR}"
    )

    # Clean up a workbook that may have been left half-written by Ctrl+C,
    # a crash, or an interrupted process. This is intentionally limited to
    # *.tmp.xlsx so completed company workbooks are never touched.
    cleanup_interrupted_excel_temps()

    # =========================================================================
    # STEP 0
    # MASTER CSV REPAIR
    # =========================================================================

    print()
    print("=" * 80)
    print(
        "STEP 0 - CHECKING EXISTING MASTER CSV"
    )
    print("=" * 80)

    existing_excel_files = get_company_excel_files()

    if MASTER_CSV.exists():

        corrupted = detect_master_corruption()
        normalized = master_has_normalized_schema()

        if corrupted or not normalized:
            print()
            if not normalized:
                print(
                    "⚠ Existing master uses the old wide/chaotic schema."
                )
            if corrupted:
                print(
                    "⚠ Existing master CSV also has inconsistent row widths."
                )

            if existing_excel_files:
                print(
                    f"Creating backup and rebuilding normalized master "
                    f"from {len(existing_excel_files)} existing Excel files..."
                )
                rebuild_master_from_excels()
            else:
                raise RuntimeError(
                    "Master needs migration, but no company Excel files "
                    "are available for a safe rebuild."
                )
        else:
            print(
                "✓ Existing master is already normalized and structurally valid."
            )

    else:
        print("No existing master CSV found.")

        if existing_excel_files:
            print(
                f"Found {len(existing_excel_files)} existing Excel files. "
                "Building normalized master..."
            )
            rebuild_master_from_excels()

    # =========================================================================
    # STEP 1
    # COMPANY INDEX
    # =========================================================================

    print()
    print("=" * 80)
    print(
        "STEP 1 - REBUILDING COMPANY INDEX"
    )
    print("=" * 80)

    rebuild_company_index_from_excels()

    # =========================================================================
    # STEP 2
    # CHECKPOINT
    # =========================================================================

    print()
    print("=" * 80)
    print(
        "STEP 2 - LOADING CHECKPOINT"
    )
    print("=" * 80)

    checkpoint = load_checkpoint()

    completed = set(
        checkpoint.get(
            "completed",
            [],
        )
    )

    print(
        f"Completed companies in checkpoint: "
        f"{len(completed)}"
    )

    # =========================================================================
    # ALSO CONSIDER EXISTING EXCEL FILES
    # =========================================================================

    if VALIDATE_EXISTING_WORKBOOKS:
        existing_excel_urls, incomplete_workbooks = validate_existing_workbooks()
    else:
        existing_excel_urls = get_existing_company_urls()
        incomplete_workbooks = []

    # IMPORTANT: an Excel file is NOT considered completed merely because it
    # exists. Remove incomplete workbook URLs from the checkpoint so they are
    # scraped again and repaired.
    incomplete_urls = set()
    for row in incomplete_workbooks:
        if row.get("Company URL"):
            incomplete_urls.add(
                normalize_company_url(row["Company URL"])
            )

    if incomplete_urls:
        completed.difference_update(incomplete_urls)
        checkpoint["completed"] = sorted(completed)
        save_checkpoint(checkpoint)
        print(
            f"Incomplete existing workbooks forced into retry queue: "
            f"{len(incomplete_urls)}"
        )

    if existing_excel_urls:
        before = len(completed)
        completed.update(existing_excel_urls)

        if len(completed) > before:
            print(
                f"Added {len(completed) - before} validated companies "
                "from existing Excel files."
            )
            checkpoint["completed"] = sorted(completed)
            save_checkpoint(checkpoint)

    # -------------------------------------------------------------------------
    # FAILED COMPANIES MUST ALWAYS BE RETRIED.
    # An older run may have produced an Excel file before failing later in
    # the pipeline. Do not let that partial file mark the company complete.
    # -------------------------------------------------------------------------

    failed_urls = {
        normalize_company_url(item.get("url", ""))
        for item in checkpoint.get("failed", [])
        if isinstance(item, dict) and item.get("url")
    }

    if failed_urls:

        completed.difference_update(failed_urls)

        checkpoint[
            "completed"
        ] = sorted(
            completed
        )

        save_checkpoint(
            checkpoint
        )

        print(
            f"Failed companies forced back into retry queue: {len(failed_urls)}"
        )

    # =========================================================================
    # STEP 3 / 4
    # DISCOVERY
    # =========================================================================
    #
    # IMPORTANT: Once discovered_companies.json exists, reuse it. The old
    # version of the script rediscovered all market/industry pages on every
    # execution, which caused hundreds of unnecessary HTTP requests.
    #
    # Set REFRESH_DISCOVERY = True at the top when you deliberately want to
    # rebuild the Screener universe.
    # =========================================================================

    cached_company_urls = []

    if not REFRESH_DISCOVERY:
        cached_company_urls = load_discovery_cache()

    if cached_company_urls:

        print()
        print("=" * 80)
        print("STEP 3 / 4 - USING DISCOVERY CACHE")
        print("=" * 80)
        print(
            f"✓ Loaded {len(cached_company_urls)} company URLs from "
            f"{DISCOVERY_CACHE_FILE}"
        )
        print(
            "✓ Market/industry discovery skipped."
        )

        industry_urls = []
        company_urls = cached_company_urls

    else:

        print()
        print("=" * 80)
        print("STEP 3 - DISCOVERING INDUSTRIES")
        print("=" * 80)

        industry_urls = discover_industry_pages()

        if not industry_urls:
            raise RuntimeError(
                "No industry pages discovered."
            )

        print()
        print("=" * 80)
        print("STEP 4 - DISCOVERING COMPANIES")
        print("=" * 80)

        company_urls = discover_companies(
            industry_urls
        )

        if not company_urls:
            raise RuntimeError(
                "No company URLs discovered."
            )

        # Save discovery result only after a successful full discovery.
        save_discovery_cache(company_urls)

        print()
        print("=" * 80)
        print("DISCOVERY COMPLETE")
        print("=" * 80)

        print(
            f"Industry/category URLs: {len(industry_urls)}"
        )

        print(
            f"Total unique company URLs: {len(company_urls)}"
        )

    # =========================================================================
    # STEP 5
    # FILTER COMPLETED
    # =========================================================================

    completed_normalized = {
        normalize_company_url(x)
        for x in completed
    }

    remaining = [
        url
        for url in company_urls
        if normalize_company_url(url)
        not in completed_normalized
    ]

    print(
        f"Already completed: "
        f"{len(completed)}"
    )

    print(
        f"Remaining: "
        f"{len(remaining)}"
    )

    # =========================================================================
    # LIMIT
    # =========================================================================

    if (
        MAX_COMPANIES
        and MAX_COMPANIES > 0
    ):

        remaining = remaining[
            :MAX_COMPANIES
        ]

    print(
        f"Will process now: "
        f"{len(remaining)}"
    )

    if not remaining:

        print()
        print(
            "✓ No remaining companies to process."
        )

        final_validation()

        print()
        print(
            "Done."
        )

        return

    # =========================================================================
    # STEP 6
    # SCRAPE
    # =========================================================================

    print()
    print("=" * 80)
    print(
        "STEP 6 - SCRAPING COMPANIES"
    )
    print("=" * 80)

    total_to_process = len(
        remaining
    )

    successful = 0

    failed = 0

    company_position_map = {
        normalize_company_url(url): position
        for position, url in enumerate(company_urls, start=1)
    }

    # -------------------------------------------------------------------------
    # PARALLEL SCRAPING
    # -------------------------------------------------------------------------
    # Network-heavy work is done by WORKERS threads. All writes to the shared
    # master CSV, company index and checkpoint stay in the main thread so
    # multiple workers can never corrupt those files.

    worker_count = max(1, int(WORKERS))
    logger.info("Parallel workers: %s", worker_count)

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="scraper") as executor:
        futures = {
            executor.submit(scrape_one_worker, company_url): (index, company_url)
            for index, company_url in enumerate(remaining, start=1)
        }

        completed_results = {}

        for future in as_completed(futures):
            index, company_url = futures[future]
            completed_results[index] = (company_url, future)

            # Process completed result immediately; files/CSV/checkpoint are
            # written by only this main thread.
            try:
                result = future.result()
            except Exception as exc:
                result = {"ok": False, "url": company_url, "error": exc}

            global_position = company_position_map.get(
                normalize_company_url(company_url),
                index,
            )

            print()
            print("#" * 80)
            print(
                f"COMPANY {global_position}/{len(company_urls)} "
                f"(remaining queue {index}/{total_to_process})"
            )
            print(f"SUCCESS: {successful} | FAILED: {failed}")
            print(company_url)
            print("#" * 80)

            if not result.get("ok"):
                failed += 1
                exc = result.get("error", RuntimeError("Unknown worker error"))
                logger.exception("Company failed", exc_info=(type(exc), exc, exc.__traceback__))
                append_failed(company_url, exc)
                mark_failed(checkpoint, company_url, exc)
                print("✗ FAILED")
                print(company_url)
                print(f"Reason: {exc}")
                continue

            info = result["info"]
            sheets = result["sheets"]
            output_file = result["output_file"]
            records = result["records"]

            logger.info("Master records generated: %s", len(records))

            # Shared-file operations are intentionally serialized here.
            append_master(records)
            append_company_index(info, output_file)
            mark_completed(checkpoint, company_url)
            completed.add(company_url)

            successful += 1

            print("✓ SUCCESS")
            print(f"Ticker: {info.get('Ticker')}")
            print(f"Company: {info.get('Company Name')}")
            print(f"Company ID: {info.get('Company ID')}")
            print(f"Warehouse ID: {info.get('Warehouse ID')}")
            print(f"Sheets: {len(sheets)}")
            print(f"Records: {len(records)}")
            print(f"Excel: {output_file}")

    # =========================================================================
    # STEP 7
    # FINAL VALIDATION
    # =========================================================================

    final_validation()

    # =========================================================================
    # FINAL SUMMARY
    # =========================================================================

    print()
    print("=" * 80)
    print(
        "SCRAPING COMPLETE"
    )
    print("=" * 80)

    print(
        f"Successful this run: "
        f"{successful}"
    )

    print(
        f"Failed this run: "
        f"{failed}"
    )

    print(
        f"Total completed in checkpoint: "
        f"{len(completed)}"
    )

    print()
    print(
        f"Companies: "
        f"{COMPANY_DIR}"
    )

    print(
        f"Master: "
        f"{MASTER_CSV}"
    )

    print(
        f"Index: "
        f"{COMPANY_INDEX_CSV}"
    )

    print(
        f"Failed: "
        f"{FAILED_CSV}"
    )

    print(
        f"Checkpoint: "
        f"{CHECKPOINT_FILE}"
    )

    print(
        f"Discovery cache: "
        f"{DISCOVERY_CACHE_FILE}"
    )

    print()
    print(
        "Done."
    )


# =============================================================================
# RUN
# =============================================================================

if __name__ == "__main__":

    main()