"""
Stock Sub Dashboard - Flask web service.

Calls free public APIs (ApeWisdom for ticker rankings, Arctic Shift for post
content, Yahoo Finance for prices) directly at request time. Caches results
in memory for CACHE_TTL seconds to avoid hammering the free APIs.

No database, no disk, no cron — just a single Flask app that fits on Render's
free tier (no persistent disk required, so deploys/restarts don't lose state).

Run:
  python dashboard.py            # dev server on :5000
  gunicorn dashboard:app         # production
"""

import os
import threading
import time
import urllib.request
import urllib.parse
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any

import requests
from flask import Flask, jsonify, render_template_string, request

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))  # 1 hour for ticker data
PRICE_CACHE_TTL = int(os.getenv("PRICE_CACHE_TTL", "300"))  # 5 min for prices
MACRO_CACHE_TTL = int(os.getenv("MACRO_CACHE_TTL", "900"))  # 15 min for macro
EARNINGS_CACHE_TTL = int(os.getenv("EARNINGS_CACHE_TTL", "21600"))  # 6 hours (rarely changes)
APEWISDOM_TIMEOUT = int(os.getenv("APEWISDOM_TIMEOUT", "20"))
ARCTIC_TIMEOUT = int(os.getenv("ARCTIC_TIMEOUT", "30"))
YAHOO_TIMEOUT = int(os.getenv("YAHOO_TIMEOUT", "10"))
POSTS_PER_SUB = int(os.getenv("POSTS_PER_SUB", "300"))  # max from Arctic Shift per call
POST_LOOKBACK_DAYS = int(os.getenv("POST_LOOKBACK_DAYS", "7"))
COMMENTS_PER_SUB = int(os.getenv("COMMENTS_PER_SUB", "200"))  # from Arctic Shift
TOP_POSTS_PER_TICKER = int(os.getenv("TOP_POSTS_PER_TICKER", "3"))
POST_BODY_EXTRACT_CHARS = int(os.getenv("POST_BODY_EXTRACT_CHARS", "5000"))  # was 2000

# Subreddits we track. ApeWisdom covers all of these with rankings.
# Arctic Shift has good coverage for all except r/wallstreetbets (sparse).
# Note: we only include subs that are actively posted to (verified via API
# freshness check). Dead subs like r/StockMarketDiscussion (last post 217d ago)
# were removed.
DEFAULT_SUBS = [
    # Mega-high-volume
    "wallstreetbets",
    "wallstreetbetsnew",
    "stocks",
    "investing",
    "options",
    "StockMarket",
    "pennystocks",
    "SPACs",
    "Superstonk",
    # Niche / company-specific
    "SNDK",
    "MSTR",
    "amcstock",
    "nvidia",
    "BBBY",
    # General stock discussion - added for coverage
    "smallstreetbets",
    "StocksAndTrading",
    "investing_discussion",
    "ValueInvesting",
    "Daytrading",
    "SwingTrading",
    "ETFs",
    "RobinHood",
    "Bogleheads",
    "personalfinance",
]

SUBS = [
    s.strip()
    for s in os.getenv("SUBS", ",".join(DEFAULT_SUBS)).split(",")
    if s.strip()
]

# Subs we fetch post content for from Arctic Shift.
# Excludes WSB (sparse) but includes everything else.
POST_SUBS = [
    s.strip()
    for s in os.getenv("POST_SUBS", ",".join([s for s in DEFAULT_SUBS if s != "wallstreetbets"])).split(",")
    if s.strip()
]

# Subs we fetch comments for (smaller set to keep fetch time sane)
COMMENT_SUBS = [
    s.strip()
    for s in os.getenv("COMMENT_SUBS", "wallstreetbets,wallstreetbetsnew,stocks,investing,options,StockMarket,Superstonk,nvidia").split(",")
    if s.strip()
]

# ----------------------------------------------------------------------------
# HTTP clients
# ----------------------------------------------------------------------------

HEADERS = {
    "User-Agent": "stock-sub-dashboard/1.0 (free; non-commercial)",
    "Accept": "application/json",
}

# Yahoo Finance requires a browser-like UA
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json",
}

_session = requests.Session()
_session.headers.update(HEADERS)

_yahoo_session = requests.Session()
_yahoo_session.headers.update(YAHOO_HEADERS)


def _get_json(url: str, params: dict | None = None, timeout: int = 20) -> dict | None:
    try:
        resp = _session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[api] error fetching {url}: {e}", flush=True)
        return None


# Yahoo Finance interval/range mapping
TIMEFRAMES = {
    "1w":  ("1d", "5d"),    # 5 daily candles
    "1mo": ("1d", "1mo"),   # ~21 daily candles (current default)
    "3mo": ("1d", "3mo"),   # ~63 daily candles
}

def fetch_yahoo_prices(tickers: list[str], timeframe: str = "1mo") -> dict[str, dict]:
    """
    Fetch price + history for a list of tickers from Yahoo Finance.
    Returns {ticker: {price, prev_close, change_pct, currency, name,
                       sparkline: [...closes...]}}
    Uses thread pool of 8 workers to parallelize the 100+ sequential calls.
    Yahoo Finance is the main bottleneck (was 20+ seconds sequential).
    """
    import concurrent.futures
    out: dict[str, dict] = {}
    interval, range_ = TIMEFRAMES.get(timeframe, TIMEFRAMES["1mo"])

    def _fetch_one(ticker: str) -> tuple[str, dict | None]:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            params = {"interval": interval, "range": range_}
            resp = _yahoo_session.get(url, params=params, timeout=YAHOO_TIMEOUT)
            if resp.status_code != 200:
                return ticker, None
            data = resp.json()
            chart = data.get("chart", {}).get("result", [None])[0]
            if not chart:
                return ticker, None
            meta = chart.get("meta", {})
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price is None:
                return ticker, None
            change_pct = ((price - prev) / prev * 100) if prev else 0
            closes = (
                chart.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            )
            sparkline = [round(c, 2) for c in closes if c is not None]
            return ticker, {
                "price": round(price, 2),
                "prev_close": round(prev, 2) if prev else None,
                "change_pct": round(change_pct, 2),
                "currency": meta.get("currency", "USD"),
                "name": meta.get("longName") or meta.get("shortName") or ticker,
                "sparkline": sparkline,
                "timeframe": timeframe,
            }
        except Exception as e:
            return ticker, None

    # Parallel fetch with 8 workers (Yahoo's rate limit is ~2000/hr, 8 concurrent is safe)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_fetch_one, tickers))
    for ticker, data in results:
        if data:
            out[ticker] = data
    return out


# ----------------------------------------------------------------------------
# API fetchers (no DB, no auth)
# ----------------------------------------------------------------------------

def fetch_apewisdom_tickers() -> dict[str, list[dict]]:
    """
    Fetch ticker rankings for all configured subs.
    Returns {subreddit: [ticker_dict, ...]}.
    """
    out: dict[str, list[dict]] = {}
    for sub in SUBS:
        url = f"https://apewisdom.io/api/v1.0/filter/{sub}/page/1"
        data = _get_json(url, timeout=APEWISDOM_TIMEOUT)
        if data and isinstance(data, dict):
            out[sub] = data.get("results", [])
        else:
            out[sub] = []
        time.sleep(0.3)  # be polite
    return out


def _arctic_get_with_retry(url: str, params: dict, max_attempts: int = 3, timeout: int = ARCTIC_TIMEOUT) -> tuple[dict | None, str | None]:
    """
    GET from Arctic Shift with automatic retry on 422/429/5xx.
    Returns (data, error_message). error_message is None on success.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            resp = _session.get(url, params=params, timeout=timeout)
            if resp.status_code in (400, 422) and "after" in params:
                # Server doesn't accept after+sort combo. Try without date filter.
                params = {k: v for k, v in params.items() if k not in ("after", "before")}
                resp = _session.get(url, params=params, timeout=timeout)
            if resp.status_code == 429:
                # Rate limited - back off exponentially
                wait = min(2 ** attempt, 8)
                time.sleep(wait)
                continue
            if resp.status_code in (500, 502, 503, 504):
                if attempt < max_attempts:
                    time.sleep(1)
                    continue
            resp.raise_for_status()
            return resp.json(), None
        except requests.RequestException as e:
            if attempt < max_attempts:
                time.sleep(1)
                continue
            return None, str(e)
    return None, "max retries exceeded"


def fetch_arctic_posts(sub: str) -> tuple[list[dict], str | None]:
    """
    Fetch recent top posts for one subreddit from Arctic Shift.
    Paginated up to 3 pages (300 posts max) to increase coverage.
    Returns (posts, error_message). error_message is None on success.
    """
    now = int(time.time())
    after = now - POST_LOOKBACK_DAYS * 86400
    url = "https://arctic-shift.photon-reddit.com/api/posts/search"
    base_params = {
        "subreddit": sub,
        "limit": 100,
        "after": after,
        "sort": "desc",
    }
    all_posts = []
    seen_ids: set[str] = set()
    cursor = None
    max_pages = 3
    last_error = None

    for _ in range(max_pages):
        params = dict(base_params)
        if cursor:
            params["cursor"] = cursor
        data, err = _arctic_get_with_retry(url, params)
        if err:
            last_error = err
            break
        if not data:
            break

        posts = data.get("data", [])
        if not posts:
            break
        new_count = 0
        for p in posts:
            pid = p.get("id")
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                all_posts.append(p)
                new_count += 1
        if new_count == 0:
            break
        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(0.3)

    all_posts.sort(key=lambda p: p.get("score", 0), reverse=True)
    return all_posts[:POSTS_PER_SUB], last_error


def fetch_arctic_comments(sub: str) -> tuple[list[dict], str | None]:
    """
    Fetch recent top comments for one subreddit from Arctic Shift.
    Returns (comments, error_message).
    """
    now = int(time.time())
    after = now - POST_LOOKBACK_DAYS * 86400
    url = "https://arctic-shift.photon-reddit.com/api/comments/search"
    base_params = {
        "subreddit": sub,
        "limit": 100,
        "after": after,
        "sort": "desc",
    }
    all_comments = []
    seen_ids: set[str] = set()
    cursor = None
    max_pages = 2
    last_error = None

    for _ in range(max_pages):
        params = dict(base_params)
        if cursor:
            params["cursor"] = cursor
        data, err = _arctic_get_with_retry(url, params)
        if err:
            last_error = err
            break
        if not data:
            break

        comments = data.get("data", [])
        if not comments:
            break
        for c in comments:
            cid = c.get("id")
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                all_comments.append(c)
        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(0.3)

    all_comments.sort(key=lambda c: c.get("score", 0), reverse=True)
    return all_comments[:COMMENTS_PER_SUB], last_error


def fetch_all_arctic_posts_parallel() -> tuple[dict[str, list[dict]], dict[str, str]]:
    """
    Fetch posts for all configured POST_SUBS in parallel.
    Arctic Shift rate-limits aggressively, so we use a small thread pool
    (4 workers) to keep concurrent requests reasonable.
    Returns (posts_by_sub, errors).
    """
    import concurrent.futures
    out: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}

    def _fetch_one(sub: str) -> tuple[str, list[dict], str | None]:
        posts, err = fetch_arctic_posts(sub)
        return sub, posts, err

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_fetch_one, sub) for sub in POST_SUBS]
        for fut in concurrent.futures.as_completed(futures):
            sub, posts, err = fut.result()
            out[sub] = posts
            if err:
                errors[sub] = err
    return out, errors


def fetch_all_arctic_comments_parallel() -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Fetch comments for all configured COMMENT_SUBS in parallel."""
    import concurrent.futures
    out: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}

    def _fetch_one(sub: str) -> tuple[str, list[dict], str | None]:
        comments, err = fetch_arctic_comments(sub)
        return sub, comments, err

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_fetch_one, sub) for sub in COMMENT_SUBS]
        for fut in concurrent.futures.as_completed(futures):
            sub, comments, err = fut.result()
            out[sub] = comments
            if err:
                errors[sub] = err
    return out, errors


def fetch_earnings_calendar(tracked_tickers: set[str] | None = None,
                              days_ahead: int = 14) -> dict[str, list[dict]]:
    """
    Fetch the upcoming earnings calendar from Nasdaq and filter to tracked tickers.
    Returns {ticker: [earnings_event, ...]} where each event is the most recent
    upcoming one. Only future events (not past ones) are returned.

    Nasdaq returns ~30-50 events per business day. With 14 days of look-ahead
    we get ~300-500 events total; filtered to ~50-100 of our tracked tickers.
    """
    from datetime import datetime, timedelta
    import concurrent.futures
    out: dict[str, list[dict]] = {}
    if tracked_tickers is None:
        tracked_tickers = set()

    today = datetime.now()
    # Build list of business-day-ish dates to query (skip weekends to avoid empty
    # responses; Nasdaq returns null for weekends)
    dates = []
    for offset in range(0, days_ahead + 1):
        d = today + timedelta(days=offset)
        if d.weekday() < 5:  # Mon-Fri
            dates.append(d.strftime("%Y-%m-%d"))

    def _fetch_one(date_str: str) -> tuple[str, list[dict]]:
        url = f"https://api.nasdaq.com/api/calendar/earnings?date={date_str}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read())
        except Exception:
            return date_str, []
        data = d.get("data") or {}
        return date_str, data.get("rows", []) or []

    # Parallel fetch - 14 dates, ~5s total instead of ~20s sequential
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as ex:
        results = list(ex.map(_fetch_one, dates))

    # Group by symbol, keep only future events for tracked tickers
    for date_str, rows in results:
        for r in rows:
            sym = r.get("symbol", "").upper()
            if not sym or sym not in tracked_tickers:
                continue
            # Normalize the event
            event = {
                "date": date_str,
                "symbol": sym,
                "name": r.get("name", ""),
                "time": r.get("time", ""),  # before-market, after-hours, time-not-supplied
                "fiscal_quarter": r.get("fiscalQuarterEnding", ""),
                "eps_forecast": r.get("epsForecast", ""),
                "no_of_ests": r.get("noOfEsts", ""),
                "last_year_date": r.get("lastYearRptDt", ""),
                "last_year_eps": r.get("lastYearEPS", ""),
                "market_cap": r.get("marketCap", ""),
            }
            # Convert time field to a friendly label
            t = event["time"]
            if t == "time-before-market" or t == "pre-market":
                event["when"] = "Before market"
            elif t == "time-after-hours" or t == "after-hours":
                event["when"] = "After hours"
            else:
                event["when"] = "Time TBD"
            out.setdefault(sym, []).append(event)
    # Sort each ticker's events by date
    for sym in out:
        out[sym].sort(key=lambda e: e["date"])
    return out


# ----------------------------------------------------------------------------
# Macro data (10Y, 2Y, VIX, WTI, S&P, NASDAQ, Gold, DXY)
# ----------------------------------------------------------------------------

MACRO_TICKERS = {
    # Yahoo tickers (^TNX = 10Y, ^FVX = 5Y, ^TYX = 30Y yields;
    # price is 10x the yield so we divide by 10)
    # ^VIX = VIX, ^GSPC = S&P 500, ^IXIC = NASDAQ, GC=F = Gold, CL=F = WTI Crude, DX-Y.NYB = DXY
    "10Y":   ("yahoo_yield", "^TNX",   "10Y Treasury"),
    "5Y":    ("yahoo_yield", "^FVX",   "5Y Treasury"),
    "30Y":   ("yahoo_yield", "^TYX",   "30Y Treasury"),
    "VIX":   ("yahoo",      "^VIX",    "VIX"),
    "SPX":   ("yahoo",      "^GSPC",   "S&P 500"),
    "IXIC":  ("yahoo",      "^IXIC",   "NASDAQ"),
    "GOLD":  ("yahoo",      "GC=F",    "Gold"),
    "OIL":   ("yahoo",      "CL=F",    "WTI Crude"),
    "DXY":   ("yahoo",      "DX-Y.NYB","Dollar Index"),
}


def fetch_fred_series(series_id: str) -> dict | None:
    """
    Fetch the latest value of a FRED CSV series.
    Returns {"value": float, "date": "YYYY-MM-DD", "source": "FRED"} or None.
    Currently unused (we get yields from Yahoo) but kept for reference.
    """
    import datetime
    end = datetime.date.today().isoformat()
    start = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}&coed={end}"
    try:
        resp = _session.get(url, timeout=30)
        resp.raise_for_status()
        lines = resp.text.strip().split("\n")
        data_lines = [l for l in lines if l and l[0].isdigit()]
        if not data_lines:
            return None
        last = data_lines[-1]
        date, value = last.split(",")
        if value.strip() in (".", ""):
            return None
        return {
            "value": float(value),
            "date": date,
            "source": "FRED",
        }
    except Exception as e:
        print(f"[macro] FRED error for {series_id}: {e}", flush=True)
        return None


def fetch_yahoo_quote(ticker: str) -> dict | None:
    """
    Fetch a single price quote from Yahoo Finance.
    Returns {"value": price, "prev_close": prev, "change_pct": pct, "source": "Yahoo"} or None.
    """
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {"interval": "1d", "range": "5d"}
        resp = _yahoo_session.get(url, params=params, timeout=YAHOO_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        chart = data.get("chart", {}).get("result", [None])[0]
        if not chart:
            return None
        meta = chart.get("meta", {})
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None:
            return None
        change_pct = ((price - prev) / prev * 100) if prev else 0
        return {
            "value": round(price, 2),
            "prev_close": round(prev, 2) if prev else None,
            "change_pct": round(change_pct, 2),
            "source": "Yahoo",
        }
    except Exception as e:
        print(f"[macro] Yahoo error for {ticker}: {e}", flush=True)
        return None


def fetch_macro_indicators() -> dict[str, dict]:
    """
    Fetch all macro indicators (10Y, 5Y, 30Y Treasury yields, VIX, S&P, NASDAQ, Gold, Oil, DXY).
    Returns {key: {value, ...}}. Missing series are just absent from the result.
    Parallelized with threads for speed.
    Treasury yield tickers (^TNX, ^FVX, ^TYX) return price 10x the actual yield,
    so we divide by 10 to get the percentage.
    """
    import concurrent.futures
    out: dict[str, dict] = {}

    def _fetch_one(item):
        key, (kind, ticker, label) = item
        data = fetch_yahoo_quote(ticker)
        if not data:
            return None
        # ^TNX, ^FVX, ^TYX already return the yield as a percentage (e.g. 4.796 = 4.796%)
        # No need to divide.
        return key, {**data, "label": label, "id": ticker}

    # Run all 9 macro fetches in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(_fetch_one, MACRO_TICKERS.items()))
    for r in results:
        if r:
            key, data = r
            out[key] = data
    # Compute yield curve spread (10Y - 5Y). Important recession indicator.
    if "10Y" in out and "5Y" in out:
        spread = out["10Y"]["value"] - out["5Y"]["value"]
        out["CURVE_10Y5Y"] = {
            "value": round(spread, 2),
            "label": "10Y-5Y Spread",
            "source": "computed",
        }
    return out


# ----------------------------------------------------------------------------
# Ticker extraction (server-side, for the "why is it popular" index)
# ----------------------------------------------------------------------------

import re

# Common WSB / stock-sub false-positives to filter out of bare-ticker matches.
# Expanded significantly to handle conversational text and comments.
COMMON_FALSE_POSITIVES = {
    # Single letters
    "I", "A", "J", "K", "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z",
    # 2-3 letter common words / filler
    "AN", "AT", "BE", "BY", "DO", "GO", "HE", "IF", "IN", "IS", "IT", "ME", "MY", "NO",
    "OF", "OH", "OK", "ON", "OR", "SO", "TO", "UP", "US", "WE", "AM", "AS", "LA", "EL",
    "ALL", "AND", "ARE", "BIG", "BUY", "CAN", "DID", "END", "FOR", "GOT", "HAS", "HAD",
    "HER", "HIM", "HIS", "HOW", "ITS", "LET", "MAY", "NEW", "NOW", "OLD", "ONE", "OUR",
    "OUT", "OWN", "PUT", "SAY", "SHE", "TOO", "TWO", "USE", "WAS", "WAY", "WHO", "WHY",
    "YET", "YOU", "LOL", "OMG", "WTF", "IMO", "TBH", "ELI", "PSA", "TLDR", "IANAL",
    "CEO", "CFO", "COO", "CTO", "CMO", "VP", "CRO", "HR", "PR", "OP", "PM", "AM", "FM",
    # Common stock-market jargon that LOOKS like tickers
    "ETF", "IPO", "ATH", "ATL", "ITM", "OTM", "ATM", "DTE", "IV", "DCA", "YOLO",
    "FOMO", "FUD", "DD", "PT", "ER", "EPS", "PE", "ROE", "ROI", "RSI", "SMA", "EMA",
    "MACD", "RSI", "VWAP", "OHLC", "PNL", "ATM", "OTM", "FD", "ITM", "DTE", "IV", "OI",
    "GDP", "CPI", "PPI", "PCE", "FOMC", "OPEC", "SEC", "FED", "FDA", "CDC", "NSA",
    "USA", "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CNY", "INR", "BTC", "ETH", "SOL",
    "YTD", "QTD", "MTD", "YOY", "QOQ", "MOM", "EOD", "EOW", "EOY", "ATH", "ATL",
    "IMO", "FYI", "IIRC", "TIL", "SMH", "WTF", "LMK", "OP", "TLDR", "BFD",
    "WSB", "DD", "OP", "PT", "ER", "EPS", "TA", "FA", "HODL", "FOMO", "BTFD", "BTD",
    # Conversational / casual chat words (from comment analysis)
    "DAMN", "DUMB", "REAL", "SURE", "COOL", "NICE", "WELL", "JUST", "LIKE", "MAKE",
    "MANY", "MUCH", "OVER", "RIDE", "SOME", "THAN", "THAT", "THEM", "THEN", "THEY",
    "THIS", "TIME", "WHAT", "WHEN", "WILL", "WORK", "YALL", "GUYS", "DAYS", "DAYS",
    "SHIT", "FUCK", "DUDE", "BRO", "MAN", "GUY", "FEEL", "LOOK", "LOVE", "HATE",
    "HOPE", "BEST", "GOOD", "BAD", "LOSE", "WON", "TOLD", "TELL", "SAID", "SEND",
    "HELP", "STOP", "KEEP", "HOLD", "WAIT", "WANT", "NEED", "MADE", "MAKE", "TAKE",
    "GIVE", "FIND", "CALL", "KNOW", "SHOW", "PLAY", "RUN", "MOVE", "LIVE", "BELIEVE",
    "MOST", "ONLY", "THAN", "MUCH", "MORE", "LESS", "VERY", "EVEN", "BACK", "DOWN",
    "FROM", "WITH", "HAVE", "THIS", "THAT", "WHAT", "WHICH", "THEIR", "THERE", "WHERE",
    "ALSO", "JUST", "BEEN", "STILL", "GOING", "STAY", "LEFT", "RIGHT", "BEING", "REALLY",
    "DA", "DAYS", "WEEK", "YEAR", "MTH", "HR", "MIN", "SEC", "LOT", "BIT", "NEXT",
    "LAST", "PAST", "SAME", "OTHER", "FEW", "MANY", "REAL", "FULL", "BEST", "LONG",
    "OPEN", "HIGH", "LOW", "CLOSE", "BUY", "SELL", "HOLD", "GAIN", "LOSS", "ROI",
    "PR", "CEO", "CTO", "IT", "DEV", "JR", "SR", "OK", "NO", "YES", "MAYBE",
    "FANG", "ETF", "REIT", "YOLO", "WSB", "OP", "PT", "TA", "FA", "DD", "ER", "EPS",
    "ATH", "ATL", "BAG", "MOON", "ROCKET", "DIAMOND", "HANDS", "PAPER", "TENDIES",
    "WALL", "STREET", "STONKS", "MEME", "APE", "BAGHOLDER", "SIR", "JACK", "POUND",
    "FLOOR", "CEILING", "GAP", "UP", "DOWN", "DIP", "RALLY", "PUMP", "DUMP", "RUG",
    # Words that show up a lot in WSB-speak
    "TITS", "CUM", "ASS", "PUSSY", "DICK",  # crude but real WSB slang
    "PUMP", "DUMP", "MOON", "RUG", "FUD", "FOMO", "SHILL", "BTD", "BTFD", "FD",
    "YOLO", "FOMO", "ATH", "ATL", "ITM", "OTM", "ATM", "DTE", "IV", "OI", "VWAP",
}


def extract_tickers(text: str) -> set[str]:
    """
    Extract stock ticker mentions from text. Returns a set of uppercase tickers.

    Two-stage extraction:
    1. Strip code blocks (```...``` and inline `code`) to avoid false positives
       like print($NVDA) or function names
    2. Cashtags ($TSLA) - very high confidence, always included
    3. Bare UPPERCASE words - included only if they pass the false-positive filter
    """
    if not text:
        return set()
    # Strip code blocks: ```...```, `...`, and indented blocks
    cleaned = re.sub(r"```[\s\S]*?```", " ", text)         # fenced code blocks
    cleaned = re.sub(r"`[^`\n]+`", " ", cleaned)             # inline code
    cleaned = re.sub(r"(?m)^[ \t]+.*$", " ", cleaned)     # indented lines
    cleaned = re.sub(r"(?m)^>.*$", " ", cleaned)          # block quotes
    found = set()
    # Stage 1: $TICKER form (very high confidence)
    found.update(re.findall(r"\$([A-Z]{1,5})\b", cleaned))
    # Stage 2: Bare UPPERCASE words (2-5 chars) - only if they pass the filter
    # We require word boundaries and skip common conversational false-positives
    bare = re.findall(r"\b([A-Z]{2,5})\b", cleaned)
    for word in bare:
        if word in COMMON_FALSE_POSITIVES:
            continue
        # Skip if the word is all one letter repeated (AAAA, etc.)
        if len(set(word)) == 1:
            continue
        found.add(word)
    return found


def build_ticker_post_index(
    posts: list[dict],
    comments: list[dict] | None = None,
) -> dict[str, list[dict]]:
    """
    Build a {ticker: [post, ...]} index from posts (and optionally comments).
    Posts and comments are scanned for tickers in both title and body.
    Sorted by adjusted score (specificity-weighted), with a small cap.

    Also tracks 'post ticker breadth' — how many tickers a post/comment mentions —
    so we can filter out 'list posts' (e.g. a market roundup that mentions
    10 tickers in passing) which aren't actually a 'why this is trending'.
    """
    # Combine posts and comments. Comments have lower engagement, so we score
    # them at 0.3x a post to reflect they're more conversational.
    all_items: list[tuple[dict, float]] = [(p, 1.0) for p in posts]
    if comments:
        all_items.extend((c, 0.3) for c in comments)

    # Pass 1: count how many tickers each item mentions
    item_ticker_count: dict[str, int] = {}
    item_to_tickers: dict[str, set[str]] = {}
    item_score_mult: dict[str, float] = {}
    for item, mult in all_items:
        # Posts use title + selftext; comments use body
        if "body" in item and "selftext" not in item:
            # comment
            text = f"{item.get('body', '') or ''}"
        else:
            # post
            text = f"{item.get('title', '')} {item.get('selftext', '')[:POST_BODY_EXTRACT_CHARS]}"
        tickers = extract_tickers(text)
        if tickers:
            iid = item.get("id", "")
            item_to_tickers[iid] = tickers
            item_ticker_count[iid] = len(tickers)
            item_score_mult[iid] = mult

    # Pass 2: build the index with quality filters
    index: dict[str, list[dict]] = {}
    cap = 10
    MIN_SCORE = 3
    MAX_BREADTH = 7

    for item, mult in all_items:
        iid = item.get("id", "")
        breadth = item_ticker_count.get(iid, 0)
        if breadth >= MAX_BREADTH:
            continue
        score = item.get("score", 0) or 0
        # Comments need lower threshold since they don't have "posts" worth of upvotes
        effective_min_score = 1 if mult < 1 else MIN_SCORE
        if score < effective_min_score:
            continue
        tickers = item_to_tickers.get(iid, set())
        if not tickers:
            continue
        # Specificity boost: posts that mention fewer tickers rank higher
        adjusted_score = (score * mult) / max(breadth, 1)
        # Posts and comments have different shapes - normalize to one
        if "body" in item and "selftext" not in item:
            # it's a comment
            permalink = item.get("permalink", "")
            # comments need /r/.../comments/ID/title/IDc/
            if permalink and not permalink.startswith("http"):
                full_permalink = f"https://reddit.com{permalink}"
            else:
                full_permalink = f"https://reddit.com{permalink}"
            slim = {
                "id": iid,
                "subreddit": item.get("subreddit", ""),
                "title": (item.get("body", "") or "")[:200],  # comments don't have titles
                "score": score,
                "breadth": breadth,
                "num_comments": 0,
                "permalink": full_permalink,
                "author": item.get("author", "[deleted]"),
                "is_comment": True,
            }
        else:
            # it's a post
            slim = {
                "id": iid,
                "subreddit": item.get("subreddit", ""),
                "title": (item.get("title") or "")[:200],
                "score": score,
                "breadth": breadth,
                "num_comments": item.get("num_comments", 0),
                "permalink": f"https://reddit.com{item.get('permalink', '')}",
                "author": item.get("author", "[deleted]"),
                "is_comment": False,
            }
        for t in tickers:
            bucket = index.setdefault(t, [])
            if len(bucket) < cap:
                bucket.append((adjusted_score, slim))
    # Sort by adjusted score desc, then raw score desc
    for ticker in index:
        index[ticker] = [
            slim for _, slim in sorted(
                index[ticker],
                key=lambda x: (x[0], x[1].get("score", 0)),
                reverse=True,
            )
        ]
    return index


# ----------------------------------------------------------------------------
# In-memory cache
# ----------------------------------------------------------------------------

class Cache:
    """Thread-safe in-memory cache with TTL."""

    def __init__(self, ttl: int):
        self.ttl = ttl
        self._data: Any = None
        self._fetched_at: float = 0
        self._lock = threading.Lock()
        self._fetching = False

    def get(self, fetch_fn) -> Any:
        now = time.time()
        with self._lock:
            if self._data is not None and (now - self._fetched_at) < self.ttl:
                return self._data
            if self._fetching:
                # Another thread is fetching; return stale or empty
                return self._data or {}
            self._fetching = True

        # Fetch outside the lock so concurrent requests don't block on each other
        try:
            print(f"[cache] refreshing (age={now - self._fetched_at:.0f}s)...", flush=True)
            fresh = fetch_fn()
            with self._lock:
                self._data = fresh
                self._fetched_at = time.time()
            return fresh
        finally:
            with self._lock:
                self._fetching = False


# ----------------------------------------------------------------------------
# Aggregation (build dashboard payload from raw API data)
# ----------------------------------------------------------------------------

def build_dashboard_payload() -> dict:
    """Fetch everything and shape it for the dashboard."""
    started = time.time()
    tickers_by_sub = fetch_apewisdom_tickers()
    posts_by_sub, post_errors = fetch_all_arctic_posts_parallel()

    # Gather all unique tickers we need prices for (top 30 per sub, deduped)
    all_tickers: set[str] = set()
    for sub, tickers in tickers_by_sub.items():
        for t in tickers[:30]:
            sym = t.get("ticker")
            if sym:
                all_tickers.add(sym)

    # Filter out tickers Yahoo can't quote (e.g. crypto tickers like BTC.X,
    # and tickers with special chars). Detect crypto/dot-suffixes and skip.
    stock_tickers = [t for t in all_tickers if "." not in t and "-" not in t]
    # Default dashboard prices are 1mo (the initial render)
    prices = price_caches["1mo"].get(lambda: fetch_yahoo_prices(stock_tickers, "1mo"))

    # Build the per-ticker post index from all fetched posts + comments
    all_posts_flat = [p for sub_posts in posts_by_sub.values() for p in sub_posts]
    # Comments: fetch from a separate config of subs (smaller set for performance)
    comments_by_sub, comment_errors = fetch_all_arctic_comments_parallel()
    all_comments_flat = [c for sub_comments in comments_by_sub.values() for c in sub_comments]
    full_ticker_index = build_ticker_post_index(all_posts_flat, all_comments_flat)

    # Fetch macro indicators (cached separately for 15 min)
    macro = macro_cache.get(fetch_macro_indicators)

    elapsed = time.time() - started

    # Flatten posts across subs, sort by score
    all_posts = []
    for sub, posts in posts_by_sub.items():
        for p in posts:
            all_posts.append({
                "id": p.get("id", ""),
                "subreddit": p.get("subreddit", sub),
                "title": (p.get("title") or "")[:500],
                "author": p.get("author", "[deleted]"),
                "score": p.get("score", 0),
                "num_comments": p.get("num_comments", 0),
                "upvote_ratio": p.get("upvote_ratio"),
                "permalink": f"https://reddit.com{p.get('permalink', '')}",
                "created_utc": p.get("created_utc"),
            })
    all_posts.sort(key=lambda p: p["score"], reverse=True)
    top_posts = [p for p in all_posts if p["score"] > 5][:30]

    # Per-sub top tickers (top 10 each, by mentions)
    per_sub_top: dict[str, list[dict]] = {}
    for sub, tickers in tickers_by_sub.items():
        ranked = sorted(tickers, key=lambda t: t.get("mentions", 0), reverse=True)[:10]
        per_sub_top[sub] = ranked

    # Cross-sub leaderboard: sum mentions across all subs
    cross_sub: dict[str, dict] = {}
    for sub, tickers in tickers_by_sub.items():
        for t in tickers:
            sym = t.get("ticker")
            if not sym:
                continue
            if sym not in cross_sub:
                cross_sub[sym] = {
                    "ticker": sym,
                    "name": t.get("name", ""),
                    "total_mentions": 0,
                    "total_upvotes": 0,
                    "sub_count": 0,
                    "subs": [],
                }
            cross_sub[sym]["total_mentions"] += t.get("mentions", 0) or 0
            cross_sub[sym]["total_upvotes"] += t.get("upvotes", 0) or 0
            cross_sub[sym]["sub_count"] += 1
            cross_sub[sym]["subs"].append(sub)
    # Filter low-volume and sort
    cross_sub_list = [v for v in cross_sub.values() if v["total_mentions"] >= 5]
    cross_sub_list.sort(key=lambda v: v["total_mentions"], reverse=True)
    cross_sub_list = cross_sub_list[:30]

    # Trending — biggest mention_delta from ApeWisdom (computed for us)
    trending_data: dict[str, dict] = {}
    for sub, tickers in tickers_by_sub.items():
        for t in tickers:
            sym = t.get("ticker")
            m24 = t.get("mentions_24h_ago")
            if not sym or m24 is None:
                continue
            if sym not in trending_data:
                trending_data[sym] = {
                    "ticker": sym,
                    "name": t.get("name", ""),
                    "mentions_now": 0,
                    "mentions_24h": 0,
                }
            trending_data[sym]["mentions_now"] += t.get("mentions", 0) or 0
            trending_data[sym]["mentions_24h"] += m24 or 0
    trending_list = []
    for v in trending_data.values():
        delta = v["mentions_now"] - v["mentions_24h"]
        if v["mentions_now"] >= 3:
            v["delta"] = delta
            trending_list.append(v)
    trending_list.sort(key=lambda v: v["delta"], reverse=True)
    trending_list = trending_list[:15]

    # Build the set of all tracked tickers (now we have per_sub_top, cross_sub_list, trending_list)
    tracked_tickers: set[str] = set()
    for tickers in per_sub_top.values():
        for t in tickers:
            tracked_tickers.add(t.get("ticker", ""))
    for t in cross_sub_list:
        tracked_tickers.add(t.get("ticker", ""))
    for t in trending_list:
        tracked_tickers.add(t.get("ticker", ""))
    tracked_tickers.discard("")

    # Fetch earnings calendar for tracked tickers (cached 6h)
    earnings = earnings_cache.get(lambda: fetch_earnings_calendar(tracked_tickers, days_ahead=14))

    # Slim the per-ticker post index to just the tickers we actually display
    # (cross-sub leaderboard + trending) to keep the payload small
    important_tickers = set()
    for t in cross_sub_list:
        important_tickers.add(t["ticker"])
    for t in trending_list:
        important_tickers.add(t["ticker"])
    ticker_posts_payload = {
        t: full_ticker_index.get(t, [])[:TOP_POSTS_PER_TICKER]
        for t in important_tickers
        if full_ticker_index.get(t)
    }

    # Per-subreddit post summary
    per_sub_posts = []
    for sub, posts in posts_by_sub.items():
        if not posts:
            continue
        per_sub_posts.append({
            "subreddit": sub,
            "posts": len(posts),
            "total_comments": sum(p.get("num_comments", 0) for p in posts),
            "avg_score": sum(p.get("score", 0) for p in posts) / max(len(posts), 1),
            "top_score": max((p.get("score", 0) for p in posts), default=0),
        })
    per_sub_posts.sort(key=lambda s: s["total_comments"], reverse=True)

    # Freshness
    freshness = [
        {
            "subreddit": sub,
            "last_fetch": datetime.now(timezone.utc).isoformat(),
            "total_records": len(tickers_by_sub.get(sub, [])) + len(posts_by_sub.get(sub, [])),
        }
        for sub in set(list(tickers_by_sub.keys()) + list(posts_by_sub.keys()))
    ]
    # Merge any fetch errors into freshness so the UI can show them
    for sub, err in {**post_errors, **comment_errors}.items():
        for f in freshness:
            if f["subreddit"] == sub:
                f["error"] = err
                break

    return {
        "last_scrape": datetime.now(timezone.utc).isoformat(),
        "fetch_time_seconds": round(elapsed, 1),
        "freshness": freshness,
        "cross_sub_leaderboard": cross_sub_list,
        "trending": trending_list,
        "per_sub_top": per_sub_top,
        "top_posts": top_posts,
        "per_sub_posts": per_sub_posts,
        "prices": prices,
        "ticker_posts": ticker_posts_payload,
        "macro": macro,
        "earnings": earnings,
        "data_sources": {
            "apewisdom_subs": len(tickers_by_sub),
            "arctic_post_subs": len(posts_by_sub),
            "arctic_comment_subs": len(comments_by_sub),
            "yahoo_prices": len(prices),
            "macro_indicators": len(macro),
            "tracked_tickers": len(tracked_tickers),
            "earnings_upcoming": len(earnings),
        },
    }


def build_ticker_detail(ticker: str) -> dict:
    """Build a per-ticker detail payload by searching current snapshots."""
    upper = ticker.upper().lstrip("$")
    tickers_by_sub = fetch_apewisdom_tickers()
    posts_by_sub, _ = fetch_all_arctic_posts_parallel()
    comments_by_sub, _ = fetch_all_arctic_comments_parallel()

    # Per-sub stats
    latest = []
    for sub, tickers in tickers_by_sub.items():
        for t in tickers:
            if t.get("ticker") == upper:
                latest.append({
                    "subreddit": sub,
                    "rank": t.get("rank"),
                    "mentions": t.get("mentions"),
                    "upvotes": t.get("upvotes"),
                    "mentions_24h_ago": t.get("mentions_24h_ago"),
                    "rank_24h_ago": t.get("rank_24h_ago"),
                })
                break

    # Use the per-ticker post index (which now includes comments) for richer coverage
    all_posts_flat = [p for sub_posts in posts_by_sub.values() for p in sub_posts]
    all_comments_flat = [c for sub_comments in comments_by_sub.values() for c in sub_comments]
    full_ticker_index = build_ticker_post_index(all_posts_flat, all_comments_flat)
    posts = full_ticker_index.get(upper, [])[:30]

    # Get next earnings event for this ticker (if any)
    tracked = {upper}
    earnings_all = earnings_cache.get(lambda: fetch_earnings_calendar(tracked, days_ahead=14))
    next_earnings = earnings_all.get(upper, [None])[0] if earnings_all.get(upper) else None

    return {
        "ticker": upper,
        "latest_per_sub": sorted(latest, key=lambda x: x.get("mentions", 0) or 0, reverse=True),
        "recent_posts": posts,
        "next_earnings": next_earnings,
        "trend_7d": [],
    }


# ----------------------------------------------------------------------------
# App + cache
# ----------------------------------------------------------------------------

app = Flask(__name__)
cache = Cache(ttl=CACHE_TTL)
# Separate price cache per timeframe (1w / 1mo / 3mo) so we don't refetch
# the same data when the user toggles between them
price_caches: dict[str, Cache] = {
    tf: Cache(ttl=PRICE_CACHE_TTL) for tf in TIMEFRAMES.keys()
}
# Macro data is less volatile - cache for 15 min
macro_cache = Cache(ttl=MACRO_CACHE_TTL)
# Earnings calendar rarely changes - cache for 6 hours
earnings_cache = Cache(ttl=EARNINGS_CACHE_TTL)


class KeyedCache:
    """Per-key TTL cache for things like /api/ticker/<T> where each
    ticker has its own entry. Prevents re-fetching the same ticker twice."""

    def __init__(self, ttl: int):
        self.ttl = ttl
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.time() - ts >= self.ttl:
                del self._data[key]
                return None
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.time(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# Per-ticker cache: 5 min TTL. Each ticker has its own entry.
ticker_cache = KeyedCache(ttl=300)


@app.route("/api/stats")
def api_stats():
    """Main dashboard payload. Cached for CACHE_TTL seconds."""
    try:
        data = cache.get(build_dashboard_payload)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/prices")
def api_prices():
    """
    Fetch prices for a given timeframe (1w / 1mo / 3mo).
    Used by the timeframe selector — only the prices dict is returned.
    Cached per-timeframe for PRICE_CACHE_TTL seconds.
    """
    timeframe = request.args.get("timeframe", "1mo")
    if timeframe not in TIMEFRAMES:
        return jsonify({"error": f"invalid timeframe; use one of {list(TIMEFRAMES)}"}), 400
    # Discover the same ticker list as the main payload
    tickers_by_sub = fetch_apewisdom_tickers()
    all_tickers = set()
    for sub, tickers in tickers_by_sub.items():
        for t in tickers[:30]:
            sym = t.get("ticker")
            if sym:
                all_tickers.add(sym)
    stock_tickers = [t for t in all_tickers if "." not in t and "-" not in t]
    try:
        prices = price_caches[timeframe].get(
            lambda: fetch_yahoo_prices(stock_tickers, timeframe)
        )
        return jsonify(prices)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/earnings")
def api_earnings():
    """Earnings calendar for the next 14 days. Filtered to tracked tickers."""
    try:
        # Build tracked tickers from cached data if possible
        tracked: set[str] = set()
        cached = cache._data
        if cached:
            for tickers in cached.get("per_sub_top", {}).values():
                for t in tickers:
                    tracked.add(t.get("ticker", ""))
            for t in cached.get("cross_sub_leaderboard", []):
                tracked.add(t.get("ticker", ""))
            for t in cached.get("trending", []):
                tracked.add(t.get("ticker", ""))
        if not tracked:
            # No cached data yet; fetch fresh
            tickers_by_sub = fetch_apewisdom_tickers()
            for tickers in tickers_by_sub.values():
                for t in tickers[:30]:
                    tracked.add(t.get("ticker", ""))
        tracked.discard("")
        earnings = earnings_cache.get(lambda: fetch_earnings_calendar(tracked, days_ahead=14))
        return jsonify(earnings)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ticker/<ticker>")
def api_ticker_detail(ticker: str):
    """Ticker detail. Cached for 5 min to avoid 30s+ cold fetches on every click."""
    upper = ticker.upper().lstrip("$")[:10]
    # Use a small per-ticker cache (5 min TTL) - the fetch is expensive
    # and ticker data is mostly static for ~5 min intervals
    cache_key = f"ticker:{upper}"
    cached = ticker_cache.get(cache_key)
    if cached is not None:
        return jsonify(cached)
    try:
        data = build_ticker_detail(ticker)
        ticker_cache.set(cache_key, data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health")
def health():
    """Health check for Render."""
    return jsonify({"status": "ok", "cache_age_seconds": int(time.time() - cache._fetched_at) if cache._data else None})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Force-refresh the cache and return the new data (same shape as /api/stats)."""
    try:
        data = build_dashboard_payload()
        cache._data = data
        cache._fetched_at = time.time()
        # Add a flag so the UI knows this was a forced refresh
        data["force_refreshed"] = True
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ----------------------------------------------------------------------------
# HTML template (mobile-friendly, same as before)
# ----------------------------------------------------------------------------

TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stock Sub Dashboard</title>
<style>
  :root {
    --bg: #0d1117;
    --panel: #161b22;
    --panel-2: #1c2128;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #58a6ff;
    --green: #3fb950;
    --red: #f85149;
    --gold: #d29922;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  header {
    padding: 18px 20px;
    background: linear-gradient(135deg, #1f6feb 0%, #58a6ff 100%);
    box-shadow: 0 2px 8px rgba(0,0,0,0.4);
  }
  h1 { margin: 0 0 4px 0; font-size: 22px; }
  .subtitle { color: rgba(255,255,255,0.85); font-size: 13px; display:flex; gap:10px; align-items:center; flex-wrap: wrap; }
  .refresh-btn {
    background: rgba(255,255,255,0.2);
    color: white;
    border: 1px solid rgba(255,255,255,0.3);
    padding: 6px 12px;
    border-radius: 4px;
    font-size: 12px;
    cursor: pointer;
    min-height: 36px;  /* touch target */
  }
  .refresh-btn:hover { background: rgba(255,255,255,0.3); }
  main { padding: 16px 20px 60px; max-width: 1100px; margin: 0 auto; }
  .grid { display: grid; grid-template-columns: 1fr; gap: 16px; }
  @media (min-width: 720px) { .grid { grid-template-columns: 1fr 1fr; } .grid-full { grid-column: 1 / -1; } }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); margin: 0 0 12px 0; font-weight: 600; }
  .row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--border); gap: 10px; }
  .row:last-child { border-bottom: none; }
  .row .lbl { color: var(--text); display: flex; align-items: center; gap: 8px; min-width: 0; }
  .row .val { color: var(--accent); font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .delta-up { color: var(--green); font-size: 11px; }
  .delta-down { color: var(--red); font-size: 11px; }
  .chip { background: var(--panel-2); border: 1px solid var(--border); padding: 4px 10px; border-radius: 12px; font-size: 11px; color: var(--muted); }
  .chip strong { color: var(--text); }
  .post { padding: 12px 0; border-bottom: 1px solid var(--border); }
  .post:last-child { border-bottom: none; }
  .post-title { font-size: 15px; font-weight: 500; color: var(--text); text-decoration: none; display: block; margin-bottom: 6px; line-height: 1.4; }
  .post-title:hover { color: var(--accent); }
  .post-meta { font-size: 12px; color: var(--muted); display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  .post-meta .sub { color: var(--accent); font-weight: 500; }
  .post-meta .score { color: var(--green); }
  .ticker-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 8px; }
  .ticker-card {
    background: var(--panel-2);
    border: 1px solid var(--border);
    padding: 10px 12px;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.15s;
    position: relative;
  }
  .ticker-card:hover { border-color: var(--accent); transform: translateY(-1px); }
  .ticker-card .sym { font-weight: 700; color: var(--accent); font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 14px; }
  .ticker-card .name { font-size: 11px; color: var(--muted); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ticker-card .price-row { display: flex; justify-content: space-between; align-items: baseline; margin-top: 6px; font-size: 12px; }
  .ticker-card .price {
    color: var(--text);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    transition: color 0.6s ease-out;
  }
  .ticker-card .price.flash-up { color: var(--green); }
  .ticker-card .price.flash-down { color: var(--red); }
  .ticker-card .change { font-weight: 600; font-size: 11px; padding: 1px 5px; border-radius: 3px; font-variant-numeric: tabular-nums; }
  .ticker-card .change.up { color: var(--green); background: rgba(63, 185, 80, 0.12); }
  .ticker-card .change.down { color: var(--red); background: rgba(248, 81, 73, 0.12); }
  .ticker-card .change.flat { color: var(--muted); background: rgba(139, 148, 158, 0.12); }
  .ticker-card .sparkline { margin-top: 6px; height: 28px; }
  .ticker-card .sparkline path { fill: none; stroke-width: 1.5; vector-effect: non-scaling-stroke; }
  .ticker-card .sparkline .up { stroke: var(--green); }
  .ticker-card .sparkline .down { stroke: var(--red); }
  .ticker-card .sparkline .flat { stroke: var(--muted); }
  .ticker-card .star { position: absolute; top: 6px; right: 8px; color: var(--muted); font-size: 14px; line-height: 1; cursor: pointer; user-select: none; transition: color 0.15s; }
  .ticker-card .star:hover { color: var(--gold); }
  .ticker-card .star.starred { color: var(--gold); }
  .ticker-card .no-price { color: var(--muted); font-size: 11px; margin-top: 6px; font-style: italic; }
  .ticker-card .why-trending {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px dashed var(--border);
    min-height: 60px;  /* reserve space so cards line up even when empty */
  }
  .ticker-card .why-trending.empty {
    border-top: none;
    padding-top: 0;
    margin-top: 4px;
  }
  .ticker-card .why-label {
    font-size: 9px;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 4px;
    letter-spacing: 0.3px;
    font-weight: 600;
  }
  .ticker-card .why-label.no-data { color: var(--border); }
  .ticker-card .why-post {
    display: block;
    font-size: 11px;
    color: var(--text);
    text-decoration: none;
    line-height: 1.35;
    padding: 3px 0;
    opacity: 0.88;
  }
  .ticker-card .why-post:hover { color: var(--accent); opacity: 1; }
  .ticker-card .why-post .meta {
    font-size: 10px;
    color: var(--muted);
    margin-top: 1px;
    font-variant-numeric: tabular-nums;
  }
  .ticker-card .why-post .score-num { color: var(--green); font-weight: 600; }
  .watchlist-section { background: linear-gradient(135deg, rgba(210, 153, 34, 0.08), rgba(31, 111, 235, 0.05)); border: 1px solid var(--gold); }
  .watchlist-section h2 { color: var(--gold) !important; }
  .watchlist-empty { color: var(--muted); font-size: 13px; padding: 16px 0; text-align: center; font-style: italic; }
  .sub-section { margin-bottom: 18px; }
  .sub-section h3 { font-size: 13px; color: var(--accent); margin: 0 0 8px 0; font-weight: 600; }
  .empty { color: var(--muted); font-style: italic; padding: 20px 0; text-align: center; }
  .footer { text-align: center; color: var(--muted); font-size: 11px; padding: 20px; border-top: 1px solid var(--border); margin-top: 30px; }
  .modal { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.7); display: none; align-items: center; justify-content: center; z-index: 1000; padding: 20px; }
  .modal.active { display: flex; }
  .modal-content { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 20px; max-width: 600px; width: 100%; max-height: 80vh; overflow-y: auto; }
  .modal-close { float: right; cursor: pointer; color: var(--muted); font-size: 24px; line-height: 1; }
  .modal-close:hover { color: var(--text); }
  .modal-header-link {
    text-align: right;
    margin-bottom: 8px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }
  .modal-header-link a {
    font-size: 11px;
    color: var(--accent);
    text-decoration: none;
  }
  .modal-header-link a:hover { text-decoration: underline; }

  /* ----- Dedicated ticker page ----- */
  .ticker-hero {
    display: flex;
    align-items: baseline;
    gap: 14px;
    padding: 8px 0 16px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 8px;
    flex-wrap: wrap;
  }
  .ticker-hero .hero-sym {
    font-size: 28px;
    font-weight: 700;
    color: var(--accent);
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
  }
  .ticker-hero .hero-price {
    font-size: 24px;
    font-weight: 600;
    color: var(--text);
    font-variant-numeric: tabular-nums;
  }
  .ticker-hero .hero-change {
    font-size: 14px;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: 4px;
    font-variant-numeric: tabular-nums;
  }
  .ticker-hero .hero-change.up { color: var(--green); background: rgba(63, 185, 80, 0.15); }
  .ticker-hero .hero-change.down { color: var(--red); background: rgba(248, 81, 73, 0.15); }
  .ticker-hero .hero-change.flat { color: var(--muted); background: rgba(139, 148, 158, 0.15); }
  .back-link {
    display: inline-block;
    margin-bottom: 14px;
    color: var(--accent);
    text-decoration: none;
    font-size: 13px;
  }
  .back-link:hover { text-decoration: underline; }

  /* ----- Skeleton loaders ----- */
  @keyframes shimmer {
    0% { background-position: -200px 0; }
    100% { background-position: calc(200px + 100%) 0; }
  }
  .skeleton {
    background: linear-gradient(90deg, var(--panel-2) 0%, var(--border) 50%, var(--panel-2) 100%);
    background-size: 200px 100%;
    animation: shimmer 1.5s infinite linear;
    border-radius: 4px;
    height: 12px;
    margin: 8px 0;
  }
  .skeleton-line { display: block; }
  .skeleton-card {
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 12px;
    min-height: 110px;
  }
  .skeleton-card .skeleton { margin: 6px 0; }
  .skeleton-card .skeleton.s1 { width: 40%; height: 14px; }
  .skeleton-card .skeleton.s2 { width: 70%; height: 10px; }
  .skeleton-card .skeleton.s3 { width: 90%; height: 10px; }
  .skeleton-card .skeleton.s4 { width: 50%; height: 28px; margin-top: 12px; }
  .skel-step {
    display: inline-block;
    padding: 2px 6px;
    background: var(--panel-2);
    border-radius: 3px;
    margin-right: 4px;
  }

  /* ----- Earnings table ----- */
  .earnings-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  .earnings-table th {
    text-align: left;
    color: var(--muted);
    font-size: 10px;
    text-transform: uppercase;
    font-weight: 600;
    letter-spacing: 0.3px;
    padding: 6px 8px;
    border-bottom: 1px solid var(--border);
  }
  .earnings-table td {
    padding: 8px;
    border-bottom: 1px solid var(--border);
  }
  .earnings-table tr:last-child td { border-bottom: none; }
  .earnings-table tr:hover td { background: var(--panel-2); }

  /* ----- Heatmap ----- */
  .heatmap-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    align-items: center;
    justify-content: flex-start;
  }
  .heatmap-cell {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    border-radius: 6px;
    color: var(--text);
    text-decoration: none;
    transition: transform 0.15s, box-shadow 0.15s;
    min-width: 70px;
    border: 1px solid rgba(255,255,255,0.05);
  }
  .heatmap-cell:hover {
    transform: scale(1.08);
    box-shadow: 0 4px 16px rgba(0,0,0,0.4);
    z-index: 1;
  }
  .heatmap-cell .heatmap-sym {
    font-size: 12px;
    font-weight: 700;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
  }
  .heatmap-cell .heatmap-chg {
    font-size: 10px;
    font-weight: 600;
    margin-top: 2px;
  }

  /* ----- Error / stale banners ----- */
  .banner {
    position: fixed;
    top: 12px;
    left: 50%;
    transform: translateX(-50%);
    z-index: 2000;
    max-width: 90vw;
    background: var(--panel);
    border: 1px solid var(--border);
    border-left: 4px solid var(--accent);
    border-radius: 6px;
    padding: 10px 16px;
    display: flex;
    align-items: center;
    gap: 10px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.4);
    font-size: 13px;
    animation: slideDown 0.3s ease-out;
  }
  @keyframes slideDown {
    from { transform: translate(-50%, -100%); opacity: 0; }
    to { transform: translate(-50%, 0); opacity: 1; }
  }
  .banner.error { border-left-color: var(--red); background: rgba(248, 81, 73, 0.08); }
  .banner.warn { border-left-color: var(--gold); background: rgba(210, 153, 34, 0.08); }
  .banner .banner-msg { flex: 1; }
  .banner .banner-close {
    background: transparent;
    border: none;
    color: var(--muted);
    font-size: 18px;
    cursor: pointer;
    padding: 0 4px;
    line-height: 1;
    min-width: 32px;
    min-height: 32px;
  }
  .banner .banner-close:hover { color: var(--text); }
  .stale-badge {
    display: inline-block;
    background: rgba(210, 153, 34, 0.2);
    color: var(--gold);
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    padding: 2px 6px;
    border-radius: 3px;
    margin-left: 6px;
    letter-spacing: 0.3px;
  }
  .footer-meta {
    text-align: center;
    color: var(--muted);
    font-size: 11px;
    padding: 4px 14px 16px;
  }

  /* ----- Auto-refresh toggle ----- */
  .auto-refresh-toggle {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--muted);
    cursor: pointer;
    user-select: none;
    padding: 4px 8px;
    border-radius: 4px;
  }
  .auto-refresh-toggle:hover { color: var(--text); background: var(--panel-2); }
  .auto-refresh-toggle input { display: none; }
  .auto-refresh-toggle .switch {
    width: 28px;
    height: 16px;
    background: var(--border);
    border-radius: 10px;
    position: relative;
    transition: background 0.2s;
  }
  .auto-refresh-toggle .switch::after {
    content: '';
    position: absolute;
    top: 2px;
    left: 2px;
    width: 12px;
    height: 12px;
    background: var(--text);
    border-radius: 50%;
    transition: transform 0.2s;
  }
  .auto-refresh-toggle input:checked + .switch { background: var(--accent); }
  .auto-refresh-toggle input:checked + .switch::after { transform: translateX(12px); }

  .loading { text-align: center; padding: 40px; color: var(--muted); }
  .stat-line { display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; }
  .stat-line .lbl { color: var(--muted); }
  .stat-line .val { color: var(--text); font-weight: 500; }
  .notice { background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; margin-bottom: 14px; font-size: 12px; color: var(--muted); }

  /* ----- Search + filter bar ----- */
  .controls-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 16px;
    margin-bottom: 16px;
  }
  .search-input {
    flex: 1 1 200px;
    min-width: 0;
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 8px 12px;
    color: var(--text);
    font-size: 14px;
    font-family: inherit;
  }
  .search-input:focus {
    outline: none;
    border-color: var(--accent);
  }
  .search-input::placeholder { color: var(--muted); }
  .timeframe-selector {
    display: flex;
    gap: 4px;
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 3px;
  }
  .timeframe-selector button {
    background: transparent;
    border: none;
    color: var(--muted);
    padding: 5px 10px;
    border-radius: 4px;
    font-size: 12px;
    cursor: pointer;
    font-family: inherit;
    font-weight: 500;
    min-height: 32px;  /* touch target */
    min-width: 44px;
  }
  .timeframe-selector button:hover { color: var(--text); }
  .timeframe-selector button.active {
    background: var(--accent);
    color: white;
  }
  .filter-count {
    font-size: 12px;
    color: var(--muted);
    white-space: nowrap;
  }
  .no-results {
    text-align: center;
    color: var(--muted);
    font-style: italic;
    padding: 30px 0;
  }
  .sub-section.hidden-by-filter { display: none; }

  /* ----- Macro strip ----- */
  .macro-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 0;
    background: linear-gradient(135deg, var(--panel) 0%, var(--panel-2) 100%);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 16px;
    overflow: hidden;
  }
  .macro-cell {
    flex: 1 1 110px;
    min-width: 110px;
    padding: 10px 14px;
    border-right: 1px solid var(--border);
    text-align: center;
  }
  .macro-cell:last-child { border-right: none; }
  .macro-cell .macro-label {
    font-size: 10px;
    text-transform: uppercase;
    color: var(--muted);
    letter-spacing: 0.4px;
    font-weight: 600;
    margin-bottom: 4px;
  }
  .macro-cell .macro-value {
    font-size: 16px;
    font-weight: 700;
    color: var(--text);
    font-variant-numeric: tabular-nums;
    line-height: 1.1;
  }
  .macro-cell .macro-change {
    font-size: 10px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    margin-top: 2px;
  }
  .macro-cell .macro-change.up { color: var(--green); }
  .macro-cell .macro-change.down { color: var(--red); }
  .macro-cell .macro-change.flat { color: var(--muted); }
  .macro-cell.inverted .macro-change.up { color: var(--red); }   /* for VIX */
  .macro-cell.inverted .macro-change.down { color: var(--green); }
  .macro-cell.warning .macro-value { color: var(--gold); }       /* for inverted yield curve */
  .macro-strip-meta {
    font-size: 10px;
    color: var(--muted);
    text-align: right;
    padding: 4px 14px 8px;
  }
</style>
</head>
<body>
<header>
  <h1>📈 Stock Sub Dashboard</h1>
  <div class="subtitle">
    <span id="last-scrape">Loading...</span>
    <button class="refresh-btn" onclick="loadData(true)">↻ Refresh</button>
    <label class="auto-refresh-toggle" title="Auto-refresh every 5 min">
      <input type="checkbox" id="auto-refresh-input" onchange="toggleAutoRefresh()">
      <span class="switch"></span>
      <span>auto</span>
    </label>
  </div>
</header>

<main>
  <div id="notice-area"></div>

  <div class="controls-bar">
    <input type="text" id="search-input" class="search-input"
           placeholder="🔍 Filter tickers by symbol or name (e.g. TSLA, AI, energy)"
           oninput="onFilterChange()" />
    <div class="timeframe-selector" role="tablist" aria-label="Sparkline timeframe">
      <button data-tf="1w" onclick="setTimeframe('1w')">1W</button>
      <button data-tf="1mo" class="active" onclick="setTimeframe('1mo')">1M</button>
      <button data-tf="3mo" onclick="setTimeframe('3mo')">3M</button>
    </div>
    <span class="filter-count" id="filter-count"></span>
  </div>

  <div class="grid" id="dashboard">
    <div class="loading">Loading dashboard data...</div>
  </div>
</main>

<div class="modal" id="modal" onclick="if(event.target===this)closeModal()">
  <div class="modal-content">
    <span class="modal-close" onclick="closeModal()">&times;</span>
    <div id="modal-body"></div>
  </div>
</div>

<div class="footer" id="footer-data">
  Data: ApeWisdom + Arctic Shift + Yahoo Finance. Cached for 1 hour. Not financial advice.
</div>
<div class="footer-meta" id="footer-meta"></div>

<script>
let dashboardData = null;

async function loadData(force = false) {
  const dash = document.getElementById('dashboard');
  if (force) {
    dash.innerHTML = renderSkeletons();
  }
  try {
    const url = force ? '/api/refresh' : '/api/stats';
    const opts = force ? { method: 'POST' } : {};
    const resp = await fetch(url, opts);
    dashboardData = await resp.json();
    if (dashboardData.error) {
      showBanner(`Refresh failed: ${escapeHtml(dashboardData.error)}`, 'error');
      return;
    }
    render(dashboardData);
    hideBanner();  // clear any prior error
  } catch (e) {
    console.error('loadData failed:', e);
    showBanner(`Couldn't reach server: ${escapeHtml(e.message || String(e))}. Showing cached data.`, 'error');
    // Keep old data visible — don't replace the page
  }
}

// ----- Banner -----

let _bannerTimer = null;
function showBanner(msg, kind = 'error', autoHideMs = 6000) {
  // Remove any existing banner
  hideBanner();
  const b = document.createElement('div');
  b.className = `banner ${kind}`;
  b.id = 'global-banner';
  b.innerHTML = `
    <span class="banner-msg">${msg}</span>
    <button class="banner-close" onclick="hideBanner()">&times;</button>
  `;
  document.body.appendChild(b);
  if (autoHideMs) {
    _bannerTimer = setTimeout(() => hideBanner(), autoHideMs);
  }
}
function hideBanner() {
  if (_bannerTimer) clearTimeout(_bannerTimer);
  _bannerTimer = null;
  const existing = document.getElementById('global-banner');
  if (existing) existing.remove();
}

// ----- Skeletons -----

function renderSkeletons() {
  // Mimic the layout with a clear progress message for the long cold-cache
  // fetch (~25-40s on first load or after TTL expiry). The 3 ⏳ indicators
  // show the user that 3 different free APIs are being hit in parallel.
  let html = `<div class="card">
    <div class="skeleton" style="width:60%;height:14px;"></div>
    <div class="skeleton" style="width:40%;"></div>
  </div>`;
  html += `<div class="card">
    <div style="margin-bottom:12px;">
      <strong style="color:var(--text);font-size:14px;">Fetching from 3 free APIs...</strong>
      <div style="font-size:11px;margin-top:6px;color:var(--muted);">
        <span class="skel-step">⏳ ApeWisdom</span> ·
        <span class="skel-step">⏳ Arctic Shift (14 subs, comments)</span> ·
        <span class="skel-step">⏳ Yahoo Finance (100+ tickers)</span>
      </div>
      <div style="font-size:11px;margin-top:8px;color:var(--muted);">
        Cold cache: ~30 seconds. After that, instant for 1 hour.
      </div>
    </div>
    <div class="ticker-grid">`;
  for (let i = 0; i < 8; i++) {
    html += `<div class="skeleton-card">
      <div class="skeleton s1"></div>
      <div class="skeleton s2"></div>
      <div class="skeleton s3"></div>
      <div class="skeleton s4"></div>
    </div>`;
  }
  html += `</div></div>`;
  return html;
}

// ----- Auto-refresh -----

let autoRefreshTimer = null;
function toggleAutoRefresh() {
  const checked = document.getElementById('auto-refresh-input').checked;
  if (checked) {
    autoRefreshTimer = setInterval(() => loadData(false), 5 * 60 * 1000);
    localStorage.setItem('auto-refresh', '1');
  } else {
    if (autoRefreshTimer) clearInterval(autoRefreshTimer);
    autoRefreshTimer = null;
    localStorage.setItem('auto-refresh', '0');
  }
}
// Restore preference on load
window.addEventListener('DOMContentLoaded', () => {
  const saved = localStorage.getItem('auto-refresh');
  if (saved === '1') {
    document.getElementById('auto-refresh-input').checked = true;
    toggleAutoRefresh();
  }
});

function renderSparkline(prices) {
  if (!prices || !prices.sparkline || prices.sparkline.length < 2) return '';
  const points = prices.sparkline;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  const w = 100, h = 28;
  // Build a smooth polyline (just straight lines, kept simple)
  const path = points.map((p, i) => {
    const x = (i / (points.length - 1)) * w;
    const y = h - ((p - min) / range) * h;
    return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const dir = prices.change_pct > 0.05 ? 'up' : prices.change_pct < -0.05 ? 'down' : 'flat';
  return `<svg class="sparkline" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <path class="${dir}" d="${path}" />
  </svg>`;
}

// ----- Search + filter state -----
let currentFilter = '';
let currentTimeframe = '1mo';

function tickerMatchesFilter(t, filter) {
  if (!filter) return true;
  const f = filter.toLowerCase().trim();
  if (!f) return true;
  return (
    t.ticker.toLowerCase().includes(f) ||
    (t.name || '').toLowerCase().includes(f)
  );
}

function onFilterChange() {
  currentFilter = document.getElementById('search-input').value;
  applyFilter();
}

function applyFilter() {
  // Update per-sub visibility
  let totalShown = 0;
  let totalAll = 0;
  if (dashboardData && dashboardData.per_sub_top) {
    for (const [sub, list] of Object.entries(dashboardData.per_sub_top)) {
      let shownInSub = 0;
      for (const t of list) {
        totalAll++;
        if (tickerMatchesFilter(t, currentFilter)) {
          shownInSub++;
          totalShown++;
        }
      }
      const section = document.querySelector(`.sub-section[data-sub="${sub}"]`);
      if (section) {
        if (shownInSub === 0 && currentFilter) {
          section.classList.add('hidden-by-filter');
        } else {
          section.classList.remove('hidden-by-filter');
        }
      }
    }
  }
  // Update per-card visibility inside each sub grid
  document.querySelectorAll('.ticker-card').forEach(card => {
    const ticker = card.getAttribute('data-ticker');
    const sub = card.getAttribute('data-sub') || '';
    const name = card.getAttribute('data-name') || '';
    const t = { ticker, name };
    if (tickerMatchesFilter(t, currentFilter)) {
      card.style.display = '';
    } else {
      card.style.display = 'none';
    }
  });
  // Update count
  const countEl = document.getElementById('filter-count');
  if (countEl) {
    if (currentFilter) {
      countEl.textContent = `${totalShown}/${totalAll} match`;
    } else {
      countEl.textContent = `${totalAll} tickers`;
    }
  }
  // Show "no results" if everything's filtered out
  if (currentFilter && totalShown === 0) {
    const dash = document.getElementById('dashboard');
    let noRes = document.getElementById('no-results-msg');
    if (!noRes) {
      noRes = document.createElement('div');
      noRes.id = 'no-results-msg';
      noRes.className = 'no-results';
      noRes.textContent = `No tickers match "${currentFilter}"`;
      dash.prepend(noRes);
    }
  } else {
    const noRes = document.getElementById('no-results-msg');
    if (noRes) noRes.remove();
  }
}

// ----- Timeframe selector -----
let timeframeData = { '1w': null, '1mo': null, '3mo': null };
let timeframeLoading = false;

async function setTimeframe(tf) {
  if (tf === currentTimeframe) return;
  currentTimeframe = tf;
  // Update button states
  document.querySelectorAll('.timeframe-selector button').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-tf') === tf);
  });
  // Fetch if we don't have it cached client-side
  if (!timeframeData[tf]) {
    try {
      const resp = await fetch(`/api/prices?timeframe=${tf}`);
      if (resp.ok) {
        timeframeData[tf] = await resp.json();
      }
    } catch (e) {
      console.error('Timeframe fetch failed:', e);
      return;
    }
  }
  // Swap into dashboardData.prices and re-render
  if (timeframeData[tf] && dashboardData) {
    dashboardData.prices = timeframeData[tf];
    render(dashboardData);
  }
}

function renderTickerCard(t, opts = {}) {
  const prices = dashboardData.prices || {};
  const tickerPosts = (dashboardData.ticker_posts || {})[t.ticker] || [];
  const p = prices[t.ticker];
  const isStarred = isInWatchlist(t.ticker);
  const priceHtml = p ? `
    <div class="price-row">
      <span class="price">$${p.price.toFixed(2)}</span>
      <span class="change ${p.change_pct > 0.05 ? 'up' : p.change_pct < -0.05 ? 'down' : 'flat'}">
        ${p.change_pct > 0 ? '+' : ''}${p.change_pct.toFixed(2)}%
      </span>
    </div>
    ${renderSparkline(p)}
  ` : `<div class="no-price">price unavailable</div>`;
  const cardClick = `onclick="openTicker('${t.ticker}')"`;
  // Why-it's-trending section: top 1-2 post titles, or placeholder if none
  let whyHtml;
  if (tickerPosts.length > 0) {
    whyHtml = `
      <div class="why-trending" onclick="event.stopPropagation()">
        <div class="why-label">Why it's trending</div>
        ${tickerPosts.slice(0, 2).map(post => {
          // Smart truncate: cut at word boundary, not mid-word
          let title = post.title;
          const MAX = 56;
          if (title.length > MAX) {
            const cut = title.slice(0, MAX);
            const lastSpace = cut.lastIndexOf(' ');
            title = (lastSpace > MAX * 0.6 ? cut.slice(0, lastSpace) : cut) + '…';
          }
          return `
            <a class="why-post" href="${post.permalink}" target="_blank" rel="noopener">
              ${escapeHtml(title)}
              <div class="meta">
                ${post.is_comment ? '💬' : '📝'}
                r/${escapeHtml(post.subreddit)} ·
                <span class="score-num">▲ ${post.score}</span>
                ${post.is_comment ? ' · comment' : ''}
              </div>
            </a>
          `;
        }).join('')}
      </div>
    `;
  } else {
    // Reserve the same vertical space so cards align in the grid
    // Explain WHY this happens - it's a real signal, not a bug
    whyHtml = `
      <div class="why-trending empty" onclick="event.stopPropagation()">
        <div class="why-label no-data">Why it's trending</div>
        <div style="font-size: 10px; color: var(--muted); font-style: italic; line-height: 1.4;">
          being mentioned across subs but no dedicated posts this week
        </div>
      </div>
    `;
  }
  return `
    <div class="ticker-card" data-ticker="${t.ticker}" data-sub="${opts.sub || ''}" data-name="${escapeHtml(t.name || '')}">
      <span class="star ${isStarred ? 'starred' : ''}" data-ticker="${t.ticker}"
            onclick="event.stopPropagation(); toggleStar('${t.ticker}')"
            title="${isStarred ? 'Remove from watchlist' : 'Add to watchlist'}">
        ${isStarred ? '★' : '☆'}
      </span>
      <div style="padding-right: 20px;" ${cardClick}>
        <div class="sym">$${t.ticker}</div>
        <div class="name">${escapeHtml((t.name || '').slice(0, 22))}</div>
      </div>
      <div style="padding-right: 20px;" ${cardClick}>
        ${priceHtml}
      </div>
      <div style="padding-right: 20px; margin-top: 4px; font-size: 11px; color: var(--muted);" ${cardClick}>
        ${t.mentions} mentions · ${t.upvotes || 0} ▲
      </div>
      ${whyHtml}
    </div>
  `;
}

// ----- Watchlist (localStorage) -----

const WATCHLIST_KEY = 'stock-sub-watchlist-v1';

function getWatchlist() {
  try {
    return JSON.parse(localStorage.getItem(WATCHLIST_KEY) || '[]');
  } catch { return []; }
}
function saveWatchlist(list) {
  localStorage.setItem(WATCHLIST_KEY, JSON.stringify(list));
}
function isInWatchlist(ticker) {
  return getWatchlist().includes(ticker);
}
function toggleStar(ticker) {
  const list = getWatchlist();
  const i = list.indexOf(ticker);
  if (i >= 0) list.splice(i, 1);
  else list.push(ticker);
  saveWatchlist(list);
  // Re-render the affected card only
  const card = document.querySelector(`.ticker-card[data-ticker="${ticker}"]`);
  if (card) {
    const sub = card.closest('.sub-section, .watchlist-section');
    if (sub && sub.classList.contains('watchlist-section')) {
      render();  // watchlist changed, re-render the watchlist section
    } else {
      // just update the star icon in this card
      const star = card.querySelector('.star');
      if (star) {
        const starred = isInWatchlist(ticker);
        star.classList.toggle('starred', starred);
        star.textContent = starred ? '★' : '☆';
        star.title = starred ? 'Remove from watchlist' : 'Add to watchlist';
      }
    }
  } else {
    render();
  }
}

// ----- Macro strip -----

function formatMacroValue(key, value) {
  // Different formatting for different series
  if (key === '10Y' || key === '5Y' || key === '30Y') return value.toFixed(2) + '%';
  if (key === 'CURVE_10Y5Y') return (value >= 0 ? '+' : '') + value.toFixed(2) + '%';
  if (key === 'VIX') return value.toFixed(2);
  if (key === 'OIL') return '$' + value.toFixed(2);
  if (key === 'SPX' || key === 'IXIC') return value.toLocaleString('en-US', { maximumFractionDigits: 1 });
  if (key === 'GOLD') return '$' + value.toLocaleString('en-US', { maximumFractionDigits: 0 });
  if (key === 'DXY') return value.toFixed(2);
  return String(value);
}

function renderMacroStrip(macro) {
  if (!macro) return '';
  // Order: rates first, then equities, then commodities, then derived
  const order = ['10Y', '5Y', '30Y', 'CURVE_10Y5Y', 'VIX', 'SPX', 'IXIC', 'GOLD', 'OIL', 'DXY'];
  const cells = [];
  for (const key of order) {
    const m = macro[key];
    if (!m) continue;
    const changePct = m.change_pct;
    let changeClass = 'flat';
    let changeText = '';
    if (changePct !== undefined && changePct !== null) {
      if (changePct > 0.05) changeClass = 'up';
      else if (changePct < -0.05) changeClass = 'down';
      changeText = `${changePct > 0 ? '+' : ''}${changePct.toFixed(2)}%`;
    }
    // Inverted-color cells: VIX (down=good) and yield spread (down=warning)
    const isInverted = (key === 'VIX');
    const isWarning = (key === 'CURVE_10Y5Y' && m.value < 0);
    cells.push(`
      <div class="macro-cell ${isInverted ? 'inverted' : ''} ${isWarning ? 'warning' : ''}">
        <div class="macro-label">${escapeHtml(m.label)}</div>
        <div class="macro-value">${formatMacroValue(key, m.value)}</div>
        ${changeText ? `<div class="macro-change ${changeClass}">${changeText}</div>` : ''}
      </div>
    `);
  }
  if (cells.length === 0) return '';
  return `
    <div class="macro-strip">
      ${cells.join('')}
    </div>
  `;
}

function renderEarningsCard(earnings) {
  // Show upcoming earnings for tracked tickers, sorted by date
  // earnings is {ticker: [event, ...]} dict
  const all = [];
  for (const ticker in (earnings || {})) {
    for (const ev of earnings[ticker]) {
      all.push({ticker, ...ev});
    }
  }
  all.sort((a, b) => (a.date || '').localeCompare(b.date || ''));
  if (all.length === 0) {
    return `<div class="card">
      <h2>📅 Upcoming Earnings</h2>
      <div class="empty" style="padding:14px 0;">No earnings for tracked tickers in the next 14 days.</div>
    </div>`;
  }
  // Take the first 8
  const top = all.slice(0, 8);
  return `<div class="card grid-full">
    <h2>📅 Upcoming Earnings (next 14 days)</h2>
    <div style="overflow-x:auto;">
      <table class="earnings-table">
        <thead>
          <tr>
            <th>Date</th>
            <th>Ticker</th>
            <th>Company</th>
            <th>When</th>
            <th>EPS Est.</th>
            <th># Est.</th>
          </tr>
        </thead>
        <tbody>
          ${top.map(ev => {
            const d = new Date(ev.date + 'T00:00:00');
            const dayName = d.toLocaleDateString('en-US', { weekday: 'short' });
            const dateStr = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
            const isToday = ev.date === new Date().toISOString().slice(0, 10);
            return `<tr>
              <td><strong>${dateStr}</strong> <span style="color:var(--muted);font-size:11px;">${dayName}${isToday ? ' (TODAY)' : ''}</span></td>
              <td><a href="/ticker/${ev.ticker}" style="color:var(--accent);text-decoration:none;font-weight:600;">$${escapeHtml(ev.ticker)}</a></td>
              <td style="color:var(--muted);">${escapeHtml((ev.name || '').slice(0, 28))}</td>
              <td style="color:var(--muted);font-size:12px;">${escapeHtml(ev.when || 'Time TBD')}</td>
              <td style="font-variant-numeric:tabular-nums;">${escapeHtml(ev.eps_forecast || '—')}</td>
              <td style="color:var(--muted);">${escapeHtml(ev.no_of_ests || '—')}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
    ${all.length > 8 ? `<div style="text-align:center;color:var(--muted);font-size:12px;margin-top:8px;">+${all.length - 8} more</div>` : ''}
  </div>`;
}

function renderHeatmap(prices, perSubTop) {
  // Heatmap of all tracked tickers colored by % change.
  // Size = mentions (bigger = more talked about).
  // Color = % change (red=down, green=up, intensity = magnitude).
  if (!prices || Object.keys(prices).length === 0) return '';
  // Collect all tickers with their mention counts
  const mentionCounts = {};
  for (const sub in (perSubTop || {})) {
    for (const t of (perSubTop[sub] || [])) {
      mentionCounts[t.ticker] = (mentionCounts[t.ticker] || 0) + (t.mentions || 0);
    }
  }
  // Filter to tickers we have prices for
  const items = Object.keys(prices)
    .filter(t => prices[t] && prices[t].price)
    .map(t => ({
      ticker: t,
      price: prices[t].price,
      change: prices[t].change_pct,
      mentions: mentionCounts[t] || 0,
    }))
    .sort((a, b) => b.mentions - a.mentions)
    .slice(0, 50);  // top 50 by mentions

  if (items.length === 0) return '';
  // Find min/max for sizing
  const maxMentions = Math.max(1, ...items.map(i => i.mentions));

  return `<div class="card grid-full">
    <h2>🔥 Market Heatmap</h2>
    <div class="heatmap-grid">
      ${items.map(i => {
        const intensity = Math.min(1, Math.abs(i.change || 0) / 5);  // cap at 5%
        const opacity = 0.3 + intensity * 0.7;
        const bg = (i.change || 0) >= 0
          ? `rgba(63, 185, 80, ${opacity})`     // green for up
          : `rgba(248, 81, 73, ${opacity})`;     // red for down
        const size = 70 + (i.mentions / maxMentions) * 40;  // 70-110px
        const changeStr = (i.change >= 0 ? '+' : '') + (i.change || 0).toFixed(2) + '%';
        return `<a href="/ticker/${i.ticker}" class="heatmap-cell" style="background:${bg};width:${size}px;height:${size}px;">
          <div class="heatmap-sym">$${escapeHtml(i.ticker)}</div>
          <div class="heatmap-chg">${changeStr}</div>
        </a>`;
      }).join('')}
    </div>
  </div>`;
}

function renderWatchlistSection() {
  const watchlist = getWatchlist();
  if (watchlist.length === 0) {
    return `
      <div class="card watchlist-section">
        <h2>⭐ My Watchlist</h2>
        <div class="watchlist-empty">
          Tap the ☆ on any ticker to add it here. Your watchlist is saved on this device only.
        </div>
      </div>
    `;
  }
  // Build a card for each watched ticker from the latest data
  const prices = (dashboardData && dashboardData.prices) || {};
  const cards = [];
  // Gather from per_sub_top (any sub) + cross_sub_leaderboard
  const seen = new Set();
  const allKnown = {};
  for (const [sub, list] of Object.entries(dashboardData.per_sub_top || {})) {
    for (const t of list) {
      if (watchlist.includes(t.ticker) && !allKnown[t.ticker]) {
        allKnown[t.ticker] = t;
      }
    }
  }
  for (const t of (dashboardData.cross_sub_leaderboard || [])) {
    if (watchlist.includes(t.ticker) && !allKnown[t.ticker]) {
      allKnown[t.ticker] = { ticker: t.ticker, name: t.name, mentions: t.total_mentions, upvotes: t.total_upvotes };
    }
  }
  for (const ticker of watchlist) {
    const t = allKnown[ticker] || { ticker, name: '', mentions: 0, upvotes: 0 };
    cards.push(renderTickerCard(t, {sub: 'watchlist'}));
  }
  return `
    <div class="card watchlist-section">
      <h2>⭐ My Watchlist (${watchlist.length})</h2>
      <div class="ticker-grid">
        ${cards.join('')}
      </div>
    </div>
  `;
}

function render(d) {
  // Defensive defaults so a partial payload never crashes the whole render
  d = d || {};
  d.trending = d.trending || [];
  d.cross_sub_leaderboard = d.cross_sub_leaderboard || [];
  d.per_sub_top = d.per_sub_top || {};
  d.top_posts = d.top_posts || [];
  d.per_sub_posts = d.per_sub_posts || [];
  d.freshness = d.freshness || [];

  const lastScrape = d.last_scrape
    ? new Date(d.last_scrape).toLocaleString()
    : 'Never';
  const age = d.last_scrape
    ? Math.round((Date.now() - new Date(d.last_scrape).getTime()) / 1000 / 60)
    : null;
  let ageStr = '';
  if (age !== null) {
    if (age < 1) ageStr = ' (just now)';
    else if (age < 60) ageStr = ` (${age}m ago)`;
    else ageStr = ` (${Math.round(age/60)}h ago)`;
  }
  let headerText = `Updated: ${lastScrape}${ageStr}`;
  let staleBadge = '';
  if (age !== null && age > 30) {
    // Data is more than 30 min old — show a "stale" indicator
    staleBadge = '<span class="stale-badge" title="Data is more than 30 minutes old. Click Refresh to update.">stale</span>';
  }
  if (d.force_refreshed) {
    headerText += '  (just refreshed)';
  }
  document.getElementById('last-scrape').innerHTML = headerText + staleBadge;

  // Footer meta
  const fm = document.getElementById('footer-meta');
  if (fm && d.last_scrape) {
    const fetchSecs = d.fetch_time_seconds || 0;
    const ageMin = age !== null ? Math.round(age) : '?';
    fm.innerHTML = `Data fetched in ${fetchSecs}s · Cached, ${ageMin}m old · <a href="/api/stats" style="color:var(--muted)">API</a> · <a href="https://github.com/godlynot/stock-sub-dashboard" style="color:var(--muted)" target="_blank">GitHub</a>`;
  }

  // Notice for first cold load
  const noticeArea = document.getElementById('notice-area');
  if (d.fetch_time_seconds && d.fetch_time_seconds > 5) {
    noticeArea.innerHTML = `<div class="notice">Cold cache refresh took ${d.fetch_time_seconds}s. Future loads will be instant (cached for 1h).</div>`;
  } else {
    noticeArea.innerHTML = '';
  }

  const html = `
    ${renderMacroStrip(d.macro)}
    ${renderHeatmap(d.prices, d.per_sub_top)}
    ${renderEarningsCard(d.earnings)}
    ${renderWatchlistSection()}

    <div class="card">
      <h2>🔥 Trending (mentions Δ vs 24h)</h2>
      ${d.trending.length === 0
        ? '<div class="empty">No trending data yet.</div>'
        : d.trending.slice(0, 12).map(t => `
            <div class="row">
              <span class="lbl">
                <strong>$${t.ticker}</strong>
                <span style="color:var(--muted);font-size:11px;">${escapeHtml((t.name || '').slice(0, 28))}</span>
              </span>
              <span class="val">
                ${t.mentions_now}
                ${t.delta > 0
                  ? `<span class="delta-up">▲${t.delta}</span>`
                  : t.delta < 0
                    ? `<span class="delta-down">▼${Math.abs(t.delta)}</span>`
                    : ''}
              </span>
            </div>
          `).join('')
      }
    </div>

    <div class="card">
      <h2>🏆 Cross-Sub Leaderboard</h2>
      ${d.cross_sub_leaderboard.length === 0
        ? '<div class="empty">No data yet.</div>'
        : d.cross_sub_leaderboard.slice(0, 12).map(t => `
            <div class="row" onclick="openTicker('${t.ticker}')" style="cursor:pointer;">
              <span class="lbl">
                <strong>$${t.ticker}</strong>
                <span style="color:var(--muted);font-size:11px;">${escapeHtml((t.name || '').slice(0, 28))}</span>
              </span>
              <span class="val">
                ${t.total_mentions}
                <span style="color:var(--muted);font-size:11px;">in ${t.sub_count} subs</span>
              </span>
            </div>
          `).join('')
      }
    </div>

    <div class="card grid-full">
      <h2>📊 Per-Subreddit Top Tickers</h2>
      ${Object.keys(d.per_sub_top).length === 0
        ? '<div class="empty">No data yet.</div>'
        : Object.keys(d.per_sub_top).sort().map(sub => `
            <div class="sub-section" data-sub="${sub}">
              <h3>r/${sub}</h3>
              <div class="ticker-grid">
                ${d.per_sub_top[sub].map(t => renderTickerCard(t, {sub: sub})).join('')}
              </div>
            </div>
          `).join('')
      }
    </div>

    <div class="card grid-full">
      <h2>🌟 Top Recent Posts</h2>
      ${d.top_posts.length === 0
        ? '<div class="empty">No posts yet.</div>'
        : d.top_posts.slice(0, 20).map(p => `
            <div class="post">
              <a class="post-title" href="${p.permalink}" target="_blank" rel="noopener">
                ${escapeHtml(p.title)}
              </a>
              <div class="post-meta">
                <span class="sub">r/${p.subreddit}</span>
                <span class="score">▲ ${p.score}</span>
                <span class="sub">💬 ${p.num_comments}</span>
                <span style="color:var(--muted);">u/${escapeHtml(p.author || '')}</span>
              </div>
            </div>
          `).join('')
      }
    </div>

    <div class="card">
      <h2>💬 Sub Activity</h2>
      ${d.per_sub_posts.length === 0
        ? '<div class="empty">No posts yet.</div>'
        : d.per_sub_posts.map(s => `
            <div class="stat-line">
              <span class="lbl">r/${s.subreddit}</span>
              <span class="val">${s.posts} posts · ${s.total_comments} comments</span>
            </div>
          `).join('')
      }
    </div>
  `;

  document.getElementById('dashboard').innerHTML = html;
  // Re-apply any active search filter to the newly-rendered cards
  applyFilter();
  // Flash price colors when they change (up=green, down=red) for 0.6s
  flashPriceChanges();
}

const _lastPrices = {};
function flashPriceChanges() {
  // For each ticker card, compare new price to last seen price, flash color
  const cards = document.querySelectorAll('.ticker-card[data-ticker]');
  cards.forEach(card => {
    const ticker = card.getAttribute('data-ticker');
    const priceEl = card.querySelector('.price');
    if (!priceEl) return;
    const text = priceEl.textContent.replace(/[$,]/g, '');
    const newPrice = parseFloat(text);
    if (isNaN(newPrice)) return;
    if (_lastPrices[ticker] !== undefined && _lastPrices[ticker] !== newPrice) {
      const dir = newPrice > _lastPrices[ticker] ? 'flash-up' : 'flash-down';
      priceEl.classList.add(dir);
      setTimeout(() => priceEl.classList.remove(dir), 800);
    }
    _lastPrices[ticker] = newPrice;
  });
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

async function openTicker(ticker) {
  const modal = document.getElementById('modal');
  document.getElementById('modal-body').innerHTML = '<div class="loading">Loading...</div>';
  modal.classList.add('active');
  try {
    const resp = await fetch(`/api/ticker/${encodeURIComponent(ticker)}`);
    const data = await resp.json();
    if (data.error) {
      document.getElementById('modal-body').innerHTML = `<div class="empty">${escapeHtml(data.error)}</div>`;
      return;
    }
    // Add "open full page" link in the modal header
    document.getElementById('modal-body').innerHTML =
      `<div class="modal-header-link"><a href="/ticker/${encodeURIComponent(ticker)}" target="_blank">↗ Open full page</a></div>` +
      renderTickerDetailHTML(data);
  } catch (e) {
    document.getElementById('modal-body').innerHTML = '<div class="empty">Error loading.</div>';
  }
}

function renderTickerDetailHTML(data) {
  const latest = data.latest_per_sub;
  const posts = data.recent_posts;
  const prices = dashboardData && dashboardData.prices ? dashboardData.prices[data.ticker] : null;
  let html = '';

  // Price + change at the top of the detail view (only on the dedicated page;
  // the modal already has it visible from the underlying card)
  if (prices) {
    const dir = prices.change_pct > 0.05 ? 'up' : prices.change_pct < -0.05 ? 'down' : 'flat';
    html += `
      <div class="ticker-hero">
        <div class="hero-sym">$${escapeHtml(data.ticker)}</div>
        <div class="hero-price">$${prices.price.toFixed(2)}</div>
        <div class="hero-change ${dir}">${prices.change_pct > 0 ? '+' : ''}${prices.change_pct.toFixed(2)}%</div>
      </div>
    `;
  } else {
    html += `<h2 style="margin-top:0;">$${escapeHtml(data.ticker)}</h2>`;
  }

  if (latest.length > 0) {
    html += `<div style="margin-top:18px;"><strong style="color:var(--muted);font-size:12px;text-transform:uppercase;">By Subreddit (latest)</strong>`;
    latest.forEach(l => {
      const delta = l.mentions_24h_ago != null ? (l.mentions - l.mentions_24h_ago) : null;
      html += `
        <div class="row">
          <span class="lbl">r/${escapeHtml(l.subreddit)}</span>
          <span class="val">
            ${l.mentions} mentions
            ${delta != null
              ? (delta > 0
                  ? `<span class="delta-up">▲${delta}</span>`
                  : delta < 0
                    ? `<span class="delta-down">▼${Math.abs(delta)}</span>`
                    : '')
              : ''}
          </span>
        </div>
      `;
    });
    html += `</div>`;
  }

  if (posts.length > 0) {
    html += `<div style="margin-top:18px;"><strong style="color:var(--muted);font-size:12px;text-transform:uppercase;">Recent Posts (${posts.length})</strong>`;
    posts.slice(0, 20).forEach(p => {
      const isComment = p.is_comment;
      html += `
        <div class="post">
          <a class="post-title" href="${p.permalink}" target="_blank" rel="noopener">
            ${escapeHtml(p.title)}
          </a>
          <div class="post-meta">
            ${isComment ? '💬' : '📝'}
            r/${escapeHtml(p.subreddit)} ·
            <span class="score">▲ ${p.score}</span>
            ${!isComment ? `· 💬 ${p.num_comments || 0}` : ' · comment'}
          </div>
        </div>
      `;
    });
    html += `</div>`;
  }

  if (latest.length === 0 && posts.length === 0) {
    html += '<div class="empty">No data for this ticker right now.</div>';
  }
  return html;
}

function closeModal() {
  document.getElementById('modal').classList.remove('active');
}

loadData();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(TEMPLATE)


# ----------------------------------------------------------------------------
# Dedicated /ticker/<T> page
# ----------------------------------------------------------------------------

# Same as TEMPLATE but without the dashboard grid, with a ticker detail body
TICKER_PAGE_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TICKER__ — Stock Sub Dashboard</title>
<style>
  :root {
    --bg: #0d1117;
    --panel: #161b22;
    --panel-2: #1c2128;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #58a6ff;
    --green: #3fb950;
    --red: #f85149;
    --gold: #d29922;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  header {
    padding: 18px 20px;
    background: linear-gradient(135deg, #1f6feb 0%, #58a6ff 100%);
    box-shadow: 0 2px 8px rgba(0,0,0,0.4);
  }
  h1 { margin: 0 0 4px 0; font-size: 22px; }
  .subtitle { color: rgba(255,255,255,0.85); font-size: 13px; }
  main { padding: 16px 20px 60px; max-width: 800px; margin: 0 auto; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 16px; }
  .row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid var(--border); gap: 10px; }
  .row:last-child { border-bottom: none; }
  .row .lbl { color: var(--text); }
  .row .val { color: var(--accent); font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .delta-up { color: var(--green); font-size: 11px; }
  .delta-down { color: var(--red); font-size: 11px; }
  .post { padding: 12px 0; border-bottom: 1px solid var(--border); }
  .post:last-child { border-bottom: none; }
  .post-title { font-size: 15px; font-weight: 500; color: var(--text); text-decoration: none; display: block; margin-bottom: 6px; line-height: 1.4; }
  .post-title:hover { color: var(--accent); }
  .post-meta { font-size: 12px; color: var(--muted); display: flex; flex-wrap: wrap; gap: 8px; }
  .post-meta .sub { color: var(--accent); font-weight: 500; }
  .post-meta .score { color: var(--green); }
  .ticker-hero {
    display: flex;
    align-items: baseline;
    gap: 14px;
    padding: 8px 0 16px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 8px;
    flex-wrap: wrap;
  }
  .ticker-hero .hero-sym {
    font-size: 28px;
    font-weight: 700;
    color: var(--accent);
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
  }
  .ticker-hero .hero-price {
    font-size: 24px;
    font-weight: 600;
    color: var(--text);
    font-variant-numeric: tabular-nums;
  }
  .ticker-hero .hero-change {
    font-size: 14px;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: 4px;
    font-variant-numeric: tabular-nums;
  }
  .ticker-hero .hero-change.up { color: var(--green); background: rgba(63, 185, 80, 0.15); }
  .ticker-hero .hero-change.down { color: var(--red); background: rgba(248, 81, 73, 0.15); }
  .ticker-hero .hero-change.flat { color: var(--muted); background: rgba(139, 148, 158, 0.15); }
  .back-link {
    display: inline-block;
    margin-bottom: 14px;
    color: var(--accent);
    text-decoration: none;
    font-size: 13px;
  }
  .back-link:hover { text-decoration: underline; }
  .empty { color: var(--muted); font-style: italic; padding: 20px 0; text-align: center; }
  .loading { text-align: center; padding: 40px; color: var(--muted); }
  .footer { text-align: center; color: var(--muted); font-size: 11px; padding: 20px; border-top: 1px solid var(--border); margin-top: 30px; }
</style>
</head>
<body>
<header>
  <h1>📈 __TICKER__</h1>
  <div class="subtitle">Stock Sub Dashboard</div>
</header>

<main>
  <a href="/" class="back-link">← Back to dashboard</a>
  <div class="card" id="ticker-content">
    <div class="loading">Loading...</div>
  </div>
</main>

<div class="footer">
  Data: ApeWisdom + Arctic Shift + Yahoo Finance. Not financial advice.
</div>

<script>
let dashboardData = null;

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function renderTickerDetailHTML(data) {
  const latest = data.latest_per_sub;
  const posts = data.recent_posts;
  const prices = dashboardData && dashboardData.prices ? dashboardData.prices[data.ticker] : null;
  let html = '';

  if (prices) {
    const dir = prices.change_pct > 0.05 ? 'up' : prices.change_pct < -0.05 ? 'down' : 'flat';
    html += `
      <div class="ticker-hero">
        <div class="hero-sym">$${escapeHtml(data.ticker)}</div>
        <div class="hero-price">$${prices.price.toFixed(2)}</div>
        <div class="hero-change ${dir}">${prices.change_pct > 0 ? '+' : ''}${prices.change_pct.toFixed(2)}%</div>
      </div>
    `;
  } else {
    html += `<h2 style="margin-top:0;">$${escapeHtml(data.ticker)}</h2>`;
  }

  if (latest.length > 0) {
    html += `<div style="margin-top:18px;"><strong style="color:var(--muted);font-size:12px;text-transform:uppercase;">By Subreddit (latest)</strong>`;
    latest.forEach(l => {
      const delta = l.mentions_24h_ago != null ? (l.mentions - l.mentions_24h_ago) : null;
      html += `
        <div class="row">
          <span class="lbl">r/${escapeHtml(l.subreddit)}</span>
          <span class="val">
            ${l.mentions} mentions
            ${delta != null
              ? (delta > 0
                  ? `<span class="delta-up">▲${delta}</span>`
                  : delta < 0
                    ? `<span class="delta-down">▼${Math.abs(delta)}</span>`
                    : '')
              : ''}
          </span>
        </div>
      `;
    });
    html += `</div>`;
  }

  if (posts.length > 0) {
    html += `<div style="margin-top:18px;"><strong style="color:var(--muted);font-size:12px;text-transform:uppercase;">Recent Posts (${posts.length})</strong>`;
    posts.slice(0, 30).forEach(p => {
      const isComment = p.is_comment;
      html += `
        <div class="post">
          <a class="post-title" href="${p.permalink}" target="_blank" rel="noopener">
            ${escapeHtml(p.title)}
          </a>
          <div class="post-meta">
            ${isComment ? '💬' : '📝'}
            r/${escapeHtml(p.subreddit)} ·
            <span class="score">▲ ${p.score}</span>
            ${!isComment ? `· 💬 ${p.num_comments || 0}` : ' · comment'}
          </div>
        </div>
      `;
    });
    html += `</div>`;
  }

  if (latest.length === 0 && posts.length === 0) {
    html += '<div class="empty">No data for this ticker right now.</div>';
  }
  return html;
}

async function loadTicker() {
  const ticker = '__TICKER__';
  try {
    // Load prices in background (for hero)
    fetch('/api/stats').then(r => r.json()).then(d => { dashboardData = d; refreshHero(); });
    // Load ticker details
    const resp = await fetch(`/api/ticker/${encodeURIComponent(ticker)}`);
    const data = await resp.json();
    if (data.error) {
      document.getElementById('ticker-content').innerHTML = `<div class="empty">${escapeHtml(data.error)}</div>`;
      return;
    }
    window._tickerData = data;
    document.getElementById('ticker-content').innerHTML = renderTickerDetailHTML(data);
  } catch (e) {
    document.getElementById('ticker-content').innerHTML = '<div class="empty">Error loading.</div>';
  }
}

function refreshHero() {
  // Re-render once prices arrive so the hero appears
  if (window._tickerData) {
    document.getElementById('ticker-content').innerHTML = renderTickerDetailHTML(window._tickerData);
  }
}

loadTicker();
</script>
</body>
</html>
"""


@app.route("/ticker/<ticker>")
def ticker_page(ticker: str):
    """Dedicated page for a single ticker. Shareable, mobile-friendly."""
    safe = ticker.upper().lstrip("$")[:10]  # sanitize
    return render_template_string(
        TICKER_PAGE_TEMPLATE.replace("__TICKER__", safe)
    )


# ----------------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    # threaded=True so multiple users can hit /api/stats simultaneously
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)