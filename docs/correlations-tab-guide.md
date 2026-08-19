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
(the threshold is configurable). Every view's collapsible **"What is this?"**
explainer opens with the study's live numbers — e.g. *"208 groups covering
34,269 videos, each with at least 10 videos"* — because the unit of analysis
is the single most common misreading of the tab: a correlation of
0.5 here means day-level feed profiles co-vary, not that individual videos do.

Why day-level? Feeds are noisy at the single-video level; a day of viewing is
the smallest window at which a feed's *composition* (how much of X, how
diverse, how fresh) is a stable, meaningful quantity. The cost is that all
findings are about day-profiles — see §8 for what you may and may not conclude.

---

## 2. Where the variables come from

The variables offered on the axes and in the heatmap are built from the study's
annotated videos in four different ways. Knowing which kind you are looking at
matters for interpretation.

### 2.1 Components of categorical variables — "Content category (C0) (15.5%)"

Categorical annotations (content category, main activity, niche, type of story,
gender, ethnicity, …) can take dozens of values, so a day's feed is a
*distribution* over those values. For each such variable the Hub runs a
principal-component analysis (PCA) over the day-level distributions and offers
the resulting **components** (C0, C1, …) as axes:

- **The percentage in the label** (e.g. "15.5%") is how much of that variable's
  day-to-day variation the component captures. C0 always captures the most.
- **Components are summary dimensions, not categories.** "Content category
  (C2)" is not a category called C2 — it is a *contrast*: days scoring high
  have more of some categories and less of others. On the scatter plot, small
  labels at the ends of each axis ("More likely: …") name the categories that
  dominate each direction. Hover them before interpreting an axis.
- **Yes/No variables never appear as components** — they get a plain
  "(share of feed)" column instead; see §2.4.
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

**Semantic position** (where the served items sit in the embedding space):
- **Typicality** — how mainstream each served video is: its percentile among all
  embedded videos for closeness to the corpus mean direction (100 = closest to
  the average of everything the corpus is about, low = distinctive). The day
  mean answers "how mainstream was this collection-day's feed?".
- **Niche isolation** — how much company the video's micro-genre keeps: its
  niche's percentile distance to the *nearest other* niche (100 = the most
  isolated niche in the corpus). Empirically independent of Typicality — a
  niche can sit far from the corpus average and still keep close company, or be
  thoroughly ordinary with nothing beside it.

> **These two are percentiles, not distances, and they are computed by the
> video-map build — not by annotation.** Percentiles because the underlying
> cosine and PCA distances are re-scaled by every rebuild, so raw values would
> not be comparable across refreshes. The practical consequence: a video that
> is not yet in the map has *no* value for either measure, and because the PCA
> drops any collection-day row with a missing feature, an out-of-date map
> silently shrinks the analysed sample **for every variable in the tab**, not
> just these two. If the tab's day count looks lower than the study's, refresh
> in dependency order — embeddings, then the semantic map, then the study
> definitions, then correlations (see §10).

> **Heavy-tailed variables are log-transformed before averaging.** Play count,
> plays-per-day and days-since-posted follow extreme power-law distributions
> (one viral video would otherwise dominate a whole day's mean). Their
> contract declares a log transform, applied to each video *before* the day
> mean. Axis positions therefore reflect orders of magnitude; the hover
> tooltip's "(Abs)" values always show untransformed natural units.

### 2.4 Yes/No variables — "Advertising (share of feed)"

Annotations answered Yes / No / Unclear (advertising, platform-native, the two
trend questions, multilingual) skip the PCA entirely. Their substantive
content *is* the share of "yes" — running them through a PCA would only dress
that share up as a "component" with an uninformative variance percentage. The
tab shows them as **the fraction of the day's coded videos answered "yes"**
(Unclear answers count in the denominator; videos the model could not code at
all are excluded). Like the other variables the plotted value is z-scored;
the hover tooltip carries the raw share.

### 2.5 Which variables get which role — the admission test

Every variable on this tab passed a four-question test, asked in order, that
is written into the data contracts themselves. It is the tab's admission
principle, and the reason some columns you may know from Explore never appear
here:

1. **Does it define the unit of analysis?** → a *grouping key* (Collection ID,
   date). Closed set; never plotted.
2. **Is it constant within every collection-day, categorical, with a few
   a-priori meaningful levels?** → a *comparison variable* (weekend, weekday,
   platform). These are what the Group-differences view tests and what
   Colour-by colours.
3. **Is it a per-video property whose day-level aggregate is meaningful?** → a
   *measure* (everything in §2.1–2.4).
4. **Is it group-constant context that describes rather than contrasts?** → a
   *descriptor* (e.g. calendar week): carried along for hover context, never
   tested or coloured — a 26-level "week effect" is a time trend wearing an
   ANOVA costume, and time trends belong to the Timelines tab.

Anything failing all four is deliberately role-free. The canonical example is
time-of-day: it varies *within* a collection-day, so it cannot be a comparison
variable of this tab's unit (§1) — analysing it needs a finer unit, not a
shortcut.

> **A limitation worth knowing — composition is closed.** A day's shares
> across one variable's categories sum to 1, so when one category rises the
> others must fall. Correlations between components of the *same* variable,
> and part of any correlation between share-type measures, are therefore
> partly mechanical. The family separators in the heatmap mark where this
> applies; cross-family cells are unaffected by this particular artifact.

### 2.6 Standardisation

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
- **Colour by** — colours dots by Collection ID or a comparison variable
  (weekend, weekday, platform). The list is exactly those roles, on purpose:
  colouring by a grouping key like the date would give each dot a near-unique
  colour (noise), and colouring by a descriptor like the calendar week would
  invite reading a 26-colour rainbow as a finding — time trends belong to the
  Timelines tab. (Date and week still appear in each dot's hover.) Colour is
  descriptive only; it does not change any statistic.

### 4.2 Regression

Ticking **Regression** fits one ordinary-least-squares line over *all* of the
study's groups (never just the plotted subsample) and prints the full readout:

- **R²** — share of Y's variance explained by X.
- **slope [low, high]** — the regression slope with its 95% confidence
  interval. Because most variables are z-scored, the slope reads as "one SD of
  X goes with this many SD of Y".
- **p** — the significance of the linear association.
- **n** — the number of groups behind the numbers.

A plain-language highlight above the plot restates the result ("a moderate
negative association…", using the conventional |r| bands: <.1 negligible,
<.3 weak, <.5 moderate, ≥.5 strong) and warns when n < 30. It also reminds
you that the line assumes linearity — look at the cloud before trusting it.
The **"What is this?"** link right after it expands a longer plain-language
explainer of the whole view (the unit of analysis with the study's live
numbers, the variable kinds, and what each control does).

**Per-series lines — the legend is the honest filter.** When a colour split
is active (and the colour variable has at most 12 series, configurable), the
Regression toggle also draws **each series' own dotted line**, fitted on the
full data for that series, and the caption lists every series' slope.
Clicking a series in the legend hides its dots *and* its line (and ellipse)
together — so you can visually isolate any subset. The axis ranges are fixed
to the full set of series, so hiding series never rescales the frame — what
you see stays comparable. What deliberately does
**not** happen: the pooled line and readout are never re-fitted to the
visible subset. A pooled r/p over a hand-picked selection of series would be
an unrecorded sampling decision — exactly what §3 removed the filter panel to
prevent. If the per-series lines disagree with the pooled line, that *is* the
finding (the pooled association mixes different relationships); if you need
citable pooled statistics without a particular collection, define a sub-study
excluding it, which documents the exclusion in the methods note.

**Per-collection slopes and the independence caveat.** When the study has few
collections (fewer than the configured threshold, default 10), the caption
additionally lists **each collection's own regression slope** (when no colour
split is active) and warns that days within a collection are not independent,
so the pooled p-value runs optimistic. Read the slopes as a robustness check: if they agree with the
pooled line, the association holds inside feeds; if they disagree — or the
pooled slope sits outside all of them — the pooled line is mixing different
relationships (Simpson's-paradox territory) and should not be reported as one
finding. With very few collections, the honest framing of *any* result on
this tab is "descriptive of these collections", not population inference.

### 4.3 Ellipses

Ticking **Ellipses** draws a 95%-coverage confidence ellipse per colour group,
computed from that group's actual covariance on the full data. The
ellipse's tilt shows the within-group x–y correlation; its size shows the
group's spread. Useful for a quick visual answer to "do these collections
occupy different regions of this plane?" before formal testing.

### 4.4 Hover and drill-down

Hovering a dot shows the group's comparison variables plus the absolute (untransformed,
unscaled) values of the numeric variables. **Clicking a dot** offers to jump to
the Video Analysis tab filtered to that collection-day — the fastest route from
a statistical outlier to the actual videos behind it. Use it: qualitative
inspection of extreme dots is the cheapest validity check the Hub offers.

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

Two bordered panels, because the view answers two statistically different
questions. Like the rest of the tab it is whole-study; centering and personal
variable preferences do not apply. Each of the four tables carries its own
collapsible **"What does this table show?"** explainer, and the view-level
"What is this?" explainer covers where the variables come from (the data
contracts and their roles) and why the lists change only with the pipeline,
never with a UI setting.

The guide's older vocabulary called panel one "personalization"; the UI now
speaks of **between-collection differences**, because a collection is any
set of timestamped feed videos — one donor's feed, but equally a scrape
session or any other capture — so "personalized" presumes an interpretation
the data may not carry. When your collections *are* individual donors' feeds,
reading strong between-collection differences as personalization is exactly
right — but that is your interpretive step, not the table's.

### 7.0 The Key findings box

The view opens with a single **Key findings** report (with the group counts
in its header): the strongest between-collection difference and how many
variables show large ones (η² ≥ .14), the strongest whole-profile separation,
the within-collection significant-test count and largest effect, and the
standing caveats. Below it sits an optional **"Ask AI to interpret these
findings"** button: it sends a digest of the precomputed statistics (never
raw data) to the instance's configured Gemini model and returns a short
plain-language interpretation, prompted to stay strictly sample-bound — no
causal claims, no population or platform generalizations. It is a reading
aid, clearly disclaimed as AI-generated: check it against the tables before
relying on it, and never cite it as a result.

### 7.1 Panel one — "How distinct are the collections?"

One row per variable: a **variance decomposition** on Collection ID. The η²
here reads as an **intraclass correlation (ICC)**: the share of a variable's
day-to-day variance that lies *between* collections. 0 means the collections
are statistically interchangeable on that variable; 0.6 means most daily
variation is "which collection is this?" rather than "which day is it?". ω²
is the same quantity with a small-sample correction — slightly lower but more
robust.

**This panel deliberately shows no p-values.** With hundreds of
serially-dependent days per collection, every such test comes out "p ≈ 0"
regardless of scientific interest — printing stars would only invite
misreading. The effect size *is* the finding. (The companion PERMANOVA table
asks the same question at the whole-profile level; rank it by pseudo-F.)

### 7.2 Panel two — "Within-collection comparisons (collection differences removed first)"

Does a comparison variable (weekend, weekday, …) move a variable *inside the
same collection*? Each test is an ANOVA **blocked on collection**: collection
differences are removed into their own term first, so they neither masquerade
as a comparison effect nor bloat the error term (in the old pooled design a
study with strong between-collection differences systematically *understated*
every other effect).

| Column | Meaning |
|---|---|
| **η²ₚ (partial eta-squared)** | Share of the *within-collection* variance the comparison explains, after blocking. The effect-size column — sort by it, read it first (.01 small, .06 medium, .14 large; the **Effect** column applies the labels). |
| **ω²ₚ (partial omega-squared)** | The less-biased partial η² — slightly lower but more robust. Slightly negative just means "indistinguishable from zero". |
| **F, p** | The blocked test statistic and its significance. |
| **q** | BH-adjusted significance across the table's testable rows — use this, not p. |
| **KW q** | The same comparison as a Kruskal–Wallis test on within-collection-centered values (a rank-based approximation of the blocked test). Trust it over q when groups are small or skewed; disagreement is a warning. |
| **n, Levels** | Groups tested and number of comparison levels. |

**† — nested comparisons.** A variable constant within every collection
(platform, when each collection donates from one platform) *cannot* be
blocked: it is statistically inseparable from the overall between-collection
differences, and its comparison has only as many independent units as there
are collections. Such rows are marked †, computed one-way, and shown
**without q** — read their p as descriptive, never confirmatory.

### 7.3 PERMANOVA — "Do whole variable profiles differ?"

Single components can miss distributed differences. PERMANOVA asks, per
variable family: does the grouping separate day-profiles across *all* of that
variable's components at once? Only variables whose PCA produced two or more
components appear as families — a single-number variable (a day average, an
entropy, a share of feed) has no multi-dimensional profile to test and is
fully covered by the per-variable tables. It appears in both panels: on raw
profiles for Collection ID (profile-level separation between collections),
and on **within-collection-centered** profiles for the comparison variables
(do days differ inside collections?). The pseudo-F is tested by permutation (999
shuffles), with BH-adjusted q per table, and never mixes components from
different variables' PCA spaces. One honesty note: the permutation shuffles
days freely rather than within collections (a strata-restricted permutation
is future work), so under strong day-to-day dependence its p runs optimistic
— another reason to rank on pseudo-F and q rather than celebrate a bare p.

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
5. **Days are not independent.** Collection-days are nested in collections
   and autocorrelated along each collection's timeline, so every pooled
   p-value on this tab is somewhat optimistic. The tab mitigates rather than
   solves this: the blocked design in §7.2, the per-collection slopes in
   §4.2, and a standing caption caveat whenever the study has few
   collections. Under that caveat, report findings as descriptive of the
   studied collections and lean on effect sizes; only design-based follow-up
   licenses population claims.
6. **Known artifacts:** entropy × volume (§2.2); compositional closure
   (§2.5); cross-platform engagement capture (§2.3); components with low
   explained variance are unstable across study refreshes; small studies (the
   caption warns below 30 groups) make everything fragile.
7. **A stale-data banner** appears when the study's underlying data is newer
   than the statistics on screen; ask an admin to refresh before reporting
   numbers.

---

## 9. Recipes: five research questions, knob by knob

**Q1 — How personalized are the feeds?** (a valid reading when your
collections are individual donors' feeds — see §7)
Group differences → the **"How distinct are the collections?" panel**, sorted
by η² (ICC): high values on content families = strongly differentiated feeds;
the panel's PERMANOVA table gives the same reading at whole-profile level
(rank by pseudo-F). Complement visually: Scatter, any content C0 × another,
Colour by Collection ID, Ellipses on — separated ellipses are
between-collection differences you can see.

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
differences: the distinctness panel's political/sensitivity rows quantify
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
  overrides under **My stuff → Preferences → Variable customizations →
  "Visualized variables…"**. If a
  variable you expect is missing from the dropdowns, check those two places in
  that order.
- The methods note (My Studies → study row) records how the study dataset was
  built — filters, versions, refresh dates — and is the provenance you cite.
- **Refresh order matters** for the embedding-derived measures (§2.3, *Semantic
  position*). They are joined into the study frame at recode time from whatever
  the video map last produced, so the sequence is **Semantic Embeddings →
  Semantic Map → Study Definitions → Correlations**. Rebuilding study
  definitions before the map is current joins missing values, and missing values
  cost you whole collection-days across every variable. The cards under
  *Rebuild Downstream Datasets* on Data Pipeline → Dataset Assembly are listed
  in that dependency order, top to bottom.
- Tunable thresholds mentioned in this guide (minimum group size 10, at most 3
  components per variable, component variance floor 5%, scatter display cap
  5,000, PERMANOVA permutations 999, independence-caveat threshold 10
  collections) are instance configuration, recorded with the study rather
  than set per session; the UI always reflects the active values.
