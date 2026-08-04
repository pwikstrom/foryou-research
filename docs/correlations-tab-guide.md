# The Correlations Tab — A Researcher's Guide

This guide explains every control on the Correlations tab, what the numbers
mean, and how to use the tab to answer real research questions about
algorithmically curated video feeds. It is written for social-science
researchers; no statistics beyond an introductory methods course is assumed,
and the places where the tab protects you from common inferential mistakes are
called out explicitly.

---

## 1. What you are looking at: the unit of analysis

**Every dot, cell, and table row on this tab describes a *collection-day*,
never an individual video.**

The tab groups each 
collection's annotated videos by calendar day and averages them. Days with fewer than 10 annotated videos are dropped
(the threshold is configurable; the banner at the top of the tab always states
the current value). The banner restates this
constantly — e.g. *"208 groups covering 34,269 videos, each with at least 10 videos"* —
because it is the single most common misreading of the tab: a correlation of
0.5 here means day-level feed profiles co-vary, not that individual videos do.

Why day-level? Feeds are noisy at the single-video level; a day of viewing is
the smallest window at which a feed's *composition* (how much of X, how
diverse, how fresh) is a stable, meaningful quantity. The cost is that all
findings are about day-profiles — see §8 for what you may and may not conclude.

---

## 2. Where the variables come from

The variables offered on the axes and in the heatmap are built from the study's
annotated videos in three different ways. Knowing which kind you are looking at
matters for interpretation.

### 2.1 Components of categorical variables — "Content category (C0) (15.5%)"

Categorical annotations (content category, main activity, niche, type of story,
gender, ethnicity, …) can take dozens of values, so a day's feed is a
*distribution* over those values. For each such variable the platform runs a
principal-component analysis (PCA) over the day-level distributions and offers
the resulting **components** (C0, C1, …) as axes:

- **The percentage in the label** (e.g. "15.5%") is how much of that variable's
  day-to-day variation the component captures. C0 always captures the most.
- **Components are summary dimensions, not categories.** "Content category
  (C2)" is not a category called C2 — it is a *contrast*: days scoring high
  have more of some categories and less of others. On the scatter plot, small
  labels at the ends of each axis ("More likely: …") name the categories that
  dominate each direction. Hover them before interpreting an axis.
- **Yes/no variables** (advertising, platform-native, trend) get a single
  component oriented so that *more "yes" = higher score*.
- **Only each variable's leading three components are offered.** PCA produces
  as many components as a variable has structure, but the tail is not
  interpretable: a variable with ~150 niches spreads its variance over a dozen
  components explaining 5–15% each, none of which anyone reads substantively.
  The cap is applied *per variable* (not as a single variance floor across all
  of them), so a small variable whose third component still carries 20%+ keeps
  it, while the long tail of a high-cardinality variable is dropped. Each
  variable always keeps its C0, so no variable disappears from the tab.
  Components below 5% variance are dropped from the remaining slots.
- **Each variable has its own PCA.** Content-category components and niche
  components live in different mathematical spaces. Correlating them is
  meaningful ("days heavy in this content contrast are also heavy in that niche
  contrast") but they are not shared axes — the heatmap marks family boundaries
  with separator lines for exactly this reason.

### 2.2 Entropy — "Niche (entropy)"

For each categorical variable you also get its **Shannon entropy**: how evenly
the day's feed was spread across the variable's values. Low entropy = a
concentrated, narrow feed that day; high entropy = a diverse one. Entropy is
the tab's diversity measure and the natural dependent variable for
"rabbit-hole" style questions.

> **Caution — entropy rises mechanically with volume.** A day with 300 videos
> has more room to touch many categories than a day with 12, so observed
> entropy correlates with *Videos watched (day)* partly by construction. Before
> interpreting an entropy × intensity association substantively, re-check it
> with **Within-collection** centering on and with **Spearman** in the heatmap,
> and treat the residual association, not the raw one, as the finding.

### 2.3 Numeric variables — plain day means

Numeric variables are averaged per collection-day directly. The current set,
organized by what they measure:

**Collection behaviour ** (what the account holder did):
- **Completion Rate** — average fraction of each video actually played (0–1).
  The tab's primary engagement-quality measure; normalised for video length.
- **Rewatched item** — share of the day's plays where play time *exceeded* the
  video's length (looping/rewatching). This recovers the signal that
  Completion Rate's cap at 1.0 discards.
- **Engaged with item** — share of the day's plays carrying the account's *own*
  engagement (like/comment/share/save/follow recorded in the donation). This is
  the collection's behaviour, **not** the item's popularity — do not confuse it
  with the per-1K-plays variables below. Platform caveat: Instagram and YouTube
  exports log fewer engagement types than TikTok, so levels are not comparable
  across platforms.
- **Videos watched (day)** — how many annotated videos the collection played
  that day (the group's size). The consumption-intensity variable, in natural
  units.

**Feed supply** (what the algorithm served):
- **Days since posted** — average age of the served items: is the feed fresh or
  recycled?
- **Plays per day** — average virality velocity of served items (plays per day
  since upload).
- **Play count** — average total popularity of served items: a
  mainstream-vs-long-tail measure.
- **Video duration** — average length of served items (format composition).
- **Faves / Comments / Shares / Saves per 1K plays** — the *items'* global
  engagement rates (crowd reaction per thousand plays). These describe the kind
  of content served, not this collection's behaviour.

**Consequential exposure** (annotation scores):
- **Political? (score 0-1)** — day-mean political-content load. The annotation
  model scores each video 0–100; the recoded value is normalized to 0–1.
- **Sensitive? (score 0-1)** — same construction for sensitive subject matter.

> **Heavy-tailed variables are log-transformed before averaging.** Play count,
> plays-per-day and days-since-posted follow extreme power-law distributions
> (one viral video would otherwise dominate a whole day's mean). Their
> contract declares a log transform, applied to each video *before* the day
> mean. Axis positions therefore reflect orders of magnitude; the hover
> tooltip's "(Abs)" values always show untransformed natural units.

### 2.4 Standardisation

All plotted values (except *Videos watched*) are **z-scores across the study's
groups**: 0 is the average collection-day, ±1 is one standard deviation. This
makes axes comparable but means raw units are gone — again, the hover tooltip
carries the absolute values.

---

## 3. There is no filter panel — the study is the sample

The tab deliberately offers **no interactive filtering**. Every statistic, on
every view, is computed over the *whole study*, identically. This is a
methodological stance, not a missing feature:

- **Sampling decisions belong in the study definition.** A study already
  carries a date window, a collection selection, and minimum-activity
  thresholds — and, unlike an on-screen filter, those choices are versioned,
  applied by the pipeline, and recorded in the study's methods note. If you
  need to exclude a collection (withdrawal, data-quality problems) or restrict
  to a fieldwork window, define the study that way — or define a sub-study —
  and the exclusion becomes documented, reproducible provenance instead of an
  unrecorded click.
- **Event windows are sub-studies.** "Before X vs. after X" is two study
  definitions over the same collections. Each gets its own tab, its own methods
  note, and its own citable numbers.
- **Comparative questions have their own tools.** "Does this differ between
  collections / weekends / platforms?" is what the **Group differences** view
  (§7) answers with actual tests, and what **Colour by** + **Ellipses** show
  visually — filtering to one side and eyeballing never could.
- **Content is never a sampling criterion.** You analyze what the feed
  contained; you do not sample on it. To inspect the videos behind a dot,
  click it — see §4.4.

One practice this design intentionally gives up: quickly unticking one
collection to check whether it drives a pooled correlation. The honest
substitutes are **Within-collection centering** (§5), the per-group
**Ellipses** (§4.3), and — for a formal check — a sub-study excluding the
collection.

---

## 4. The Scatter view

One dot per collection-day in the study.

### 4.1 Axis and colour controls

- **X Axis / Y Axis** — any two of the variables from §2. The dropdowns group
  a variable's components and entropy together under one heading.
- **Colour by** — colours dots by a factor (Collection ID, weekday, weekend,
  platform, …). Colour is descriptive only; it does not change any statistic.

### 4.2 Regression

Ticking **Regression** fits one ordinary-least-squares line over *all* of the
study's groups (never just the plotted subsample) and prints the full readout:

- **R²** — share of Y's variance explained by X.
- **slope [low, high]** — the regression slope with its 95% confidence
  interval. Because most variables are z-scored, the slope reads as "one SD of
  X goes with this many SD of Y".
- **p** — the significance of the linear association.
- **n** — the number of groups behind the numbers.

A plain-language caption restates the result ("a moderate negative
association…", using the conventional |r| bands: <.1 negligible, <.3 weak,
<.5 moderate, ≥.5 strong) and warns when n < 30. The caption also reminds you
that the line assumes linearity — look at the cloud before trusting it.

### 4.3 Ellipses

Ticking **Ellipses** draws a 95%-coverage confidence ellipse per colour group,
computed from that group's actual covariance on the full data. The
ellipse's tilt shows the within-group x–y correlation; its size shows the
group's spread. Useful for a quick visual answer to "do these collections
occupy different regions of this plane?" before formal testing.

### 4.4 Hover and drill-down

Hovering a dot shows the group's factors plus the absolute (untransformed,
unscaled) values of the numeric variables. **Clicking a dot** offers to jump to
the Video Analysis tab filtered to that collection-day — the fastest route from
a statistical outlier to the actual videos behind it. Use it: qualitative
inspection of extreme dots is the cheapest validity check this platform offers.

### 4.5 Point cap

Very large selections are displayed as a deterministic 5,000-point sample (the
counter shows "Showing X / Y points"). Only the *display* is sampled — every
statistic, regression, and ellipse uses every group in the study.

---

## 5. Within-collection centering (available in Scatter and Heatmap)

Ticking **Within-collection** subtracts each collection's own average from
every variable before anything else is computed. This is the tab's guard
against the ecological fallacy:

- **Off (default):** associations mix two sources — differences *between*
  collections and fluctuations *within* each collection's timeline.
  Between-collection composition usually dominates.
- **On:** all between-collection differences are removed; what remains is
  purely *"on days when this collection's feed had more X, did it also have
  more Y?"* — the within-person claim.

If a correlation survives centering, it reflects genuine day-to-day co-movement
inside feeds. If it collapses, it was a between-collection composition effect.
**Both are findings** — but they are different claims, and papers regularly
confuse them. The captions always state which mode produced the numbers.

---

## 6. The Heatmap view

The all-pairs correlation matrix over the study's groups.

- **Method** — Pearson (linear) or Spearman (rank-based). Prefer Spearman when
  you suspect skew, outliers, or merely monotone relationships; for the
  log-transformed supply variables and entropy×volume questions it is the more
  conservative choice.
- **Cells** — colour encodes r from −1 (blue) to +1 (red). Hover any cell for
  the exact r, the pairwise n (pairs are computed on all rows where *both*
  variables are present, so n varies by cell), the p-value, and the
  **Benjamini–Hochberg q**.
- **Why q, not just p:** a 40-variable matrix tests ~800 pairs; at p < .05
  alone, ~40 would "significant" by chance. The BH q-value controls the
  expected share of false positives among everything you flag: q < .05 means
  "of the cells I'm calling real, under 5% are expected to be noise".
- **Hide non-significant** — blanks every cell with q ≥ .05 (the caption
  reports how many were blanked). Recommended ON for exploration: the surviving
  cells are your candidate findings.
- **Family separators** — thin lines group components of the same variable.
  Within-family cells (C0 × C2 of the same variable) are near-zero by
  construction (PCA components are uncorrelated); the scientifically
  interesting cells are the **cross-family** ones, especially across construct
  families: behavior × composition, supply × exposure.
- **Download CSV** — exports every pair with variable names, family, method,
  r, n, p, q, plus whether centering was on. This file is your
  supplementary-table backbone; the filename encodes study, method, and
  centering.

---

## 7. The Group differences view

Answers *"which factors structure the feeds at all?"* with two precomputed
tables. Like the rest of the tab this view is whole-study; centering and
personal variable preferences do not apply, and the header states when the
tables were generated.

### 7.1 One-way ANOVA sweep — "Which factors move single components?"

Every factor × variable pair, one row each:

| Column | Meaning |
|---|---|
| **η² (eta-squared)** | Share of the variable's variance explained by the factor. The effect-size column — sort by it, read it first. Conventions: .01 small, .06 medium, .14 large (the **Effect** column applies these labels). |
| **ω² (omega-squared)** | A less-biased η²; trust it over η² for factors with many levels or few groups per level. |
| **F, p** | The ANOVA test statistic and its significance. |
| **q** | BH-adjusted significance across the whole table — use this, not p. |
| **KW q** | The same comparison run as a rank-based Kruskal–Wallis test. When groups are small or skewed, trust KW q over the ANOVA q; when the two disagree, be suspicious. |
| **n, Levels** | Groups tested and number of factor levels. |

Rows significant after correction (q < .05) are bold. Note that with 26 weeks
as a factor, statistically significant η² values can still be scientifically
boring — effect size, not the star, is the finding.

*Reading example:* "Collection ID explains 63% of Niche (C0)" = the two
collections' feeds differ enormously in this niche contrast — a direct
**personalization-strength** measurement (most day-to-day variance is
between-collection, not shared).

### 7.2 PERMANOVA — "Do whole variable profiles differ?"

Single components can miss distributed differences. PERMANOVA asks, per
variable family: does the factor separate day-profiles across *all* of that
variable's components at once? The pseudo-F is tested by permutation (999
shuffles), with BH-adjusted q across the table. It never mixes components from
different variables' PCA spaces. Significant PERMANOVA + unimpressive per-component
η² means the difference is real but spread across many small contrasts.

---

## 8. Reading results responsibly

1. **Everything is a day-profile claim.** "Political load correlates with low
   diversity" means *days* with more political content are less diverse days —
   not that political videos cause narrow feeds, and not that political *users*
   have narrow feeds (that's the between/within distinction; use centering to
   separate it).
2. **Exposure ≠ preference.** These are feeds as *served*. Without the
   behavioral variables you cannot distinguish "the algorithm pushed it" from
   "the account cultivated it" — and even with them, only association.
3. **No causal claims.** Nothing here identifies direction. Day-level
   correlation is a screening instrument; treat findings as hypotheses for
   design-based follow-up.
4. **Check the three robustness switches** before believing any correlation:
   does it survive (a) Spearman, (b) Within-collection centering, (c) the q
   threshold? A result that needs Pearson, pooled data, and uncorrected p is
   not a result.
5. **Known artifacts:** entropy × volume (§2.2); cross-platform engagement
   capture (§2.3); components with low explained variance are unstable across
   study refreshes; small studies (the caption warns below 30 groups) make
   everything fragile.
6. **A stale-data banner** appears when the study's underlying data is newer
   than the statistics on screen; ask an admin to refresh before reporting
   numbers.

---

## 9. Recipes: five research questions, knob by knob

**Q1 — How personalized are the feeds?**
Group differences → sort ANOVA by η², read the Collection ID rows, then the
PERMANOVA Collection ID rows. High η² on content families = strongly
personalized feeds. Complement visually: Scatter, any content C0 × another,
Colour by Collection ID, Ellipses on — separated ellipses are personalization
you can see.

**Q2 — Does intensity narrow the feed?**
Scatter: X = *Videos watched (day)*, Y = a content entropy. Regression on.
Then: Within-collection ON (is it a within-feed dynamic?), heatmap method
Spearman, and remember the mechanical entropy×volume component before claiming
a rabbit-hole effect.

**Q3 — What does the supply side look like?**
Heatmap, Hide non-significant ON. Read the cross-family cells between supply
variables (freshness, virality, popularity, duration, ad rate) and content
composition. E.g. *Days since posted* × *Is it a trend? (C0)* asks whether
trend content is systematically fresher.

**Q4 — Who gets the consequential content?**
Scatter: Y = *Political? (score 0-1)*, X = another variable; Colour by
Collection ID (for a time window, define a sub-study over that period). Group
differences: the Collection ID × political/sensitivity rows quantify
between-collection exposure inequality.

**Q5 — Does an association hold for everyone?**
Set up the scatter, Colour by Collection ID, Ellipses on: if the clouds sit in
different places or tilt differently, the pooled correlation is not one
population. Then tick **Within-collection** — an association that survives
centering holds inside feeds, not just between them. For a formal per-group
test, run the association in sub-studies (one per group of interest) and
compare the readouts.

---

## 10. Housekeeping

- **Which variables appear at all** is governed by the admin's *Variable
  Visibility* page (the "Explore / Correlations" column) and by your personal
  overrides under **My stuff → Preferences → Visualized variables**. If a
  variable you expect is missing from the dropdowns, check those two places in
  that order.
- The methods note (My Studies → study row) records how the study dataset was
  built — filters, versions, refresh dates — and is the provenance you cite.
- Tunable thresholds mentioned in this guide (minimum group size 10, at most 3
  components per variable, component variance floor 5%, scatter display cap
  5,000, PERMANOVA permutations 999) are instance configuration, recorded with
  the study rather than set per session; the UI always reflects the active
  values.
