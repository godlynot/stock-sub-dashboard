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
from datetime import datetime, timezone
from typing import Any

import requests
from flask import Flask, jsonify, render_template_string, request

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))  # 1 hour for ticker data
PRICE_CACHE_TTL = int(os.getenv("PRICE_CACHE_TTL", "300"))  # 5 min for prices
APEWISDOM_TIMEOUT = int(os.getenv("APEWISDOM_TIMEOUT", "20"))
ARCTIC_TIMEOUT = int(os.getenv("ARCTIC_TIMEOUT", "30"))
YAHOO_TIMEOUT = int(os.getenv("YAHOO_TIMEOUT", "10"))
POSTS_PER_SUB = int(os.getenv("POSTS_PER_SUB", "100"))  # max from Arctic Shift per call
POST_LOOKBACK_DAYS = int(os.getenv("POST_LOOKBACK_DAYS", "7"))
TOP_POSTS_PER_TICKER = int(os.getenv("TOP_POSTS_PER_TICKER", "3"))

# Subreddits we track. ApeWisdom covers all of these with rankings.
# Arctic Shift has good coverage for all except r/wallstreetbets (sparse).
DEFAULT_SUBS = [
    "wallstreetbets",
    "stocks",
    "investing",
    "options",
    "StockMarket",
    "pennystocks",
    "SPACs",
]

SUBS = [
    s.strip()
    for s in os.getenv("SUBS", ",".join(DEFAULT_SUBS)).split(",")
    if s.strip()
]

# Subs we fetch post content for. WSB omitted — Arctic Shift has sparse WSB data.
POST_SUBS = [
    s.strip()
    for s in os.getenv("POST_SUBS", "stocks,investing,options,StockMarket,pennystocks,SPACs").split(",")
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
    Missing tickers get an empty dict (or just absent from result).
    """
    out: dict[str, dict] = {}
    interval, range_ = TIMEFRAMES.get(timeframe, TIMEFRAMES["1mo"])
    for ticker in tickers:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            params = {"interval": interval, "range": range_}
            resp = _yahoo_session.get(url, params=params, timeout=YAHOO_TIMEOUT)
            if resp.status_code != 200:
                continue
            data = resp.json()
            chart = data.get("chart", {}).get("result", [None])[0]
            if not chart:
                continue
            meta = chart.get("meta", {})
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price is None:
                continue
            change_pct = ((price - prev) / prev * 100) if prev else 0
            closes = (
                chart.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            )
            # Filter out None values that Yahoo sometimes returns
            sparkline = [round(c, 2) for c in closes if c is not None]
            out[ticker] = {
                "price": round(price, 2),
                "prev_close": round(prev, 2) if prev else None,
                "change_pct": round(change_pct, 2),
                "currency": meta.get("currency", "USD"),
                "name": meta.get("longName") or meta.get("shortName") or ticker,
                "sparkline": sparkline,
                "timeframe": timeframe,
            }
        except Exception as e:
            print(f"[yahoo] error for {ticker}: {e}", flush=True)
            continue
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


def fetch_arctic_posts(sub: str) -> list[dict]:
    """Fetch recent top posts for one subreddit from Arctic Shift."""
    now = int(time.time())
    after = now - POST_LOOKBACK_DAYS * 86400
    url = "https://arctic-shift.photon-reddit.com/api/posts/search"
    params = {
        "subreddit": sub,
        "limit": 100,
        "after": after,
        "sort": "desc",
    }
    data = _get_json(url, params=params, timeout=ARCTIC_TIMEOUT)
    if not data or "data" not in data:
        return []
    # Sort by score client-side (Arctic Shift doesn't support sort=score)
    posts = data["data"]
    posts.sort(key=lambda p: p.get("score", 0), reverse=True)
    return posts[:POSTS_PER_SUB]


def fetch_all_arctic_posts() -> dict[str, list[dict]]:
    """Fetch posts for all configured POST_SUBS."""
    out: dict[str, list[dict]] = {}
    for sub in POST_SUBS:
        out[sub] = fetch_arctic_posts(sub)
        time.sleep(0.5)
    return out


# ----------------------------------------------------------------------------
# Ticker extraction (server-side, for the "why is it popular" index)
# ----------------------------------------------------------------------------

import re

# Common WSB / stock-sub false-positives to filter out of bare-ticker matches
COMMON_FALSE_POSITIVES = {
    "I", "A", "AN", "AT", "BE", "BY", "DO", "GO", "HE", "IF", "IN", "IS",
    "IT", "ME", "MY", "NO", "OF", "OH", "OK", "ON", "OR", "SO", "TO", "UP",
    "US", "WE", "ALL", "AND", "ARE", "BIG", "BUY", "CAN", "DD", "END", "ERA",
    "FOR", "FUN", "GAIN", "GDP", "GET", "GOD", "GOT", "HAS", "HOD", "HOW",
    "IMO", "IPO", "ITM", "LOSS", "LOW", "MAN", "MOM", "NEW", "NOT", "NOW",
    "OLD", "ONE", "OTM", "OUT", "OWN", "PAY", "PE", "PER", "PUT", "RE",
    "ROI", "RSI", "RUN", "SAY", "SEE", "SPY", "SOLD", "STONK", "STUDY",
    "THE", "TOO", "TOP", "TRY", "USA", "USE", "VERY", "WANT", "WAS", "WAY",
    "WIN", "WITH", "YOLO", "YOU", "YOUR", "MOON", "TICKER", "STOCK", "SHARES",
    "PRICE", "TODAY", "WEEK", "YEAR", "GOOD", "BAD", "BEST", "HOPE", "FEEL",
    "LOOK", "REAL", "LOVE", "HATE", "JUST", "LIKE", "MAKE", "MANY", "MUCH",
    "OVER", "RIDE", "SOME", "THAN", "THAT", "THEM", "THEN", "THEY", "THIS",
    "TIME", "WHAT", "WHEN", "WILL", "WORK", "YALL", "ETF", "CEO", "CFO",
    "OTC", "ATH", "ATL", "FD", "DCA", "YOY", "QOQ", "EOD", "EOW", "EOY",
    "FED", "FDX", "UPS", "PCE", "CPI", "PPI", "OPEC", "IIRC", "TBH", "IMO",
    "ELI", "PSA", "HSA", "IRA", "RMD", "USD", "EUR", "GBP", "JPY", "CAD",
    "AUD", "NYT", "WSJ", "CNBC", "BND", "DTE", "AMC", "CEO", "GDP",
    "HOLD", "GAIN", "PUMP", "DUMP", "MOON", "RUG", "FUD", "FOMO", "BTD",
    "ATH", "ATL", "BTD", "BTFD", "FD", "ITM", "OTM", "ATM", "DTE", "IV",
    "IV", "OPRA", "MEME", "APE", "DIAMOND", "HANDS", "PAPER", "TENDIES",
    "BAGHOLDER", "AUTIST", "SIR", "JACK", "POUND", "FLOOR", "CEILING",
}


def extract_tickers(text: str) -> set[str]:
    """Extract stock ticker mentions from text. Returns a set of uppercase tickers."""
    if not text:
        return set()
    found = set()
    # $TICKER form
    found.update(re.findall(r"\$([A-Z]{1,5})\b", text))
    # Bare UPPERCASE words (2-5 chars, longer than 1 to reduce noise)
    bare = re.findall(r"\b([A-Z]{2,5})\b", text)
    found.update(bare)
    return {t for t in found if t not in COMMON_FALSE_POSITIVES and not t.isdigit()}


def build_ticker_post_index(posts: list[dict]) -> dict[str, list[dict]]:
    """
    Build a {ticker: [post, ...]} index from a list of posts.
    Posts are scanned for tickers in both title and body.
    Sorted by score (highest first), with a small cap to keep memory bounded.

    Also tracks 'post ticker breadth' — how many tickers a post mentions —
    so we can filter out 'list posts' (e.g. a market roundup that mentions
    10 tickers in passing) which aren't actually a 'why this is trending'.
    Posts that only mention 1-2 tickers are weighted higher (more specific).
    """
    # Pass 1: count how many tickers each post mentions
    post_ticker_count: dict[str, int] = {}
    post_to_tickers: dict[str, set[str]] = {}
    for p in posts:
        text = f"{p.get('title', '')} {p.get('selftext', '')[:2000]}"
        tickers = extract_tickers(text)
        if tickers:
            pid = p.get("id", "")
            post_to_tickers[pid] = tickers
            post_ticker_count[pid] = len(tickers)

    # Pass 2: build the index, skipping "list posts" (mention >= 7 tickers)
    # and low-quality posts (score < 3)
    # Posts that mention fewer tickers get a boost so they appear first
    # (they're more likely to be specifically about that ticker)
    index: dict[str, list[dict]] = {}
    cap = 10
    MIN_SCORE = 3
    MAX_BREADTH = 7    # only filter out the broadest roundup posts
    for p in posts:
        pid = p.get("id", "")
        breadth = post_ticker_count.get(pid, 0)
        if breadth >= MAX_BREADTH:
            continue
        score = p.get("score", 0)
        if score < MIN_SCORE:
            continue
        tickers = post_to_tickers.get(pid, set())
        if not tickers:
            continue
        # Specificity boost: posts that mention fewer tickers rank higher
        # (e.g. "TSLA hits new high" is better than "market roundup mentions TSLA")
        # Divide score by breadth — so a 100-score 1-ticker post beats a 100-score 5-ticker post
        adjusted_score = score / max(breadth, 1)
        slim = {
            "id": pid,
            "subreddit": p.get("subreddit", ""),
            "title": (p.get("title") or "")[:200],
            "score": score,
            "breadth": breadth,
            "num_comments": p.get("num_comments", 0),
            "permalink": f"https://reddit.com{p.get('permalink', '')}",
            "author": p.get("author", "[deleted]"),
        }
        for t in tickers:
            bucket = index.setdefault(t, [])
            if len(bucket) < cap:
                # Use a tuple (adjusted_score, raw_score) for sorting
                bucket.append((adjusted_score, slim))
    # Sort each bucket by adjusted score desc, then raw score desc, drop the key
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
    posts_by_sub = fetch_all_arctic_posts()

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

    # Build the per-ticker post index from all fetched posts
    all_posts_flat = [p for sub_posts in posts_by_sub.values() for p in sub_posts]
    full_ticker_index = build_ticker_post_index(all_posts_flat)

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
    }


def build_ticker_detail(ticker: str) -> dict:
    """Build a per-ticker detail payload by searching current snapshots."""
    upper = ticker.upper().lstrip("$")
    tickers_by_sub = fetch_apewisdom_tickers()
    posts_by_sub = fetch_all_arctic_posts()

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

    # Use the per-ticker post index for richer post coverage
    all_posts_flat = [p for sub_posts in posts_by_sub.values() for p in sub_posts]
    full_ticker_index = build_ticker_post_index(all_posts_flat)
    posts = full_ticker_index.get(upper, [])[:30]

    return {
        "ticker": upper,
        "latest_per_sub": sorted(latest, key=lambda x: x.get("mentions", 0) or 0, reverse=True),
        "recent_posts": posts,
        # 7-day trend unavailable in this stateless design (would need a DB)
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


@app.route("/api/ticker/<ticker>")
def api_ticker_detail(ticker: str):
    """Ticker detail. Not cached (per-ticker lookups are cheap)."""
    try:
        return jsonify(build_ticker_detail(ticker))
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
    padding: 4px 10px;
    border-radius: 4px;
    font-size: 12px;
    cursor: pointer;
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
  .ticker-card .price { color: var(--text); font-weight: 600; font-variant-numeric: tabular-nums; }
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
</style>
</head>
<body>
<header>
  <h1>📈 Stock Sub Dashboard</h1>
  <div class="subtitle">
    <span id="last-scrape">Loading...</span>
    <button class="refresh-btn" onclick="loadData(true)">↻ Refresh</button>
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

<div class="footer">
  Data: ApeWisdom + Arctic Shift. Cached for 1 hour. Not financial advice.
</div>

<script>
let dashboardData = null;

async function loadData(force = false) {
  const dash = document.getElementById('dashboard');
  if (force) {
    dash.innerHTML = '<div class="loading">Refreshing (cold cache can take ~10-30s)...</div>';
  }
  try {
    const url = force ? '/api/refresh' : '/api/stats';
    const opts = force ? { method: 'POST' } : {};
    const resp = await fetch(url, opts);
    dashboardData = await resp.json();
    if (dashboardData.error) {
      dash.innerHTML = `<div class="card"><div class="empty">Error: ${escapeHtml(dashboardData.error)}</div></div>`;
      return;
    }
    render(dashboardData);
  } catch (e) {
    console.error('loadData failed:', e);
    dash.innerHTML = `<div class="card"><div class="empty">Error loading data: ${escapeHtml(e.message || String(e))}</div></div>`;
  }
}

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
              <div class="meta">r/${escapeHtml(post.subreddit)} · <span class="score-num">▲ ${post.score}</span></div>
            </a>
          `;
        }).join('')}
      </div>
    `;
  } else {
    // Reserve the same vertical space so cards align in the grid
    whyHtml = `
      <div class="why-trending empty" onclick="event.stopPropagation()">
        <div class="why-label no-data">Why it's trending</div>
        <div style="font-size: 10px; color: var(--muted); font-style: italic; opacity: 0.6;">
          no post match
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
  if (d.force_refreshed) {
    headerText += '  (just refreshed)';
  }
  document.getElementById('last-scrape').textContent = headerText;

  // Notice for first cold load
  const noticeArea = document.getElementById('notice-area');
  if (d.fetch_time_seconds && d.fetch_time_seconds > 5) {
    noticeArea.innerHTML = `<div class="notice">Cold cache refresh took ${d.fetch_time_seconds}s. Future loads will be instant (cached for 1h).</div>`;
  } else {
    noticeArea.innerHTML = '';
  }

  const html = `
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
    document.getElementById('modal-body').innerHTML = renderTickerModal(data);
  } catch (e) {
    document.getElementById('modal-body').innerHTML = '<div class="empty">Error loading.</div>';
  }
}

function renderTickerModal(data) {
  const latest = data.latest_per_sub;
  const posts = data.recent_posts;
  let html = `<h2 style="margin-top:0;">$${data.ticker}</h2>`;

  if (latest.length > 0) {
    html += `<div style="margin-top:14px;"><strong style="color:var(--muted);font-size:12px;text-transform:uppercase;">By Subreddit (latest)</strong>`;
    latest.forEach(l => {
      const delta = l.mentions_24h_ago != null ? (l.mentions - l.mentions_24h_ago) : null;
      html += `
        <div class="row">
          <span class="lbl">r/${l.subreddit}</span>
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
    html += `<div style="margin-top:14px;"><strong style="color:var(--muted);font-size:12px;text-transform:uppercase;">Recent Posts (${posts.length})</strong>`;
    posts.slice(0, 10).forEach(p => {
      html += `
        <div class="post">
          <a class="post-title" href="${p.permalink}" target="_blank" rel="noopener">
            ${escapeHtml(p.title)}
          </a>
          <div class="post-meta">
            <span class="sub">r/${p.subreddit}</span>
            <span class="score">▲ ${p.score}</span>
            <span>💬 ${p.num_comments}</span>
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
# Entrypoint
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    # threaded=True so multiple users can hit /api/stats simultaneously
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)