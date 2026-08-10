// Sessions tab — session-quality explorer + binge / low-entropy-sequence inspector.
//
// Left rail: the active study's sessions as a sticky-header table, sortable by
// clicking column headings. Main pane: the selected session's play-by-play
// strip (SVG; solid binge bands, dashed low-entropy-sequence bands,
// dwell-width play marks), a sequence selector column (binges + low-entropy
// sequences as stacked entries), and ONE shared video player the researcher
// steps through the selected sequence with — metadata (niche, story, author,
// …) and the Video Analysis drill-down sit under the player. All data comes
// from /api/sessions/*; playback reuses /api/video/<study>/<item_id>.

let _sessLoaded = false;

const sessState = {
    study: null,
    overview: null,      // last overview payload
    detail: null,        // last detail payload
    selected: null,      // {collection_id, session_id}
    activeSeq: null,     // {kind: 'binge'|'window', idx, pos}
    sort: { key: 'min_window_cosdist', order: 'asc' },
    params: null,        // effective [sessions] limits, from /api/sessions/overview
    page: 0,             // 0-based table page (200 rows per page, server-side)
    filters: {},         // per-filter {min, max} in SLIDER units (see SESS_FILTERS)
    filterMeta: {},      // per-filter slider bounds {lo, hi}, from the overview's ranges
    filtersOpen: true,   // filter panel expanded?
};




// The [sessions] limits the copy below quotes. Server-supplied (the artifact's
// own build parameters + the live context_plays); these are only the fallbacks
// for a payload that predates the `params` key.
const SESS_PARAM_FALLBACKS = {
    min_videos: 4, min_minutes: 1, max_skip: 2, window_n: 6, max_windows: 3,
    context_plays: 3, drift_p: 0.05, trend_min_videos: 7,
};




// "3 creators" / "1 creator", with the attribution denominator when some of
// the run's videos have no known creator (unscraped ones never will).
function sessCreatorText(c) {
    if (!c || !c.n_items) { return null; }
    if (!c.n_attributed) { return 'creators unknown'; }
    const label = c.n_creators === 1 ? '1 creator' : `${c.n_creators} creators`;
    return c.n_attributed < c.n_items
        ? `${label} (of ${c.n_attributed}/${c.n_items} known)` : label;
}




function sessCreatorTooltip(c) {
    if (!c || !c.n_items) { return ''; }
    if (!c.n_attributed) {
        return 'None of these videos has a known creator — they have not been scraped, '
            + 'so this run cannot be classified as single- or multi-creator.';
    }
    const base = c.n_creators === 1
        ? 'Every attributed video in this run is by the SAME creator — the donor was '
          + 'sitting on one account, rather than being served a topic assembled across creators.'
        : `${c.n_creators} different creators — the feed assembled this run across accounts, `
          + 'not from one creator’s page.';
    return c.n_attributed < c.n_items
        ? `${base} Counted over the ${c.n_attributed} of ${c.n_items} videos with a known creator.`
        : base;
}




function sessNum(value, fallback) {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
}




// Effective limits — never read a hardcoded number in the UI, or the tab lies
// about its own artifact the moment an admin edits config.toml.
function sessParams() {
    const p = sessState.params || {};
    const out = {};
    for (const [key, fallback] of Object.entries(SESS_PARAM_FALLBACKS)) {
        out[key] = sessNum(p[key], fallback);
    }
    return out;
}

// The filter panel's slider rows. Slider values are INTEGERS; each def maps
// its column's domain into slider units and back:
//   bounds(range)  → [lo, hi] slider ints from the server's `ranges` pair
//   fmt(v)         → readout text for one slider value
//   param(v)       → query-param value (domain units) for one slider value
// A filter is only sent when narrowed from its bounds, so an untouched slider
// keeps sessions with a missing value (e.g. no low-entropy score) visible.
// The date slider's unit is whole days since the epoch, in UTC — pure
// calendar arithmetic on the wall-clock `start_ts` dates, with no viewer-
// timezone conversion in either direction.
function sessDayToIsoDate(day) {
    const d = new Date(day * 86400000);
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}




const SESS_FILTERS = [
    { key: 'date', label: 'Date', rangeKey: 'start_date',
      paramMin: 'f_start_min', paramMax: 'f_start_max',
      bounds: (r) => [Math.floor(Date.parse(r[0]) / 86400000),
                      Math.ceil(Date.parse(r[1]) / 86400000)],
      fmt: (v) => fypWallDate(sessDayToIsoDate(v), sessDayToIsoDate(v)),
      param: (v) => sessDayToIsoDate(v) },
    { key: 'length', label: 'Length', rangeKey: 'duration_min',
      paramMin: 'f_length_min', paramMax: 'f_length_max',
      bounds: (r) => [Math.floor(r[0]), Math.ceil(r[1])],
      fmt: (v) => sessFmtMinutes(v),
      param: (v) => String(v) },
    { key: 'plays', label: 'Plays', rangeKey: 'n_plays',
      paramMin: 'f_plays_min', paramMax: 'f_plays_max',
      bounds: (r) => [Math.floor(r[0]), Math.ceil(r[1])],
      fmt: (v) => String(v),
      param: (v) => String(v) },
    { key: 'coverage', label: 'Coverage', rangeKey: 'coverage_embedded',
      paramMin: 'f_coverage_min', paramMax: 'f_coverage_max',
      bounds: (r) => [Math.floor(r[0] * 100), Math.ceil(r[1] * 100)],
      fmt: (v) => `${v}%`,
      param: (v) => String(v / 100) },
    { key: 'entropy', label: 'Low entropy', rangeKey: 'min_window_cosdist',
      paramMin: 'f_entropy_min', paramMax: 'f_entropy_max',
      tooltip: 'The session’s best low-entropy sequence score (smaller = a more '
        + 'homogeneous stretch existed). Narrowing this range hides sessions '
        + 'that have no score at all (too few embedded videos).',
      bounds: (r) => [Math.floor(r[0] * 1000), Math.ceil(r[1] * 1000)],
      fmt: (v) => (v / 1000).toFixed(2),
      param: (v) => String(v / 1000) },
    { key: 'binges', label: 'Binges', rangeKey: 'n_episodes',
      paramMin: 'f_binges_min', paramMax: 'f_binges_max',
      bounds: (r) => [Math.floor(r[0]), Math.ceil(r[1])],
      fmt: (v) => String(v),
      param: (v) => String(v) },
];




// True when this filter's sliders are narrowed from their bounds.
function sessFilterActive(key) {
    const meta = sessState.filterMeta[key];
    const val = sessState.filters[key];
    if (!meta || !val) { return false; }
    return val.min > meta.lo || val.max < meta.hi;
}




// One dual-handle range slider row: two stacked native range inputs over a
// shared track, with a fill bar between the handles and a live readout.
// 'input' updates the readout/fill only; 'change' (handle released) commits
// the filter and reloads page 0.
function sessBuildFilterRow(def, meta) {
    const val = sessState.filters[def.key];
    const row = document.createElement('div');
    row.className = 'sess-filter-row';

    const label = def.tooltip
        ? `<span class="meta-tooltip" data-tooltip="${escapeHtml(def.tooltip)}">${escapeHtml(def.label)}</span>`
        : escapeHtml(def.label);
    row.innerHTML = `
        <div class="sess-filter-head text-xxs">
            <span class="sess-filter-label">${label}</span>
            <span class="sess-filter-val"></span>
        </div>
        <div class="sess-dual-range">
            <div class="sess-dual-track"></div>
            <div class="sess-dual-fill"></div>
            <input type="range" class="sess-dr-min" min="${meta.lo}" max="${meta.hi}" step="1" value="${val.min}">
            <input type="range" class="sess-dr-max" min="${meta.lo}" max="${meta.hi}" step="1" value="${val.max}">
        </div>`;

    const inMin = row.querySelector('.sess-dr-min');
    const inMax = row.querySelector('.sess-dr-max');
    const fill = row.querySelector('.sess-dual-fill');
    const readout = row.querySelector('.sess-filter-val');

    const sync = () => {
        let lo = Number(inMin.value);
        let hi = Number(inMax.value);
        if (lo > hi) {
            // The dragged handle wins; the other is pushed along.
            if (document.activeElement === inMin) { hi = lo; inMax.value = hi; }
            else { lo = hi; inMin.value = lo; }
        }
        const span = Math.max(meta.hi - meta.lo, 1);
        fill.style.left = `${(100 * (lo - meta.lo)) / span}%`;
        fill.style.right = `${100 - (100 * (hi - meta.lo)) / span}%`;
        const narrowed = lo > meta.lo || hi < meta.hi;
        readout.textContent = `${def.fmt(lo)} – ${def.fmt(hi)}`;
        readout.classList.toggle('is-active', narrowed);
        return { lo, hi };
    };
    const commit = () => {
        const { lo, hi } = sync();
        sessState.filters[def.key] = { min: lo, max: hi };
        sessState.page = 0;
        sessLoadOverview();
    };
    inMin.addEventListener('input', sync);
    inMax.addEventListener('input', sync);
    inMin.addEventListener('change', commit);
    inMax.addEventListener('change', commit);
    sync();
    return row;
}




// (Re)build the filter panel from the overview's `ranges` bounds. Existing
// slider positions survive a rebuild (clamped into the fresh bounds); a
// filter whose column has no usable values gets no row.
function sessRenderFilters(ranges) {
    const body = document.getElementById('sess-filters-body');
    if (!body) { return; }
    body.innerHTML = '';
    sessState.filterMeta = {};
    for (const def of SESS_FILTERS) {
        const r = ranges ? ranges[def.rangeKey] : null;
        if (!r || r[0] == null || r[1] == null) {
            delete sessState.filters[def.key];
            continue;
        }
        const [lo, hi] = def.bounds(r);
        if (!Number.isFinite(lo) || !Number.isFinite(hi) || lo >= hi) {
            delete sessState.filters[def.key];
            continue;
        }
        const meta = { lo, hi };
        sessState.filterMeta[def.key] = meta;
        const prev = sessState.filters[def.key];
        sessState.filters[def.key] = prev
            ? { min: Math.max(Math.min(prev.min, hi), lo),
                max: Math.min(Math.max(prev.max, lo), hi) }
            : { min: lo, max: hi };
        body.appendChild(sessBuildFilterRow(def, meta));
    }
    sessUpdateFilterChrome();
}




// Badge ("2 active") + Clear button + collapsed/expanded state.
function sessUpdateFilterChrome() {
    const nActive = SESS_FILTERS.filter(d => sessFilterActive(d.key)).length;
    const badge = document.getElementById('sess-filters-badge');
    const clear = document.getElementById('sess-filters-clear');
    const body = document.getElementById('sess-filters-body');
    const caret = document.getElementById('sess-filters-caret');
    if (badge) {
        badge.style.display = nActive ? '' : 'none';
        badge.textContent = `${nActive} active`;
    }
    if (clear) { clear.style.display = nActive ? '' : 'none'; }
    if (body) { body.style.display = sessState.filtersOpen ? '' : 'none'; }
    if (caret) { caret.textContent = sessState.filtersOpen ? '▾' : '▸'; }
}




function sessInitFilterPanel() {
    const toggle = document.getElementById('sess-filters-toggle');
    const clear = document.getElementById('sess-filters-clear');
    if (toggle) {
        toggle.addEventListener('click', () => {
            sessState.filtersOpen = !sessState.filtersOpen;
            sessUpdateFilterChrome();
        });
    }
    if (clear) {
        clear.addEventListener('click', () => {
            sessState.filters = {};
            sessState.page = 0;
            sessLoadOverview();
        });
    }
    const prev = document.getElementById('sess-page-prev');
    const next = document.getElementById('sess-page-next');
    if (prev) { prev.addEventListener('click', () => sessGoPage(-1)); }
    if (next) { next.addEventListener('click', () => sessGoPage(1)); }
}




function sessGoPage(delta) {
    const data = sessState.overview;
    if (!data) { return; }
    const size = sessNum(data.page_size, 200) || 200;
    const maxPage = Math.max(Math.ceil(sessNum(data.total_matching, 0) / size) - 1, 0);
    const page = Math.min(Math.max(sessState.page + delta, 0), maxPage);
    if (page === sessState.page) { return; }
    sessState.page = page;
    sessLoadOverview();
}




function sessRenderPager(data) {
    const pager = document.getElementById('sess-pager');
    if (!pager) { return; }
    const size = sessNum(data.page_size, 200) || 200;
    const total = sessNum(data.total_matching, 0);
    if (total <= size) {
        pager.style.display = 'none';
        return;
    }
    const page = sessNum(data.page, 0);
    const maxPage = Math.max(Math.ceil(total / size) - 1, 0);
    const first = page * size + 1;
    const last = page * size + sessNum(data.returned, 0);
    pager.style.display = 'flex';
    document.getElementById('sess-page-info').textContent =
        `Page ${page + 1} of ${maxPage + 1} · ${first.toLocaleString()}–${last.toLocaleString()} of ${total.toLocaleString()}`;
    document.getElementById('sess-page-prev').disabled = page <= 0;
    document.getElementById('sess-page-next').disabled = page >= maxPage;
}




// Session-table columns. `key` is the server-side sort key (null = not
// sortable); `defaultOrder` is the direction a first click applies.
const SESS_COLUMNS = [
    { key: null, label: 'Collection' },
    { key: 'start_ts', label: 'Start', defaultOrder: 'desc' },
    { key: 'duration_min', label: 'Length', defaultOrder: 'desc' },
    { key: 'n_plays', label: 'Plays', defaultOrder: 'desc' },
    { key: 'coverage_embedded', label: 'Coverage', defaultOrder: 'desc',
      tooltip: 'Share of the session’s distinct videos with a semantic embedding. '
        + 'Low-coverage sessions can look artificially homogeneous — the entropy '
        + 'score only sees the embedded subset.' },
    { key: 'min_window_cosdist', label: 'Low entropy', defaultOrder: 'asc',
      tooltip: (p) => 'The session’s best low-entropy sequence: the smallest average pairwise '
        + `embedding distance over any ${p.window_n} consecutive distinct embedded videos. `
        + '0 = near-identical content, ~0.9+ = unrelated. Lower = a more homogeneous '
        + 'stretch existed. Click the header to rank sessions by it.' },
    { key: 'n_episodes', label: 'Binges', defaultOrder: 'desc',
      tooltip: (p) => 'Detected binges in this session. A superscript ✳ marks a session '
        + 'containing at least one DIRECTED binge — one whose content travels rather than '
        + 'circling a topic, established against reorderings of that binge’s own videos '
        + `at p < ${p.drift_p}.` },
];




function initSessions() {
    if (_sessLoaded) { return; }
    _sessLoaded = true;

    sessInitFilterPanel();
    window.studyState.ready.then(() => {
        sessState.study = window.studyState.current;
        sessLoadStatus();
        sessLoadOverview();
    });
    document.addEventListener('study:changed', (ev) => {
        if (!_sessLoaded) { return; }
        sessState.study = ev.detail.study;
        // A different study has different sessions — its slider bounds are
        // rebuilt from the fresh ranges, so stale narrowings must not carry.
        sessState.filters = {};
        sessState.page = 0;
        sessClearDetail();
        sessLoadOverview();
    });
}




function pauseSessionsVideos() {
    document.querySelectorAll('#sessions video').forEach(v => {
        try { v.pause(); } catch (e) { /* detached element */ }
    });
}




async function sessLoadStatus() {
    try {
        const resp = await fetch('/api/sessions/status');
        if (!resp.ok) { return; }
        const st = await resp.json();
        const banner = document.getElementById('sess-banner');
        const text = document.getElementById('sess-banner-text');
        let msg = '';
        if (!st.artifact_exists) {
            msg = 'The sessions index has not been built yet. An admin can build it from '
                + 'Data Pipeline → Refresh Caches → Sessions (embeddings must exist first).';
        } else if (st.refresh_running) {
            msg = 'The sessions index is being rebuilt — results below may be replaced shortly.';
        } else if (st.model_mismatch) {
            msg = `The sessions index was built with a different embedding model (${st.meta && st.meta.embedding_model}) `
                + `than the active backend (${st.active_embedding_model}). Rebuild it to reflect the current model.`;
        }
        if (msg) {
            text.textContent = msg;
            banner.style.display = 'flex';
        } else {
            banner.style.display = 'none';
        }
    } catch (e) { /* status is informational only */ }
}




function sessSortBy(key, defaultOrder) {
    if (!key) { return; }
    if (sessState.sort.key === key) {
        sessState.sort.order = sessState.sort.order === 'asc' ? 'desc' : 'asc';
    } else {
        sessState.sort = { key, order: defaultOrder || 'asc' };
    }
    sessState.page = 0;
    sessLoadOverview();
}




async function sessLoadOverview() {
    const study = sessState.study;
    const listEl = document.getElementById('sess-list');
    const statusEl = document.getElementById('sess-status');
    if (!study) {
        statusEl.textContent = 'No study selected.';
        return;
    }
    statusEl.textContent = 'Loading sessions…';
    // The list floors (plays / minutes / coverage) are deliberately NOT sent,
    // so the admin-controlled defaults apply and the status line reports what
    // they removed. min_emb_plays stays off — the researcher ranks sessions via
    // the table headers, and the Coverage column carries that signal already.
    const qs = new URLSearchParams({
        study, min_emb_plays: '0',
        sort: sessState.sort.key, order: sessState.sort.order,
        page: String(sessState.page),
    });
    // Range filters ride along only when narrowed from their slider bounds —
    // an untouched slider must not exclude sessions missing that metric.
    for (const def of SESS_FILTERS) {
        const meta = sessState.filterMeta[def.key];
        const val = sessState.filters[def.key];
        if (!meta || !val) { continue; }
        if (val.min > meta.lo) { qs.set(def.paramMin, def.param(val.min)); }
        if (val.max < meta.hi) { qs.set(def.paramMax, def.param(val.max)); }
    }
    try {
        const resp = await fetch(`/api/sessions/overview?${qs}`);
        const data = await resp.json();
        if (!resp.ok) {
            listEl.innerHTML = '';
            document.getElementById('sess-list-summary').textContent = '';
            statusEl.textContent = data.error || 'Failed to load sessions.';
            return;
        }
        sessState.overview = data;
        sessState.params = data.params || null;
        sessState.page = sessNum(data.page, 0);
        sessApplyParamCopy();
        sessRenderFilters(data.ranges);
        sessRenderList(data);
        sessRenderPager(data);
        statusEl.textContent = sessStatusLine(data);
    } catch (e) {
        statusEl.textContent = 'Failed to load sessions.';
    }
}




// "N sessions in this study" — plus, when the admin-controlled list floors
// actually removed something, how many and on what rule. A count the
// researcher can't reconcile with the rows on screen is worse than no count.
function sessStatusLine(data) {
    const total = sessNum(data.total_in_study, 0);
    const above = sessNum(data.total_above_floors, total);
    const hidden = Math.max(total - above, 0);
    if (!hidden) {
        return `${total.toLocaleString()} session(s) in this study.`;
    }
    const floors = data.floors || {};
    const rules = [];
    if (sessNum(floors.min_plays, 0) > 0) {
        rules.push(`${floors.min_plays} plays`);
    }
    if (sessNum(floors.min_session_minutes, 0) > 0) {
        rules.push(`${floors.min_session_minutes} min`);
    }
    if (sessNum(floors.min_coverage, 0) > 0) {
        rules.push(`${Math.round(floors.min_coverage * 100)}% coverage`);
    }
    const rule = rules.length ? ` (min ${rules.join(', ')})` : '';
    return `${above.toLocaleString()} of ${total.toLocaleString()} session(s) in this study — `
        + `${hidden.toLocaleString()} below the listing floor${rule}.`;
}




// Rewrite the two static section tooltips with the artifact's real limits.
function sessApplyParamCopy() {
    const p = sessParams();
    const binge = document.getElementById('sess-binge-help');
    if (binge) {
        binge.dataset.tooltip = `A binge is a maximal run of ${p.min_videos}+ distinct videos `
            + `(over ${p.min_minutes}+ minutes) within the session where each next video stays `
            + 'semantically close to the running centre of the previous ones. Rewatches extend a '
            + 'binge but don’t count as new videos. '
            + `Up to ${p.max_skip} off-theme videos in a row (typically ads) are tolerated without `
            + 'ending the binge — an immediate return to the theme means it never ended. Those are '
            + 'not members and are reported separately as "off-theme skipped".';
    }
    const seq = document.getElementById('sess-seq-help');
    if (seq) {
        seq.dataset.tooltip = 'The session’s most semantically homogeneous stretches: up to '
            + `${p.max_windows} non-overlapping windows of ${p.window_n} consecutive distinct `
            + 'embedded videos, ranked by the average pairwise distance between their content '
            + 'embeddings (the best one is the session’s score in the table). Because all windows '
            + 'are the same size, their spectral entropy is directly comparable — “low entropy” '
            + 'means the content spans few independent semantic directions. Found by exhaustive '
            + 'search, independent of the binge detector, so they can confirm a binge or reveal a '
            + 'focused stretch it missed. '
            + `Sequences carry no trend scan: at ${p.window_n} videos only a perfectly ordered `
            + 'variable could survive correction, and roughly 3% of sequences show one by chance, '
            + 'so any result would be noise. Binges are scanned instead.';
    }
}




function sessFmtMinutes(m) {
    if (m == null) { return '–'; }
    if (m < 60) { return `${Math.round(m)} min`; }
    return `${Math.floor(m / 60)}h ${Math.round(m % 60)}m`;
}




// Session timestamps come from `local_timestamp` — the participant's own wall
// clock, serialized zone-less — so they render verbatim through the shared
// formatter (24-hour, "16-Oct-2024 15:32"), exactly like the Video Analysis
// detail panel's "Activity timestamp". Do not route them through
// toLocaleString(): that renders 12-hour on en-US and disagrees with the rest
// of the UI.
function sessFmtTs(ts) {
    if (!ts) { return '–'; }
    return fypFmtAuto(ts, String(ts).slice(0, 16));
}




// Binge count for one table row, with a ✳ when at least one of them is
// directed. `n_directed_episodes` is null (not 0) on an artifact built before
// the test existed — those rows get no marker rather than a false "none".
function sessBingeCell(s) {
    if (!s.n_episodes) {
        return '<span style="color: var(--color-text-muted);">–</span>';
    }
    const directed = s.n_directed_episodes;
    const mark = (directed != null && directed > 0)
        ? `<sup class="meta-tooltip" style="color: var(--color-warning); cursor: help;" data-tooltip="${escapeHtml(
            `${directed} of ${s.n_episodes} binge(s) here are DIRECTED — the content travels `
            + `rather than circling one topic, at p < ${sessParams().drift_p} against `
            + 'reorderings of the binge’s own videos.')}">✳</sup>`
        : '';
    return `<span style="color: var(--color-accent);" class="font-medium">${s.n_episodes}</span>${mark}`;
}




function sessRenderList(data) {
    const listEl = document.getElementById('sess-list');
    const summary = document.getElementById('sess-list-summary');
    listEl.innerHTML = '';
    const filtered = SESS_FILTERS.some(d => sessFilterActive(d.key));
    if (!data.sessions.length) {
        summary.textContent = filtered ? 'No sessions match the current filters.' : '';
    } else if (data.total_matching > data.returned) {
        const first = sessNum(data.page, 0) * (sessNum(data.page_size, 200) || 200);
        summary.textContent = `Sessions ${(first + 1).toLocaleString()}–${(first + data.returned).toLocaleString()} `
            + `of ${data.total_matching.toLocaleString()}${filtered ? ' matching the filters' : ''} — `
            + 'click a column heading to re-rank.';
    } else {
        summary.textContent = `Showing all ${data.returned} ${filtered ? 'matching ' : ''}session(s).`;
    }

    const table = document.createElement('table');
    table.className = 'sess-table';

    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    const params = sessParams();
    for (const col of SESS_COLUMNS) {
        const th = document.createElement('th');
        th.className = 'text-xs';
        const sorted = col.key && sessState.sort.key === col.key;
        const arrow = sorted ? (sessState.sort.order === 'asc' ? ' ▲' : ' ▼') : '';
        const label = `${escapeHtml(col.label)}${arrow}`;
        const tooltip = (typeof col.tooltip === 'function') ? col.tooltip(params) : col.tooltip;
        th.innerHTML = tooltip
            ? `<span class="meta-tooltip tooltip-below" data-tooltip="${escapeHtml(tooltip)}">${label}</span>`
            : label;
        if (col.key) {
            th.classList.add('sortable');
            if (sorted) { th.classList.add('sorted'); }
            th.addEventListener('click', () => sessSortBy(col.key, col.defaultOrder));
        }
        headRow.appendChild(th);
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    if (!data.sessions.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-sm" style="padding: 20px; color: var(--color-text-muted);">'
            + (filtered ? 'No sessions match the current filters.' : 'No sessions in this study.')
            + '</td></tr>';
    }
    for (const s of data.sessions) {
        const tr = document.createElement('tr');
        tr.dataset.cid = s.collection_id;
        tr.dataset.sid = s.session_id;
        const score = (s.min_window_cosdist == null) ? '–' : s.min_window_cosdist.toFixed(3);
        const covPct = Math.round((s.coverage_embedded || 0) * 100);
        tr.innerHTML = `
            <td class="text-xs font-medium">${escapeHtml(s.collection_label || s.collection_id)}</td>
            <td class="text-xs" style="white-space: nowrap; color: var(--color-text-tertiary);">${escapeHtml(sessFmtTs(s.start_ts))}</td>
            <td class="text-xs" style="white-space: nowrap;">${sessFmtMinutes(s.duration_min)}</td>
            <td class="text-xs">${s.n_plays}</td>
            <td><span class="sess-cov-bar meta-tooltip" data-tooltip="${covPct}% of distinct videos embedded"><span class="sess-cov-fill" style="width: ${covPct}%;"></span></span></td>
            <td class="text-xs">${score}</td>
            <td class="text-xs">${sessBingeCell(s)}</td>`;
        tr.addEventListener('click', () => sessSelect(s.collection_id, s.session_id, tr));
        tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    listEl.appendChild(table);
}




function sessClearDetail() {
    pauseSessionsVideos();
    sessState.detail = null;
    sessState.selected = null;
    sessState.activeSeq = null;
    document.getElementById('sess-detail').style.display = 'none';
    document.getElementById('sess-detail-empty').style.display = 'block';
    document.querySelectorAll('#sess-list tr.active').forEach(r => r.classList.remove('active'));
}




async function sessSelect(collectionId, sessionId, rowEl) {
    pauseSessionsVideos();
    document.querySelectorAll('#sess-list tr.active').forEach(r => r.classList.remove('active'));
    if (rowEl) { rowEl.classList.add('active'); }
    sessState.selected = { collection_id: collectionId, session_id: sessionId };

    const emptyEl = document.getElementById('sess-detail-empty');
    const detailEl = document.getElementById('sess-detail');
    emptyEl.style.display = 'block';
    emptyEl.textContent = 'Loading session…';
    detailEl.style.display = 'none';

    const qs = new URLSearchParams({
        study: sessState.study, collection_id: collectionId, session_id: sessionId,
    });
    try {
        const resp = await fetch(`/api/sessions/detail?${qs}`);
        const data = await resp.json();
        if (!resp.ok) {
            emptyEl.textContent = data.error || 'Failed to load the session.';
            return;
        }
        // A slow response for a previously-selected session must not clobber
        // the current selection.
        if (!sessState.selected || sessState.selected.session_id !== sessionId) { return; }
        sessState.detail = data;
        // The detail payload carries the same params block; a session can be
        // opened via a deep link before any overview has landed.
        if (data.params) { sessState.params = data.params; }
        sessState.activeSeq = null;
        emptyEl.style.display = 'none';
        detailEl.style.display = 'flex';
        sessRenderDetailHeader(data);
        sessRenderStrip(data);
        sessRenderSeqList(data);
        // Auto-activate the top binge (or, failing that, the top low-entropy
        // sequence) THROUGH the selector so its entry is visibly focused too.
        if (data.episodes.length) {
            sessSelectSeq('binge', data.episodes[0].episode_idx, 0);
        } else if (data.windows && data.windows.length) {
            sessSelectSeq('window', data.windows[0].window_idx, 0);
        } else {
            sessRenderPlayer();
        }
        sessRenderPlaylist(data);
    } catch (e) {
        emptyEl.textContent = 'Failed to load the session.';
    }
}




function sessRenderDetailHeader(data) {
    const s = data.session;
    const el = document.getElementById('sess-detail-header');
    const score = (s.min_window_cosdist == null) ? '–' : s.min_window_cosdist.toFixed(3);
    const stats = [
        `${sessFmtTs(s.start_ts)} → ${sessFmtTs(s.end_ts)}`,
        sessFmtMinutes(s.duration_min),
        `${s.n_plays} plays (${s.n_distinct} distinct)`,
        `coverage ${(100 * (s.coverage_embedded || 0)).toFixed(0)}% embedded / ${(100 * (s.coverage_annotated || 0)).toFixed(0)}% annotated / ${(100 * (s.coverage_scraped || 0)).toFixed(0)}% scraped`,
        `low-entropy min ${score}`,
        s.dominant_niche ? `mostly “${escapeHtml(s.dominant_niche)}”` : null,
    ].filter(Boolean);
    el.innerHTML = `
        <div class="text-h3 font-semibold" style="margin-bottom: 2px;">${escapeHtml(s.collection_label || s.collection_id)}</div>
        <div class="text-xs" style="color: var(--color-text-tertiary);">${stats.join(' · ')}</div>`;
}




// Distinct band colours per binge, cycled. Read from the palette tokens so
// both themes stay legible.
function sessEpisodeColor(i) {
    const tokens = ['--color-accent', '--color-success', '--color-warning', '--color-info'];
    const name = tokens[i % tokens.length];
    const v = getCSSVar(name);
    return v || getCSSVar('--color-accent') || '#5B7E98';
}




function sessRenderStrip(data) {
    const host = document.getElementById('sess-strip');
    host.innerHTML = '';
    const plays = data.plays;
    if (!plays.length) {
        host.innerHTML = '<div class="text-xs" style="color: var(--color-text-muted);">No plays.</div>';
        return;
    }

    const W = Math.max(host.clientWidth || 800, 400);
    const H = 84;
    const PAD = 6;
    const BAR_H = 48;
    const t0 = new Date(plays[0].ts).getTime();
    const t1 = new Date(plays[plays.length - 1].ts).getTime();
    const span = Math.max(t1 - t0, 1);
    const x = (ts) => PAD + ((new Date(ts).getTime() - t0) / span) * (W - 2 * PAD);

    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('width', '100%');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.style.display = 'block';

    // Binge bands behind the marks: a faint fill + an outline, so the play
    // marks inside stay legible against them.
    for (const ep of data.episodes) {
        const rect = document.createElementNS(svgNS, 'rect');
        const x0 = x(ep.start_ts);
        const x1 = x(ep.end_ts);
        rect.setAttribute('x', x0);
        rect.setAttribute('y', 4);
        rect.setAttribute('width', Math.max(x1 - x0, 3));
        rect.setAttribute('height', H - 22);
        rect.setAttribute('rx', 3);
        rect.setAttribute('fill', sessEpisodeColor(ep.episode_idx));
        rect.setAttribute('fill-opacity', '0.10');
        rect.setAttribute('stroke', sessEpisodeColor(ep.episode_idx));
        rect.setAttribute('stroke-opacity', '0.55');
        rect.style.cursor = 'pointer';
        rect.addEventListener('click', () => sessSelectSeq('binge', ep.episode_idx, 0));
        svg.appendChild(rect);
    }

    // Low-entropy sequence bands: dashed outlines (no fill), slightly taller
    // than the binge bands so the two band kinds never merge visually. They
    // are detector-independent of binges and may overlap them.
    for (const w of (data.windows || [])) {
        const rect = document.createElementNS(svgNS, 'rect');
        const x0 = x(w.start_ts);
        const x1 = x(w.end_ts);
        rect.setAttribute('x', x0);
        rect.setAttribute('y', 1);
        rect.setAttribute('width', Math.max(x1 - x0, 3));
        rect.setAttribute('height', H - 14);
        rect.setAttribute('rx', 3);
        rect.setAttribute('fill', 'none');
        rect.setAttribute('stroke', getCSSVar('--color-text-secondary'));
        rect.setAttribute('stroke-dasharray', '4 3');
        rect.setAttribute('stroke-width', '1.2');
        rect.style.cursor = 'pointer';
        rect.addEventListener('click', () => sessSelectSeq('window', w.window_idx, 0));
        svg.appendChild(rect);
    }

    // One mark per play, laid out on a real-time axis: x = when it started,
    // width = how long it was watched (capped at the next play's start so
    // marks never overlap). Height is constant — watch time is already the
    // width, and colour carries binge membership / embedding status.
    const tooltip = document.getElementById('sess-strip-tooltip');
    for (let i = 0; i < plays.length; i++) {
        const p = plays[i];
        const mark = document.createElementNS(svgNS, 'rect');
        const px = x(p.ts);
        const dwellMs = (p.dwell_s || 0) * 1000;
        let endPx = PAD + ((new Date(p.ts).getTime() + dwellMs - t0) / span) * (W - 2 * PAD);
        if (i + 1 < plays.length) { endPx = Math.min(endPx, x(plays[i + 1].ts)); }
        const w = Math.max(endPx - px, 2);
        mark.setAttribute('x', px);
        mark.setAttribute('y', H - 16 - BAR_H);
        mark.setAttribute('width', w);
        mark.setAttribute('height', BAR_H);
        mark.setAttribute('rx', 1);
        const color = (p.episode_idx != null)
            ? sessEpisodeColor(p.episode_idx)
            : (p.embedded ? getCSSVar('--color-text-secondary') : getCSSVar('--color-border'));
        mark.setAttribute('fill', color);
        mark.setAttribute('fill-opacity', p.episode_idx != null ? '1' : '0.85');
        mark.style.cursor = 'pointer';
        mark.addEventListener('mouseenter', (ev) => {
            const bits = [
                p.niche_name ? `<b>${escapeHtml(p.niche_name)}</b>` : '<i>no niche</i>',
                p.story ? escapeHtml(p.story.slice(0, 160)) : null,
                `${sessFmtTs(p.ts)} · dwell ${p.dwell_s != null ? Math.round(p.dwell_s) + 's' : '–'}`,
                p.embedded ? null : 'not embedded',
                (p.episode_idx != null) ? `binge ${p.episode_idx + 1}` : null,
            ].filter(Boolean);
            tooltip.innerHTML = bits.join('<br>');
            tooltip.style.display = 'block';
            const tx = Math.min(ev.clientX + 14, window.innerWidth - 340);
            tooltip.style.left = `${tx}px`;
            tooltip.style.top = `${ev.clientY + 14}px`;
        });
        mark.addEventListener('mouseleave', () => {
            tooltip.style.display = 'none';
        });
        mark.addEventListener('click', () => {
            if (p.episode_idx != null) {
                const ep = sessState.detail.episodes.find(e => e.episode_idx === p.episode_idx);
                const pos = ep ? Math.max(0, ep.members.findIndex(m => m.item_id === p.item_id)) : 0;
                sessSelectSeq('binge', p.episode_idx, pos);
            }
        });
        svg.appendChild(mark);
    }

    // Time axis labels.
    const mk = (px, anchor, txt) => {
        const t = document.createElementNS(svgNS, 'text');
        t.setAttribute('x', px);
        t.setAttribute('y', H - 3);
        t.setAttribute('text-anchor', anchor);
        t.setAttribute('fill', getCSSVar('--color-text-tertiary'));
        t.setAttribute('font-size', '10');
        t.setAttribute('font-family', getCSSVar('--font-sans'));
        t.textContent = txt;
        svg.appendChild(t);
    };
    mk(PAD, 'start', sessFmtTs(plays[0].ts));
    mk(W - PAD, 'end', sessFmtTs(plays[plays.length - 1].ts));

    host.appendChild(svg);
}




// Off-theme videos the binge survived. Shown whenever there are any, so a
// long binge cannot quietly present itself as an unbroken run.
function sessSkippedHtml(ep) {
    const n = sessNum(ep.n_skipped, 0);
    if (!n) { return ''; }
    const max = sessParams().max_skip;
    return `<span class="meta-tooltip sess-skipped" data-tooltip="${escapeHtml(
        `${n} video${n > 1 ? 's' : ''} inside this binge were off-theme — most often ads. `
        + `The binge survives up to ${max} in a row, on the view that an immediate return `
        + 'to the theme means the binge never ended. They are not counted as members: they '
        + 'do not enter the focus score, the distance metrics, or the creator count.')}">${n} off-theme skipped</span>`;
}




// Directed vs stationary, from the permutation p-value (direction_p) rather
// than a fixed straightness cut. Raw straightness falls like 1/sqrt(steps), so
// the old fixed 0.5 threshold was a length test that never once fired on the
// corpus; direction_p compares a binge against reorderings of its OWN members.
function sessBingeShape(ep) {
    const cut = sessParams().drift_p;
    if (ep.direction_p == null || !Number.isFinite(Number(ep.direction_p))) {
        return {
            label: 'shape untested',
            tooltip: ep.n_distinct != null && ep.n_distinct < 5
                ? `Only ${ep.n_distinct} videos. Reversing an order leaves straightness `
                  + 'unchanged, so with 4 videos the smallest reachable p is 2/4! = 0.083 — '
                  + 'no binge this short can be shown to be directed, whatever its shape.'
                : 'This binge predates the directedness test. Rebuild the sessions '
                  + 'artifacts (Refresh Caches → Sessions) to compute it.',
        };
    }
    const p = Number(ep.direction_p);
    if (p < cut) {
        return {
            label: 'directed',
            tooltip: `Directed: this binge travels. Only ${(100 * p).toFixed(1)}% of `
                + 'reorderings of its own videos are as straight, so the order it was '
                + 'watched in carries a real direction — it ends somewhere semantically '
                + 'different from where it began (the rabbit-hole shape). '
                + `p = ${p.toFixed(3)}, threshold ${cut}.`,
        };
    }
    return {
        label: 'stationary',
        tooltip: 'Stationary: the viewer circles one semantic neighbourhood. '
            + `${(100 * p).toFixed(0)}% of reorderings of these same videos are at least `
            + 'as straight, so the watch order carries no direction the content does not '
            + `already have. p = ${p.toFixed(3)}, threshold ${cut}.`,
    };
}




// One line under a binge: the strongest variable trending across it, or an
// explicit statement of why there is none. Silence would read as "we checked
// and found nothing" even where the test could not run at all.
function sessTrendHtml(scan) {
    if (!scan) { return ''; }
    const wrap = (text, tip) =>
        `<span class="text-xxs sess-trend meta-tooltip" data-tooltip="${escapeHtml(tip)}">${escapeHtml(text)}</span>`;

    if (scan.trend) {
        const t = scan.trend;
        const arrow = t.direction === 'rising' ? '↑' : '↓';
        return wrap(
            `${arrow} ${t.label} ${t.direction} across this binge (ρ ${t.rho > 0 ? '+' : ''}${t.rho})`,
            `${t.label} ${t.direction} monotonically as the binge progresses. `
            + `Spearman ρ = ${t.rho} over ${t.n} videos, exact permutation p = ${t.p}, `
            + `q = ${t.q} after Benjamini-Hochberg correction across the ${scan.scanned} `
            + 'variables that had enough data. Correlation with position, not causation: '
            + 'it says the feed served these videos in this order, not that anything drove it.');
    }
    if (!scan.scanned) {
        const why = scan.n_members < scan.min_n
            ? `this binge has ${scan.n_members} videos and the scan needs ${scan.min_n}`
            : 'none of the numeric variables covers enough of its videos';
        return wrap('no trend scan', `Not tested — ${why}. `
            + 'Below that length the exact permutation test cannot clear multiplicity '
            + 'correction across the scanned variables, so any "trend" would be an '
            + 'artifact of looking at many variables at once. This is a limit of the '
            + 'test, NOT evidence that nothing trends.');
    }
    return wrap(`no trend (${scan.scanned} variables)`,
        `${scan.scanned} numeric variables were tested for a monotone trend across this `
        + 'binge; none survived Benjamini-Hochberg correction at q < 0.05. With this few '
        + 'videos only a strong, near-perfectly ordered trend can survive, so a real but '
        + 'modest gradient would not show here.');
}




function sessRenderSeqList(data) {
    const epHost = document.getElementById('sess-episode-chips');
    const winHost = document.getElementById('sess-window-chips');
    epHost.innerHTML = '';
    winHost.innerHTML = '';

    if (!data.episodes.length) {
        epHost.innerHTML = '<div class="text-xs" style="color: var(--color-text-muted);">'
            + 'No binges detected in this session.</div>';
    }
    for (const ep of data.episodes) {
        const shape = sessBingeShape(ep);
        const creators = sessCreatorText(ep.creators);
        const entry = document.createElement('button');
        entry.type = 'button';
        entry.className = 'sess-chip sess-seq-entry';
        entry.dataset.seq = `binge:${ep.episode_idx}`;
        entry.innerHTML = `
            <span class="text-sm font-medium" style="color: ${sessEpisodeColor(ep.episode_idx)};">
                Binge ${ep.episode_idx + 1} · ${escapeHtml(ep.dominant_niche || 'mixed')}
            </span>
            <span class="text-xs" style="color: var(--color-text-tertiary); display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
                <span>${ep.n_distinct} videos</span>
                <span>${sessFmtMinutes(ep.duration_min)}</span>
                ${sessSkippedHtml(ep)}
                ${creators ? `<span class="meta-tooltip${(ep.creators && ep.creators.n_creators === 1) ? ' sess-solo-creator' : ''}" data-tooltip="${escapeHtml(sessCreatorTooltip(ep.creators))}">${escapeHtml(creators)}</span>` : ''}
                <span class="meta-tooltip" data-tooltip="Average pairwise embedding distance across ALL ${ep.n_distinct} of this binge's videos (no best-window search). Same 0–1 scale as the low-entropy sequences, but an average over the binge's whole membership — a single loosely-matching member raises it.">avg distance ${ep.focus != null ? ep.focus.toFixed(3) : '–'}</span>
                <span class="sess-badge text-xxs meta-tooltip" data-tooltip="${escapeHtml(shape.tooltip)}">${escapeHtml(shape.label)}</span>
            </span>
            ${sessTrendHtml(ep.trend_scan)}`;
        entry.addEventListener('click', () => sessSelectSeq('binge', ep.episode_idx, 0));
        epHost.appendChild(entry);
    }

    const windows = data.windows || [];
    if (!windows.length) {
        winHost.innerHTML = '<div class="text-xs" style="color: var(--color-text-muted);">'
            + `None — the session has fewer than ${sessParams().window_n} distinct embedded videos.</div>`;
    }
    for (const w of windows) {
        const entry = document.createElement('button');
        entry.type = 'button';
        entry.className = 'sess-chip sess-chip--seq sess-seq-entry';
        entry.dataset.seq = `window:${w.window_idx}`;
        entry.innerHTML = `
            <span class="text-sm font-medium" style="color: var(--color-text-secondary);">
                Sequence ${w.window_idx + 1} · ${escapeHtml(w.dominant_niche || 'mixed')}
            </span>
            <span class="text-xs" style="color: var(--color-text-tertiary); display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
                <span>${w.n_distinct} videos</span>
                <span>${sessFmtMinutes(w.duration_min)}</span>
                ${sessCreatorText(w.creators) ? `<span class="meta-tooltip${(w.creators && w.creators.n_creators === 1) ? ' sess-solo-creator' : ''}" data-tooltip="${escapeHtml(sessCreatorTooltip(w.creators))}">${escapeHtml(sessCreatorText(w.creators))}</span>` : ''}
                <span class="meta-tooltip" data-tooltip="Average pairwise embedding distance between this window's ${w.n_distinct} videos — the rank key. Sequence 1 is the session's score in the table.">distance ${w.mean_cosdist != null ? w.mean_cosdist.toFixed(3) : '–'}</span>
                <span class="meta-tooltip" data-tooltip="Normalised spectral entropy of the same ${w.n_distinct} videos (0 = all content along one semantic direction, 1 = maximally spread). Comparable across windows because they are all the same size.">entropy ${w.entropy_norm != null ? w.entropy_norm.toFixed(2) : '–'}</span>
            </span>`;
        entry.addEventListener('click', () => sessSelectSeq('window', w.window_idx, 0));
        winHost.appendChild(entry);
    }
}




// Build the full step list for one sequence: up to [sessions] context_plays
// session plays before the first member, the members themselves, and up to
// context_plays plays after the last member. Context steps are what the donor
// actually saw around the sequence — explicitly NOT part of it.
function sessBuildSteps(kind, idx) {
    const d = sessState.detail;
    if (!d) { return null; }
    const contextPlays = sessParams().context_plays;
    const src = kind === 'binge'
        ? d.episodes.find(e => e.episode_idx === idx)
        : (d.windows || []).find(w => w.window_idx === idx);
    if (!src || !src.members.length) { return null; }
    const plays = d.plays;

    const findPlayIdx = (member, fromEnd) => {
        if (fromEnd) {
            for (let i = plays.length - 1; i >= 0; i--) {
                if (plays[i].item_id === member.item_id && plays[i].ts === member.ts) { return i; }
            }
        } else {
            for (let i = 0; i < plays.length; i++) {
                if (plays[i].item_id === member.item_id && plays[i].ts === member.ts) { return i; }
            }
        }
        return -1;
    };
    const firstIdx = findPlayIdx(src.members[0], false);
    const lastIdx = findPlayIdx(src.members[src.members.length - 1], true);

    const steps = [];
    if (firstIdx > 0 && contextPlays > 0) {
        const before = plays.slice(Math.max(0, firstIdx - contextPlays), firstIdx);
        before.forEach((p, i) => steps.push({
            context: 'before', offset: before.length - i,
            item_id: p.item_id, ts: p.ts, dwell_s: p.dwell_s,
        }));
    }
    const memberStart = steps.length;
    for (const m of src.members) { steps.push({ context: null, ...m }); }
    if (contextPlays > 0 && lastIdx >= 0 && lastIdx + 1 < plays.length) {
        const after = plays.slice(lastIdx + 1, lastIdx + 1 + contextPlays);
        after.forEach((p, i) => steps.push({
            context: 'after', offset: i + 1,
            item_id: p.item_id, ts: p.ts, dwell_s: p.dwell_s,
        }));
    }
    return { steps, memberStart, memberCount: src.members.length };
}




function sessSelectSeq(kind, idx, memberPos) {
    pauseSessionsVideos();
    const built = sessBuildSteps(kind, idx);
    if (!built) { return; }
    sessState.activeSeq = {
        kind, idx, ...built,
        pos: built.memberStart + Math.min(memberPos || 0, built.memberCount - 1),
    };
    document.querySelectorAll('.sess-seq-entry').forEach(c =>
        c.classList.toggle('active', c.dataset.seq === `${kind}:${idx}`));
    sessRenderPlayer();
}




function sessStep(delta) {
    const a = sessState.activeSeq;
    if (!a || !a.steps) { return; }
    const pos = Math.min(Math.max(a.pos + delta, 0), a.steps.length - 1);
    if (pos === a.pos) { return; }
    a.pos = pos;
    sessRenderPlayer(true);
}




// Render the shared player pane for the active step: the video (auto-created;
// the same component for binges and low-entropy sequences), triangle
// prev/next stepping across context + members, and metadata below. Context
// steps are unmistakably flagged: dashed warning border, a banner, and a
// "before/after" position label instead of the member count.
function sessRenderPlayer(autoplay) {
    const pane = document.getElementById('sess-player-pane');
    const a = sessState.activeSeq;
    if (!a || !a.steps || !a.steps.length) {
        pane.classList.remove('is-context');
        pane.innerHTML = '<div class="text-sm" style="padding: 24px; color: var(--color-text-muted);">'
            + 'Select a binge or low-entropy sequence to inspect its videos.</div>';
        return;
    }
    pauseSessionsVideos();
    const m = a.steps[a.pos];
    const isContext = !!m.context;
    const playsById = {};
    for (const p of sessState.detail.plays) { playsById[p.item_id] = p; }
    const play = playsById[m.item_id] || {};
    const seqLabel = a.kind === 'binge' ? `Binge ${a.idx + 1}` : `Sequence ${a.idx + 1}`;
    const seqColor = a.kind === 'binge' ? sessEpisodeColor(a.idx) : 'var(--color-text-secondary)';

    const posLabel = isContext
        ? `${m.offset} ${m.context === 'before' ? 'before' : 'after'}`
        : `${a.pos - a.memberStart + 1} / ${a.memberCount}`;
    const banner = isContext
        ? `<div class="sess-context-banner text-xs">CONTEXT — this video is <b>not part of ${seqLabel}</b>; `
          + `the donor watched it ${m.offset} play${m.offset > 1 ? 's' : ''} ${m.context === 'before' ? 'before the sequence began' : 'after it ended'}.</div>`
        : '';

    pane.classList.toggle('is-context', isContext);
    pane.innerHTML = `
        <div class="sess-player-head text-sm">
            <span class="font-medium" style="color: ${seqColor};">${seqLabel}${isContext ? ' <span class="sess-badge text-xxs">context</span>' : ''}</span>
            <span class="sess-player-nav">
                <button type="button" class="btn-discreet" id="sess-prev" aria-label="Previous video" ${a.pos === 0 ? 'disabled' : ''}>◀</button>
                <span class="text-xs" style="color: var(--color-text-tertiary);">${posLabel}</span>
                <button type="button" class="btn-discreet" id="sess-next" aria-label="Next video" ${a.pos === a.steps.length - 1 ? 'disabled' : ''}>▶</button>
            </span>
        </div>
        ${banner}
        <div class="sess-player-media" id="sess-player-media"></div>
        <div class="sess-player-meta" id="sess-player-meta"></div>`;

    document.getElementById('sess-prev').addEventListener('click', () => sessStep(-1));
    document.getElementById('sess-next').addEventListener('click', () => sessStep(1));

    // Media slot: create the <video> immediately when the item is streamable —
    // stepping is the whole interaction, an extra click per step would grate.
    const media = document.getElementById('sess-player-media');
    if (play.streamable) {
        const video = document.createElement('video');
        video.controls = true;
        video.playsInline = true;
        video.preload = 'metadata';
        const q = play.platform ? `?platform=${encodeURIComponent(play.platform)}` : '';
        video.src = `/api/video/${encodeURIComponent(sessState.study)}/${encodeURIComponent(m.item_id)}${q}`;
        video.addEventListener('error', () => {
            media.innerHTML = '<div class="text-xs" style="color: var(--color-text-muted); padding: 12px; text-align: center;">'
                + 'Media could not be loaded (not available in this environment).</div>';
        });
        media.appendChild(video);
        if (autoplay) { video.play().catch(() => { /* user presses play */ }); }
    } else {
        media.innerHTML = '<div class="text-xs" style="color: var(--color-text-muted); padding: 24px; text-align: center;">'
            + 'Media not available in this study — metadata only.</div>';
    }

    // Metadata below the player: sequence context + annotation fields.
    const rows = [];
    const add = (label, value, tooltip) => {
        if (value == null || value === '') { return; }
        const lbl = tooltip
            ? `<span class="meta-tooltip" data-tooltip="${escapeHtml(tooltip)}">${escapeHtml(label)}</span>`
            : escapeHtml(label);
        rows.push(`<div class="sess-meta-row"><span class="sess-meta-label text-xs">${lbl}</span>`
            + `<span class="text-xs">${value}</span></div>`);
    };
    add('Niche', play.niche_name ? escapeHtml(play.niche_name) : null);
    add('Category', play.category ? escapeHtml(play.category) : null);
    add('Creator', play.author ? escapeHtml(play.author) : null);
    add('Watched', m.dwell_s != null ? `${Math.round(m.dwell_s)}s` : null);
    add('Played at', escapeHtml(sessFmtTs(m.ts)));
    if (a.kind === 'binge' && !isContext) {
        add('Δ distance',
            m.rolling_cosdist != null ? m.rolling_cosdist.toFixed(3) : 'start of binge',
            'Semantic distance from the running centre of the binge so far — how tightly this video continued the thread.');
    }
    add('Political', play.political_score != null ? play.political_score.toFixed(2) : null);
    add('Sensitivity', play.sensitivity_score != null ? play.sensitivity_score.toFixed(2) : null);
    add('Story', play.story ? escapeHtml(play.story) : null);

    document.getElementById('sess-player-meta').innerHTML = `
        ${rows.join('')}
        <div style="margin-top: 8px;">
            <button type="button" class="btn-discreet text-xs" id="sess-open-va" style="padding: 3px 10px;">Open in Video Analysis</button>
        </div>`;
    document.getElementById('sess-open-va').addEventListener('click', () =>
        sessOpenInVideoAnalysis(m.item_id, play.platform));
}




// Same drill-down contract Explore / Semantic Space use (consumed by
// checkPendingDrillDown in video_analysis.js, 5s freshness window).
function sessOpenInVideoAnalysis(itemId, platform) {
    pauseSessionsVideos();
    const study = sessState.study;
    const platformUrl = (typeof fypPlatformUrl === 'function') ? fypPlatformUrl(platform, itemId) : null;
    window._pendingDrillDown = {
        filters: {},
        searchQuery: '',
        itemId: itemId,
        platformUrl: platformUrl,
        missNotice: `That video isn't in "${study}".`,
        timestamp: Date.now(),
    };
    const tabBtn = document.querySelector('.tab-button[onclick*="video_analysis"]');
    if (tabBtn) {
        tabBtn.click();
    } else if (platformUrl) {
        window._pendingDrillDown = null;
        window.open(platformUrl, '_blank', 'noopener');
    }
}




function sessRenderPlaylist(data) {
    const el = document.getElementById('sess-playlist');
    if (!data.plays.length) { el.innerHTML = ''; return; }
    const rows = data.plays.map(p => `
        <tr class="sess-play-row${p.episode_idx != null ? ' in-episode' : ''}">
            <td class="text-xs" style="color: var(--color-text-tertiary); white-space: nowrap;">${p.seq + 1}</td>
            <td class="text-xs" style="white-space: nowrap;">${escapeHtml(sessFmtTs(p.ts))}</td>
            <td class="text-xs" style="white-space: nowrap;">${p.dwell_s != null ? Math.round(p.dwell_s) + 's' : '–'}</td>
            <td class="text-xs">${p.niche_name ? escapeHtml(p.niche_name) : '<span style="color: var(--color-text-muted);">–</span>'}</td>
            <td class="text-xs" style="color: var(--color-text-tertiary);">${p.story ? escapeHtml(p.story.slice(0, 120)) : ''}</td>
            <td class="text-xs" style="white-space: nowrap;">${p.episode_idx != null ? `<span style="color: ${sessEpisodeColor(p.episode_idx)};">binge ${p.episode_idx + 1}</span>` : ''}</td>
        </tr>`).join('');
    el.innerHTML = `
        <div style="max-height: 420px; overflow-y: auto; border: 1px solid var(--color-border-subtle); border-radius: 4px;">
            <table style="width: 100%; border-collapse: collapse;">
                <thead>
                    <tr class="text-xs" style="color: var(--color-text-secondary); text-align: left; position: sticky; top: 0; background: var(--table-header-bg);">
                        <th style="padding: 4px 8px;">#</th><th style="padding: 4px 8px;">Time</th>
                        <th style="padding: 4px 8px;">Dwell</th><th style="padding: 4px 8px;">Niche</th>
                        <th style="padding: 4px 8px;"><span class="meta-tooltip tooltip-below" data-tooltip="The machine annotation's 'video story' summary of the video's content. Blank when the video has no active machine annotation (e.g. never scraped/annotated).">Story</span></th><th style="padding: 4px 8px;"><span class="meta-tooltip tooltip-below tooltip-right-anchored" data-tooltip="Which binge this play belongs to, matching the coloured bands in the strip above. Blank = not part of any detected binge — including plays that merely happened during one's time span but were semantically unrelated (or unembedded).">Binge</span></th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        </div>`;
}
