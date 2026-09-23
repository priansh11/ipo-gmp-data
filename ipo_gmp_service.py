"""
IPO GMP scraper service.

Exposes the grey-market-premium data from ipowatch.in as JSON so the
IpoMaster frontend can consume it (the site sends no CORS headers, so the
browser cannot fetch it directly).

Run:
    pip install fastapi uvicorn requests beautifulsoup4 lxml
    uvicorn ipo_gmp_service:app --host 0.0.0.0 --port 8081

Endpoints:
    GET /api/ipo/gmp            -> all GMP rows (mainboard + SME)
    GET /api/ipo/gmp?status=closed  -> only rows with that status
    GET /api/ipo/gmp/health     -> health + cache info
"""

import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

# FastAPI is only needed for the HTTP service (--serve). The --write mode works
# with just requests + beautifulsoup4, so make these imports optional.
try:
    from fastapi import FastAPI, Query
    from fastapi.middleware.cors import CORSMiddleware
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ipo_gmp")

GMP_URL = "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/"
SCREENER_URL = "https://www.screener.in/ipo/recent/"

# Be a polite scraper: identify, and don't hammer the source.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

CACHE_TTL_SECONDS = 300      # serve from cache for 5 minutes
REQUEST_TIMEOUT = 15

# Build the web app only when FastAPI is available.
if _HAS_FASTAPI:
    app = FastAPI(title="IPO GMP Service")
    # Allow the trading UI to call this service from the browser.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],     # tighten to your UI origin(s) in production
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )
else:
    app = None


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------

def to_number(text: Optional[str]) -> Optional[float]:
    """Pull a number out of a cell like '₹1,785', '+55', '₹-' or '(13.58%)'."""
    if not text:
        return None
    cleaned = re.sub(r"[₹,\s]", "", str(text))
    # keep digits, sign and decimal point only
    cleaned = re.sub(r"[^0-9.\-+]", "", cleaned)
    if cleaned in ("", "-", "+", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_est_listing(text: Optional[str]):
    """'₹460 (13.58%)' -> (460.0, 13.58)."""
    if not text:
        return None, None
    price = None
    pct = None

    pct_match = re.search(r"\(([-+]?[\d.]+)\s*%\)", text)
    if pct_match:
        pct = float(pct_match.group(1))

    # strip the percentage part before reading the price
    price_part = re.sub(r"\(.*?\)", "", text)
    price = to_number(price_part)
    return price, pct


def clean_text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(strip=True)) if node else ""


def normalize_name(name: Optional[str]) -> str:
    """Normalize a company/IPO name so the two data sources can be matched."""
    if not name:
        return ""
    up = name.upper()
    up = re.sub(r"\b(LIMITED|LTD|IPO|PRIVATE|PVT|INDIA|THE)\b", "", up)
    return re.sub(r"[^A-Z0-9]", "", up)


def parse_status(text: str) -> str:
    """Status cells carry padding characters; reduce to a clean token."""
    t = re.sub(r"[=\s]", "", text or "").upper()
    if "UPCOMING" in t:
        return "UPCOMING"
    if "CLOSE" in t:
        return "CLOSED"
    if "OPEN" in t:
        return "OPEN"
    return t or "UNKNOWN"


def parse_trend(text: str) -> str:
    if "🟢" in text:
        return "UP"
    if "🔴" in text:
        return "DOWN"
    if "🟡" in text:
        return "FLAT"
    return "UNKNOWN"


# --------------------------------------------------------------------------
# scraping
# --------------------------------------------------------------------------

def _header_index(headers: List[str], *keywords) -> Optional[int]:
    """Find the column index whose header contains any of the keywords."""
    for i, h in enumerate(headers):
        hl = h.lower()
        if any(k in hl for k in keywords):
            return i
    return None


def parse_gmp_table(table, category: str) -> List[Dict]:
    """Parse one GMP table into row dicts, driven by its header names."""
    rows: List[Dict] = []

    trs = table.find_all("tr")
    if not trs:
        return rows

    headers = [clean_text(th) for th in trs[0].find_all(["th", "td"])]
    if not headers:
        return rows

    idx_name = _header_index(headers, "ipo name", "name") or 0
    idx_gmp = _header_index(headers, "gmp")
    idx_trend = _header_index(headers, "trend")
    idx_band = _header_index(headers, "price band", "ipo price", "price")
    idx_est = _header_index(headers, "est. listing", "est listing", "listing")
    idx_date = _header_index(headers, "date")
    idx_status = _header_index(headers, "status")
    idx_updated = _header_index(headers, "last updated", "updated")

    for tr in trs[1:]:
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue

        cells = [clean_text(td) for td in tds]

        def cell(i):
            return cells[i] if i is not None and i < len(cells) else None

        ipo_name = cell(idx_name)
        if not ipo_name:
            continue

        gmp = to_number(cell(idx_gmp))
        issue_price = to_number(cell(idx_band))
        est_price, est_gain_pct = parse_est_listing(cell(idx_est))

        # Fall back to issue + gmp if the site didn't give an estimate
        if est_price is None and issue_price is not None and gmp is not None:
            est_price = issue_price + gmp

        # Detail page link, useful for drill-down
        link = None
        a = tds[idx_name].find("a") if idx_name < len(tds) else None
        if a and a.get("href"):
            link = a["href"]

        rows.append(
            {
                "ipoName": ipo_name,
                "normalizedName": normalize_name(ipo_name),
                "category": category,                       # MAINBOARD | SME
                "gmp": gmp,
                "trend": parse_trend(cell(idx_trend) or ""),
                "issuePrice": issue_price,
                "expectedPrice": est_price,
                "expectedGainPct": est_gain_pct,
                "ipoDate": cell(idx_date),
                "status": parse_status(cell(idx_status) or ""),
                "lastUpdated": cell(idx_updated),
                "url": link,
            }
        )

    return rows


def categorize_table(table) -> Optional[str]:
    """
    Work out which table we're looking at from the nearest preceding heading.
    Returns MAINBOARD / SME, or None for tables we don't want (e.g. the
    historical performance table).
    """
    heading = table.find_previous(["h2", "h3", "h4"])
    title = clean_text(heading).upper() if heading else ""

    if "PERFORMANCE" in title:
        return None                      # historical table - skip
    if "SME" in title:
        return "SME"
    if "MAINBOARD" in title or "MAIN BOARD" in title:
        return "MAINBOARD"
    return None


def scrape_gmp() -> List[Dict]:
    """Fetch and parse the GMP page."""
    log.info("Fetching GMP page")
    resp = requests.get(GMP_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    all_rows: List[Dict] = []

    for table in soup.find_all("table"):
        category = categorize_table(table)
        if not category:
            continue
        all_rows.extend(parse_gmp_table(table, category))

    log.info("Parsed %d GMP rows", len(all_rows))
    return all_rows


# --------------------------------------------------------------------------
# screener.in - listing date, IPO market cap (issue size), IPO price
# --------------------------------------------------------------------------

def parse_listing_date(text: Optional[str]):
    """
    Screener shows 'today' / 'tomorrow' / 'yesterday' / '24 Sep 2026'.
    Returns (display_text, iso_date_or_None, is_today_bool).
    """
    if not text:
        return None, None, False

    raw = text.strip()
    low = raw.lower()
    today = datetime.now().date()

    if low == "today":
        return raw, today.isoformat(), True
    if low == "tomorrow":
        return raw, (today + timedelta(days=1)).isoformat(), False
    if low == "yesterday":
        return raw, (today - timedelta(days=1)).isoformat(), False

    # e.g. "24 Sep 2026"
    for fmt in ("%d %b %Y", "%d %B %Y", "%d-%b-%Y"):
        try:
            d = datetime.strptime(raw, fmt).date()
            return raw, d.isoformat(), d == today
        except ValueError:
            continue

    return raw, None, False


def scrape_screener() -> List[Dict]:
    """
    Fetch the screener.in recent-IPO table: listing date, IPO market cap
    (issue size in Rs Cr), IPO price, current price and % change.
    """
    log.info("Fetching screener recent IPOs")
    resp = requests.get(SCREENER_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    rows: List[Dict] = []

    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if not trs:
            continue

        headers = [clean_text(th) for th in trs[0].find_all(["th", "td"])]
        if not headers:
            continue

        idx_name = _header_index(headers, "name")
        idx_date = _header_index(headers, "listing date", "list date")
        idx_mcap = _header_index(headers, "ipo mcap", "mcap")
        idx_price = _header_index(headers, "ipo price")
        idx_curr = _header_index(headers, "current price")
        idx_chg = _header_index(headers, "% change", "change")

        # Only the recent-IPO table has a listing date + mcap
        if idx_name is None or idx_date is None:
            continue

        for tr in trs[1:]:
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            cells = [clean_text(td) for td in tds]

            def cell(i):
                return cells[i] if i is not None and i < len(cells) else None

            name = cell(idx_name)
            if not name:
                continue

            date_text, date_iso, is_today = parse_listing_date(cell(idx_date))

            # "⇣ 3%" / "⇡ 45%" -> -3.0 / 45.0
            chg_raw = cell(idx_chg) or ""
            change_pct = to_number(chg_raw)
            if change_pct is not None and ("⇣" in chg_raw or "-" in chg_raw):
                change_pct = -abs(change_pct)

            link = None
            a = tds[idx_name].find("a") if idx_name < len(tds) else None
            if a and a.get("href"):
                href = a["href"]
                link = href if href.startswith("http") else "https://www.screener.in" + href

            rows.append(
                {
                    "name": name,
                    "normalizedName": normalize_name(name),
                    "listingDate": date_text,
                    "listingDateIso": date_iso,
                    "listedToday": is_today,
                    "ipoMarketCapCr": to_number(cell(idx_mcap)),   # issue size, Rs Cr
                    "ipoPrice": to_number(cell(idx_price)),
                    "currentPrice": to_number(cell(idx_curr)),
                    "changePct": change_pct,
                    "screenerUrl": link,
                }
            )

        if rows:
            break   # first matching table is the one we want

    log.info("Parsed %d screener rows", len(rows))
    return rows


def merge_sources(gmp_rows: List[Dict], screener_rows: List[Dict]) -> List[Dict]:
    """
    Enrich GMP rows with screener data (listing date, IPO market cap).
    Matching is by normalized company name, with a contains-fallback.
    """
    by_name = {r["normalizedName"]: r for r in screener_rows if r.get("normalizedName")}

    for row in gmp_rows:
        key = row.get("normalizedName") or ""
        match = by_name.get(key)

        # contains-match fallback (names differ slightly between sources)
        if not match and key:
            for sk, sv in by_name.items():
                if sk and (sk in key or key in sk):
                    match = sv
                    break

        if match:
            row["listingDate"] = match.get("listingDate")
            row["listingDateIso"] = match.get("listingDateIso")
            row["listedToday"] = match.get("listedToday", False)
            row["ipoMarketCapCr"] = match.get("ipoMarketCapCr")
            row["currentPrice"] = match.get("currentPrice")
            row["changePct"] = match.get("changePct")
            row["screenerUrl"] = match.get("screenerUrl")
            # prefer screener's IPO price when ipowatch didn't have one
            if row.get("issuePrice") is None:
                row["issuePrice"] = match.get("ipoPrice")
        else:
            row.setdefault("listingDate", None)
            row.setdefault("listingDateIso", None)
            row.setdefault("listedToday", False)
            row.setdefault("ipoMarketCapCr", None)
            row.setdefault("currentPrice", None)
            row.setdefault("changePct", None)
            row.setdefault("screenerUrl", None)

    return gmp_rows


def scrape_all() -> List[Dict]:
    """Scrape both sources and merge. Screener failure is non-fatal."""
    gmp_rows = scrape_gmp()

    try:
        screener_rows = scrape_screener()
    except Exception as exc:                        # noqa: BLE001
        log.warning("Screener fetch failed (%s) - continuing with GMP only", exc)
        screener_rows = []

    return merge_sources(gmp_rows, screener_rows)


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

class _Cache:
    def __init__(self):
        self.rows: List[Dict] = []
        self.fetched_at: Optional[float] = None
        self.error: Optional[str] = None
        self.lock = threading.Lock()

    def is_fresh(self) -> bool:
        return (
            self.fetched_at is not None
            and (time.time() - self.fetched_at) < CACHE_TTL_SECONDS
            and bool(self.rows)
        )


_cache = _Cache()


def get_rows(force: bool = False) -> List[Dict]:
    """Return cached rows, refreshing if stale. Serves stale data on error."""
    with _cache.lock:
        if not force and _cache.is_fresh():
            return _cache.rows

        try:
            rows = scrape_all()
            if rows:
                _cache.rows = rows
                _cache.fetched_at = time.time()
                _cache.error = None
            else:
                _cache.error = "Parsed zero rows - page layout may have changed"
                log.warning(_cache.error)
        except Exception as exc:                      # noqa: BLE001
            _cache.error = f"{type(exc).__name__}: {exc}"
            log.exception("GMP fetch failed")
            # keep serving the last good data rather than failing outright

        return _cache.rows


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def gmp(
    status: Optional[str] = None,
    category: Optional[str] = None,
    listed_today: bool = False,
    force: bool = False,
):
    rows = get_rows(force=force)

    if status:
        want = status.strip().upper()
        rows = [r for r in rows if r["status"] == want]
    if category:
        want_cat = category.strip().upper()
        rows = [r for r in rows if r["category"] == want_cat]
    if listed_today:
        rows = [r for r in rows if r.get("listedToday")]

    return {
        "rows": rows,
        "count": len(rows),
        "fetchedAt": (
            datetime.fromtimestamp(_cache.fetched_at).isoformat()
            if _cache.fetched_at
            else None
        ),
        "stale": not _cache.is_fresh(),
        "error": _cache.error,
        "source": GMP_URL,
    }


def health():
    return {
        "ok": bool(_cache.rows),
        "rowCount": len(_cache.rows),
        "fetchedAt": (
            datetime.fromtimestamp(_cache.fetched_at).isoformat()
            if _cache.fetched_at
            else None
        ),
        "cacheTtlSeconds": CACHE_TTL_SECONDS,
        "error": _cache.error,
    }


# Register HTTP routes only when FastAPI is present.
if _HAS_FASTAPI:
    @app.get("/api/ipo/gmp")
    def _gmp_route(
        status: Optional[str] = Query(None, description="OPEN | CLOSED | UPCOMING"),
        category: Optional[str] = Query(None, description="MAINBOARD | SME"),
        listed_today: bool = Query(False, description="Only IPOs listing today"),
        force: bool = Query(False, description="Bypass the cache"),
    ):
        return gmp(status=status, category=category,
                   listed_today=listed_today, force=force)

    @app.get("/api/ipo/gmp/health")
    def _health_route():
        return health()


def write_json_file(path: str) -> int:
    """Scrape and write the GMP data to a JSON file. Returns row count."""
    import json

    rows = get_rows(force=True)
    payload = {
        "rows": rows,
        "count": len(rows),
        "fetchedAt": (
            datetime.fromtimestamp(_cache.fetched_at).isoformat()
            if _cache.fetched_at else None
        ),
        "error": _cache.error,
        "source": GMP_URL,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    log.info("Wrote %d rows to %s", len(rows), path)
    return len(rows)


if __name__ == "__main__":
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="IPO GMP scraper")
    parser.add_argument(
        "--write",
        metavar="PATH",
        help="Scrape once and write the JSON to PATH (e.g. ipo_gmp.json), then exit.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run the HTTP service (needs fastapi + uvicorn).",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()

    if args.write:
        count = write_json_file(args.write)
        if count == 0:
            print("WARNING: 0 rows parsed - page layout may have changed", file=sys.stderr)
            sys.exit(1)
        print(f"Wrote {count} rows to {args.write}")
        sys.exit(0)

    if args.serve:
        import uvicorn
        uvicorn.run(app, host=args.host, port=args.port)
        sys.exit(0)

    # Default: quick manual check
    data = get_rows(force=True)
    print(json.dumps(data[:5], indent=2, ensure_ascii=False))
    print(f"\nTotal rows: {len(data)}")
