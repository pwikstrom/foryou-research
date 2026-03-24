# Timeline Analysis Feature — Handoff Spec

## Overview

Add an **auto-report** capability to the existing timeline charts. When triggered, the system analyses *all* available categories (not just the ones currently selected in the chart), ranks them by how dynamically interesting their data is, and renders a report view showing the top N most interesting categories with visual annotations and plain-English findings.

Heavy computation runs **offline** and is cached as JSON. The UI fetches from cache and renders — no significant computation at request time.

---

## User interaction model

- Each timeline chart gets an **"Analyze" button** in the existing icon cluster (top-right of chart)
- Clicking enters **report mode**: the chart switches to showing the auto-selected top categories (not the user's current selection), with overlays rendered
- A **findings panel** appears below the chart with one card per selected category
- A **top-N selector** (e.g. 2 / 3 / 4 / 5 buttons) lets the user expand or narrow the report
- Clicking Analyze again exits report mode and restores the user's original category selection
- The chart y-axis in report mode scales to the selected categories' value range (not 0–100%), since these are absolute share values not a stacked percentage view

---

## Offline computation pipeline

Run this for every chart type (content category, main activity, Aussie relevance, etc.) whenever the underlying data cache is refreshed.

### Per-category metrics to compute

For each category's time series `vals` (array of share % values, one per time bucket):

#### 1. Linear regression (trend)
```python
import numpy as np

def linreg(vals):
    x = np.arange(len(vals))
    slope, intercept = np.polyfit(x, vals, 1)
    return {
        "slope": slope,                        # pp per time unit
        "intercept": intercept,
        "total_change": slope * (len(vals)-1), # pp across full period
        "mean": float(np.mean(vals)),
    }
```

#### 2. Anomaly detection (z-scores)
```python
def anomalies(vals, threshold=1.75):
    mean, std = np.mean(vals), np.std(vals)
    results = []
    for i, v in enumerate(vals):
        z = (v - mean) / std if std > 0 else 0
        if abs(z) > threshold:
            results.append({"index": i, "value": round(v,1), "z": round(z,2), "mean": round(mean,1)})
    return sorted(results, key=lambda r: abs(r["z"]), reverse=True)
```

Threshold of 1.75 works well for weekly/fortnightly share data. Tune if your series are short (< 20 points) — consider 1.5.

#### 3. Structural break detection
```python
def detect_break(vals):
    best_i, best_delta = 0, 0
    n = len(vals)
    for i in range(4, n-4):
        m1 = np.mean(vals[:i])
        m2 = np.mean(vals[i:])
        if abs(m2 - m1) > abs(best_delta):
            best_delta = m2 - m1
            best_i = i
    return {
        "index": best_i,
        "delta": round(best_delta, 1),         # pp shift
        "mean_before": round(np.mean(vals[:best_i]), 1),
        "mean_after":  round(np.mean(vals[best_i:]), 1),
    }
```

For more rigorous detection (better calibrated thresholds, handles multiple breaks) use `ruptures` with PELT:
```python
import ruptures as rpt

def detect_breaks_pelt(vals, penalty=3, min_size=3):
    signal = np.array(vals).reshape(-1,1)
    model = rpt.Pelt(model="l2", min_size=min_size).fit(signal)
    breakpoints = model.predict(pen=penalty)
    return breakpoints[:-1]  # last value is always len(vals) by convention
```

#### 4. Volatility
```python
def volatility(vals):
    return {"std": round(float(np.std(vals)), 2), "mean": round(float(np.mean(vals)), 2)}
```

### Interestingness score

Combine the above into a single rank score per category:

```python
def score(cat_metrics):
    trend_score     = abs(cat_metrics["trend"]["total_change"]) * 1.8
    anomaly_score   = max((a["z"] for a in cat_metrics["anomalies"]), default=0, key=abs) * 4.0
    break_score     = abs(cat_metrics["break"]["delta"]) * 1.2
    volatility_score = cat_metrics["volatility"]["std"] * 0.8
    return trend_score + abs(anomaly_score) + break_score + volatility_score
```

Tune the weights against your actual data. In practice structural breaks and strong trends tend to be the most visually salient; anomaly score can dominate if your data has rare but large spikes.

---

## Cache schema

Store one JSON file (or cache key) per chart type:

```json
{
  "chart_id": "content_category",
  "generated_at": "2024-12-23T10:00:00Z",
  "time_labels": ["13/01", "27/01", "..."],
  "categories": [
    {
      "id": "performance",
      "label": "Performance",
      "color": "#E9B224",
      "count": 1469,
      "vals": [18.2, 19.1, "..."],
      "score": 84.3,
      "trend": {
        "slope": 0.9,
        "total_change": 22.5,
        "mean": 31.2
      },
      "anomalies": [
        {"index": 15, "value": 51.0, "z": 2.4, "mean": 31.2}
      ],
      "break": {
        "index": 14,
        "delta": 9.1,
        "mean_before": 25.3,
        "mean_after": 34.4
      },
      "volatility": {
        "std": 5.1,
        "mean": 31.2
      }
    }
  ]
}
```

Categories array should be **pre-sorted by score descending** so the UI just slices `categories.slice(0, N)` without needing to sort client-side.

---

## Chart overlay logic

In report mode the chart renders the top N categories as **line charts** (not stacked bars — categories are now selected independently by the algorithm, not chosen to sum to 100%).

| Element | When to render | Visual spec |
|---|---|---|
| Line | Always | 1.5px solid, category colour, opacity 0.9 |
| Area fill | Always | Same colour, opacity 0.07, fills down to y-axis min |
| Trend line | `abs(total_change) > 4` | 1px dashed (4px on, 3px off), same colour, opacity 0.5 |
| Break marker | `abs(delta) > 4` | Vertical dashed line at break index, 0.8px, opacity 0.5 |
| Anomaly circle | `abs(z) > 1.75` | Outer ring r=5 (stroke only) + inner dot r=2 (filled), same colour |

Y-axis: scale to `[min(allVals) - 5, max(allVals) + 5]`, clamped to `[0, 65]`. Show 4–5 gridlines with % labels.

---

## Findings card logic

One card per selected category, in rank order. Each card shows:
- Rank number + colour dot + category label
- Badge tags (see below)
- Bullet findings with numbers included

### Badge + finding rules

| Condition | Badge | Finding text pattern |
|---|---|---|
| `abs(total_change) > 4` and positive | ↑ Rising | "Overall upward trend at X pp/fortnight — total shift of +Y pp over the period." |
| `abs(total_change) > 4` and negative | ↓ Falling | "Overall downward trend at X pp/fortnight — total shift of −Y pp over the period." |
| `abs(break.delta) > 4` | ⋮ Step change | "Step change around [DATE]: share jumped/dropped from ~A% to ~B% (+C pp) and held." |
| anomalies present | ◎ N spike/s | "Peak/Trough at [DATE]: X% vs. mean Y% (+Z σ)." — one line per outlier, max 2 |
| `std > 4.5` | ~ Volatile | "High week-to-week variation — std dev X pp around mean of Y%." |
| none of the above | Stable | "No significant dynamics detected. Steady around X% with low variation." |

Multiple badges can apply to the same category. Show all that apply.

---

## Implementation notes

- The existing chart components use stacked bar rendering. Report mode should swap in a line/area renderer for the same time axis — consider whether this is a mode flag on the existing component or a separate component that shares the axis/label logic.
- The top-N selector state is local UI state, no server call needed — the full sorted category list is already in the cache payload.
- Consider adding a `"data_window"` field to the cache payload (e.g. `"last_90_days"` vs `"full_year"`) so the UI can display what period the ranking was computed over. Patterns that are interesting in the full-year view may differ from what's interesting in the last month.
- If you later want to add a conversational layer ("why did performance spike in September?"), the cache payload already contains everything needed to construct a tight context prompt — no additional computation required.

---

## Suggested implementation order

1. **Offline script** — write the Python scoring pipeline, validate output JSON against the schema above, run against real data
2. **Cache endpoint** — expose the JSON via whatever endpoint pattern the existing cache uses
3. **Report mode toggle** — wire the Analyze button to fetch cache and flip chart into report mode, restore on second click
4. **Line chart renderer** — render top-N categories as lines with the overlay elements
5. **Findings panel** — render the cards from the cache payload with badge + bullet logic
6. **Top-N selector** — add the 2/3/4/5 buttons, re-render from already-fetched cache (no new fetch)
7. **Polish** — y-axis rescaling, legend, subtitle showing N-of-total and data window
