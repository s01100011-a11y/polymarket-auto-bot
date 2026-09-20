# BK Game List Muting + Title — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mute non-filtered dimension columns in the BK game list, and add a title with bet/game counts.

**Architecture:** Add `data-dim` attributes to dimension `<td>` cells during table rendering, then in `_levApplyFilters()` apply muting based on which filters are active. Add a title div above the table updated on filter changes.

**Tech Stack:** Vanilla JS, existing dashboard patterns.

**Spec:** `docs/superpowers/specs/2026-07-04-bk-game-list-muting-title-design.md`

---

### Task 1: Add data-dim Attributes + Title + Muting Logic

**Files:**
- Modify: `dashboard.html`

- [ ] **Step 1: Add `data-dim` to dimension cells in `loadLeadingEv()`**

Find line 8513 (Context column):
```javascript
        html += '<td style="text-align:center;font-size:0.76em">' + (ctx === 'leading' ? 'Lead' : 'Trail') + '</td>';
```
Change to:
```javascript
        html += '<td data-dim="margin_context" style="text-align:center;font-size:0.76em">' + (ctx === 'leading' ? 'Lead' : 'Trail') + '</td>';
```

Find line 8515 (Entry Min):
```javascript
        html += '<td style="text-align:center">' + (strat.entry_minute || '—') + '\'</td>';
```
Change to:
```javascript
        html += '<td data-dim="entry_minute" style="text-align:center">' + (strat.entry_minute || '—') + '\'</td>';
```

Find line 8517 (Spread Price):
```javascript
        html += '<td style="text-align:center;font-weight:600;color:#ff9800">$' + (strat.spread_price != null ? strat.spread_price.toFixed(2) : '—') + '</td>';
```
Change to:
```javascript
        html += '<td data-dim="spread_price" style="text-align:center;font-weight:600;color:#ff9800">$' + (strat.spread_price != null ? strat.spread_price.toFixed(2) : '—') + '</td>';
```

Find line 8518 (Margin at Entry):
```javascript
        html += '<td style="text-align:center">' + (strat.margin_at_entry || '—') + '</td>';
```
Change to:
```javascript
        html += '<td data-dim="margin" style="text-align:center">' + (strat.margin_at_entry || '—') + '</td>';
```

Find line 8519 (EV Edge):
```javascript
        html += '<td style="text-align:center;font-weight:600;color:' + evC + '">' + evEdge + '</td>';
```
Change to:
```javascript
        html += '<td data-dim="ev_edge" style="text-align:center;font-weight:600;color:' + evC + '">' + evEdge + '</td>';
```

- [ ] **Step 2: Add title div and set initial counts**

Find line 8535 where the table is written to the DOM:
```javascript
    html += '</tbody></table></div>';
    histEl.innerHTML = html;
```

Change to:
```javascript
    html += '</tbody></table></div>';
    const totalBets = games.reduce((n, g) => n + (g.leading_strategy?.triggered ? 1 : 0) + (g.trailing_strategy?.triggered ? 1 : 0), 0);
    histEl.innerHTML = '<div id="lev-history-title" data-total-games="' + games.length + '" data-total-bets="' + totalBets + '" style="font-size:0.88em;margin-bottom:6px"><strong style="color:var(--nrl-green)">Spread Strategy — Game History</strong> <span style="color:var(--muted)">(' + totalBets + ' bets / ' + games.length + ' games)</span></div>' + html;
```

- [ ] **Step 3: Add muting + title update logic to `_levApplyFilters()`**

Find the end of `_levApplyFilters()` — after the localStorage save (line 8625):
```javascript
  localStorage.setItem('lev_filters', JSON.stringify({side: fSide, role: fRole, margin: fMargin, entry: fEntry, ev: fEv, context: fContext, price: fPrice}));
}
```

Add BEFORE the closing `}`:

```javascript

  // Mute non-filtered dimension columns (#232)
  const activeDims = new Set();
  if (fSide) activeDims.add('side');
  if (fRole) activeDims.add('role');
  if (fMargin) activeDims.add('margin');
  if (fEntry) activeDims.add('entry_minute');
  if (fEv) activeDims.add('ev_edge');
  if (fContext) activeDims.add('margin_context');
  if (fPrice) activeDims.add('spread_price');
  const hasAnyDimFilter = activeDims.size > 0;
  table.querySelectorAll('td[data-dim]').forEach(td => {
    td.style.color = hasAnyDimFilter && !activeDims.has(td.dataset.dim) ? 'var(--muted)' : '';
  });

  // Update title with filtered count (#232)
  const titleEl = document.getElementById('lev-history-title');
  if (titleEl) {
    const totalGames = parseInt(titleEl.dataset.totalGames || '0');
    const totalBets = parseInt(titleEl.dataset.totalBets || '0');
    const betCount = hasFilter ? visibleBets : totalBets;
    titleEl.innerHTML = '<strong style="color:var(--nrl-green)">Spread Strategy — Game History</strong> <span style="color:var(--muted)">(' + betCount + ' bets / ' + totalGames + ' games)</span>';
  }
```

Note: The muting must NOT override the EV Edge and Spread Price cells' existing colors when they are active. The `td.style.color = ''` reset handles this — when not muted, the inline `color` from the original render takes precedence (since it's set in the `style` attribute of the `<td>`). Wait — both EV Edge and Spread Price have color set inline. If we set `td.style.color = ''`, the inline color from rendering is ALREADY in the style string. The issue is that `data-dim` is added alongside the existing style. When muting, we SET `color:var(--muted)` which overrides. When un-muting, we need to restore the original color.

**Fix:** For cells that have their own color logic (EV Edge, Spread Price), store the original color in a data attribute:

Change the EV Edge cell to:
```javascript
        html += '<td data-dim="ev_edge" data-orig-color="' + evC + '" style="text-align:center;font-weight:600;color:' + evC + '">' + evEdge + '</td>';
```

Change the Spread Price cell to:
```javascript
        html += '<td data-dim="spread_price" data-orig-color="#ff9800" style="text-align:center;font-weight:600;color:#ff9800">$' + (strat.spread_price != null ? strat.spread_price.toFixed(2) : '—') + '</td>';
```

Then update the muting logic to restore original color:
```javascript
  table.querySelectorAll('td[data-dim]').forEach(td => {
    if (hasAnyDimFilter && !activeDims.has(td.dataset.dim)) {
      td.style.color = 'var(--muted)';
    } else {
      td.style.color = td.dataset.origColor || '';
    }
  });
```

- [ ] **Step 4: Commit**

```bash
git add dashboard.html
git commit -m "feat: BK game list — mute non-filtered dimension columns + table title (#232)"
```

---

### Task 2: Testing + CHANGES.md + Issue Close

**Files:**
- Modify: `CHANGES.md`

- [ ] **Step 1: Restart and browser test**

```bash
systemctl --user restart nrl-dashboard.service
```

1. Load dashboard → Spread Strategy → BK Spread
2. Verify title shows "Spread Strategy — Game History (X bets / Y games)"
3. No filters active → all columns normal color
4. Set Side filter to "Home" → Context, Entry Min, Spread Price, Margin, EV Edge columns should be muted (grey)
5. Set Margin filter to "0-6" → Margin column un-mutes (now active), others still muted except Side
6. Reset all → all columns back to normal
7. Click a profitable pattern → filters set, title updates count, unfiltered dimension columns muted
8. EV Edge and Spread Price retain their custom colors when their filter IS active

- [ ] **Step 2: Update CHANGES.md**

Add to the existing #231 entry or create new #232 entry:

```markdown
## 2026-07-04 — BK Spread: game list muting + title (#232)

- **`dashboard.html`**: Dimension columns (Context, Entry Min, Spread Price, Margin, EV Edge) mute to grey when their filter is not active — highlights the filtered dimensions. Non-dimension columns never muted. Added "Spread Strategy — Game History (X bets / Y games)" title above table, updates with filter count.

**Files changed:** `dashboard.html`
```

- [ ] **Step 3: Commit and push**

```bash
git add CHANGES.md
git commit -m "docs: CHANGES.md for game list muting + title (#232)"
git push
```

- [ ] **Step 4: Close issue**

```bash
gh issue comment 232 --body "Implementation complete. Dimension columns mute when not filtered, title shows bet/game counts."
gh issue close 232
```
