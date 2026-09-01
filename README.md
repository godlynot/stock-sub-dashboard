# Stock Sub Dashboard

A mobile-friendly web dashboard that shows trending tickers and top posts from r/wallstreetbets and other stock subreddits. Data flows from two **free, no-auth** public APIs.

## What you get

- 📊 Trending tickers (mentions delta vs. previous snapshot)
- 🏆 Cross-sub leaderboard (which tickers are being talked about everywhere)
- 🔥 Per-subreddit top tickers (WSB, stocks, investing, options, StockMarket, pennystocks, SPACs)
- 🌟 Top recent posts with score, comments, and links to Reddit
- 📱 Mobile-friendly dark dashboard — open from your phone

## Architecture

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
              │  data_fetcher.py │  ← weekly cron
              │  (no auth!)      │
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
              │  SQLite (local) │
              │  data/dashboard.db│
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
              │  dashboard.py    │  ← web service
              │  (Flask, gunicorn)│
              └────────┬────────┘
                       ▼
                  Browser / Phone
```

**Why no Reddit API?** Reddit now blocks anonymous JSON access from most IPs and requires you to register a script app + OAuth. Instead, we use two services that already do the hard work of mining Reddit:

- **ApeWisdom** (apewisdom.io) — a public service that has already done the ticker extraction for r/wallstreetbets and 100+ other stock subs. They expose a free JSON API with rankings + 24h deltas.
- **Arctic Shift** (photon-reddit.com) — a Pushshift replacement that archives Reddit posts. We use it to fetch the actual post titles/links so the dashboard can link out.

This means: **no Reddit credentials, no auth dance, no rate-limit fights, fresh data.**

## Local setup

```bash
# Install
pip install -r requirements.txt

# (Optional) Run a one-time fetch to populate the DB
python data_fetcher.py

# Start dashboard at http://localhost:5000
python dashboard.py
```

Then open `http://localhost:5000` in your browser (or phone, if on same WiFi).

### CLI usage

```bash
python data_fetcher.py                  # fetch from both sources
python data_fetcher.py --source apewisdom  # only ticker rankings
python data_fetcher.py --source arctic     # only post content
python data_fetcher.py --sub wallstreetbets  # one subreddit
python data_fetcher.py --stats          # DB summary
python data_fetcher.py --ticker TSLA    # recent posts about a ticker
```

## Deploy to Render (free)

1. Push this folder to a new GitHub repo
2. Sign up at https://render.com (free, GitHub login)
3. Click **New → Blueprint Instance**, point at your repo
4. Render reads `render.yaml` and creates both services:
   - `stock-sub-dashboard` (web) — your dashboard
   - `weekly-scrape` (cron) — refreshes data every Sunday midnight UTC
5. (Optional) After first deploy, manually trigger a fetch via Render shell: `python data_fetcher.py`
6. Open your dashboard URL on your phone

**Note:** Render's free tier web service "sleeps" after 15 min of inactivity — the first dashboard load will take ~30s while the container wakes up. After that it's instant.

## Customizing

Edit `.env` (or set env vars in Render):

- `APEWISDOM_SUBS` — comma-separated subs to track ticker rankings on
- `ARCTIC_SUBS` — comma-separated subs to fetch post content from
- `PAGES_PER_SUB` — how many pages of 100 tickers to pull (1 = top 100)
- `POSTS_PER_SUB` — how many recent top posts per sub
- `POST_LOOKBACK_DAYS` — how far back to look for posts

## Limitations & honesty

- **WSB post content is sparse** on Arctic Shift. We can show r/wallstreetbets ticker rankings (via ApeWisdom — works great) but recent WSB post titles may be missing. Other subs (stocks, investing, options, etc.) are well-covered.
- **Trending deltas** need 2+ snapshots to show movement. On a fresh deploy, wait 24h after the first fetch for the trending section to populate.
- **Free tier Render sleeps.** First dashboard load after 15+ min idle takes ~30s.
- **Third-party dependency:** if ApeWisdom or Arctic Shift go down, our dashboard goes too. We mitigate by caching snapshots in SQLite.
- **Not financial advice.** Use as a sentiment signal, not a trading signal.

## Project layout

```
reddit-scraper/
├── data_fetcher.py     # Pulls from ApeWisdom + Arctic Shift, writes SQLite
├── dashboard.py        # Flask web dashboard
├── requirements.txt
├── render.yaml         # Render deployment (web + weekly cron)
├── Dockerfile          # Alternative explicit Docker build
├── .env.example        # Template for config
├── data/               # SQLite DB (gitignored)
│   └── dashboard.db
└── README.md
```

## License

MIT
