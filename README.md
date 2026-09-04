# Stock Sub Dashboard

A mobile-friendly web dashboard that shows trending tickers, prices, sparklines, upcoming earnings, and top posts from r/wallstreetbets and 23 other stock subreddits. Data comes from **four free, no-auth** public APIs.

**Live at:** https://stock-sub-dashboard.onrender.com

## What you get

### Trending data
- 📈 **Trending tickers** — biggest mention delta vs 24h ago, with prices
- 🏆 **Cross-sub leaderboard** — which tickers are being talked about across all 24 subs
- 📊 **Per-subreddit top tickers** — top 10 by mention count for each of 24 subs
- 🔥 **Market Heatmap** — top 50 tickers as colored squares (size = mentions, color = % change)
- 📅 **Upcoming earnings** — sorted by Reddit buzz, with EPS estimates and timing
- 🌟 **Top recent posts** — with score, comments, and "why it's trending" context
- 📊 **Macro panel** — 10Y/5Y/30Y Treasury, VIX, S&P 500, NASDAQ, Gold, Oil, DXY

### Personal features
- ⭐ **Watchlist** — star any ticker, saved on this device only (localStorage)
- 🔍 **Filter / search** — filter any section by symbol or name
- 📱 **Dedicated `/ticker/<T>` pages** — shareable, full detail view for any ticker

### UI
- 📱 **Mobile-friendly** dark dashboard
- 🗂️ **Collapsible sections** — click title to collapse, persists in localStorage
- 🔍 **Sticky section nav** — jump between sections
- 💫 **Today's Moves** — one-line summary of top movers + earnings + most-discussed
- 📏 **Compact mode** — toggle in the bottom-right corner for denser layout
- ⌨️ **Auto-refresh** — opt-in 5-min polling, state persists
- 💫 **Price flash** — green/red flash when prices change
- 📊 **Count-up animation** — macro values animate on first load

## How it works

```
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│   ApeWisdom     │  │  Arctic Shift   │  │  Yahoo Finance  │  │  Nasdaq Earnings│
│  ticker rankings│  │  Reddit posts   │  │  prices +       │  │  upcoming       │
│  + 24h deltas   │  │  + comments     │  │  30d history    │  │  reports        │
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘
         │                    │                    │                    │
         └────────────────────┴────────────────────┴────────────────────┘
                                       │
                              ┌────────▼────────┐
                              │   dashboard.py  │  ← single Flask app
                              │  (parallel      │     (no DB needed)
                              │   fetchers +    │
                              │   in-memory     │
                              │   cache 1h)     │
                              └────────┬────────┘
                                       │
                              Browser / Phone
```

**Why no Reddit API?** Reddit now blocks anonymous JSON access from most IPs and requires OAuth. Instead, we use four services that already do the hard work of mining Reddit + markets:

- **ApeWisdom** (apewisdom.io) — public service that has already done ticker extraction for r/wallstreetbets and 100+ other stock subs. Free JSON API with rankings + 24h deltas.
- **Arctic Shift** (photon-reddit.com) — a Pushshift replacement archiving Reddit posts. Free JSON API for post titles/links + comments.
- **Yahoo Finance** (query1.finance.yahoo.com) — free, no auth, returns `regularMarketPrice`, `chartPreviousClose`, and 30-day close history for sparklines.
- **Nasdaq Earnings** (api.nasdaq.com) — public earnings calendar, no auth, ~50 events per business day.

The dashboard calls these in parallel at request time, with a 1-hour in-memory cache. **No database, no disk, no cron job needed** — fits on Render's free tier with no extra services.

## Local setup

```bash
pip install -r requirements.txt
python dashboard.py
```

Then open `http://localhost:5000` in your browser.

- First request takes ~10-30s (fetches from all APIs in parallel)
- Subsequent requests are instant (served from cache)
- Cache refreshes every hour, or click "↻ Refresh" in the header to force
- Per-ticker pages cached 5 min
- Prices cached 5 min
- Macro indicators cached 15 min
- Earnings calendar cached 6 hours

## API endpoints

| Method | Path | Purpose | Cache |
|---|---|---|---|
| GET | `/` | Main dashboard | 1h |
| GET | `/ticker/<T>` | Dedicated ticker page | (HTML) |
| GET | `/api/stats` | Main dashboard JSON | 1h |
| POST | `/api/refresh` | Force refresh + return | (forces) |
| GET | `/api/earnings` | Upcoming earnings for tracked tickers | 6h |
| GET | `/api/prices?timeframe=1w\|1mo\|3mo` | Yahoo prices + sparklines | 5min/timeframe |
| GET | `/api/ticker/<T>` | Ticker detail JSON | 5min/ticker |
| GET | `/health` | `{"status":"ok","cache_age_seconds":N}` | (live) |

## Deploy to Render (free)

1. Push this folder to a new GitHub repo
2. Sign up at https://render.com (free, GitHub login)
3. Go to **https://dashboard.render.com/blueprints/new** (or: click "Blueprints" in left sidebar → "New Blueprint")
4. Connect your repo, accept the defaults
5. Render reads `render.yaml` and creates one web service
6. **No first-deploy data fetch needed** — the dashboard fetches on first request
7. Open your dashboard URL on your phone

**Note:** Render's free tier web service "sleeps" after 15 min of inactivity — the first dashboard load will take ~30s while the container wakes up. After that it's instant.

## Customizing

Edit env vars in `render.yaml` (or in `.env` for local):

- `SUBS` — comma-separated subs to track ticker rankings on
- `POST_SUBS` — comma-separated subs to fetch post content from
- `COMMENT_SUBS` — comma-separated subs to fetch comments from
- `CACHE_TTL` — seconds to cache before re-fetching (default 3600 = 1h)
- `PRICE_CACHE_TTL` — price cache (default 300 = 5min)
- `MACRO_CACHE_TTL` — macro indicators cache (default 900 = 15min)
- `EARNINGS_CACHE_TTL` — earnings calendar cache (default 21600 = 6h)
- `POSTS_PER_SUB` — how many recent top posts per sub
- `POST_LOOKBACK_DAYS` — how far back to look for posts

## Tracked subreddits

**24 subs total:**

Mega-high-volume: `wallstreetbets, wallstreetbetsnew, stocks, investing, options, StockMarket, pennystocks, SPACs, Superstonk`

Niche / company-specific: `SNDK, MSTR, amcstock, nvidia, BBBY`

General stock discussion: `smallstreetbets, StocksAndTrading, investing_discussion, ValueInvesting, Daytrading, SwingTrading, ETFs, RobinHood, Bogleheads, personalfinance`

Dead subs (filtered out): `StockMarketDiscussion` (last post 217d ago), `wallstreetbetsVIP` (1453d), `Stock_Picks` (1284d).

## Limitations & honesty

- **WSB post content is sparse** on Arctic Shift. We can show r/wallstreetbets ticker rankings (via ApeWisdom — works great) but recent WSB post titles may be missing. Other subs are well-covered.
- **"No post match" is real signal, not a bug.** Many WSB tickers (PLTR, RKLB, SOXL, HPE) are mentioned a lot in passing but have no dedicated posts in our 1500-record sample. This is honest data, not a missing feature.
- **No per-ticker search.** A search API for "find me posts about $X" would let us show context for any ticker, but no free one exists. Reddit's official API blocks it; Arctic Shift doesn't support keyword search.
- **Cache is per-process.** On Render's free tier, the web service can have multiple workers, and each has its own cache. With our `workers=2` config, this is fine for moderate traffic.
- **Cache resets on deploy.** Every time Render redeploys (or the service wakes from sleep), the cache is empty and the first request will be slow.
- **Free tier Render sleeps** after 15 min idle — first load takes ~30s.
- **Third-party dependencies:** if ApeWisdom, Arctic Shift, Yahoo Finance, or Nasdaq go down, our dashboard degrades gracefully (each section can fail independently).
- **Not financial advice.** Use as a sentiment signal, not a trading signal.

## Project layout

```
reddit-scraper/
├── dashboard.py        # Flask web service — fetches APIs + serves UI (single file, ~3000 lines)
├── requirements.txt    # Flask, requests, gunicorn
├── render.yaml         # Render deployment (one free web service)
├── Dockerfile          # Alternative explicit Docker build
├── README.md           # This file
└── ROADMAP.md          # Future feature ideas
```

## Security

- All HTML/JS outputs use `escapeHtml` for user data
- `/ticker/<T>` page additionally HTML-escapes the ticker via Python's `html.escape`
- Tickers are uppercased, `$`-prefixed stripped, and capped at 10 characters
- 24 free public APIs, no API keys required
- All data is fetched server-side; no client-side secrets

## License

MIT
