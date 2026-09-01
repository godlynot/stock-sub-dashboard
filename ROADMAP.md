# Stock Sub Dashboard — Roadmap

Current state: live on Render with prices, sparklines, watchlist. No auth, no storage, $0/month.

## What we're missing (ordered by impact)

| # | Feature | Impact | Effort | Cost | Why |
|---|---|---|---|---|---|
| 1 | "Why is it popular" — per-ticker post index | 🔥 High | ~3h | $0 | Biggest missing piece. We have rankings but no reason. |
| 2 | Search/filter bar | 🔥 High | ~30m | $0 | 200 tickers is a wall of text. Need to filter. |
| 3 | Timeframe selector for sparklines | • Medium | ~1h | $0 | 1mo is good default but user should choose. |
| 4 | AI summaries (Groq) | • Medium | ~2h | $0 (free tier) | One-line "why trending" if we have post context. |
| 5 | Macro panel (10Y, VIX, S&P) | • Medium | ~1.5h | $0 | Context for individual moves. |
| 6 | Dedicated /ticker/<T> pages | • Medium | ~2h | $0 | Modals suck on mobile. Pages are linkable. |
| 7 | Polish (auto-refresh toggle, skeletons, errors) | low | ~3h | $0 | Quality of life. |
| — | Push notifications | out of scope | — | — | Too complex for this scope. |

---

## Phase 1 — "Why is it popular" (3h, $0) ← **RECOMMENDED NEXT**

**Problem:** Click a trending ticker, get nothing useful. We don't have post-per-ticker data.

**What we'll build:**

1. Pull more posts per sub: 30 → 150 (5x more)
2. Build a per-ticker index in memory: `{ticker: [post, post, ...]}`
3. Add to `/api/stats` payload: `ticker_posts: {TSLA: [top 3 posts], ...}`
4. Show top 3 post titles under each trending ticker card:
   ```
   $TSLA  🔥 +5 mentions
   $367.95 +18.23%   ↑↑↑ (sparkline)
   ── Why it's trending ──
   • "Tesla unveils new Model 3 refresh"
   • "My $50k YOLO on TSLA calls, AMA"
   • "Why I'm selling all my TSLA shares"
   ```
5. Click any post title → opens Reddit thread

**Files changed:** `dashboard.py` (server aggregation + frontend card template)

**No new dependencies, no API keys, no storage.**

---

## Phase 2 — Search + filter (1.5h, $0)

**Two improvements:**

1. **Search bar at top:** type to filter tickers by symbol or name. Pure client-side JS, instant.
2. **Timeframe selector** (1w / 1mo / 3mo / 6mo) — a small toggle above the per-sub grid. Server caches per (ticker, timeframe).

**Files changed:** `dashboard.py` (small server change, more client JS)

---

## Phase 3 — AI summaries (2h, $0 with free Groq key)

**Optional.** Only valuable if Phase 1 is done (need post context to summarize).

**How:**
- User adds `GROQ_API_KEY` to env vars (or skips — graceful degradation)
- For top 5 trending tickers, send top 3 posts to Groq Llama 3.1 8B
- Prompt: *"Given these Reddit posts, summarize in 1 sentence why this ticker is trending."*
- Show result as italic subtitle on the trending card
- Cached for 5 min (same as prices)

**Cost:** $0. Groq free tier = 14,400 requests/day. We'd use ~1,500/day.

**Risk:** Hallucinations. Mitigated by: showing the source posts right below the AI summary so user can verify.

---

## Phase 4 — Macro panel (1.5h, $0)

**A thin context strip at the top:**
```
10Y Treasury  4.73%  ▼  |  S&P 500  $767.05  ▲ +0.5%  |  VIX  14.20  ▼
```

**Sources:** FRED CSV (10Y) + Yahoo Finance (S&P, VIX). All free, no auth.

---

## Phase 5 — Dedicated /ticker/AAPL pages (2h, $0)

**Right now** the ticker detail is a modal. On mobile, modals are fiddly.

**Build:** A real `/ticker/AAPL` route with the same content but in a full page. Shareable, linkable, works on every device.

**File changes:** new route in `dashboard.py`, new template.

---

## Phase 6 — Polish (3h, $0)

- Auto-refresh toggle (off by default to be nice to free APIs)
- Skeleton loading states instead of "Loading..."
- Better error messages (toast at top instead of replacing the whole page)
- Touch-target improvements for mobile
- Pull-to-refresh on mobile
- Settings drawer (cache TTL, theme color, etc.)

---

## My recommendation

**Do Phase 1 now.** It's the single biggest missing piece and the user's question ("why is it popular") maps directly to it. ~3 hours, $0, no new dependencies.

After Phase 1, ask which phase the user wants next. Each phase is independent and shippable.

Want me to start Phase 1?