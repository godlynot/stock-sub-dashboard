# Stock Sub Dashboard

A mobile-friendly web dashboard that shows trending tickers and top posts from r/wallstreetbets and other stock subreddits. Data comes from two **free, no-auth** public APIs.

## What you get

- 📊 Trending tickers (mentions delta vs. 24h ago)
- 🏆 Cross-sub leaderboard (which tickers are being talked about everywhere)
- 🔥 Per-subreddit top tickers (WSB, stocks, investing, options, StockMarket, pennystocks, SPACs)
- 🌟 Top recent posts with score, comments, and links to Reddit
- 📱 Mobile-friendly dark dashboard — open from your phone

## How it works

```
┌────────────────────┐    ┌─────────────────────┐
│   ApeWisdom API    │    │   Arctic Shift API  │
│  (ticker rankings, │    │  (post titles/links │
│   24h deltas)      │    │   from Reddit hist) │
└─────────┬──────────┘    └─────────┬───────────┘
          │                         │
          └────────────┬────────────┘
                       ▼
              ┌─────────────────┐
              │   dashboard.py  │  ← single Flask app
              │  (in-memory     │     (no DB needed)
              │   cache 1h)     │
              └────────┬────────┘
                       ▼
                  Browser / Phone
```

**Why no Reddit API?** Reddit now blocks anonymous JSON access from most IPs and requires OAuth. Instead, we use two services that already do the hard work of mining Reddit:

- **ApeWisdom** (apewisdom.io) — a public service that has already done ticker extraction for r/wallstreetbets and 100+ other stock subs. Free JSON API with rankings + 24h deltas.
- **Arctic Shift** (photon-reddit.com) — a Pushshift replacement archiving Reddit posts. Free JSON API for post titles/links.

The dashboard calls these directly at request time, with a 1-hour in-memory cache. **No database, no disk, no cron job needed** — fits on Render's free tier with no extra services.

## Local setup

```bash
pip install -r requirements.txt
python dashboard.py
```

Then open `http://localhost:5000` in your browser.

- First request takes ~10-30s (fetches from all APIs)
- Subsequent requests are instant (served from cache)
- Cache refreshes every hour, or click "↻ Refresh" in the header to force

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
- `CACHE_TTL` — seconds to cache before re-fetching (default 3600 = 1h)
- `POSTS_PER_SUB` — how many recent top posts per sub
- `POST_LOOKBACK_DAYS` — how far back to look for posts

## Limitations & honesty

- **WSB post content is sparse** on Arctic Shift. We can show r/wallstreetbets ticker rankings (via ApeWisdom — works great) but recent WSB post titles may be missing. Other subs are well-covered.
- **Cache is per-process.** On Render's free tier, the web service can have multiple workers, and each has its own cache. With our `workers=1` config, this is fine.
- **Cache resets on deploy.** Every time Render redeploys (or the service wakes from sleep), the cache is empty and the first request will be slow.
- **Free tier Render sleeps** after 15 min idle — first load takes ~30s.
- **Third-party dependency:** if ApeWisdom or Arctic Shift go down, our dashboard fails. The error is shown gracefully.
- **No 7-day historical trend** in this version (would need persistent storage).
- **Not financial advice.** Use as a sentiment signal, not a trading signal.

## Project layout

```
reddit-scraper/
├── dashboard.py        # Flask web service — fetches APIs + serves UI
├── requirements.txt
├── render.yaml         # Render deployment (one web service, no cron, no disk)
├── Dockerfile          # Alternative explicit Docker build
└── README.md
```

## License

MIT
