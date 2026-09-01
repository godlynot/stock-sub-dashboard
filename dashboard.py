"""
Stock Sub Dashboard - Flask web service.

Calls free public APIs (ApeWisdom for ticker rankings, Arctic Shift for post
content) directly at request time. Caches results in memory for CACHE_TTL
seconds to avoid hammering the free APIs.

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

CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))  # 1 hour
APEWISDOM_TIMEOUT = int(os.getenv("APEWISDOM_TIMEOUT", "20"))
ARCTIC_TIMEOUT = int(os.getenv("ARCTIC_TIMEOUT", "30"))
POSTS_PER_SUB = int(os.getenv("POSTS_PER_SUB", "30"))
POST_LOOKBACK_DAYS = int(os.getenv("POST_LOOKBACK_DAYS", "7"))

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

_session = requests.Session()
_session.headers.update(HEADERS)


def _get_json(url: str, params: dict | None = None, timeout: int = 20) -> dict | None:
    try:
        resp = _session.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[api] error fetching {url}: {e}", flush=True)
        return None


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

    # Posts mentioning this ticker
    posts = []
    for sub, plist in posts_by_sub.items():
        for p in plist:
            title = (p.get("title") or "")
            if upper in title.upper() or f"${upper}" in title.upper():
                posts.append({
                    "id": p.get("id"),
                    "subreddit": p.get("subreddit", sub),
                    "title": title,
                    "score": p.get("score", 0),
                    "num_comments": p.get("num_comments", 0),
                    "permalink": f"https://reddit.com{p.get('permalink', '')}",
                    "author": p.get("author"),
                })
    posts.sort(key=lambda p: p["score"], reverse=True)
    posts = posts[:30]

    return {
        "ticker": upper,
        "latest_per_sub": sorted(latest, key=lambda x: x.get("mentions", 0) or 0, reverse=True),
        "recent_posts": posts[:30],
        # 7-day trend unavailable in this stateless design (would need a DB)
        "trend_7d": [],
    }


# ----------------------------------------------------------------------------
# App + cache
# ----------------------------------------------------------------------------

app = Flask(__name__)
cache = Cache(ttl=CACHE_TTL)


@app.route("/api/stats")
def api_stats():
    """Main dashboard payload. Cached for CACHE_TTL seconds."""
    try:
        data = cache.get(build_dashboard_payload)
        return jsonify(data)
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
  }
  .ticker-card:hover { border-color: var(--accent); transform: translateY(-1px); }
  .ticker-card .sym { font-weight: 700; color: var(--accent); font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 14px; }
  .ticker-card .name { font-size: 11px; color: var(--muted); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ticker-card .cnt { font-size: 11px; color: var(--text); margin-top: 6px; display: flex; justify-content: space-between; }
  .ticker-card .cnt .num { color: var(--accent); font-weight: 600; }
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
            <div class="sub-section">
              <h3>r/${sub}</h3>
              <div class="ticker-grid">
                ${d.per_sub_top[sub].map(t => `
                  <div class="ticker-card" onclick="openTicker('${t.ticker}')">
                    <div class="sym">$${t.ticker}</div>
                    <div class="name">${escapeHtml((t.name || '').slice(0, 22))}</div>
                    <div class="cnt">
                      <span>${t.mentions} mentions</span>
                      <span class="num">${t.upvotes || 0} ▲</span>
                    </div>
                  </div>
                `).join('')}
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