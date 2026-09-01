"""
Flask dashboard for stock-subreddit data.

Reads from a SQLite DB populated by data_fetcher.py. Shows:
  - Top tickers by mentions per subreddit (with 24h delta)
  - Trending tickers (mentions surge)
  - Top recent posts per subreddit
  - Cross-subreddit leaderboard

Run:
  python dashboard.py            # dev server on :5000
  gunicorn dashboard:app         # production
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, g, jsonify, render_template_string, request

from data_fetcher import DB_PATH, init_db

app = Flask(__name__)


# ----------------------------------------------------------------------------
# DB helpers
# ----------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    db = getattr(g, "_db", None)
    if db is None:
        db = init_db()
        g._db = db
    return db


@app.teardown_appcontext
def close_db(_exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()


def query_dicts(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ----------------------------------------------------------------------------
# Aggregations
# ----------------------------------------------------------------------------

def _latest_snapshot_filter(alias: str = "ts") -> str:
    """SQL fragment: only rows from the most recent snapshot per subreddit.
    The alias must match the FROM clause alias of the ticker_snapshots table."""
    return f"""
        {alias}.source = 'apewisdom'
        AND {alias}.snapshot_at = (
            SELECT MAX(snapshot_at) FROM ticker_snapshots
            WHERE subreddit = {alias}.subreddit AND source = 'apewisdom'
        )
    """


# ----------------------------------------------------------------------------
# API endpoints
# ----------------------------------------------------------------------------

@app.route("/api/stats")
def api_stats():
    """Main dashboard payload."""
    conn = get_db()

    # 1. Last fetch time + per-subreddit freshness
    freshness = query_dicts(conn, """
        SELECT subreddit, MAX(finished_at) AS last_fetch,
               SUM(records) AS total_records
        FROM fetch_log
        WHERE error IS NULL OR error = ''
        GROUP BY subreddit
        ORDER BY subreddit
    """)
    last_scrape_iso = None
    for f in freshness:
        if f["last_fetch"] and (last_scrape_iso is None or f["last_fetch"] > last_scrape_iso):
            last_scrape_iso = f["last_fetch"]

    # 2. Cross-sub leaderboard (latest snapshot, sum mentions across subs)
    cross_sub = query_dicts(conn, f"""
        SELECT ts.ticker, MAX(ts.name) AS name,
               SUM(ts.mentions) AS total_mentions,
               SUM(ts.upvotes) AS total_upvotes,
               COUNT(*) AS sub_count,
               GROUP_CONCAT(DISTINCT ts.subreddit) AS subs
        FROM ticker_snapshots ts
        WHERE {_latest_snapshot_filter('ts')}
        GROUP BY ts.ticker
        HAVING total_mentions >= 5
        ORDER BY total_mentions DESC
        LIMIT 30
    """)

    # 3. Trending — biggest gainers vs ~24h ago (compare latest snapshot to previous-day)
    trending = query_dicts(conn, """
        WITH latest AS (
            SELECT * FROM ticker_snapshots ts_latest
            WHERE ts_latest.source = 'apewisdom'
              AND ts_latest.snapshot_at = (
                  SELECT MAX(snapshot_at) FROM ticker_snapshots
                  WHERE subreddit = ts_latest.subreddit AND source = 'apewisdom'
              )
        ),
        older AS (
            SELECT * FROM ticker_snapshots ts_older
            WHERE ts_older.source = 'apewisdom'
              AND ts_older.snapshot_at = (
                  SELECT MAX(snapshot_at) FROM ticker_snapshots
                  WHERE subreddit = ts_older.subreddit
                    AND source = 'apewisdom'
                    AND snapshot_at < (
                        SELECT MAX(snapshot_at) FROM ticker_snapshots
                        WHERE subreddit = ts_older.subreddit AND source = 'apewisdom'
                    )
              )
        )
        SELECT l.ticker, MAX(l.name) AS name,
               SUM(l.mentions) AS mentions_now,
               COALESCE(SUM(o.mentions), 0) AS mentions_24h,
               (SUM(l.mentions) - COALESCE(SUM(o.mentions), 0)) AS delta
        FROM latest l
        LEFT JOIN older o ON o.ticker = l.ticker AND o.subreddit = l.subreddit
        GROUP BY l.ticker
        HAVING mentions_now >= 3
        ORDER BY delta DESC
        LIMIT 15
    """)

    # 4. Per-subreddit top tickers
    per_sub_top = {}
    for row in query_dicts(conn, f"""
        SELECT subreddit, ticker, name, mentions, upvotes,
               rank_24h_ago, mentions_24h_ago
        FROM ticker_snapshots ts
        WHERE {_latest_snapshot_filter('ts')}
        ORDER BY subreddit, mentions DESC
    """):
        per_sub_top.setdefault(row["subreddit"], []).append(row)
    # Trim to top 10 per sub
    per_sub_top = {k: v[:10] for k, v in per_sub_top.items()}

    # 5. Top recent posts across all subs
    top_posts = query_dicts(conn, """
        SELECT id, subreddit, title, author, score, num_comments,
               permalink, upvote_ratio, created_utc
        FROM posts
        WHERE score > 5
        ORDER BY score DESC
        LIMIT 30
    """)

    # 6. Per-subreddit post summary
    per_sub_posts = query_dicts(conn, """
        SELECT subreddit,
               COUNT(*) AS posts,
               AVG(score) AS avg_score,
               SUM(num_comments) AS total_comments,
               MAX(score) AS top_score
        FROM posts
        GROUP BY subreddit
        ORDER BY total_comments DESC
    """)

    return jsonify({
        "last_scrape": last_scrape_iso,
        "freshness": freshness,
        "cross_sub_leaderboard": cross_sub,
        "trending": trending,
        "per_sub_top": per_sub_top,
        "top_posts": top_posts,
        "per_sub_posts": per_sub_posts,
    })


@app.route("/api/ticker/<ticker>")
def api_ticker_detail(ticker: str):
    """Show all info for one ticker across subs."""
    conn = get_db()
    upper = ticker.upper().lstrip("$")

    # Latest snapshot for this ticker
    latest = query_dicts(conn, f"""
        SELECT ts.* FROM ticker_snapshots ts
        WHERE ts.ticker = ? AND {_latest_snapshot_filter('ts')}
        ORDER BY ts.mentions DESC
    """, (upper,))

    # Trend over the last 7 days (if we have that many snapshots)
    trend = query_dicts(conn, """
        SELECT DATE(snapshot_at) AS day, SUM(mentions) AS mentions, SUM(upvotes) AS upvotes
        FROM ticker_snapshots
        WHERE ticker = ? AND snapshot_at >= datetime('now', '-7 days')
        GROUP BY DATE(snapshot_at)
        ORDER BY day
    """, (upper,))

    # Recent posts with this ticker in title
    posts = query_dicts(conn, """
        SELECT id, subreddit, title, score, num_comments, permalink, author
        FROM posts
        WHERE (UPPER(title) LIKE ? OR UPPER(title) LIKE ?)
        ORDER BY score DESC
        LIMIT 30
    """, (f"%{upper}%", f"%${upper}%"))

    return jsonify({
        "ticker": upper,
        "latest_per_sub": latest,
        "trend_7d": trend,
        "recent_posts": posts,
    })


@app.route("/api/scrape-now", methods=["POST"])
def api_scrape_now():
    """Trigger a refresh (admin endpoint, requires SCRAPE_TOKEN)."""
    token = os.getenv("SCRAPE_TOKEN", "")
    if token and request.headers.get("X-Scrape-Token") != token:
        return jsonify({"error": "unauthorized"}), 401
    import threading
    from data_fetcher import main as run_fetch
    def _go():
        try:
            run_fetch()
        except Exception as e:
            print(f"[scrape-now error] {e}", flush=True)
    threading.Thread(target=_go, daemon=True).start()
    return jsonify({"status": "started"})


# ----------------------------------------------------------------------------
# HTML template (mobile-friendly)
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
  .sub-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
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
  .trend-bar { display: flex; align-items: flex-end; gap: 4px; height: 60px; margin: 12px 0; }
  .trend-bar .bar { flex: 1; background: var(--accent); border-radius: 3px 3px 0 0; min-height: 2px; transition: height 0.3s; }
</style>
</head>
<body>
<header>
  <h1>📈 Stock Sub Dashboard</h1>
  <div class="subtitle">
    <span id="last-scrape">Loading...</span>
    <button class="refresh-btn" onclick="loadData()">↻ Refresh</button>
  </div>
</header>

<main>
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
  Data: ApeWisdom (ticker rankings) + Arctic Shift (post archive). Not financial advice.
</div>

<script>
let dashboardData = null;

async function loadData() {
  try {
    const resp = await fetch('/api/stats');
    dashboardData = await resp.json();
    render(dashboardData);
  } catch (e) {
    document.getElementById('dashboard').innerHTML =
      '<div class="card"><div class="empty">Error loading data. Check server logs.</div></div>';
  }
}

function render(d) {
  const lastScrape = d.last_scrape
    ? new Date(d.last_scrape).toLocaleString()
    : 'Never';
  document.getElementById('last-scrape').textContent = `Last update: ${lastScrape}`;

  const html = `
    <div class="card">
      <h2>🔥 Trending (mentions Δ vs 24h)</h2>
      ${d.trending.length === 0
        ? '<div class="empty">Need more data to compute trends.</div>'
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
      <h2>🏆 Cross-Sub Leaderboard (latest)</h2>
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
      <h2>📡 Data Freshness</h2>
      ${d.freshness.length === 0
        ? '<div class="empty">No fetches yet.</div>'
        : d.freshness.map(f => `
            <div class="stat-line">
              <span class="lbl">r/${f.subreddit}</span>
              <span class="val">${f.total_records} records · ${timeAgo(f.last_fetch)}</span>
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

function timeAgo(iso) {
  if (!iso) return '?';
  const ms = Date.now() - new Date(iso).getTime();
  const min = Math.floor(ms / 60000);
  if (min < 1) return 'just now';
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const d = Math.floor(hr / 24);
  return `${d}d ago`;
}

async function openTicker(ticker) {
  const modal = document.getElementById('modal');
  document.getElementById('modal-body').innerHTML =
    '<div class="loading">Loading...</div>';
  modal.classList.add('active');

  try {
    const resp = await fetch(`/api/ticker/${encodeURIComponent(ticker)}`);
    const data = await resp.json();
    document.getElementById('modal-body').innerHTML = renderTickerModal(data);
  } catch (e) {
    document.getElementById('modal-body').innerHTML =
      '<div class="empty">Error loading.</div>';
  }
}

function renderTickerModal(data) {
  const latest = data.latest_per_sub;
  const trend = data.trend_7d;
  const posts = data.recent_posts;

  let html = `<h2 style="margin-top:0;">$${data.ticker}</h2>`;

  // Per-sub stats
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

  // 7-day trend bar chart
  if (trend.length > 1) {
    const maxM = Math.max(...trend.map(t => t.mentions || 0));
    html += `<div style="margin-top:14px;"><strong style="color:var(--muted);font-size:12px;text-transform:uppercase;">7-day mentions</strong>`;
    html += `<div class="trend-bar">`;
    trend.forEach(t => {
      const h = maxM > 0 ? (t.mentions / maxM) * 100 : 0;
      html += `<div class="bar" style="height:${h}%" title="${t.day}: ${t.mentions}"></div>`;
    });
    html += `</div>`;
    html += `<div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);">
      <span>${trend[0].day}</span>
      <span>${trend[trend.length-1].day}</span>
    </div></div>`;
  }

  // Recent posts
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
    html += '<div class="empty">No data for this ticker.</div>';
  }

  return html;
}

function closeModal() {
  document.getElementById('modal').classList.remove('active');
}

loadData();
setInterval(loadData, 5 * 60 * 1000);
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
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)