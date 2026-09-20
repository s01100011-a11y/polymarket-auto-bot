# BK Spread Game List — Mute Non-Filtered Columns, Table Title, Game Count

**Issue:** NRL#232
**Date:** 2026-07-04
**Status:** Design approved
**Parent:** NRL#231 (threshold consistency, closed)

## Overview

Three UI enhancements to the BK Spread game history list:
1. Mute dimension columns that are not actively filtered
2. Add table title with bet/game counts
3. Counts update when filters change

## 1. Column Muting

### Concept

When dimension filters are active, the filtered dimensions are "highlighted" (normal text) while unfiltered dimension columns are muted (`color:var(--muted)`). This visually draws attention to the dimensions that matter for the current filter/pattern selection.

### Dimension Column Mapping

| Column | `data-dim` attribute | Filter ID |
|--------|---------------------|-----------|
| Context | `margin_context` | `lev-f-context` |
| Entry Min | `entry_minute` | `lev-f-entry` |
| Spread Price | `spread_price` | `lev-f-price` |
| Margin at Entry | `margin` | `lev-f-margin` |
| EV Edge | `ev_edge` | `lev-f-ev` |

Note: Side and Role don't have visible columns (they're data attributes only), so they can't be muted visually. If their filter is active, it still affects row visibility but no column muting applies.

### Non-Dimension Columns (never muted)

Date, Teams, Final Score, Final Margin, Team, Spread Line, Covered, Cover Margin, P&L.

### Behaviour

- **No filters active:** all columns normal (no muting)
- **Any filter(s) active:** dimension columns whose filter is NOT active get `color:var(--muted)`. Dimension columns whose filter IS active stay normal.
- Muting applies to `<td>` cells in the table body, not headers.

### Implementation

**In `loadLeadingEv()` row rendering**, add `data-dim` to dimension cells:

```javascript
html += '<td data-dim="margin_context" ...>' + ctx + '</td>';
html += '<td data-dim="entry_minute" ...>' + entryMin + '</td>';
html += '<td data-dim="spread_price" ...>' + spreadPrice + '</td>';
html += '<td data-dim="margin" ...>' + marginAtEntry + '</td>';
html += '<td data-dim="ev_edge" ...>' + evEdge + '</td>';
```

**In `_levApplyFilters()`**, after the row visibility loop, apply muting:

```javascript
const activeDims = new Set();
if (fSide) activeDims.add('side');
if (fRole) activeDims.add('role');
if (fMargin) activeDims.add('margin');
if (fEntry) activeDims.add('entry_minute');
if (fEv) activeDims.add('ev_edge');
if (fContext) activeDims.add('margin_context');
if (fPrice) activeDims.add('spread_price');
const hasAnyFilter = activeDims.size > 0;
table.querySelectorAll('td[data-dim]').forEach(td => {
  td.style.color = hasAnyFilter && !activeDims.has(td.dataset.dim) ? 'var(--muted)' : '';
});
```

## 2. Table Title

### Format

```
Spread Strategy — Game History (11 bets / 41 games)
```

- "11 bets" = number of visible triggered rows (after filters)
- "41 games" = total games monitored (from aggregate data, constant)
- When no filters active: shows total triggered bets

### Implementation

Add a title `<div>` with ID `lev-history-title` above the table in the `lev-history` section. Set in two places:

1. **`loadLeadingEv()`** — after table built, set initial title with total bets / games
2. **`_levApplyFilters()`** — update title with filtered bet count

```javascript
// In _levApplyFilters(), after counting visibleBets:
const titleEl = document.getElementById('lev-history-title');
if (titleEl) {
  const totalGames = parseInt(titleEl.dataset.totalGames || '0');
  const betCount = hasFilter ? visibleBets : parseInt(titleEl.dataset.totalBets || '0');
  titleEl.innerHTML = '<strong style="color:var(--nrl-green)">Spread Strategy — Game History</strong>' +
    ' <span style="color:var(--muted)">(' + betCount + ' bets / ' + totalGames + ' games)</span>';
}
```

The title div stores `data-total-games` and `data-total-bets` as attributes set during `loadLeadingEv()`.

## Files Changed

| File | Changes |
|------|---------|
| `dashboard.html` | Add `data-dim` attributes to dimension `<td>` cells in `loadLeadingEv()`. Add `lev-history-title` div. Update `_levApplyFilters()` with muting logic + title update. |

## Out of Scope

- Muting table headers (only body cells)
- Server-side changes (purely client-side feature)
- Historical Win Rate view (BK only)
