# The For You Data Hub — User Guide

A tab-by-tab guide to the web app for researchers and students. It explains
how to find your way around, what each control does, and how to read each
view. It does not cover statistical methodology (see the
[Correlations tab guide](correlations-tab-guide.md) for that) or the
internals of the data pipeline (see [pipeline.md](pipeline.md)).

---

## 1. Orientation

### Logging in

You log in with your email address at `/login`. Signing up requires accepting
the terms of use (`/terms`), and by default new accounts also wait for admin
approval before they become active (Admin → Site Settings → "Require approval
for new user signups" — on by default on a fresh install). Before login you
see the public mini-site — the landing page plus About, Participate (with its
step-by-step start wizard), The Hub, FAQ and the terms, ethics and
data-donation pages; after login the same landing content appears on the
**Home** tab, inside the app shell.

The header shows the app name (click it to return Home), the **active study
dropdown**, a spinner badge when background tasks are running, and your
username, role and a Logout link.

### The active study

Everything on the analysis tabs revolves around a **study** — a named dataset
a researcher defines from a set of participant collections, a date window and
sampling rules. Pick your active study from the dropdown in the header; every
analysis tab works against it. Two useful facts for new users: an empty
picker can be normal for a fresh account, because studies are shared
explicitly (per role or per user, or site-wide via the default study) — ask
the researcher who invited you to share one. And if you have donated your own
data, two auto-managed studies appear the first time you log in after the
donation is registered: **Just Me** (your own collections only) and
**Everyone & Me** (the site's default study plus your own data).

### The tab bar

The tabs you see depend on your role. The full set, in order, is: **Semantic
Space**, **Explore**, **Correlations**, **Video Analysis**, **Sessions**,
**Timelines**, **My stuff**, **Data Pipeline** and **Admin**. The last three
open a sidebar of sub-pages; on narrow screens the tab bar collapses into a
hamburger drawer that mirrors the sub-pages too. The **?** button at the right
end of the tab bar always opens a help text for the tab you are currently on,
and the Home tab's dismissible "Getting started" panel gives a first-run
overview (restore it any time under My stuff → My Preferences → Onboarding).
A **guided tour** walks new users through the analysis tabs using the site's
demo collection; it is offered from the help panel and the getting-started
panel, and offered again on your own data once your first annotated batch is
ready.

### Roles

Three roles are seeded at installation, and admins can define more (Admin →
User Roles):

- **admin** — everything, including the Data Pipeline and Admin tabs.
- **viewer** — the analysis tabs plus the personal My stuff pages. This is
  the typical researcher/collaborator role.
- **student** — like viewer but read-only: no Semantic Space tab, no Sessions
  tab (chronological session sequences are the Hub's most re-identifying view
  of a donor) and no annotation voting.

Each section below notes who typically sees it. Because the permission matrix
is editable per role, your installation may differ.

### Where the data comes from

Participants donate their platform data exports (TikTok, Instagram, YouTube) —
either handed to the research team and uploaded on the Data Pipeline tab, or
self-served through My stuff → My Collections (section 2.8), which feeds the
same processing pipeline. The Hub ingests these into *collections*, scrapes
each watched video's
metadata and media, annotates the videos with an AI model against a declared
annotation contract, embeds the annotations into a semantic space, and
assembles study datasets from the results. The analysis tabs read those
assembled datasets. The full mechanics are in [pipeline.md](pipeline.md);
the researcher-facing controls are on the **Data Pipeline** tab (section 3).

---

## 2. Analysis tabs

### 2.1 Explore

*Visible to all seeded roles.*

Explore compares variable distributions across the active study, optionally
split into two filtered slices.

**Layout.** A filter panel on the left ("Filters Slice 1"), distribution
charts in the centre, and — in two-slice mode — a second filter panel on the
right ("Filters Slice 2"). The panel-toggle buttons at either end of the
control bar collapse the filter panels to give the charts more room.

**Building a slice.** Each filter panel lists the study's variables with
checkboxes (categorical variables) and range sliders (numeric variables).
Section headers turn bold when filters are active inside them, so you can see
at a glance where your selection comes from. Each panel also has:

- a free-text **search box** ("Search Slice 1...") that searches across every
  searchable column at once (comma-separate multiple terms);
- a per-variable value search inside capped dropdowns — categorical dropdowns
  show only the ~200 most frequent values, but the search box above each one
  matches against *all* values of that variable;
- a **Reset** button that clears the slice.

The count next to the panel title shows how many activities match the current
selection.

**One vs two slices.** The "Slices to explore" toggle in the centre header
switches between **One** and **Two**. In two-slice mode the charts show both
slices side by side and significance stars (\*, \*\*, \*\*\*) flag
distributional differences between them.

**Reading the charts.** Numeric variables render as histograms with the mean
marked; categorical variables render as stacked bar charts. The **Sort by**
dropdown orders categories by Total Frequency, Slice 1 Frequency, Slice 2
Frequency (two-slice mode) or Name (A–Z).

**Drilling down.** Click any bar or histogram segment to jump to the Video
Analysis tab with the matching videos pre-filtered.

Which variables appear as filters and charts is configurable — global
defaults live in Admin → Variable Visibility, and your personal overrides in
My stuff → My Preferences → Variable customizations.

### 2.2 Timelines

*Visible to all seeded roles.*

Timelines shows how a single participant's activity evolves day by day.

**Picking a participant.** Choose a collection from the **Collection**
dropdown in the control bar. The collection's tags appear as chips beside it,
and an (i) icon opens a participant summary: age, country, post code, date
added, display ID, inferred timezone, active days, total activities, peak day
segment, first/last activity and tags.

**The charts.** Each timeline variable becomes its own chart, a smoothed line
over the study period. Controls:

- **Show daily values** overlays the raw daily values as faint bars behind
  the smoothed line, so you can see which day drove a bump.
- **Drag across a chart to zoom into a period** — all charts share one time
  window, so zooming or panning one keeps the rest synchronised. The control
  bar shows the visible period ("Showing …") with a **Reset** button.
- The **Engagement** dropdown in the control bar toggles engagement series.
- For categorical variables, filter chips (Rising, Falling, Spikes, Breaks,
  Volatile, Stable) keep only categories whose temporal pattern matches, and
  a "Show Findings" control opens analysis cards listing detected trends,
  anomalies and structural breaks.

**Day detail.** Click any point on a chart for a "Period Stats" modal with
that day's numbers, a **View videos** button that drills into Video Analysis,
and (for users with the annotation-votes permission — not students) a **Vote
to annotate** button.

Two per-user toggles under My stuff → My Preferences → Timelines control whether
empty dates and pre-first-play activity are included in the charts.

### 2.3 Video Analysis

*Visible to all seeded roles.*

Video Analysis is where you browse, watch and tag the individual videos of
the active study.

**Layout.** A **Filters** panel on the left (same filter mechanics as
Explore, including the free-text search and per-variable value search; the
button at the bottom shows whether filters are applied), the video player in
the centre, and a **Details** panel on the right listing the current video's
full metadata — donation fields, scraped metadata and AI annotations.

**Navigating the result set.** The slider above the player scrubs through
the selection in chronological order (the timestamp under the thumb shows
where you are; markers on the track flag items of interest). The **<** and
**>** buttons step one activity at a time; **<<** and **>>** jump to the
previous/next activity that has engagement data. The control bar shows the
selection's date range and the **Hide Duplicate Videos** switch suppresses
repeated views of the same video.

**Tagging.** Click any field label in the Details panel to open the **Tags
and Notes** modal. There you can add free-form **Open Tags** (semicolon-
separate several; previously used tags are offered as quick-select chips),
pick from **Closed Tags** schemes where one is defined, and write **Notes**.
Your tags are yours; if other researchers have opted into sharing (My stuff →
Preferences → "Share my annotations"), their tags appear anonymously with a
team badge. Manage or delete your own tags under My stuff → My Video Tags.

**Arriving from elsewhere.** Drill-downs from Explore, Correlations,
Timelines, Sessions and Semantic Space all land here with the relevant filter
pre-applied. A notice appears when a drill-down names a video the active
study does not contain (possible from Semantic Space, which spans the whole
corpus, not just your study).

Video autostart is a per-user preference (My stuff → My Preferences).

### 2.4 Timelines vs Sessions vs Explore — which tab answers what?

- *What did participants watch overall, and how do groups differ?* → Explore.
- *How did one participant's feed change over weeks or months?* → Timelines.
- *What happened within a single sitting?* → Sessions.

### 2.5 Semantic Space

*Visible to admin and viewer; **not** to the seeded student role.*

Semantic Space is a map of the whole annotated corpus — every dot is a video,
placed so that videos described in similar ways sit near each other. Around
150 automatically named **niches** (micro-genres found by clustering) organise
the map. It is a browsing surface, not a chart: there are no axes or units,
and only closeness carries meaning — never read distance, blob size, empty
space or an island's isolation as a finding. The tab's own help text (the
**?** button) explains these caveats at length, and the map publishes its own
accuracy figure ("layout keeps N% of true neighbours") in the status bar.

**Controls.**

- **Colour by** — Niche, Content category or Popularity (plays). Content
  category is deliberately excluded from the text the embedding is built
  from, so structure in those colours independently corroborates the niches.
- **Focus niche** — a searchable picker (hundreds of entries, sortable by
  Most videos, A–Z, Most typical, Most isolated). Focusing a niche isolates
  it on the map and fills the detail bar below the controls with its size,
  typicality, isolation, genuinely closest niches, defining terms and
  category shares. Typicality and isolation are measured in the full
  embedding space, not on the 2D picture, so they are trustworthy where the
  layout is not. Clear the focus with the × button.
- **Niche labels** toggles the name overlays.
- **Explore collection trajectories** — a disclosure that opens overlay
  controls: pick a **Collection** and an **Interval** (Monthly / Weekly /
  Daily / All-time only), tick **Show trajectory**, and use **▶ Play** or the
  scrubber to animate how that participant's feed moved through the space
  over time.

**Interacting with dots.** Hover a dot for its niche and a one-line summary;
click it to open the video in Video Analysis, filtered to its niche. A
freshness banner appears when the embedding store is being topped up or a
fresher map is available.

### 2.6 Sessions

*Visible to all seeded roles.*

Sessions explores individual viewing sessions and the **binges** inside them —
runs of consecutive videos whose content stays semantically close together on
the AI embeddings.

**Left rail: the session table.** All of the active study's sessions, one
row each; sort by clicking any column heading. A collapsible **Filters**
panel offers range sliders over the session measures, and a search box
matches stories, captions and creators. Notable columns:

- **Coverage** — the share of the session's videos that are embedded. Read
  the entropy score together with coverage: low-coverage sessions can look
  artificially homogeneous.
- **Low entropy** — the session's single most homogeneous stretch: a window
  of six consecutive distinct embedded videos slides across the session and
  the lowest average pairwise embedding distance found is reported (lower =
  more homogeneous).

**Right pane: session detail.** Selecting a session draws:

- The **Session strip** — a real-time axis with one mark per play, width =
  watch time. Coloured bands are detected binges; grey bars are plays whose
  video has no embedding; dashed outlines are the low-entropy sequences.
  Hover any mark for details; clicking a variable in a "(more info)" panel
  adds a per-variable line plot on the same time axis.
- A selector column listing the session's **Binges** and **Low-entropy
  sequences** (up to three non-overlapping best windows, found independently
  of the binge detector — so they can confirm a binge or reveal a focused
  stretch it missed). Each carries an (i) tooltip with its exact definition,
  and each binge is badged as *stationary* (dwelling in one topic) or
  *drifting* (the rabbit-hole shape).
- Picking a binge or sequence loads it into the shared video player, where
  Prev/Next steps through it in watch order; the video's niche, creator,
  watch time and story summary sit below the player with an "Open in Video
  Analysis" button.
- **Full play sequence** (Show/hide) lists every play in the session.

### 2.7 Correlations

*Visible to all seeded roles.*

Correlations charts relationships between numeric measures across the study.
Its unit of analysis is the **collection-day** — one day of one participant's
feed, averaged over its annotated videos — never a single video; the (i) icon
in the control bar restates this with the study's live counts. Three views
share a toggle: **Scatter** (pick X, Y and a colour variable, with optional
regression readout and 95% group ellipses; click a dot to drill into Video
Analysis), **Heatmap** (the full correlation matrix, Pearson or Spearman,
with multiple-comparison-corrected significance, an option to hide
non-significant cells, and a CSV download), and **Group diff.** (variance
between vs within participants' feeds). A **Within-collection** toggle
re-centres each collection on its own average so associations describe
variation inside a feed rather than differences between participants. There
is deliberately no filter panel: the study definition is the sample.

This tab has a full researcher-grade guide covering every control, every
number and the inferential caveats: read the
**[Correlations tab guide](correlations-tab-guide.md)** before using it in
earnest.

### 2.8 My stuff

*Visible to all seeded roles (individual sub-pages are permission-gated).*

Your personal pages, grouped behind one tab with a sidebar. The sidebar order
is **My Studies, My Collections, My Video Tags, My Tasks, My Profile, My
Preferences**, followed by an **Information** group of links (Data donation,
Consent & ethics, About the project, FAQ, How to cite the Hub — all opening
the public pages in a new tab).

**My Studies.** The study datasets you have access to, with their headline
counts (date window, sampling, collections, activities, videos, scraped,
annotated). Click a row to see how a study was defined — its collections,
date window and sampling rules, read-only. Each row's **Methods** button
opens the study's **Methods** panel: an auto-generated plain-language
provenance note describing how the dataset was built (with a JSON download).
This panel lives here, on the study row — not on the Explore tab.

**My Collections.** Your own donated data (the sidebar item reads **"Share
your data"** until you own a collection). It has four parts:

- *Your collections table* — one row per donation, with the collection's
  activity period, activity count, scraped/annotated coverage (the same
  figures admins see in Edit Collections), date added and status.
- *Adding data* — the **Add your data** flow covers TikTok, Instagram and
  YouTube exports, with a per-platform "how do I get my data" walkthrough.
  Before anything is uploaded, the export is parsed **entirely in your
  browser** and shown in a review step where you can prune whole sections or
  individual rows; only what you keep is uploaded, after you confirm the
  consent statement. A freshly uploaded donation gets an instant
  short-video-persona preview before it is processed into the corpus.
- *Withdrawing data* — deleting a collection here withdraws it from the
  research dataset. The original donation file stays in the archive for
  **30 days**, during which the row stays visible (greyed out) with a
  **Restore** button; after that it is gone for good. See
  [ethics_and_data_handling.md](ethics_and_data_handling.md).
- *Your short-video persona* — a playful profile computed **only from your
  own donated activity data** (no scraping, no AI annotation, nobody else's
  data). Tick the Persona checkbox on several collections to compose a
  combined persona across platforms.

**My Video Tags.** Every custom tag you have created, with usage counts.
Deleting a tag removes it from all videos where it is applied.

**My Tasks.** Appears only when you have been invited to annotation testing
(Admin → Reliability Control). In a **coding** task you watch each video and
fill in the variables blind — no machine answers shown; in a **preference
vote** task you compare anonymous annotation options per video and pick the
best (or a tie). Work saves automatically; submit when done.

**My Profile.** Your login email (fixed), your display username (3–15
characters, shown instead of your email across the app), and an **About you**
block — full name, age, postcode, country, occupation and an
"OK to contact me about this research?" consent — plus a line listing the
collections linked to your account.

**My Preferences.** Your personal settings, grouped as:

- *Appearance* — Dark theme.
- *Video Analysis* — Video autostart; Share my annotations (share your video
  tags anonymously with other researchers).
- *Correlations* — Large dots in correlations.
- *Timelines* — Include empty dates; Include activity before first play.
- *Variable customizations* — your per-surface variable selections
  (Filters…, Visualized variables…, Video detail panel…, Timelines…), applied
  across all studies, with a "Reset all to defaults" button. Filters cover
  Explore and Video Analysis; visualized variables cover Explore's charts and
  Correlations.
- *Onboarding* — restore the Home tab's "Getting started" panel.

---

## 3. Data Pipeline

*Typically admin-only (each sub-page has its own permission key). This is
where raw donations become analysis-ready study datasets; the mechanics
behind every step are documented in [pipeline.md](pipeline.md).*

The sidebar steps through the pipeline in order. Sidebar items show a spinner
while any of their background processes run, and every long-running job has a
progress bar, a stop control (graceful "stop after this batch" or immediate)
and a **View Log** link opening a durable per-run log (last 10 runs, with
filter, copy and download). A badge in the app header counts active tasks.

**Ingest Collections.** Upload participant export files (or whole folders)
per registered platform source. The upload modal assigns a collection ID (use
the filename, join an existing collection, or mint a new one), optional tags,
and an optional donor timezone (the authoritative source for local-time
conversion — recommended for YouTube/Instagram where the export's label can
be ambiguous). "Process New Collections" parses the pending uploads into the
dataset; a **Structure review** panel quarantines uploads that deviate from
the learned structure of past donations until you approve them; the **Last
run results** and permanent **Ingestion history** panels record what happened
to every file (rows read, rows kept, why rows were dropped — uploaded files
are never modified).

**Edit Collections.** Search and multi-select already-ingested collections,
then edit them: change the display ID, link the collection to a user account,
add or remove tags, hide a collection from the primary study interface, or
(admins) delete it. The table shows each collection's first event, date
added, activity count, active days, its **automatic-enrichment state**
(Running/Paused/Idle/Needs attention; blank when no plan exists) and
**scraped/annotated coverage** (the same figures a participant sees on
My Collections; the last event date lives in the edit modal).

The edit modal's **Automatic enrichment** panel arms a collection to scrape
and annotate itself toward an **annotation target** — a number of unique
videos to have annotated, set with a log-scaled slider. The panel shows a
per-day activity chart (stacked by enrichment state, with a red line
estimating where each day would land under the current settings), a linear
coverage bar with the target marked on it, and a live readout translating
the target into items, estimated cost (with the active model) and cycles.
The loop always annotates the already-scraped backlog first, then splits
new scraping between a recent-days **deep dive** and a capped **spread**
across the history (the balance slider and the spread's month/day limits
live under *Advanced*); what one of the two cannot spend the other uses.
With *Auto* items per cycle, each cycle is one annotation job's worth
(2,000), sized up for the videos expected to fail on the way — measured
from the plan's own recent runs — so the target is met without a trailing
cycle for the shortfall; the last slice may buy part of a day. An amber
warning appears when the settings cannot reach the chosen target. The target is a running total: to continue a
finished (Idle) plan, raise the target and press *Arm again*. Arming runs
the first cycle at once (the slice is cut and the scraper started); the
later cycles follow on their own. Automatic ticking also requires the
site-wide switch in Admin → Site Settings; *Run a cycle now* works
regardless. Before *Arm*, *Resume* or *Run a cycle
now* the panel checks the shared queues: the platform's scrape queue and
the annotation queue are one file each for the whole site, and the loop
drains them before it can do its own work — so if either holds videos
queued elsewhere (a study's *Queue videos for scraping*, say), a dialog
shows how many and whose, and asks whether to drain them first or empty
them now. A **History** disclosure under the status line lists what
happened to the collection, newest first: the plan armed, paused or parked
(and why), each slice queued, each handoff to annotation, and every
scraper, annotator and consolidation run the plan shares, with their
numbers. Deletion removes the participant-linked rows and archives
(not destroys) the raw uploads — participants can also withdraw their own
collections from My Collections, with a 30-day restore window; see the
deletion section of
[ethics_and_data_handling.md](ethics_and_data_handling.md) for what a full
withdrawal involves. "Refresh Collections Metadata" recomputes the
collection stats.

**Define Studies.** The study table (the same one viewers see read-only under
My Studies) plus a **Define new study** button. The study editor sets: the
selected collections (searchable checklist), the **date range** (type dates,
step by day, or drag the edge handles on the activities-per-day chart), the
**sampling frame** (activities / scraped / annotated / off) and per-day and
per-collection min/max sampling caps, and — via the **Access** dropdown — who
the study is shared with (roles or users; an unshared study is visible only to
study managers — the one exception is the study chosen as the default under
Admin → Site Settings, which everyone can see). The footer offers Delete,
Rename (moves the study's artifacts in place, no rebuild), Duplicate, and
**Save/Refresh Study**, which rebuilds the study dataset.

**Scrape.** Per-study queueing and per-platform scraper workers. Pick a
**Target Study** and press "Queue videos for scraping" (optionally including
previously failed attempts, or retrying items whose metadata scraped but
whose media never downloaded). One block per platform (TikTok, Instagram,
YouTube) shows the pending count, a scraper-health pill, batch size / max
batches inputs, Start/Stop, and an alert banner when the worker suspects the
scraper itself is broken.

**Annotate short videos.** Builds and runs the machine-annotation queue.
Three queueing modes: **Not-yet-annotated** (scraped but unannotated videos
in the target study), **Annotated with version** (re-annotate videos stamped
with a chosen annotation version), or **Annotated between** two dates — the
latter two re-annotate with the currently active version while the archive
keeps every past result. The **Annotator** card runs the queue immediately
with the active backend (shown as a badge; changed under Admin → Backends);
where media is cloud-stored, a **Gemini Async Annotator** card runs the same
queue through the Gemini Batch API at roughly half the cost with a
minutes-to-24h turnaround.

**Dataset Assembly.** Folds finished scrape/annotation output into the
datasets the analysis tabs read. The **Consolidate Scrape and Annotation
Data** card is the normal entry point: it consolidates new enrichment output,
reports its impact (which collections and studies changed), and — with
"Refresh caches afterwards" ticked — dispatches the downstream refresh
pipeline for exactly what changed, showing each planned step's live state.
The **Enrichment History** card below the pipeline chart is the durable,
high-level record of what happened to the hub's scraping, annotation and
analyses, newest first — plans armed, paused or stopped (and why), scrape
queues built from a study, queues emptied, a scraper or annotator started
on a queue (and how much of that queue the plans queued themselves), and
every scraper, annotator, consolidation and analysis refresh that
finished, with its totals and who started it.
Filter it by collection to read one plan's story; the same view is under
*History* in that collection's Edit Collections panel.
Below it, the **Rebuild Downstream Datasets** cards run any single step by
hand, ordered as the pipeline dispatches them: Semantic Embeddings → Semantic
Map → Study Definitions → then, in parallel, 'Explore' Metadata,
Correlations, Timelines and Sessions. Each card's (i) tooltip explains
exactly what it rebuilds and when to run it alone. A stale-dot on the sidebar
item warns when new enrichment data has not been consolidated yet.

---

## 4. Admin

*Admin role only (sub-pages individually permission-gated). The sidebar is
grouped into Users, Annotation Pipeline, Data & Variables and System.*

**Users**

- **New Users** — approve or reject pending signups (gating and default role
  are set under Site Settings).
- **Active Users** — every user with their annotation stats; click a row for
  a detail modal with the full activity log, password reset and account
  deletion.
- **User Roles** — the permission matrix deciding which tabs and sub-pages
  each role sees; add or delete custom roles. The admin role is fixed.
- **User Annotations** — every human tag and note recorded across the Hub,
  item by item.

**Annotation Pipeline**

- **Backends** — choose the machine-annotation backend and, independently,
  the embedding backend, with per-backend health and requirement checks.
  Backend parameters live in the configuration files — see
  [configuration.md](configuration.md).
- **Contracts** — author, test (A/B evaluation runs) and activate annotation
  contracts: the recipe for what the AI is asked about each video and the
  form its answers must take. See [contracts.md](contracts.md).
- **Versions** — the annotation version history: every activated
  contract+model+settings combination, which version analyses read, and
  re-activation of earlier versions. See [contracts.md](contracts.md).
- **Reliability Control** — invite human coders to blind-code a finished
  annotation test run; submitted codings are compared against every machine
  arm and each other (agreement, Cohen's κ, Jaccard, correlations). The
  coders work under My stuff → My Tasks.

**Data & Variables**

- **Variable Visibility** — the global defaults for which variables appear
  on each surface (Filters, Timelines, Explore, Video Analysis); users
  override these personally under My stuff → My Preferences.
- **Data Contracts** — a read-only view of the scrape, activity and derived
  contracts and their version history; these change only with deployed code.
  See [contracts.md](contracts.md).
- **Hashtag Stoplist** — hashtags dropped when captions are tokenised
  (repeated-letter and prefix `*` matching, with a live tester), plus a tool
  to re-apply the list to already-stored hashtags.
- **Scrapers** — placeholder for scraper configuration tools; day-to-day
  scraper operations live on Data Pipeline → Scrape.

**System**

- **Site Settings** — signup approval gating, the default role for new
  users, the default study, the **demo collection** (the collection the
  guided tour walks through; it must belong to the default study, and with
  none chosen the tour skips those steps), the Sessions tab's list floors
  (minimum plays / minutes / coverage), and cost guardrails (per-request
  queue caps for non-admin users).
- **System Information** — runtime environment, revision, storage locations
  and a system-health panel.
- **Daily Ops Report** — a colour-coded status board over accounts, worker
  runs, task failures, queues, collections, scraper health and the public
  site, with an AI-written assessment. It is generated automatically once a
  day and emailed to the site contact address; the pane shows the latest
  report and a **Generate now** button.

The **default study** is worth spelling out. Picking one under Site Settings
does two things: that study becomes readable by *every* role, whatever its own
Access list says, and it is the study the Hub opens on for anyone who has not
already chosen one. A user's own last selection is remembered and still wins.
Setting the picker back to *None* takes the every-role sharing away again — the
study's own Access list is never edited, so nothing else changes. With no
default study set, users see only the studies explicitly shared with them and
the Hub opens on the first of those. If the chosen study is later deleted the
setting simply stops matching anything and that same behaviour returns;
renaming it carries the setting across. The auto-managed participant studies
("Just Me" / "Everyone & Me") are excluded from the picker, and the demo
collection can only be chosen once a default study is set. The default study
is also what "Everyone & Me" composes with.
