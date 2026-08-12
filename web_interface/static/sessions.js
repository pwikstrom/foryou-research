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
    searchQ: '',         // committed free-text search (server-side, on search_text)
    varMax: { col: null, min: null, max: null, scope: 'session' },  // "session max of variable" filter (slider units); scope 'session'|'binges'
    varMaxMeta: null,    // the chosen variable's slider bounds {lo, hi}
    playsById: {},       // item_id -> play row of the current detail payload
    rangesJson: null,    // last-rendered filter bounds (skip rebuilds when unchanged)
    playlistShown: false, // playlist rendered for the current detail payload?
    stripGeom: null,     // last strip render's {x(t), times, cursor} for the marker + var plot
    varPlot: null,       // variable plotted above the strip (null = plot hidden)
};

// Response-ordering guard for the overview fetch: a slow response for an
// older filter/search state must not clobber a newer one. The abort
// controllers additionally CANCEL the superseded request — without them the
// server keeps computing responses nobody will render.
let _sessOverviewSeq = 0;
let _sessOverviewAbort = null;
let _sessDetailAbort = null;

// Small client-side cache of detail payloads: re-clicking a session renders
// instantly instead of re-reading five parquet artifacts server-side.
// Entries expire so a rebuilt artifact shows up within a few minutes.
const _sessDetailCache = new Map();
const SESS_DETAIL_CACHE_MAX = 20;
const SESS_DETAIL_CACHE_TTL_MS = 10 * 60 * 1000;




// The [sessions] limits the copy below quotes. Server-supplied (the artifact's
// own build parameters + the live context_plays); these are only the fallbacks
// for a payload that predates the `params` key.
const SESS_PARAM_FALLBACKS = {
    min_videos: 4, min_minutes: 1, max_skip: 2, flick_seconds: 3, window_n: 6,
    max_windows: 3, context_plays: 3, drift_p: 0.05, trend_min_videos: 7,
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
    { key: 'date', label: 'Session date range', rangeKey: 'start_date',
      paramMin: 'f_start_min', paramMax: 'f_start_max',
      bounds: (r) => [Math.floor(Date.parse(r[0]) / 86400000),
                      Math.ceil(Date.parse(r[1]) / 86400000)],
      fmt: (v) => fypWallDate(sessDayToIsoDate(v), sessDayToIsoDate(v)),
      param: (v) => sessDayToIsoDate(v) },
    { key: 'length', label: 'Session duration', rangeKey: 'duration_min',
      paramMin: 'f_length_min', paramMax: 'f_length_max',
      bounds: (r) => [Math.floor(r[0]), Math.ceil(r[1])],
      fmt: (v) => sessFmtMinutes(v),
      param: (v) => String(v) },
    { key: 'plays', label: 'Video play count', rangeKey: 'n_plays',
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
    { key: 'binges', label: 'Binge count', rangeKey: 'n_episodes',
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




// Wire a dual-handle range slider already in `row` (two stacked native range
// inputs over a shared track, a fill bar between the handles, a live
// readout). 'input' updates the readout/fill only; 'change' (handle
// released) calls onCommit(lo, hi).
function sessWireDualRange(row, meta, fmt, onCommit) {
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
        readout.textContent = `${fmt(lo)} – ${fmt(hi)}`;
        readout.classList.toggle('is-active', narrowed);
        return { lo, hi };
    };
    const commit = () => {
        const { lo, hi } = sync();
        onCommit(lo, hi);
    };
    inMin.addEventListener('input', sync);
    inMax.addEventListener('input', sync);
    inMin.addEventListener('change', commit);
    inMax.addEventListener('change', commit);
    sync();
}




// The shared inner markup of one slider row.
function sessDualRangeHtml(labelHtml, meta, min, max) {
    return `
        <div class="sess-filter-head text-xxs">
            <span class="sess-filter-label">${labelHtml}</span>
            <span class="sess-filter-val"></span>
        </div>
        <div class="sess-dual-range">
            <div class="sess-dual-track"></div>
            <div class="sess-dual-fill"></div>
            <input type="range" class="sess-dr-min" min="${meta.lo}" max="${meta.hi}" step="1" value="${min}">
            <input type="range" class="sess-dr-max" min="${meta.lo}" max="${meta.hi}" step="1" value="${max}">
        </div>`;
}




function sessBuildFilterRow(def, meta) {
    const val = sessState.filters[def.key];
    const row = document.createElement('div');
    row.className = 'sess-filter-row';
    const label = def.tooltip
        ? `<span class="meta-tooltip" data-tooltip="${escapeHtml(def.tooltip)}">${escapeHtml(def.label)}</span>`
        : escapeHtml(def.label);
    row.innerHTML = sessDualRangeHtml(label, meta, val.min, val.max);
    sessWireDualRange(row, meta, def.fmt, (lo, hi) => {
        sessState.filters[def.key] = { min: lo, max: hi };
        sessState.page = 0;
        sessLoadOverview();
    });
    return row;
}




// Slider units for the variable-max filter: generic ×1000 fixed-point (the
// entropy filter's pattern) so any variable's real-valued range maps onto
// integer slider steps.
const SESS_VARMAX_SCALE = 1000;

function sessVarMaxBounds(range) {
    return { lo: Math.floor(range[0] * SESS_VARMAX_SCALE),
             hi: Math.ceil(range[1] * SESS_VARMAX_SCALE) };
}


function sessVarMaxFmt(v) {
    const x = v / SESS_VARMAX_SCALE;
    return Number.isInteger(x) ? String(x) : String(Math.round(x * 100) / 100);
}


// True when the variable-max filter is narrowing the result set.
function sessVarMaxActive() {
    const vm = sessState.varMax;
    const meta = sessState.varMaxMeta;
    if (!vm.col || !meta || vm.min == null || vm.max == null) { return false; }
    return vm.min > meta.lo || vm.max < meta.hi;
}




// The "session max of variable" filter row: a variable picker, then a range
// slider over that variable's session-max values. Server-side it filters the
// baked vmax_<variable> index column.
function sessBuildVarMaxRow(varMaxRanges, varLabels) {
    const row = document.createElement('div');
    row.className = 'sess-filter-row';
    const vars = Object.keys(varMaxRanges).sort((a, b) => {
        const la = (varLabels && varLabels[a]) || a;
        const lb = (varLabels && varLabels[b]) || b;
        return la.toLowerCase().localeCompare(lb.toLowerCase());
    });

    // Keep the previous selection only while the variable still exists.
    if (sessState.varMax.col && !varMaxRanges[sessState.varMax.col]) {
        sessState.varMax = { col: null, min: null, max: null, scope: 'session' };
        sessState.varMaxMeta = null;
    }

    const tooltip = 'Pick a per-video variable; sessions are kept when the LARGEST '
        + 'value across the session’s videos falls in this range. Sessions with '
        + 'no value for the variable are hidden while the range is narrowed.';
    const scopeTooltip = 'ON: keep sessions where at least one BINGE’s largest value '
        + 'of the variable falls in the range — the filter looks inside binges only, '
        + 'so sessions without a matching binge are hidden. OFF: the largest value '
        + 'is taken across the whole session.';
    const options = ['<option value="">Filter by variable max…</option>']
        .concat(vars.map(v => `<option value="${escapeHtml(v)}"${v === sessState.varMax.col ? ' selected' : ''}>`
            + `${escapeHtml((varLabels && varLabels[v]) || v)}</option>`));
    row.innerHTML = `
        <div class="sess-filter-head text-xxs">
            <span class="sess-filter-label"><span class="meta-tooltip" data-tooltip="${escapeHtml(tooltip)}">Session max of variable</span></span>
        </div>
        <div class="sess-varmax-controls">
            <select class="sess-varmax-select text-xxs">${options.join('')}</select>
            <label class="sess-varmax-scope text-xxs meta-tooltip" data-tooltip="${escapeHtml(scopeTooltip)}">
                <input type="checkbox" class="sess-varmax-scope-cb"${sessState.varMax.scope === 'binges' ? ' checked' : ''}${sessState.varMax.col ? '' : ' disabled'}>
                binges only
            </label>
        </div>
        <div class="sess-varmax-slider"></div>`;

    const select = row.querySelector('.sess-varmax-select');
    const scopeCb = row.querySelector('.sess-varmax-scope-cb');
    const sliderHost = row.querySelector('.sess-varmax-slider');

    const renderSlider = () => {
        sliderHost.innerHTML = '';
        const col = sessState.varMax.col;
        if (!col) { return; }
        const meta = sessVarMaxBounds(varMaxRanges[col]);
        if (meta.lo >= meta.hi) {
            sessState.varMaxMeta = null;
            sliderHost.innerHTML = '<div class="text-xxs" style="color: var(--color-text-muted);">'
                + 'Every session has the same max for this variable.</div>';
            return;
        }
        sessState.varMaxMeta = meta;
        const vm = sessState.varMax;
        vm.min = vm.min == null ? meta.lo : Math.max(Math.min(vm.min, meta.hi), meta.lo);
        vm.max = vm.max == null ? meta.hi : Math.min(Math.max(vm.max, meta.lo), meta.hi);
        const inner = document.createElement('div');
        inner.innerHTML = sessDualRangeHtml('', meta, vm.min, vm.max);
        // Drop the empty label row — the picker above is this row's label.
        inner.querySelector('.sess-filter-label').textContent = '';
        sliderHost.appendChild(inner);
        sessWireDualRange(inner, meta, sessVarMaxFmt, (lo, hi) => {
            sessState.varMax.min = lo;
            sessState.varMax.max = hi;
            sessState.page = 0;
            sessLoadOverview();
        });
    };

    select.addEventListener('change', () => {
        const col = select.value || null;
        const wasNarrowed = sessVarMaxActive();
        // The binges-only scope survives a variable switch — it is a mode of
        // the filter, not of one variable.
        sessState.varMax = { col, min: null, max: null, scope: sessState.varMax.scope };
        sessState.varMaxMeta = null;
        scopeCb.disabled = !col;
        renderSlider();
        sessUpdateFilterChrome();
        // Deselecting (or switching) a variable that was narrowing the list
        // must widen it again immediately.
        if (wasNarrowed) {
            sessState.page = 0;
            sessLoadOverview();
        }
    });
    scopeCb.addEventListener('change', () => {
        sessState.varMax.scope = scopeCb.checked ? 'binges' : 'session';
        // Only re-query when the filter is actually narrowing something.
        if (sessVarMaxActive()) {
            sessState.page = 0;
            sessLoadOverview();
        }
    });
    renderSlider();
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
    const varMaxRanges = ranges ? ranges.var_max : null;
    if (varMaxRanges && Object.keys(varMaxRanges).length) {
        body.appendChild(sessBuildVarMaxRow(varMaxRanges, ranges.var_labels || {}));
    } else {
        sessState.varMax = { col: null, min: null, max: null, scope: 'session' };
        sessState.varMaxMeta = null;
        if (ranges && varMaxRanges === null) {
            // Old artifact: the vmax_ columns don't exist yet.
            const hint = document.createElement('div');
            hint.className = 'sess-filter-row text-xxs';
            hint.style.color = 'var(--color-text-muted)';
            hint.textContent = 'Variable-max filter unavailable — rebuild the sessions index '
                + '(Refresh Caches → Sessions) to enable it.';
            body.appendChild(hint);
        }
    }
    sessUpdateFilterChrome();
}




// Badge ("2 active") + Clear button + collapsed/expanded state.
function sessUpdateFilterChrome() {
    const nActive = SESS_FILTERS.filter(d => sessFilterActive(d.key)).length
        + (sessVarMaxActive() ? 1 : 0)
        + (sessState.searchQ ? 1 : 0);
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
            sessState.varMax = { col: null, min: null, max: null, scope: 'session' };
            sessState.varMaxMeta = null;
            sessState.searchQ = '';
            const search = document.getElementById('sess-search');
            if (search) { search.value = ''; }
            sessState.page = 0;
            // The bounds won't change, but the slider positions must — force
            // the panel rebuild the unchanged-ranges shortcut would skip.
            sessState.rangesJson = null;
            sessLoadOverview();
        });
    }
    // Free-text search over what the detail panel displays (stories, niches,
    // categories, creators, captions + hashtags). Debounced and immediate —
    // no Apply button; the response-sequence guard in sessLoadOverview
    // discards out-of-order responses.
    const search = document.getElementById('sess-search');
    if (search) {
        let debounce = null;
        search.addEventListener('input', () => {
            clearTimeout(debounce);
            debounce = setTimeout(() => {
                const raw = search.value.trim();
                const q = raw.length >= 2 ? raw : '';
                if (q === sessState.searchQ) { return; }
                sessState.searchQ = q;
                sessState.page = 0;
                sessLoadOverview();
            }, 300);
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




// The pager shows for any non-empty result — on a single page it reads
// "Page 1 of 1" with both arrows disabled. Hiding it there saved a row but
// made pagination undiscoverable: a table that scrolls past 200 rows with no
// footer looks like the whole result set.
function sessRenderPager(data) {
    const pager = document.getElementById('sess-pager');
    if (!pager) { return; }
    const size = sessNum(data.page_size, 200) || 200;
    const total = sessNum(data.total_matching, 0);
    if (!total) {
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
    { key: 'collection_id', label: 'Collection', defaultOrder: 'asc' },
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
    // The playlist starts hidden; this button is its only way in.
    const plToggle = document.getElementById('sess-playlist-toggle');
    if (plToggle) {
        plToggle.addEventListener('click', () => {
            const pl = document.getElementById('sess-playlist');
            if (!pl) { return; }
            const open = pl.style.display === 'none';
            pl.style.display = open ? '' : 'none';
            plToggle.textContent = open ? 'Hide' : 'Show';
            // Lazily built: the table renders on first open per payload.
            if (open && !sessState.playlistShown && sessState.detail) {
                sessRenderPlaylist(sessState.detail);
                sessState.playlistShown = true;
            }
        });
    }
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
        sessState.varMax = { col: null, min: null, max: null, scope: 'session' };
        sessState.varMaxMeta = null;
        sessState.searchQ = '';
        const search = document.getElementById('sess-search');
        if (search) { search.value = ''; }
        sessState.page = 0;
        sessState.rangesJson = null;
        _sessDetailCache.clear();
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
    if (sessVarMaxActive()) {
        const vm = sessState.varMax;
        const meta = sessState.varMaxMeta;
        qs.set('f_varmax_col', vm.col);
        if (vm.min > meta.lo) { qs.set('f_varmax_min', String(vm.min / SESS_VARMAX_SCALE)); }
        if (vm.max < meta.hi) { qs.set('f_varmax_max', String(vm.max / SESS_VARMAX_SCALE)); }
        if (vm.scope === 'binges') { qs.set('f_varmax_scope', 'binges'); }
    }
    if (sessState.searchQ) { qs.set('q', sessState.searchQ); }
    const seq = ++_sessOverviewSeq;
    if (_sessOverviewAbort) { _sessOverviewAbort.abort(); }
    const abort = new AbortController();
    _sessOverviewAbort = abort;
    try {
        const resp = await fetch(`/api/sessions/overview?${qs}`, { signal: abort.signal });
        const data = await resp.json();
        if (seq !== _sessOverviewSeq) { return; }
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
        // The slider bounds are invariant across the user's own filters, so
        // most responses carry the same `ranges` — skipping the rebuild keeps
        // the slider the user just released alive (focus, keyboard stepping)
        // and saves re-wiring the whole panel per keystroke.
        const rangesJson = JSON.stringify(data.ranges == null ? null : data.ranges);
        if (rangesJson !== sessState.rangesJson) {
            sessState.rangesJson = rangesJson;
            sessRenderFilters(data.ranges);
        } else {
            sessUpdateFilterChrome();
        }
        sessUpdateSearchAvailability(data);
        sessRenderList(data);
        sessRenderPager(data);
        statusEl.textContent = sessStatusLine(data);
    } catch (e) {
        if (e && e.name === 'AbortError') { return; }
        statusEl.textContent = 'Failed to load sessions.';
    }
}




// Disable the search box (with an explanatory placeholder) when the index
// predates the search_text column; re-enable after a rebuild.
function sessUpdateSearchAvailability(data) {
    const search = document.getElementById('sess-search');
    if (!search) { return; }
    const available = data.search_available !== false;
    search.disabled = !available;
    search.placeholder = available
        ? 'Search stories, captions, creators…'
        : 'Search needs a rebuilt sessions index';
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
            + 'ending the binge — an immediate return to the theme means it never ended. '
            + (p.flick_seconds > 0
                ? `Off-theme videos flicked past in under ${p.flick_seconds}s don't count toward `
                    + 'that limit — rejecting a video is not leaving the theme. '
                : '')
            + 'Tolerated videos are not members and are reported separately as "off-theme skipped".';
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




// Share of the video's full length that was watched, as a whole percent
// (dwell ÷ video duration, clipped to 0–100). Null when either is unknown.
function sessCompletionPct(dwellS, durationS) {
    const dwell = Number(dwellS);
    const dur = Number(durationS);
    if (!Number.isFinite(dwell) || !Number.isFinite(dur) || dur <= 0) { return null; }
    return Math.round(100 * Math.min(Math.max(dwell / dur, 0), 1));
}




// "12s (40%)" — watch time with the completion share when the video's length
// is known (it isn't for unscraped videos).
function sessDwellText(dwellS, durationS) {
    if (dwellS == null) { return null; }
    const pct = sessCompletionPct(dwellS, durationS);
    return `${Math.round(dwellS)}s${pct != null ? ` (${pct}%)` : ''}`;
}




// The collapsed per-variable min/max block: a "▸ more info" toggle plus a
// hidden two-column list. `ranges` is the API's [{variable, label, min, max,
// n}, ...]. Each label is clickable: it toggles the per-variable line plot
// above the session strip.
function sessMinMaxHtml(ranges) {
    const fmtVal = (v) => (Number.isInteger(v) ? String(v) : Number(v).toFixed(2));
    return ranges.map(r =>
        `<span class="sess-minmax-label sess-varplot-link${sessState.varPlot === r.variable ? ' is-plotted' : ''}"`
        + ` data-var="${escapeHtml(r.variable)}" role="button" tabindex="0"`
        + ` title="Plot this variable across the session">${escapeHtml(r.label)}</span>`
        + `<span>${fmtVal(r.min)} – ${fmtVal(r.max)}</span>`).join('');
}




// Attach a hidden .sess-minmax panel after `anchorEl` (inside `host`) and
// wire `toggleEl` to expand/collapse it, flipping the ▸/▾ caret.
function sessWireMinMax(host, toggleEl, ranges, title) {
    const panel = document.createElement('div');
    panel.className = 'sess-minmax text-xxs';
    panel.style.display = 'none';
    panel.innerHTML = (title ? `<span class="sess-minmax-title">${escapeHtml(title)}</span><span></span>` : '')
        + sessMinMaxHtml(ranges);
    host.appendChild(panel);
    // One delegated listener per panel: clicking a variable name toggles the
    // session line plot for that variable.
    panel.addEventListener('click', (ev) => {
        const link = ev.target.closest('.sess-varplot-link');
        if (!link) { return; }
        ev.stopPropagation();
        sessToggleVarPlot(link.dataset.var);
    });
    const caret = toggleEl.querySelector('.sess-more-caret');
    toggleEl.addEventListener('click', (ev) => {
        // The toggle can sit inside the binge-card <button>; expanding the
        // panel must not also activate the sequence.
        ev.stopPropagation();
        const open = panel.style.display === 'none';
        panel.style.display = open ? 'grid' : 'none';
        if (caret) { caret.textContent = open ? '▾' : '▸'; }
    });
}




// The "▸ (more info)" toggle markup shared by the binge cards and the
// session header.
function sessMoreToggleHtml() {
    return '<span class="sess-more-toggle text-xxs meta-tooltip" data-tooltip="Show the '
        + 'smallest and largest value of each numeric video variable observed here. '
        + 'Click a variable’s name to plot it across the session, above the strip.">'
        + '<span class="sess-more-caret">▸</span> (more info)</span>';
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
    return fypFmtAuto(ts, String(ts).slice(0, 19));
}




// Binge count for one table row, with a ✳ when at least one of them is
// directed. `n_directed_episodes` is null (not 0) on an artifact built before
// the test existed — those rows get no marker rather than a false "none".
function sessBingeCell(s, params) {
    if (!s.n_episodes) {
        return '<span style="color: var(--color-text-muted);">–</span>';
    }
    const p = params || sessParams();
    const directed = s.n_directed_episodes;
    const mark = (directed != null && directed > 0)
        ? `<sup class="meta-tooltip" style="color: var(--color-warning); cursor: help;" data-tooltip="${escapeHtml(
            `${directed} of ${s.n_episodes} binge(s) here are DIRECTED — the content travels `
            + `rather than circling one topic, at p < ${p.drift_p} against `
            + 'reorderings of the binge’s own videos.')}">✳</sup>`
        : '';
    return `<span style="color: var(--color-accent);" class="font-medium">${s.n_episodes}</span>${mark}`;
}




function sessRenderList(data) {
    const listEl = document.getElementById('sess-list');
    const summary = document.getElementById('sess-list-summary');
    listEl.innerHTML = '';
    const filtered = SESS_FILTERS.some(d => sessFilterActive(d.key))
        || sessVarMaxActive() || !!sessState.searchQ;
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

    // One string parse for the whole body + a single delegated click
    // listener, instead of 200 per-row innerHTML parses and listeners.
    const tbody = document.createElement('tbody');
    if (!data.sessions.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-sm" style="padding: 20px; color: var(--color-text-muted);">'
            + (filtered ? 'No sessions match the current filters.' : 'No sessions in this study.')
            + '</td></tr>';
    } else {
        tbody.innerHTML = data.sessions.map((s) => {
            const score = (s.min_window_cosdist == null) ? '–' : s.min_window_cosdist.toFixed(3);
            const covPct = Math.round((s.coverage_embedded || 0) * 100);
            return `<tr data-cid="${escapeHtml(s.collection_id)}" data-sid="${escapeHtml(s.session_id)}">
            <td class="text-xs font-medium">${escapeHtml(s.collection_label || s.collection_id)}</td>
            <td class="text-xs" style="white-space: nowrap; color: var(--color-text-tertiary);">${escapeHtml(sessFmtTs(s.start_ts))}</td>
            <td class="text-xs" style="white-space: nowrap;">${sessFmtMinutes(s.duration_min)}</td>
            <td class="text-xs">${s.n_plays}</td>
            <td><span class="sess-cov-bar meta-tooltip" data-tooltip="${covPct}% of distinct videos embedded"><span class="sess-cov-fill" style="width: ${covPct}%;"></span></span></td>
            <td class="text-xs">${score}</td>
            <td class="text-xs">${sessBingeCell(s, params)}</td></tr>`;
        }).join('');
        tbody.addEventListener('click', (ev) => {
            const tr = ev.target.closest('tr[data-cid]');
            if (!tr) { return; }
            sessSelect(tr.dataset.cid, tr.dataset.sid, tr);
        });
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

    const cacheKey = `${sessState.study}\x1f${collectionId}\x1f${sessionId}`;
    const cached = _sessDetailCache.get(cacheKey);
    if (cached && Date.now() - cached.ts < SESS_DETAIL_CACHE_TTL_MS) {
        sessRenderDetail(cached.data);
        return;
    }

    emptyEl.style.display = 'block';
    emptyEl.textContent = 'Loading session…';
    detailEl.style.display = 'none';

    const qs = new URLSearchParams({
        study: sessState.study, collection_id: collectionId, session_id: sessionId,
    });
    if (_sessDetailAbort) { _sessDetailAbort.abort(); }
    const abort = new AbortController();
    _sessDetailAbort = abort;
    try {
        const resp = await fetch(`/api/sessions/detail?${qs}`, { signal: abort.signal });
        const data = await resp.json();
        if (!resp.ok) {
            emptyEl.textContent = data.error || 'Failed to load the session.';
            return;
        }
        // A slow response for a previously-selected session must not clobber
        // the current selection.
        if (!sessState.selected || sessState.selected.session_id !== sessionId) { return; }
        _sessDetailCache.set(cacheKey, { ts: Date.now(), data });
        while (_sessDetailCache.size > SESS_DETAIL_CACHE_MAX) {
            _sessDetailCache.delete(_sessDetailCache.keys().next().value);
        }
        sessRenderDetail(data);
    } catch (e) {
        if (e && e.name === 'AbortError') { return; }
        emptyEl.textContent = 'Failed to load the session.';
    }
}




// Render one detail payload (fresh or from the client cache).
function sessRenderDetail(data) {
    const emptyEl = document.getElementById('sess-detail-empty');
    const detailEl = document.getElementById('sess-detail');
    sessState.detail = data;
    // One item_id -> play map per payload; the player re-renders per step and
    // must not rebuild it every time.
    sessState.playsById = {};
    for (const p of data.plays) { sessState.playsById[p.item_id] = p; }
    // The detail payload carries the same params block; a session can be
    // opened via a deep link before any overview has landed.
    if (data.params) { sessState.params = data.params; }
    sessState.activeSeq = null;
    // A different session has different variables — the plot must not carry.
    sessState.varPlot = null;
    const plotHost = document.getElementById('sess-varplot');
    if (plotHost) { plotHost.style.display = 'none'; plotHost.innerHTML = ''; }
    emptyEl.style.display = 'none';
    detailEl.style.display = 'flex';
    sessRenderDetailHeader(data);
    sessRenderStrip(data);
    sessRenderSeqList(data);
    // Auto-activate the top binge (or, failing that, the top VISIBLE
    // low-entropy sequence) THROUGH the selector so its entry is visibly
    // focused too.
    const visibleWindows = sessVisibleWindows(data);
    if (data.episodes.length) {
        sessSelectSeq('binge', data.episodes[0].episode_idx, 0);
    } else if (visibleWindows.length) {
        sessSelectSeq('window', visibleWindows[0].window_idx, 0);
    } else {
        sessRenderPlayer();
    }
    // The playlist starts hidden and most sessions are never opened that far —
    // building its full table eagerly was wasted work. Render only when it is
    // already open (a fresh payload must not leave stale rows on screen).
    sessState.playlistShown = false;
    const pl = document.getElementById('sess-playlist');
    if (pl && pl.style.display !== 'none') {
        sessRenderPlaylist(data);
        sessState.playlistShown = true;
    } else if (pl) {
        pl.innerHTML = '';
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
    const ranges = data.session_ranges || [];
    el.innerHTML = `
        <div class="text-h3 font-semibold" style="margin-bottom: 2px;">${escapeHtml(s.collection_label || s.collection_id)}</div>
        <div class="text-xs" style="color: var(--color-text-tertiary);">${stats.join(' · ')}${ranges.length ? ' · ' + sessMoreToggleHtml() : ''}</div>`;
    if (ranges.length) {
        sessWireMinMax(el, el.querySelector('.sess-more-toggle'), ranges,
                       'Min – max across this session’s videos');
    }
}




// Distinct band colours per binge, cycled. Read from the palette tokens so
// both themes stay legible.
function sessEpisodeColor(i) {
    const tokens = ['--color-accent', '--color-success', '--color-warning', '--color-info'];
    const name = tokens[i % tokens.length];
    const v = getCSSVar(name);
    return v || getCSSVar('--color-accent') || '#5B7E98';
}




// A low-entropy sequence that merely re-detects a binge adds noise, not
// signal: when at least this share of its member videos are members of
// detected binges, the sequence is hidden from the strip and the selector
// (nothing is re-searched — the artifact rows are only not shown).
const SESS_WINDOW_COVERED_SHARE = 0.8;

// The low-entropy sequences worth showing: those NOT largely covered by the
// session's binges.
function sessVisibleWindows(data) {
    const windows = data.windows || [];
    if (!windows.length || !data.episodes.length) { return windows; }
    const bingeIds = new Set();
    for (const ep of data.episodes) {
        for (const m of ep.members) { bingeIds.add(m.item_id); }
    }
    return windows.filter(w => {
        if (!w.members.length) { return true; }
        const covered = w.members.filter(m => bingeIds.has(m.item_id)).length;
        return covered / w.members.length < SESS_WINDOW_COVERED_SHARE;
    });
}




// Move the ▾ position marker above the strip to the play at `ts` (null hides
// it). The marker lives inside the strip SVG's top gutter; sessRenderStrip
// stores the geometry it needs in sessState.stripGeom.
function sessUpdateStripCursor(ts) {
    const geom = sessState.stripGeom;
    if (!geom || !geom.cursor) { return; }
    if (!ts) {
        geom.cursor.style.display = 'none';
        return;
    }
    const px = geom.x(ts);
    geom.cursor.setAttribute('points', `${px - 5},1 ${px + 5},1 ${px},9`);
    geom.cursor.style.display = '';
}




function sessRenderStrip(data) {
    const host = document.getElementById('sess-strip');
    host.innerHTML = '';
    sessState.stripGeom = null;
    const plays = data.plays;
    if (!plays.length) {
        host.innerHTML = '<div class="text-xs" style="color: var(--color-text-muted);">No plays.</div>';
        return;
    }

    const W = Math.max(host.clientWidth || 800, 400);
    // TOP is a gutter above the bands reserved for the ▾ marker that tracks
    // the video currently shown in the player.
    const TOP = 10;
    const H = 84 + TOP;
    const PAD = 6;
    const BAR_H = 48;
    // Parse every play's timestamp once (the mark, its dwell end and its
    // neighbour all need it), and read each CSS token once per render —
    // getCSSVar forces a style resolution, so calling it per mark made a long
    // session's strip O(plays) forced style recalcs.
    const times = plays.map(p => new Date(p.ts).getTime());
    const t0 = times[0];
    const t1 = times[plays.length - 1];
    const span = Math.max(t1 - t0, 1);
    const xt = (t) => PAD + ((t - t0) / span) * (W - 2 * PAD);
    const x = (ts) => xt(new Date(ts).getTime());
    const palette = {
        episodes: ['--color-accent', '--color-success', '--color-warning', '--color-info']
            .map(name => getCSSVar(name)),
        textSecondary: getCSSVar('--color-text-secondary'),
        textPrimary: getCSSVar('--color-text-primary'),
        border: getCSSVar('--color-border'),
        textTertiary: getCSSVar('--color-text-tertiary'),
        fontSans: getCSSVar('--font-sans'),
    };
    const epColor = (i) => palette.episodes[i % palette.episodes.length]
        || palette.episodes[0] || '#5B7E98';

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
        rect.setAttribute('y', 4 + TOP);
        rect.setAttribute('width', Math.max(x1 - x0, 3));
        rect.setAttribute('height', H - 22 - TOP);
        rect.setAttribute('rx', 3);
        rect.setAttribute('fill', epColor(ep.episode_idx));
        rect.setAttribute('fill-opacity', '0.10');
        rect.setAttribute('stroke', epColor(ep.episode_idx));
        rect.setAttribute('stroke-opacity', '0.55');
        rect.style.cursor = 'pointer';
        rect.addEventListener('click', () => sessSelectSeq('binge', ep.episode_idx, 0));
        svg.appendChild(rect);
    }

    // Low-entropy sequence bands: dashed outlines (no fill), slightly taller
    // than the binge bands so the two band kinds never merge visually. They
    // are detector-independent of binges and may overlap them — but ones
    // largely covered by a binge are hidden (see sessVisibleWindows).
    for (const w of sessVisibleWindows(data)) {
        const rect = document.createElementNS(svgNS, 'rect');
        const x0 = x(w.start_ts);
        const x1 = x(w.end_ts);
        rect.setAttribute('x', x0);
        rect.setAttribute('y', 1 + TOP);
        rect.setAttribute('width', Math.max(x1 - x0, 3));
        rect.setAttribute('height', H - 14 - TOP);
        rect.setAttribute('rx', 3);
        rect.setAttribute('fill', 'none');
        rect.setAttribute('stroke', palette.textSecondary);
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
        const px = xt(times[i]);
        const dwellMs = (p.dwell_s || 0) * 1000;
        let endPx = xt(times[i] + dwellMs);
        if (i + 1 < plays.length) { endPx = Math.min(endPx, xt(times[i + 1])); }
        const w = Math.max(endPx - px, 2);
        mark.setAttribute('x', px);
        mark.setAttribute('y', H - 16 - BAR_H);
        mark.setAttribute('width', w);
        mark.setAttribute('height', BAR_H);
        mark.setAttribute('rx', 1);
        const color = (p.episode_idx != null)
            ? epColor(p.episode_idx)
            : (p.embedded ? palette.textSecondary : palette.border);
        mark.setAttribute('fill', color);
        mark.setAttribute('fill-opacity', p.episode_idx != null ? '1' : '0.85');
        mark.style.cursor = 'pointer';
        // Three delegated listeners on the <svg> below serve every mark —
        // the index rides in a data attribute.
        mark.dataset.play = String(i);
        svg.appendChild(mark);
    }

    svg.addEventListener('mouseover', (ev) => {
        const idx = ev.target.dataset ? ev.target.dataset.play : undefined;
        if (idx === undefined) { return; }
        const p = plays[Number(idx)];
        const bits = [
            `#${Number(idx) + 1}` + (p.niche_name
                ? ` · <b>${escapeHtml(p.niche_name)}</b>` : ' · <i>no niche</i>'),
            p.story ? escapeHtml(p.story.slice(0, 160)) : null,
            `${sessFmtTs(p.ts)} · dwell ${sessDwellText(p.dwell_s, p.duration_s) || '–'}`,
            p.embedded ? null : 'not embedded',
            (p.episode_idx != null) ? `binge ${p.episode_idx + 1}` : null,
        ].filter(Boolean);
        tooltip.innerHTML = bits.join('<br>');
        tooltip.style.display = 'block';
        const tx = Math.min(ev.clientX + 14, window.innerWidth - 340);
        tooltip.style.left = `${tx}px`;
        tooltip.style.top = `${ev.clientY + 14}px`;
    });
    svg.addEventListener('mouseout', (ev) => {
        if (ev.target.dataset && ev.target.dataset.play !== undefined) {
            tooltip.style.display = 'none';
        }
    });
    svg.addEventListener('click', (ev) => {
        const idx = ev.target.dataset ? ev.target.dataset.play : undefined;
        if (idx === undefined) { return; }
        const p = plays[Number(idx)];
        if (p.episode_idx != null) {
            const ep = sessState.detail.episodes.find(e => e.episode_idx === p.episode_idx);
            const pos = ep ? Math.max(0, ep.members.findIndex(m => m.item_id === p.item_id)) : 0;
            sessSelectSeq('binge', p.episode_idx, pos);
        }
    });

    // Time axis labels.
    const mk = (px, anchor, txt) => {
        const t = document.createElementNS(svgNS, 'text');
        t.setAttribute('x', px);
        t.setAttribute('y', H - 3);
        t.setAttribute('text-anchor', anchor);
        t.setAttribute('fill', palette.textTertiary);
        t.setAttribute('font-size', '10');
        t.setAttribute('font-family', palette.fontSans);
        t.textContent = txt;
        svg.appendChild(t);
    };
    mk(PAD, 'start', sessFmtTs(plays[0].ts));
    mk(W - PAD, 'end', sessFmtTs(plays[plays.length - 1].ts));

    // The ▾ marker in the top gutter tracking the video shown in the player;
    // positioned by sessUpdateStripCursor.
    const cursor = document.createElementNS(svgNS, 'polygon');
    cursor.setAttribute('fill', palette.textPrimary || '#888');
    cursor.style.display = 'none';
    cursor.style.pointerEvents = 'none';
    svg.appendChild(cursor);

    host.appendChild(svg);
    // The geometry the strip cursor and the variable line plot reuse — both
    // must share this exact time→x mapping to stay aligned with the strip.
    sessState.stripGeom = { x, times, W, PAD, cursor };
    if (sessState.varPlot) { sessRenderVarPlot(); }
}




// Display name for a plottable variable, from the detail payload's ranges.
function sessVarLabel(name) {
    const d = sessState.detail;
    const hit = d && (d.session_ranges || []).find(r => r.variable === name);
    return hit ? hit.label : name.replace(/_/g, ' ');
}




// Toggle the per-variable line plot above the strip (clicking the plotted
// variable's name again hides it; clicking another switches).
function sessToggleVarPlot(name) {
    sessState.varPlot = sessState.varPlot === name ? null : name;
    document.querySelectorAll('.sess-varplot-link').forEach(el =>
        el.classList.toggle('is-plotted', el.dataset.var === sessState.varPlot));
    sessRenderVarPlot();
}




// Line plot of one variable across the session's plays, sharing the strip's
// exact time→x mapping (same width, same padding) so peaks line up with the
// play marks below. Missing values break the line rather than interpolating
// through content that has no value.
function sessRenderVarPlot() {
    const host = document.getElementById('sess-varplot');
    if (!host) { return; }
    const d = sessState.detail;
    const geom = sessState.stripGeom;
    const name = sessState.varPlot;
    if (!d || !geom || !name) {
        host.style.display = 'none';
        host.innerHTML = '';
        return;
    }
    const values = name === 'dwell_s'
        ? d.plays.map(p => (p.dwell_s == null ? null : Number(p.dwell_s)))
        : ((d.play_variables || {})[name] || null);
    host.style.display = '';
    const label = sessVarLabel(name);
    const head = `
        <div class="sess-varplot-head text-xxs">
            <span style="color: var(--color-text-secondary);">${escapeHtml(label)} across this session</span>
            <button type="button" class="btn-discreet text-xxs sess-varplot-close" aria-label="Hide plot">×</button>
        </div>`;
    const finite = [];
    if (values) {
        for (let i = 0; i < d.plays.length; i++) {
            const v = Number(values[i]);
            if (Number.isFinite(v)) { finite.push(v); }
        }
    }
    if (!values || !finite.length) {
        host.innerHTML = `${head}<div class="text-xxs" style="color: var(--color-text-muted);">`
            + 'No per-play values available for this variable in this session.</div>';
        host.querySelector('.sess-varplot-close').addEventListener('click', () => sessToggleVarPlot(name));
        return;
    }

    const { W, PAD } = geom;
    const PH = 64;
    const yTop = 8;
    const yBot = PH - 10;
    let vMin = Math.min(...finite);
    let vMax = Math.max(...finite);
    if (vMin === vMax) { vMin -= 0.5; vMax += 0.5; }
    const y = (v) => yBot - ((v - vMin) / (vMax - vMin)) * (yBot - yTop);
    const accent = getCSSVar('--color-accent') || '#5B7E98';
    const tertiary = getCSSVar('--color-text-tertiary');
    const fontSans = getCSSVar('--font-sans');

    // Step-after path segments: each value holds flat until the next play,
    // then steps vertically — a play's value is a property of that play, not
    // a trend between plays. A gap (missing value) breaks the line. An
    // isolated value (both neighbours missing) keeps a single dot — a bare
    // step needs two points, and dropping the value entirely would lie.
    const segments = [];
    let seg = [];
    const dots = [];
    for (let i = 0; i < d.plays.length; i++) {
        const v = Number(values[i]);
        if (!Number.isFinite(v)) {
            if (seg.length === 1) { dots.push(seg[0]); }
            if (seg.length > 1) { segments.push(seg); }
            seg = [];
            continue;
        }
        seg.push([geom.x(d.plays[i].ts), y(v)]);
    }
    if (seg.length === 1) { dots.push(seg[0]); }
    if (seg.length > 1) { segments.push(seg); }
    const stepPath = (pts) => pts.map(([px, py], i) =>
        i === 0 ? `M ${px.toFixed(1)} ${py.toFixed(1)}`
                : `H ${px.toFixed(1)} V ${py.toFixed(1)}`).join(' ');
    const paths = segments.map(s =>
        `<path d="${stepPath(s)}" fill="none" stroke="${accent}" stroke-width="1.4"/>`).join('')
        + dots.map(([px, py]) =>
            `<circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="1.5" fill="${accent}"/>`).join('');
    const fmtV = (v) => (Number.isInteger(v) ? String(v) : v.toFixed(2));
    host.innerHTML = `${head}
        <svg width="100%" viewBox="0 0 ${W} ${PH}" style="display: block;">
            <line x1="${PAD}" y1="${yBot}" x2="${W - PAD}" y2="${yBot}" stroke="${tertiary}" stroke-opacity="0.35"/>
            ${paths}
            <text x="${PAD}" y="${yTop}" fill="${tertiary}" font-size="9" font-family="${fontSans}">${fmtV(vMax)}</text>
            <text x="${PAD}" y="${yBot - 2}" fill="${tertiary}" font-size="9" font-family="${fontSans}">${fmtV(vMin)}</text>
        </svg>`;
    host.querySelector('.sess-varplot-close').addEventListener('click', () => sessToggleVarPlot(name));
}




// Off-theme videos the binge survived. Shown whenever there are any, so a
// long binge cannot quietly present itself as an unbroken run.
function sessSkippedHtml(ep) {
    const n = sessNum(ep.n_skipped, 0);
    if (!n) { return ''; }
    const max = sessParams().max_skip;
    return `<span class="meta-tooltip sess-skipped" data-tooltip="${escapeHtml(
        `${n} video${n > 1 ? 's' : ''} inside this binge were off-theme — most often ads. `
        + `The binge survives up to ${max} watched ones in a row (flicked-past videos don't `
        + 'count), on the view that an immediate return to the theme means the binge never '
        + 'ended. They are not counted as members: they '
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
                ? `Only ${ep.n_distinct} videos — too few to tell a real direction from `
                  + 'chance: with so few videos, every possible watch order looks about '
                  + 'as "straight" as every other, so no binge this short can be shown '
                  + 'to be directed, whatever its shape.'
                : 'This binge predates the directedness check. Rebuild the sessions '
                  + 'artifacts (Refresh Caches → Sessions) to compute it.',
        };
    }
    const p = Number(ep.direction_p);
    if (p < cut) {
        return {
            label: 'directed',
            tooltip: 'Directed: the content travels — the binge ends on noticeably '
                + 'different content from where it began (the "rabbit hole" shape). '
                + 'How we know: we compared the actual watch order against random '
                + `shufflings of the same videos, and only ${(100 * p).toFixed(1)}% of `
                + 'shufflings move this consistently from start to finish — so the '
                + 'real order almost certainly heads somewhere on purpose rather than '
                + `by chance (p = ${p.toFixed(3)}; we call a binge directed below ${cut}).`,
        };
    }
    return {
        label: 'stationary',
        tooltip: 'Stationary: the viewer circles one topic area rather than moving '
            + 'through it towards something else. How we know: '
            + `${(100 * p).toFixed(0)}% of random shufflings of these same videos look `
            + 'at least as "directed" as the actual watch order — so the order adds no '
            + `direction of its own (p = ${p.toFixed(3)}; directed would need p below ${cut}).`,
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
            `${t.label} keeps ${t.direction} from the start of this binge to its end. `
            + 'We measured how consistently it moves in one direction across the '
            + `${t.n} videos (a rank correlation, Spearman’s ρ = ${t.rho}: +1 would be `
            + 'a perfectly steady rise, −1 a perfectly steady fall), and how often '
            + 'randomly shuffling the watch order would look this consistent just by '
            + `chance (p = ${t.p} — the smaller, the less likely it is luck). Because `
            + `${scan.scanned} variables were checked at once, the bar is raised so one `
            + `of them doesn’t stand out by luck alone (adjusted value q = ${t.q}). `
            + 'Important: this only says the videos ARRIVED in this order — not that '
            + 'anything caused it, or that the viewer responded to it.');
    }
    if (!scan.scanned) {
        const why = scan.n_members < scan.min_n
            ? `this binge has ${scan.n_members} videos and the check needs at least ${scan.min_n}`
            : 'none of the numeric variables has values for enough of its videos';
        return wrap('no trend scan', `Not tested — ${why}. `
            + 'With this few videos, pure chance can easily produce a clean-looking '
            + 'rise or fall, so any "trend" reported here would be unreliable. That is '
            + 'a limit of the check, NOT evidence that nothing changes across the binge.');
    }
    return wrap(`no trend (${scan.scanned} variables)`,
        `${scan.scanned} numeric variables were each checked for a steady rise or fall `
        + 'across this binge; none passed. With a run this short, only a very strong, '
        + 'almost perfectly ordered change can pass the check — so a real but modest '
        + 'change could still be present without showing up here.');
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
        // Per-variable min/max, collapsed under the card. Wrapped in a plain
        // div so the expanding panel sits OUTSIDE the <button> (a button must
        // not contain the block the toggle reveals).
        const ranges = (ep.trend_scan && ep.trend_scan.ranges) || [];
        if (ranges.length) {
            const wrap = document.createElement('div');
            wrap.className = 'sess-seq-wrap';
            entry.insertAdjacentHTML('beforeend', sessMoreToggleHtml());
            wrap.appendChild(entry);
            sessWireMinMax(wrap, entry.querySelector('.sess-more-toggle'), ranges, null);
            epHost.appendChild(wrap);
        } else {
            epHost.appendChild(entry);
        }
    }

    const allWindows = data.windows || [];
    const windows = sessVisibleWindows(data);
    const nHidden = allWindows.length - windows.length;
    if (!allWindows.length) {
        winHost.innerHTML = '<div class="text-xs" style="color: var(--color-text-muted);">'
            + `None — the session has fewer than ${sessParams().window_n} distinct embedded videos.</div>`;
    } else if (!windows.length) {
        winHost.innerHTML = '<div class="text-xs" style="color: var(--color-text-muted);">'
            + `${nHidden === 1 ? 'The one detected sequence is' : `All ${nHidden} detected sequences are`} `
            + 'hidden — the same videos already appear in a binge above.</div>';
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
    if (windows.length && nHidden > 0) {
        const note = document.createElement('div');
        note.className = 'text-xxs';
        note.style.color = 'var(--color-text-muted)';
        note.textContent = `${nHidden} more sequence${nHidden > 1 ? 's' : ''} hidden — `
            + 'the same videos already appear in a binge above.';
        winHost.appendChild(note);
    }
}




// Build the full step list for one sequence: up to [sessions] context_plays
// session plays before the first member, every play between the first and the
// last member — members plus the "off-theme" plays the detector skipped — and
// up to context_plays plays after the last member. Context and off-theme
// steps are what the donor actually saw; both are explicitly NOT part of the
// sequence and flagged as such in the player.
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
    // Members interleaved with the off-theme plays the detector skipped:
    // walk every session play between the first and last member, emitting a
    // member step (with its 1-based memberPos) or an off-theme step.
    const memberByKey = new Map();
    src.members.forEach((m, i) => memberByKey.set(`${m.item_id}@${m.ts}`, i + 1));
    if (firstIdx >= 0 && lastIdx >= firstIdx) {
        for (let i = firstIdx; i <= lastIdx; i++) {
            const p = plays[i];
            const memberPos = memberByKey.get(`${p.item_id}@${p.ts}`);
            if (memberPos != null) {
                steps.push({ context: null, memberPos, ...src.members[memberPos - 1] });
            } else {
                steps.push({
                    context: null, offTheme: true,
                    item_id: p.item_id, ts: p.ts, dwell_s: p.dwell_s,
                });
            }
        }
    } else {
        // Members not found in the play list (defensive) — show them alone.
        src.members.forEach((m, i) => steps.push({ context: null, memberPos: i + 1, ...m }));
    }
    if (contextPlays > 0 && lastIdx >= 0 && lastIdx + 1 < plays.length) {
        const after = plays.slice(lastIdx + 1, lastIdx + 1 + contextPlays);
        after.forEach((p, i) => steps.push({
            context: 'after', offset: i + 1,
            item_id: p.item_id, ts: p.ts, dwell_s: p.dwell_s,
        }));
    }
    return { steps, memberCount: src.members.length };
}




// Session-wide id of a play: its 1-based position in the chronological play
// list of the current detail payload (#1–n). Null when the play is not found.
function sessPlayNumber(itemId, ts) {
    const d = sessState.detail;
    if (!d || !d.plays) { return null; }
    for (let i = 0; i < d.plays.length; i++) {
        if (d.plays[i].item_id === itemId && d.plays[i].ts === ts) { return i + 1; }
    }
    return null;
}




function sessSelectSeq(kind, idx, memberPos) {
    pauseSessionsVideos();
    const built = sessBuildSteps(kind, idx);
    if (!built) { return; }
    const wantPos = Math.min(memberPos || 0, built.memberCount - 1) + 1;
    let pos = built.steps.findIndex(s => s.memberPos === wantPos);
    if (pos < 0) { pos = Math.max(built.steps.findIndex(s => s.memberPos != null), 0); }
    sessState.activeSeq = { kind, idx, ...built, pos };
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
        pane.classList.remove('is-context', 'is-offtheme');
        pane.innerHTML = '<div class="text-sm" style="padding: 24px; color: var(--color-text-muted);">'
            + 'Select a binge or low-entropy sequence to inspect its videos.</div>';
        sessUpdateStripCursor(null);
        return;
    }
    pauseSessionsVideos();
    const m = a.steps[a.pos];
    const isContext = !!m.context;
    const isOffTheme = !!m.offTheme;
    const play = sessState.playsById[m.item_id] || {};
    const seqLabel = a.kind === 'binge' ? `Binge ${a.idx + 1}` : `Sequence ${a.idx + 1}`;
    const kindNoun = a.kind === 'binge' ? 'binge' : 'sequence';
    const seqColor = a.kind === 'binge' ? sessEpisodeColor(a.idx) : 'var(--color-text-secondary)';
    sessUpdateStripCursor(m.ts);

    const posLabel = isContext
        ? `${m.offset} ${m.context === 'before' ? 'before' : 'after'}`
        : (isOffTheme ? 'off-theme' : `${m.memberPos} / ${a.memberCount}`);
    const banner = isContext
        ? `<div class="sess-context-banner text-xs">Note: not part of the ${kindNoun}.</div>`
        : (isOffTheme
            ? `<div class="sess-offtheme-banner text-xs">Off-theme: played during the ${kindNoun} but not part of it.</div>`
            : '');

    pane.classList.toggle('is-context', isContext);
    pane.classList.toggle('is-offtheme', isOffTheme);
    pane.innerHTML = `
        <div class="sess-player-head text-sm">
            <span class="font-medium" style="color: ${seqColor};">${seqLabel}</span>
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
    const playNum = sessPlayNumber(m.item_id, m.ts);
    add('Session index',
        playNum != null ? `#${playNum} of ${sessState.detail.plays.length}` : null,
        'Position of this video within the whole session’s chronological play list.');
    add('Niche', play.niche_name ? escapeHtml(play.niche_name) : null);
    add('Category', play.category ? escapeHtml(play.category) : null);
    add('Creator', play.author ? escapeHtml(play.author) : null);
    add('Watched', sessDwellText(m.dwell_s, play.duration_s),
        play.duration_s != null
            ? 'Watch time, with the share of the video’s full length watched in '
              + 'parentheses (watch time ÷ video length, capped at 100%).'
            : null);
    add('Played at', escapeHtml(sessFmtTs(m.ts)));
    if (a.kind === 'binge' && !isContext && !isOffTheme) {
        add('Δ distance',
            m.rolling_cosdist != null ? m.rolling_cosdist.toFixed(3) : 'start of binge',
            'Semantic distance from the running centre of the binge so far — how tightly this video continued the thread.');
    }
    if (isContext || isOffTheme) {
        // Distance from the sequence's member centroid, server-computed —
        // the "why was this one left out" number for context/off-theme steps.
        const src = a.kind === 'binge'
            ? sessState.detail.episodes.find(e => e.episode_idx === a.idx)
            : (sessState.detail.windows || []).find(w => w.window_idx === a.idx);
        const dist = src && src.context_distances
            ? src.context_distances[`${m.item_id}@${m.ts}`] : null;
        // No distance + no embedding is the common honest case: an unembedded
        // video can never join a binge, and that IS the explanation.
        const distText = dist != null
            ? dist.toFixed(3)
            : (play.embedded === false ? 'not embedded' : null);
        add('Δ distance', distText,
            `Semantic distance from the centre of the ${kindNoun}’s videos. A large value `
            + `shows why this video was not part of the ${kindNoun}; a small one usually means `
            + 'it was blocked by another rule (e.g. it fell outside the run). "Not embedded" '
            + `means the video has no semantic embedding at all — it could never join a ${kindNoun}.`);
    }
    add('Political', play.political_score != null ? play.political_score.toFixed(2) : null);
    add('Sensitivity', play.sensitivity_score != null ? play.sensitivity_score.toFixed(2) : null);
    add('Story', play.story ? escapeHtml(play.story) : null);
    add('Description', play.desc ? escapeHtml(play.desc) : null,
        'The video’s caption as posted (scraped or donated).');
    add('Hashtags', play.hashtags ? escapeHtml(play.hashtags) : null);

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
    // Scope the viewer to the session's collection so prev/next stay inside it.
    const collectionId = sessState.selected ? sessState.selected.collection_id : null;
    const filters = collectionId
        ? { collection_id: { type: 'category', value: [collectionId] } }
        : {};
    window._pendingDrillDown = {
        filters: filters,
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
            <td class="text-xs" style="white-space: nowrap;">${sessDwellText(p.dwell_s, p.duration_s) || '–'}</td>
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
