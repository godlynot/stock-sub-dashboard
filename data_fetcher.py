"""
Data fetcher for the Reddit stock dashboard.

Pulls ticker mention rankings from ApeWisdom (free, no auth) and post content
from Arctic Shift (free, no auth). Persists to SQLite for historical tracking
and trend deltas.

Data sources:
  - ApeWisdom: https://apewisdom.io/api/v1.0/filter/<subreddit>/page/<n>
      Returns ranked ticker mentions with 24h deltas. Covers WSB, stocks,
      investing, options, StockMarket, pennystocks, SPACs, crypto subs.
      No auth, no rate limits visible.

  - Arctic Shift: https://arctic-shift.photon-reddit.com/api/posts/search
      Historical Reddit post archive (Pushshift replacement). Used to fetch
      recent top posts so the dashboard can show actual post titles/links.
      No auth. Note: WSB coverage is partial on Arctic Shift; works fine
      for r/stocks, r/investing, etc.

Usage:
  python data_fetcher.py              # fetch all configured subs
  python data_fetcher.py --stats      # show DB stats
  python data_fetcher.py --ticker TSLA  # show recent posts mentioning TSLA
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "data" / "dashboard.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Subs we track on ApeWisdom. Each gets its own ticker leaderboard.
# Picked based on confirmed availability from ApeWisdom's API.
DEFAULT_APEWISDOM_SUBS = [
    "wallstreetbets",   # 557 tickers
    "stocks",           # 273 tickers
    "investing",        # 166 tickers
    "options",          # 136 tickers
    "StockMarket",      # 127 tickers
    "pennystocks",      # 128 tickers
    "SPACs",            # 40 tickers
]

# Subs we fetch post content for via Arctic Shift.
# WSB is intentionally omitted — Arctic Shift has very sparse WSB data
# (most recent posts have score=1-2). The other subs are well-covered.
DEFAULT_ARCTIC_SUBS = [
    "stocks",
    "investing",
    "options",
    "StockMarket",
    "pennystocks",
    "SPACs",
]

APEWISDOM_SUBS = [
    s.strip()
    for s in os.getenv("APEWISDOM_SUBS", ",".join(DEFAULT_APEWISDOM_SUBS)).split(",")
    if s.strip()
]
ARCTIC_SUBS = [
    s.strip()
    for s in os.getenv("ARCTIC_SUBS", ",".join(DEFAULT_ARCTIC_SUBS)).split(",")
    if s.strip()
]

# How many ticker pages (100 per page) to pull per sub
PAGES_PER_SUB = int(os.getenv("PAGES_PER_SUB", "1"))
# How many recent top posts to pull per sub from Arctic Shift
POSTS_PER_SUB = int(os.getenv("POSTS_PER_SUB", "30"))
# How many days back to look for "top of week" posts
POST_LOOKBACK_DAYS = int(os.getenv("POST_LOOKBACK_DAYS", "7"))


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------

def init_db() -> sqlite3.Connection:
    """Create tables if they don't exist and return a connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ticker_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_at TEXT NOT NULL,
            source TEXT NOT NULL,
            subreddit TEXT NOT NULL,
            rank INTEGER,
            ticker TEXT NOT NULL,
            name TEXT,
            mentions INTEGER,
            upvotes INTEGER,
            rank_24h_ago INTEGER,
            mentions_24h_ago INTEGER,
            UNIQUE(snapshot_at, source, subreddit, ticker)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            subreddit TEXT NOT NULL,
            title TEXT NOT NULL,
            author TEXT,
            url TEXT,
            permalink TEXT,
            score INTEGER,
            num_comments INTEGER,
            upvote_ratio REAL,
            created_utc INTEGER,
            fetched_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            source TEXT NOT NULL,
            subreddit TEXT NOT NULL,
            records INTEGER DEFAULT 0,
            error TEXT,
            UNIQUE(started_at, source, subreddit)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_sub ON ticker_snapshots(subreddit, snapshot_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_ticker ON ticker_snapshots(ticker, snapshot_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_sub ON posts(subreddit, score)")
    conn.commit()
    return conn


# ----------------------------------------------------------------------------
# API clients
# ----------------------------------------------------------------------------

class ApeWisdomClient:
    BASE = "https://apewisdom.io/api/v1.0"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "stock-sub-dashboard/1.0 (free; non-commercial)",
            "Accept": "application/json",
        })

    def get_tickers(self, subreddit: str, page: int = 1) -> dict:
        """Fetch one page of ticker rankings for a subreddit."""
        url = f"{self.BASE}/filter/{subreddit}/page/{page}"
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_all_tickers(self, subreddit: str, max_pages: int = 1) -> list[dict]:
        """Fetch all pages (up to max_pages) for a subreddit."""
        all_results = []
        for page in range(1, max_pages + 1):
            data = self.get_tickers(subreddit, page)
            results = data.get("results", [])
            if not results:
                break
            all_results.extend(results)
            if page >= data.get("pages", 1):
                break
            time.sleep(0.5)  # be polite
        return all_results


class ArcticShiftClient:
    BASE = "https://arctic-shift.photon-reddit.com/api/posts/search"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "stock-sub-dashboard/1.0 (free; non-commercial)",
            "Accept": "application/json",
        })

    def get_top_posts(self, subreddit: str, days: int = 7, limit: int = 30) -> list[dict]:
        """
        Fetch recent top posts from a subreddit.

        Arctic Shift doesn't support sorting by score, so we pull the latest
        100 posts from the lookback window and sort by score client-side.
        For high-volume subs this gives us a good sample of recent buzz.
        """
        now = int(time.time())
        after = now - days * 86400
        # Pull a few pages to get a good sample
        results = []
        cursor = None
        seen_ids = set()
        max_pages = 3

        for _ in range(max_pages):
            params = {
                "subreddit": subreddit,
                "limit": 100,
                "after": after,
                "before": now,
                "sort": "desc",
            }
            if cursor:
                params["cursor"] = cursor
            try:
                resp = self.session.get(self.BASE, params=params, timeout=30)
                if resp.status_code == 400 and cursor is None:
                    # before+after may be unsupported; try without them
                    params.pop("before", None)
                    params.pop("after", None)
                    resp = self.session.get(self.BASE, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as e:
                print(f"  [!] Arctic Shift error for r/{subreddit}: {e}", file=sys.stderr)
                break

            posts = data.get("data", [])
            if not posts:
                break
            for p in posts:
                pid = p.get("id")
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    results.append(p)
            cursor = data.get("cursor")
            if not cursor:
                break
            time.sleep(0.5)

        # Sort by score, return top N
        results.sort(key=lambda p: p.get("score", 0), reverse=True)
        return results[:limit]


# ----------------------------------------------------------------------------
# Fetch + persist
# ----------------------------------------------------------------------------

def _normalize_post(raw: dict) -> dict:
    """Convert Arctic Shift post to flat dict for storage."""
    return {
        "id": raw.get("id", ""),
        "subreddit": raw.get("subreddit", ""),
        "title": (raw.get("title") or "")[:500],
        "author": raw.get("author", "[deleted]"),
        "url": raw.get("url_overridden_by_dest", raw.get("url", "")),
        "permalink": f"https://reddit.com{raw.get('permalink', '')}",
        "score": raw.get("score", 0),
        "num_comments": raw.get("num_comments", 0),
        "upvote_ratio": raw.get("upvote_ratio"),
        "created_utc": raw.get("created_utc"),
    }


def fetch_apewisdom(
    client: ApeWisdomClient,
    conn: sqlite3.Connection,
    subreddit: str,
) -> int:
    """Pull ticker rankings for one subreddit. Returns number of rows saved."""
    started_at = datetime.now(timezone.utc).isoformat()
    print(f"[apewisdom/{subreddit}] Fetching ticker rankings (pages=1-{PAGES_PER_SUB})...")
    saved = 0
    error_msg = None
    try:
        tickers = client.get_all_tickers(subreddit, max_pages=PAGES_PER_SUB)
        snapshot_at = datetime.now(timezone.utc).isoformat()
        for t in tickers:
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO ticker_snapshots
                      (snapshot_at, source, subreddit, rank, ticker, name,
                       mentions, upvotes, rank_24h_ago, mentions_24h_ago)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_at, "apewisdom", subreddit,
                        t.get("rank"), t.get("ticker"), t.get("name"),
                        t.get("mentions"), t.get("upvotes"),
                        t.get("rank_24h_ago"), t.get("mentions_24h_ago"),
                    ),
                )
                saved += 1
            except Exception as e:
                print(f"  [!] Error saving ticker {t.get('ticker')}: {e}", file=sys.stderr)
        conn.commit()
    except Exception as e:
        error_msg = str(e)
        print(f"  [!] Failed r/{subreddit}: {e}", file=sys.stderr)

    conn.execute(
        """
        INSERT OR REPLACE INTO fetch_log
          (started_at, finished_at, source, subreddit, records, error)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (started_at, datetime.now(timezone.utc).isoformat(), "apewisdom", subreddit, saved, error_msg),
    )
    conn.commit()
    print(f"[apewisdom/{subreddit}] Saved {saved} tickers.")
    return saved


def fetch_arctic(
    client: ArcticShiftClient,
    conn: sqlite3.Connection,
    subreddit: str,
) -> int:
    """Pull top posts for one subreddit from Arctic Shift."""
    started_at = datetime.now(timezone.utc).isoformat()
    print(f"[arctic/{subreddit}] Fetching top {POSTS_PER_SUB} posts (last {POST_LOOKBACK_DAYS}d)...")
    saved = 0
    error_msg = None
    try:
        posts = client.get_top_posts(subreddit, days=POST_LOOKBACK_DAYS, limit=POSTS_PER_SUB)
        fetched_at = datetime.now(timezone.utc).isoformat()
        for raw in posts:
            try:
                p = _normalize_post(raw)
                if not p["id"]:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO posts
                      (id, source, subreddit, title, author, url, permalink,
                       score, num_comments, upvote_ratio, created_utc, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        p["id"], "arctic_shift", p["subreddit"], p["title"],
                        p["author"], p["url"], p["permalink"],
                        p["score"], p["num_comments"], p["upvote_ratio"],
                        p["created_utc"], fetched_at,
                    ),
                )
                saved += 1
            except Exception as e:
                print(f"  [!] Error saving post: {e}", file=sys.stderr)
        conn.commit()
    except Exception as e:
        error_msg = str(e)
        print(f"  [!] Failed r/{subreddit}: {e}", file=sys.stderr)

    conn.execute(
        """
        INSERT OR REPLACE INTO fetch_log
          (started_at, finished_at, source, subreddit, records, error)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (started_at, datetime.now(timezone.utc).isoformat(), "arctic_shift", subreddit, saved, error_msg),
    )
    conn.commit()
    print(f"[arctic/{subreddit}] Saved {saved} posts.")
    return saved


# ----------------------------------------------------------------------------
# Stats / queries
# ----------------------------------------------------------------------------

def print_stats(conn: sqlite3.Connection) -> None:
    """Print a quick summary of what's in the DB."""
    print("\n=== Database Stats ===")
    print(f"Path: {DB_PATH}")
    print(f"Size: {DB_PATH.stat().st_size / 1024:.1f} KB" if DB_PATH.exists() else "Size: 0")
    print()

    for table, label in [
        ("ticker_snapshots", "Ticker snapshots"),
        ("posts", "Posts"),
        ("fetch_log", "Fetch log entries"),
    ]:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        print(f"  {label}: {row[0]}")

    print("\nLatest snapshot per sub:")
    rows = conn.execute("""
        SELECT subreddit, MAX(snapshot_at) AS last
        FROM ticker_snapshots GROUP BY subreddit ORDER BY subreddit
    """).fetchall()
    for sub, last in rows:
        print(f"  r/{sub}: {last}")

    print("\nTop 10 tickers (most recent snapshot, all subs combined):")
    rows = conn.execute("""
        WITH latest AS (
            SELECT subreddit, MAX(snapshot_at) AS mx FROM ticker_snapshots GROUP BY subreddit
        )
        SELECT ts.ticker, SUM(ts.mentions) AS total_mentions,
               SUM(ts.upvotes) AS total_upvotes,
               COUNT(*) AS sub_count
        FROM ticker_snapshots ts
        JOIN latest l ON l.subreddit = ts.subreddit AND l.mx = ts.snapshot_at
        GROUP BY ts.ticker
        ORDER BY total_mentions DESC LIMIT 10
    """).fetchall()
    for ticker, mentions, upvotes, sub_count in rows:
        print(f"  ${ticker:6s}  {mentions:5d} mentions  {upvotes:6d} upvotes  in {sub_count} subs")


def show_ticker_posts(conn: sqlite3.Connection, ticker: str) -> None:
    """Show recent posts mentioning a ticker."""
    upper = ticker.upper().lstrip("$")
    # Look for ticker in title (heuristic — would need full text for proper extraction)
    rows = conn.execute("""
        SELECT subreddit, title, score, num_comments, permalink
        FROM posts
        WHERE UPPER(title) LIKE ? OR UPPER(title) LIKE ?
        ORDER BY score DESC LIMIT 20
    """, (f"%{upper}%", f"%${upper}%")).fetchall()

    print(f"\n=== Posts mentioning ${upper} ===")
    if not rows:
        print("  (no posts found in local DB)")
        return
    for sub, title, score, comments, permalink in rows:
        print(f"  [{score:5d} ▲ {comments:4d} 💬] r/{sub}: {title[:70]}")
        print(f"      {permalink}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Stock-subreddit data fetcher")
    parser.add_argument("--source", choices=["apewisdom", "arctic", "all"],
                        default="all", help="Which data source to fetch")
    parser.add_argument("--sub", help="Fetch a single subreddit from both sources")
    parser.add_argument("--stats", action="store_true", help="Show DB stats and exit")
    parser.add_argument("--ticker", help="Show recent posts mentioning this ticker (e.g. TSLA)")
    args = parser.parse_args()

    conn = init_db()

    if args.stats:
        print_stats(conn)
        return

    if args.ticker:
        show_ticker_posts(conn, args.ticker)
        return

    if args.sub:
        # Fetch this one sub from both sources
        print(f"Fetching r/{args.sub} from both sources...")
        if args.sub in APEWISDOM_SUBS or args.sub in DEFAULT_APEWISDOM_SUBS:
            aw = ApeWisdomClient()
            fetch_apewisdom(aw, conn, args.sub)
        if args.sub in ARCTIC_SUBS or args.sub in DEFAULT_ARCTIC_SUBS:
            ar = ArcticShiftClient()
            fetch_arctic(ar, conn, args.sub)
        return

    total = 0
    if args.source in ("apewisdom", "all"):
        aw = ApeWisdomClient()
        for sub in APEWISDOM_SUBS:
            try:
                total += fetch_apewisdom(aw, conn, sub)
            except Exception as e:
                print(f"[!] r/{sub} failed: {e}", file=sys.stderr)

    if args.source in ("arctic", "all"):
        ar = ArcticShiftClient()
        for sub in ARCTIC_SUBS:
            try:
                total += fetch_arctic(ar, conn, sub)
            except Exception as e:
                print(f"[!] r/{sub} failed: {e}", file=sys.stderr)

    print(f"\nDone. {total} records collected.")
    print(f"Database: {DB_PATH}")


if __name__ == "__main__":
    main()