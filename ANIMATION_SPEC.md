# Animation Improvements Spec Sheet

## Project: Stock Sub Dashboard - Animation Polish

### Current State
- ✅ Skeleton shimmer loading
- ✅ Price flash (green/red on change)
- ✅ Card hover (lift + shadow)
- ✅ Heatmap hover (scale + shadow)
- ✅ Price flash on change
- ✅ Collapsible sections
- ✅ Smooth scroll for nav
- ✅ Count-up animation for numbers
- ✅ Skeleton shimmer

### Gaps to Address
1. **Staggered card entrance** - Cards pop in all at once
2. **Page transition** `/ticker/<T>` - Instant, jarring
3. **Initial page load** - Skeleton → instant pop
4. **Heatmap hover** - Slightly too dramatic

---

## Spec: Animation Improvements

### 1. Staggered Card Entrance (CSS Only)
**Target**: `.ticker-grid .ticker-card` and `.skeleton-card`

```css
/* Card entrance animation */
@keyframes cardEntrance {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}

.ticker-grid .ticker-card {
  opacity: 0;
  animation: cardEntrance 0.35s ease forwards;
}

/* Stagger delays - covers up to 12 cards */
.ticker-grid .ticker-card:nth-child(1)  { animation-delay: 0ms; }
.ticker-grid .ticker-card:nth-child(2)  { animation-delay: 30ms; }
.ticker-grid .ticker-card:nth-child(3)  { animation-delay: 60ms; }
.ticker-grid .ticker-card:nth-child(4)  { animation-delay: 90ms; }
.ticker-grid .ticker-card:nth-child(5)  { animation-delay: 120ms; }
.ticker-grid .ticker-card:nth-child(6)  { animation-delay: 150ms; }
.ticker-grid .ticker-card:nth-child(7)  { animation-delay: 150ms; }
.ticker-grid .ticker-card:nth-child(8)  { animation-delay: 180ms; }
.ticker-grid .ticker-card:nth-child(9)  { animation-delay: 180ms; }
.ticker-grid .ticker-card:nth-child(10) { animation-delay: 210ms; }
.ticker-grid .ticker-card:nth-child(11) { animation-delay: 210ms; }
.ticker-grid .ticker-card:nth-child(12) { animation-delay: 240ms; }
.ticker-grid .ticker-card:nth-child(n+13) { animation-delay: 240ms; }

/* Same for skeleton cards */
.skeleton-card {
  opacity: 0;
  animation: cardEntrance 0.35s ease forwards;
}
.skeleton-card:nth-child(1)  { animation-delay: 0ms; }
.skeleton-card:nth-child(2)  { animation-delay: 30ms; }
.skeleton-card:nth-child(3)  { animation-delay: 60ms; }
.skeleton-card:nth-child(4)  { animation-delay: 90ms; }
.skeleton-card:nth-child(5)  { animation-delay: 120ms; }
.skeleton-card:nth-child(6)  { animation-delay: 150ms; }
.skeleton-card:nth-child(7)  { animation-delay: 150ms; }
.skeleton-card:nth-child(8)  { animation-delay: 180ms; }
.skeleton-card:nth-child(8)  { animation-delay: 180ms; }
.skeleton-card:nth-child(9)  { animation-delay: 180ms; }
.skeleton-card:nth-child(10) { animation-delay: 210ms; }
.skeleton-card:nth-child(11) { animation-delay: 210ms; }
.skeleton-card:nth-child(12) { animation-delay: 240ms; }
.skeleton-card:nth-child(n+13) { animation-delay: 240ms; }

@keyframes cardEntrance {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}
```

### 2. Page Transition for `/ticker/<T>` (Ticker Detail Page)

```css
/* Add to TICKER_PAGE_TEMPLATE <style> */
@keyframes pageFadeIn {
  from { opacity: 0; transform: translateY(16px); }
  to { opacity: 1; transform: none; }
}

main {
  animation: pageFadeIn 0.3s ease forwards;
}
```

### 3. Initial Load Stagger (Skeleton Cards)
- Already covered by skeleton-card stagger above

### 4. Tone Down Heatmap Hover
```css
.heatmap-cell:hover {
  transform: scale(1.05);  /* was scale(1.1) translateY(-2px) */
  box-shadow: 0 4px 16px rgba(0,0,0,0.4);  /* was 0 8px 24px rgba(0,0,0,0.5) */
}
```

### 5. Card Hover - Subtle
```css
.ticker-card:hover {
  border-color: var(--muted);
  background: var(--panel);
  /* REMOVE: transform: translateY(-2px); box-shadow: 0 6px 20px... */
}
```

---

## Implementation Checklist

- [ ] Add staggered card entrance CSS (ticker cards + skeleton cards)
- [ ] Add page fade-in for `/ticker/<T>` page
- [ ] Tone down heatmap hover (scale 1.05, no translateY)
- [ ] Tone down ticker card hover (remove lift/shadow, just background)
- [ ] Tone down heatmap hover (scale 1.05, smaller shadow)
- [ ] Verify hermes verify passes
- [ ] Deploy to Render

---

## Testing Checklist

- [ ] Initial load: skeleton cards stagger in
- [ ] After data loads: ticker cards stagger in
- [ ] Navigate to `/ticker/AAPL` - page fades in smoothly
- [ ] Hover ticker card - subtle background change, no lift
- [ ] Hover heatmap cell - subtle scale 1.05, no lift
- [ ] Watchlist cards stagger in
- [ ] Section cards stagger in (macro, heatmap, earnings, etc.)
- [ ] hermes verify passes
- [ ] Render deploy successful

---

## Files to Modify

1. `dashboard.py` - CSS section (add animations)
2. `dashboard.py` - TICKER_PAGE_TEMPLATE (add page fade-in)
3. `dashboard.py` - Ticker card hover CSS
3. `dashboard.py` - Heatmap hover CSS

---

## Rollback Plan
If issues: `git revert HEAD` - all changes are CSS only, no JS logic changes.

---

## Effort Estimate
- CSS changes: 10 minutes
- Testing: 5 minutes
- Deploy: 2 minutes
- **Total: ~17 minutes**