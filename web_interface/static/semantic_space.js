// Semantic Space tab — global video embedding map.
// Loads recoded/video_map.parquet (via /api/semantic_space/map) once and
// renders a WebGL scatter of the 2D-projected videos. The map is corpus-wide
// (not study-scoped), so it loads independently of the active study.

let _ssData = null;
let _ssLoaded = false;
let _ssHandlersWired = false;
let _ssStatusTimer = null;
let _ssLoadedMapBuiltAt = null;   // mtime of the map currently rendered
let _ssLabelTimer = null;         // debounce for zoom-driven label refresh
let _ssHidden = new Set();        // categories toggled off via the legend swatches
let _ssLastColorMode = null;      // detect colour-variable switches to reset _ssHidden
let _ssLegendCats = null;         // distinct categories backing the current swatches
let _ssTrajectory = null;         // last-fetched collection trajectory payload
let _ssTrajOn = false;            // whether the trajectory overlay is shown
let _ssCollectionsStudy;          // study the collection list was loaded for (undefined = not loaded)
let _ssAnimPos = null;            // continuous playback position (float over points); null = static
let _ssAnimRAF = null;            // requestAnimationFrame handle while playing
let _ssAnimLastTime = null;       // last rAF timestamp, for dt-based advance
let _ssAnimStepMs = 800;          // ms to morph across one period (set per run)
let _ssAnimPlaying = false;       // whether playback is running

// Categorical data palette (tab20-style). Niche colour = palette[niche % 20];
// category colours are assigned by index. Numeric overlays use _SS_NUMERIC_SCALE.
const _SS_PALETTE = [
    '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
    '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
    '#aec7e8', '#ffbb78', '#98df8a', '#ff9896', '#c5b0d5',
    '#c49c94', '#f7b6d2', '#c7c7c7', '#dbdb8d', '#9edae5'
];
const _SS_MAX_LABELS = 30;

// Numeric colour scale (low → high). Deliberately bounded to saturated,
// mid-luminance hues (blue → teal → green → orange → red) — it avoids the
// near-black and near-white ends of perceptual ramps like Viridis, so points
// stay visible on both the dark and light chart backgrounds. Used for the
// point colours (as a Plotly colorscale) and the HTML gradient legend, so the
// two always match.
const _SS_NUMERIC_SCALE = [
    '#2c6fd6', '#159ca8', '#1faa6d', '#5fb53b', '#e08a2b', '#cf3a3a'
];
const _SS_NUMERIC_COLORSCALE = _SS_NUMERIC_SCALE.map(
    (c, i, a) => [a.length === 1 ? 0 : i / (a.length - 1), c]
);

// Numeric overlays are coloured over a ROBUST range — the 2nd/98th percentile of
// the field, not its raw min/max — so a few outliers can't compress the bulk of
// the distribution into a single hue (e.g. "Face age estimate", whose spread is
// concentrated but has young/old outliers). Values past the range saturate at
// the ends, and the legend marks this with ≤/≥.
const _SS_ROBUST_PCT = 0.02;

// Trajectory overlay colours, deliberately chosen from hues the tab20 niche/
// category background palette never uses, so the overlay never reads as a
// background dot. The all-time centre of gravity is a neutral WHITE anchor;
// per-period centroids + dispersion clouds use a magenta recency ramp (early →
// late): deep magenta → bright pink (see _ssTimeColorRGB). Staying within the
// magenta family means the ramp never passes through a background hue.
const _SS_TRAJ_ACCENT = '#ffffff';
const _SS_TRAJ_T0 = [150, 45, 140];    // earliest period — deep magenta
const _SS_TRAJ_T1 = [255, 120, 225];   // latest period — bright pink
const _SS_TRAJ_MAX_ELLIPSES = 52;   // above this many periods, skip clouds (path only)

// Visual scale for the dispersion ellipses. Absolute size is arbitrary (the
// ellipse semi-axes are k·sigma of the period's 2D spread); this shrinks them
// uniformly so the RELATIVE sizes — how diversity changes over time and across
// collections — stay legible without the clouds swamping the map. Fixed at 0.30.
const _SS_ELLIPSE_SCALE = 0.30;

// Playback: older periods decay by _SS_ANIM_FADE per step back (comet trail);
// a period is dropped once its fade falls below _SS_ANIM_MIN_ALPHA. Slow decay
// + a higher floor → a long, clearly-visible trail.
const _SS_ANIM_FADE = 0.78;
const _SS_ANIM_MIN_ALPHA = 0.12;


// Called by openTab() the first time the Semantic Space tab is shown.
function initSemanticSpace() {
    _ssStartStatusPoll();
    if (_ssLoaded) {
        // Container width may have changed while hidden — nudge a resize.
        const div = document.getElementById('semantic-space-plot');
        if (div && div.data) { Plotly.Plots.resize(div); }
        return;
    }
    _ssLoaded = true;
    loadSemanticSpace();
}


async function loadSemanticSpace() {
    const status = document.getElementById('ss-status');
    if (status) { status.innerText = 'Loading map…'; }
    try {
        const res = await fetch('/api/semantic_space/map');
        const data = await res.json();
        if (!res.ok || data.error) {
            if (status) { status.innerText = data.error || `Error ${res.status}`; }
            _ssLoaded = false;   // allow a retry on next open
            return;
        }
        _ssData = data;
        _ssLoadedMapBuiltAt = (data.map_built_at !== undefined) ? data.map_built_at : null;
        _ssComputeCentroids();
        _ssPopulateNicheFocus();
        _ssPopulateColorModes();
        if (status) {
            status.innerText =
                `${data.total_mapped.toLocaleString()} videos shown · `
                + `${data.n_niches} niches · ${data.total_videos.toLocaleString()} embedded`;
        }
        _ssWireControls();
        // Load the study-scoped collection list once the active study is known.
        if (window.studyState && window.studyState.ready) {
            window.studyState.ready.then(() => _ssLoadCollections());
        } else {
            _ssLoadCollections();
        }
        renderSemanticSpace();
        _ssPollStatus();   // surface the freshness banner without a poll delay
    } catch (e) {
        console.error(e);
        if (status) { status.innerText = 'Failed to load map.'; }
        _ssLoaded = false;
    }
}


// Per-niche median position + size, used for centroid labels and the focus list.
function _ssComputeCentroids() {
    const P = _ssData.points;
    const acc = {};   // niche -> {xs:[], ys:[]}
    for (let i = 0; i < P.x.length; i++) {
        const n = P.niche[i];
        (acc[n] = acc[n] || { xs: [], ys: [] });
        acc[n].xs.push(P.x[i]);
        acc[n].ys.push(P.y[i]);
    }
    const median = arr => {
        const s = arr.slice().sort((a, b) => a - b);
        return s[Math.floor(s.length / 2)];
    };
    _ssData._centroids = Object.keys(acc).map(n => ({
        niche: +n,
        name: (_ssData.niches[n] || {}).name || `Niche ${n}`,
        size: (_ssData.niches[n] || {}).size || acc[n].xs.length,
        x: median(acc[n].xs),
        y: median(acc[n].ys)
    }));
}


function _ssPopulateNicheFocus() {
    const sel = document.getElementById('ss-niche-focus');
    if (!sel) { return; }
    const sorted = _ssData._centroids.slice().sort((a, b) => b.size - a.size);
    sel.innerHTML = '<option value="">— all niches —</option>'
        + sorted.map(c => `<option value="${c.niche}">${c.name} (${c.size.toLocaleString()})</option>`).join('');
}


function _ssWireControls() {
    if (_ssHandlersWired) { return; }
    _ssHandlersWired = true;
    ['ss-color-mode', 'ss-niche-focus', 'ss-show-labels'].forEach(id => {
        const el = document.getElementById(id);
        if (el) { el.addEventListener('change', renderSemanticSpace); }
    });
    const legend = document.getElementById('ss-legend');
    if (legend) { legend.addEventListener('click', _ssOnLegendClick); }

    // Trajectory overlay controls. Selecting a collection (or changing the
    // interval) refetches; the toggle just flips overlay visibility on the
    // already-loaded payload, so it never hits the network.
    const coll = document.getElementById('ss-collection');
    if (coll) { coll.addEventListener('change', _ssLoadTrajectory); }
    const ivSel = document.getElementById('ss-traj-interval');
    if (ivSel) {
        ivSel.addEventListener('change', () => {
            if ((document.getElementById('ss-collection') || {}).value) { _ssLoadTrajectory(); }
        });
    }
    const tog = document.getElementById('ss-show-trajectory');
    if (tog) {
        tog.addEventListener('change', () => {
            _ssTrajOn = tog.checked;
            if (!_ssTrajOn) { _ssAnimReset(); } else { renderSemanticSpace(); }
        });
    }
    // Play/Stop: step through the periods, fading older clouds into a trail.
    const play = document.getElementById('ss-anim-play');
    if (play) { play.addEventListener('click', _ssAnimToggle); }
    // Scrub slider: jump straight to any period frame (no smooth tween needed).
    const scrub = document.getElementById('ss-scrub');
    if (scrub) { scrub.addEventListener('input', _ssOnScrub); }
    // Reload the (study-scoped) collection list when the active study changes,
    // dropping any trajectory whose collection isn't in the new study.
    document.addEventListener('study:changed', async () => {
        const coll = document.getElementById('ss-collection');
        const before = coll ? coll.value : '';
        await _ssLoadCollections();
        if (before && coll && coll.value !== before) { _ssLoadTrajectory(); }
    });
}


// Build the "Colour by" dropdown: Niche (always) + every overlay the backend
// advertised for this map.
function _ssPopulateColorModes() {
    const sel = document.getElementById('ss-color-mode');
    if (!sel) { return; }
    const opts = ['<option value="niche">Niche</option>'];
    (_ssData.overlays || []).forEach(o => {
        opts.push(`<option value="${o.key}">${o.label}</option>`);
    });
    sel.innerHTML = opts.join('');
}


function _ssOverlay(key) {
    return (_ssData.overlays || []).find(o => o.key === key) || null;
}


function _ssDistinct(field) {
    return Array.from(new Set(_ssData.points[field] || [])).sort();
}


// Stable category → colour map for a categorical overlay field.
function _ssCatColorMap(field) {
    const map = {};
    _ssDistinct(field).forEach((v, i) => { map[v] = _SS_PALETTE[i % _SS_PALETTE.length]; });
    return map;
}


// Current zoom window in data coordinates, or null when the map is fully zoomed
// out (autoranged) — in which case every centroid stays eligible for a label.
function _ssCurrentRanges(div) {
    const fl = div && div._fullLayout;
    if (!fl || !fl.xaxis || !fl.yaxis) { return null; }
    if (fl.xaxis.autorange || fl.yaxis.autorange) { return null; }
    if (!fl.xaxis.range || !fl.yaxis.range) { return null; }
    return { x: fl.xaxis.range.slice(), y: fl.yaxis.range.slice() };
}


// Centroids whose median position falls inside the current view. With no zoom
// window every centroid qualifies; zooming in shrinks the set so smaller niches
// (previously crowded out) become eligible for a label.
function _ssVisibleCentroids(ranges) {
    const cents = _ssData._centroids || [];
    if (!ranges) { return cents; }
    const xlo = Math.min(ranges.x[0], ranges.x[1]);
    const xhi = Math.max(ranges.x[0], ranges.x[1]);
    const ylo = Math.min(ranges.y[0], ranges.y[1]);
    const yhi = Math.max(ranges.y[0], ranges.y[1]);
    return cents.filter(c => c.x >= xlo && c.x <= xhi && c.y >= ylo && c.y <= yhi);
}


function _ssLabelAnnotation(c) {
    return {
        x: c.x, y: c.y, text: c.name, showarrow: false,
        font: { family: getCSSVar('--font-sans'), size: 10, color: getCSSVar('--white') },
        bgcolor: getCSSVar('--chart-badge-bg'), borderpad: 2, opacity: 0.92
    };
}


// Centroid label annotations for the current view. Labels are niche markers, so
// they show whenever the checkbox is on regardless of which variable colours the
// points. While focusing, only the focused niche is labelled; otherwise the
// largest in-view niches are kept (capped at _SS_MAX_LABELS), so zooming in
// reveals more of them.
function _ssBuildLabels(focusNiche, showLabels, ranges) {
    if (!showLabels) { return []; }
    if (focusNiche !== null) {
        const c = (_ssData._centroids || []).find(cc => cc.niche === focusNiche);
        return c ? [_ssLabelAnnotation(c)] : [];
    }
    return _ssVisibleCentroids(ranges)
        .slice().sort((a, b) => b.size - a.size)
        .slice(0, _SS_MAX_LABELS)
        .map(_ssLabelAnnotation);
}


// Re-label as the user zooms/pans: recompute which centroids are in view and
// update just the annotation layer (no scatter redraw). Debounced so scroll-zoom
// bursts coalesce. Ignores the annotation-only relayouts this handler triggers.
function _ssOnZoomRelayout(ev) {
    const div = document.getElementById('semantic-space-plot');
    if (!div || !_ssData) { return; }
    const axisChange = Object.keys(ev).some(k => k.indexOf('xaxis') === 0 || k.indexOf('yaxis') === 0);
    if (!axisChange) { return; }
    clearTimeout(_ssLabelTimer);
    _ssLabelTimer = setTimeout(() => {
        const focus = (document.getElementById('ss-niche-focus') || {}).value || '';
        const showLabels = (document.getElementById('ss-show-labels') || {}).checked;
        Plotly.relayout(div, {
            annotations: _ssBuildLabels(focus === '' ? null : +focus, showLabels, _ssCurrentRanges(div))
        });
    }, 100);
}


function renderSemanticSpace() {
    if (!_ssData) { return; }
    const div = document.getElementById('semantic-space-plot');
    if (!div || typeof Plotly === 'undefined') { return; }

    const mode = (document.getElementById('ss-color-mode') || {}).value || 'niche';
    const focus = (document.getElementById('ss-niche-focus') || {}).value || '';
    const showLabels = (document.getElementById('ss-show-labels') || {}).checked;
    const P = _ssData.points;
    const n = P.x.length;
    const focusNiche = focus === '' ? null : +focus;

    // Switching the colour variable clears any per-category hide toggles (the
    // hidden set only makes sense for the categories currently on screen).
    if (mode !== _ssLastColorMode) { _ssHidden.clear(); _ssLastColorMode = mode; }

    // Per-point colour by the selected mode. Niche + categorical overlays use
    // the discrete palette; numeric overlays use the continuous _SS_NUMERIC_SCALE.
    const overlay = mode === 'niche' ? null : _ssOverlay(mode);
    let colorArr;
    let markerExtra = {};
    let catColorMap = null;
    if (overlay && overlay.kind === 'numeric') {
        // Clamp the colour range to the field's robust [p2, p98] so outliers
        // saturate at the ends instead of washing the bulk into one hue. Nulls
        // map to the low end.
        const [clo, chi] = _ssRobustRange(overlay.field);
        colorArr = (P[overlay.field] || []).map(v => (v == null ? clo : v));
        // Colour the points by the numeric scale, but DON'T draw Plotly's
        // in-figure colourbar — it resizes the plot box and (via the 1:1 aspect
        // lock) shifts the scatter when the scale changes. The scale is shown as
        // an HTML gradient legend below the plot instead (see _ssRenderLegend),
        // so the plot box never changes between colour modes.
        markerExtra = { colorscale: _SS_NUMERIC_COLORSCALE, showscale: false, cmin: clo, cmax: chi };
    } else if (overlay && overlay.kind === 'categorical') {
        catColorMap = _ssCatColorMap(overlay.field);
        colorArr = (P[overlay.field] || []).map(v => catColorMap[v] || '#888');
    } else {
        colorArr = P.niche.map(nn => _SS_PALETTE[nn % _SS_PALETTE.length]);
    }

    // Per-point size/opacity. Focus enlarges one niche and fades the rest;
    // legend toggles hide whole categories (size + opacity 0 so they vanish and
    // stop catching hovers). Hiding wins over focus. None of this changes the
    // plot box, so the scatter stays put.
    // While the trajectory overlay is on, fade the base dots so the clouds and
    // path (which now draw above them) clearly dominate; niche colour stays for
    // context.
    const baseOpacity = _ssTrajOn ? 0.18 : 0.75;
    let sizeArr = 4;
    let opacityArr = baseOpacity;
    const hideField = (overlay && overlay.kind === 'categorical' && _ssHidden.size) ? overlay.field : null;
    if (focusNiche !== null || hideField) {
        sizeArr = new Array(n);
        opacityArr = new Array(n);
        for (let i = 0; i < n; i++) {
            if (hideField && _ssHidden.has(P[hideField][i])) {
                sizeArr[i] = 0;
                opacityArr[i] = 0;
            } else if (focusNiche !== null) {
                const inFocus = P.niche[i] === focusNiche;
                sizeArr[i] = inFocus ? 7 : 3;
                opacityArr[i] = inFocus ? 0.9 : 0.08;
            } else {
                sizeArr[i] = 4;
                opacityArr[i] = baseOpacity;
            }
        }
    }

    const ovField = overlay ? overlay.field : null;
    const hover = new Array(n);
    for (let i = 0; i < n; i++) {
        const extra = ovField ? `<br>${_ssWrap(`${overlay.label}: ${P[ovField][i]}`)}` : '';
        hover[i] = `<b>${_ssWrap(P.niche_name[i])}</b>${extra}<br>${_ssWrap(P.story[i])}`;
    }

    const trace = {
        type: 'scattergl', mode: 'markers',
        x: P.x, y: P.y,
        customdata: P.item_id,
        text: hover, hoverinfo: 'text',
        // Plotly sizes the hover box to its longest line, so the wrapping above
        // is what keeps it narrow; left-align so the wrapped lines read as a
        // paragraph rather than a centred stack.
        hoverlabel: { align: 'left' },
        marker: Object.assign({ size: sizeArr, color: colorArr, opacity: opacityArr,
            line: { width: 0 } }, markerExtra)
    };

    // Centroid niche labels, scoped to the current zoom window so more (smaller)
    // niches get labelled as the user zooms in. Carry that window into the layout
    // too, so recolouring keeps the user's zoom instead of snapping to overview.
    const ranges = _ssCurrentRanges(div);
    const annotations = _ssBuildLabels(focusNiche, showLabels, ranges);

    const layout = {
        hovermode: 'closest', showlegend: false,
        // Symmetric margins: with no in-figure colourbar (the scale lives in the
        // HTML legend below the plot), the plot box is identical in every colour
        // mode, so the scatter never shifts when the colour variable changes.
        margin: { l: 10, r: 10, t: 10, b: 10 },
        xaxis: Object.assign({ visible: false, fixedrange: false },
            ranges ? { range: ranges.x, autorange: false } : {}),
        yaxis: Object.assign({ visible: false, scaleanchor: 'x', scaleratio: 1 },
            ranges ? { range: ranges.y, autorange: false } : {}),
        paper_bgcolor: getCSSVar('--chart-bg'),
        plot_bgcolor: getCSSVar('--chart-bg'),
        font: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text') },
        annotations: annotations,
        // Per-period dispersion ellipses (layer:'above' → on top of the dots).
        // Empty when the overlay is off, so toggling clears them.
        shapes: _ssTrajectoryShapes()
    };

    // Base scatter stays trace 0; trajectory overlays (if any) are appended
    // after it, so the click/zoom/focus/legend logic above is untouched.
    Plotly.react(div, [trace].concat(_ssTrajectoryTraces()), layout,
        { responsive: true, displayModeBar: true, scrollZoom: true });
    _ssRenderLegend(mode, overlay, catColorMap);

    if (!div._ssClickWired) {
        div._ssClickWired = true;
        div.on('plotly_click', function (ev) {
            const pt = ev.points && ev.points[0];
            // Only the base scatter (curve 0) opens a video; overlay traces ignore clicks.
            if (pt && pt.curveNumber === 0 && pt.customdata) {
                _ssOpenInVideoAnalysis(pt.customdata, pt.pointNumber);
            }
        });
        div.on('plotly_relayout', _ssOnZoomRelayout);
    }
}


// Clicking a dot drills into Video Analysis: filter to the dot's niche and land
// on that exact video. This map covers the whole corpus while Video Analysis is
// scoped to the active study, so the video may not be there — the drill-down
// carries the platform URL as a fallback and Video Analysis explains the miss.
function _ssOpenInVideoAnalysis(itemId, i) {
    const P = _ssData && _ssData.points;
    if (!P || !itemId) { return; }
    // The trace is built from the payload arrays unfiltered, so Plotly's point
    // index addresses the parallel arrays directly. Guard anyway: a mismatch
    // would attach the wrong niche to the drill-down.
    const ok = (i != null && i < P.item_id.length && P.item_id[i] === itemId);
    const nicheId = ok ? P.niche[i] : null;
    const nicheName = ok ? ((_ssData.niches[nicheId] || {}).name || P.niche_name[i]) : null;
    const platform = (ok && P.source_platform) ? P.source_platform[i] : null;
    const platformUrl = (typeof fypPlatformUrl === 'function') ? fypPlatformUrl(platform, itemId) : null;

    const study = (window.studyState && window.studyState.current) || null;
    if (!study) {
        // Nothing to drill into — fall back to the post itself.
        if (platformUrl) window.open(platformUrl, '_blank', 'noopener');
        return;
    }

    // Same contract Explore / Correlations / Timelines use (consumed by
    // checkPendingDrillDown, which enforces a 5s freshness window — hence the
    // synchronous tab click below).
    window._pendingDrillDown = {
        filters: nicheName
            ? { niche_name: { type: 'category', value: [nicheName] } }
            : {},
        searchQuery: '',
        itemId: itemId,
        platformUrl: platformUrl,
        missNotice: `That video isn't in "${study}"${nicheName ? ` — showing the "${nicheName}" niche instead` : ''}.`,
        timestamp: Date.now()
    };

    const tabBtn = document.querySelector('.tab-button[onclick*="video_analysis"]');
    if (tabBtn) {
        tabBtn.click();
    } else if (platformUrl) {
        // No Video Analysis permission — the post itself is still useful.
        window._pendingDrillDown = null;
        window.open(platformUrl, '_blank', 'noopener');
    }
}


// Robust [low, high] colour range for a numeric overlay: the field's
// _SS_ROBUST_PCT / (1 - _SS_ROBUST_PCT) percentiles over its non-null values
// (cached per field on _ssData). Used as Plotly's cmin/cmax and for the legend
// endpoints, so the same robust range drives both the dots and the scale.
function _ssRobustRange(field) {
    if (!_ssData) { return [0, 1]; }
    _ssData._robust = _ssData._robust || {};
    if (_ssData._robust[field]) { return _ssData._robust[field]; }
    const src = _ssData.points[field] || [];
    const arr = [];
    for (let i = 0; i < src.length; i++) {
        const v = src[i];
        if (v != null && isFinite(v)) { arr.push(v); }
    }
    let range;
    if (!arr.length) {
        range = [0, 1];
    } else {
        arr.sort((a, b) => a - b);
        const at = q => arr[Math.min(arr.length - 1, Math.max(0, Math.round(q * (arr.length - 1))))];
        let lo = at(_SS_ROBUST_PCT), hi = at(1 - _SS_ROBUST_PCT);
        if (!(hi > lo)) { hi = lo + (Math.abs(lo) || 1) * 1e-3 + 1e-6; }
        range = [lo, hi];
    }
    _ssData._robust[field] = range;
    return range;
}


function _ssFmtNum(v) {
    return formatMetricNumber(v);
}


// Break a hover line at word boundaries so Plotly's hover box stops widening to
// fit it. Video stories are a single unbroken 140-character run and categorical
// overlay values can be nearly as long, either of which produces a hover box
// wider than the plot. A word longer than the limit is left intact (it would
// only be hyphenated mid-token otherwise).
const _SS_HOVER_WRAP = 48;

function _ssWrap(text, width) {
    const s = (text === null || text === undefined) ? '' : String(text);
    const limit = width || _SS_HOVER_WRAP;
    if (s.length <= limit) { return s; }
    const lines = [];
    let line = '';
    for (const word of s.split(/\s+/)) {
        if (!word) { continue; }
        if (!line) {
            line = word;
        } else if (line.length + 1 + word.length <= limit) {
            line += ` ${word}`;
        } else {
            lines.push(line);
            line = word;
        }
    }
    if (line) { lines.push(line); }
    return lines.join('<br>');
}


// Click a categorical swatch to hide/show that category's points (delegated
// from #ss-legend; swatches are recreated on every render).
function _ssOnLegendClick(ev) {
    const sw = ev.target.closest('[data-cat-idx]');
    if (!sw || !_ssLegendCats) { return; }
    const cat = _ssLegendCats[+sw.dataset.catIdx];
    if (cat === undefined) { return; }
    if (_ssHidden.has(cat)) { _ssHidden.delete(cat); } else { _ssHidden.add(cat); }
    renderSemanticSpace();
}


// Colour legend, rendered as HTML below the plot (never inside the Plotly
// figure — see the template note). Category swatches (clickable to hide/show)
// or a numeric gradient bar.
function _ssRenderLegend(mode, overlay, catColorMap) {
    const legend = document.getElementById('ss-legend');
    if (!legend) { return; }
    const openHint = '<span style="margin-left:auto;white-space:nowrap;">click a point to open it in Video Analysis</span>';
    if (overlay && overlay.kind === 'categorical' && catColorMap) {
        _ssLegendCats = _ssDistinct(overlay.field);
        const swatches = _ssLegendCats.map((c, i) => {
            const off = _ssHidden.has(c);
            return `<span data-cat-idx="${i}" style="display:inline-flex;align-items:center;gap:4px;`
                + `white-space:nowrap;cursor:pointer;opacity:${off ? 0.45 : 1};`
                + `text-decoration:${off ? 'line-through' : 'none'};">`
                + `<span style="width:9px;height:9px;border-radius:2px;background:${catColorMap[c]};`
                + `display:inline-block;${off ? 'filter:grayscale(1);' : ''}"></span>${c}</span>`;
        }).join('');
        const hint = '<span style="margin-left:auto;white-space:nowrap;">click a swatch to show/hide · click a point to open it in Video Analysis</span>';
        legend.innerHTML = swatches + hint;
    } else if (overlay && overlay.kind === 'numeric') {
        _ssLegendCats = null;
        // Endpoints are the robust [p2, p98] range used to colour the dots; the
        // ≤/≥ marks that values beyond it saturate at the ends.
        const [lo, hi] = _ssRobustRange(overlay.field);
        const grad = `linear-gradient(to right, ${_SS_NUMERIC_SCALE.join(', ')})`;
        legend.innerHTML =
            `<span class="font-medium" style="white-space:nowrap;">${overlay.label}</span>`
            + `<span>≤${_ssFmtNum(lo)}</span>`
            + `<span style="width:140px;height:10px;border-radius:2px;background:${grad};display:inline-block;"></span>`
            + `<span>≥${_ssFmtNum(hi)}</span>` + openHint;
    } else {
        _ssLegendCats = null;
        legend.innerHTML = `<span>Coloured by niche</span>${openHint}`;
    }
}


// ---------------------------------------------------------------------------
// Collection trajectory overlay — projects one collection's play activity onto
// the map as a centre of gravity, a dispersion "entropy halo", and a daily
// path through semantic space. The geometry comes from the backend
// (/api/semantic_space/trajectory); see web_interface/semantic_trajectory.py.
// ---------------------------------------------------------------------------

// Populate the collection selector for the currently-selected study. Each
// option shows the collection's display id (falling back to the raw id) as its
// label, with the raw collection_id as the value. Reloads when the active study
// changes; cheap to re-call (no-ops if already loaded for this study).
async function _ssLoadCollections(attempt) {
    const sel = document.getElementById('ss-collection');
    if (!sel) { return; }
    const study = (window.studyState && window.studyState.current) || '';
    if (_ssCollectionsStudy === study && sel.options.length > 1) { return; }
    attempt = attempt || 0;
    try {
        const qs = study ? ('?study=' + encodeURIComponent(study)) : '';
        const res = await fetch('/api/semantic_space/collections' + qs);
        if (!res.ok) { throw new Error(`status ${res.status}`); }
        const data = await res.json();
        const cols = (data && data.collections) || [];
        const prev = sel.value;   // preserve the user's choice if still in-study
        sel.innerHTML = '';
        const ph = document.createElement('option');
        ph.value = ''; ph.textContent = '— select collection —';
        sel.appendChild(ph);
        cols.forEach(c => {
            const o = document.createElement('option');
            o.value = c.id; o.textContent = c.label || c.id;
            sel.appendChild(o);
        });
        if (prev && cols.some(c => c.id === prev)) { sel.value = prev; }
        _ssCollectionsStudy = study;
    } catch (e) {
        // Network / cold-start failure — retry a few times before giving up. An
        // OK-but-empty response is a valid "no collections in this study" answer,
        // not an error, so it is accepted (placeholder only).
        if (attempt < 5) {
            setTimeout(() => _ssLoadCollections(attempt + 1), 1000);
        } else {
            console.error('Failed to load collections', e);
        }
    }
}


// Fetch the selected collection's trajectory for the current interval, then
// re-render. Deselecting clears the overlay.
async function _ssLoadTrajectory() {
    const sel = document.getElementById('ss-collection');
    const cid = sel ? sel.value : '';
    const statusEl = document.getElementById('ss-traj-status');
    // A new collection / interval invalidates any running playback or scrub.
    _ssAnimStop();
    _ssAnimPos = null;
    if (!cid) {
        _ssTrajectory = null;
        _ssSetupScrub(0);
        if (statusEl) { statusEl.textContent = ''; }
        renderSemanticSpace();
        return;
    }
    const interval = (document.getElementById('ss-traj-interval') || {}).value || 'month';
    if (statusEl) { statusEl.textContent = 'Loading trajectory…'; }
    try {
        const qs = new URLSearchParams({ collection_id: cid, interval });
        const res = await fetch('/api/semantic_space/trajectory?' + qs.toString());
        const data = await res.json();
        if (!res.ok || data.error) {
            if (statusEl) { statusEl.textContent = data.error || `Error ${res.status}`; }
            return;
        }
        _ssTrajectory = data;
        _ssSetupScrub((data.points || []).length);
        // Selecting a collection turns the overlay on (the toggle stays the
        // master switch thereafter).
        const tog = document.getElementById('ss-show-trajectory');
        if (tog) { tog.checked = true; }
        _ssTrajOn = true;
        if (statusEl) { statusEl.textContent = _ssTrajSummary(data); }
        renderSemanticSpace();
    } catch (e) {
        console.error(e);
        if (statusEl) { statusEl.textContent = 'Failed to load trajectory.'; }
    }
}


// Configure the scrub slider for a trajectory of n periods (disabled when
// there's nothing to scrub through, e.g. "All-time only" or a single period).
function _ssSetupScrub(n) {
    const scrub = document.getElementById('ss-scrub');
    if (!scrub) { return; }
    scrub.max = Math.max(0, n - 1);
    scrub.value = 0;
    scrub.disabled = n < 2;
}


// Scrub handler: jump to the dragged period (frame mode). Stops any running
// playback; no smooth tween — each input just renders that frame.
function _ssOnScrub() {
    if (!_ssTrajOn || !_ssTrajectory) { return; }
    const n = (_ssTrajectory.points || []).length;
    if (n < 2) { return; }
    _ssAnimStop();
    const scrub = document.getElementById('ss-scrub');
    _ssAnimPos = Math.max(0, Math.min(n - 1, parseFloat(scrub.value) || 0));
    _ssAnimFrame();
    _ssAnimRefreshCaption();
}


// One-line summary for the status span (plays · days · all-time entropy).
// ↗/↘/→ for a {slope} trend object (from the payload's `trends`).
function _ssTrendArrow(t) {
    if (!t || t.slope === 0) { return '→'; }
    return t.slope > 0 ? '↗' : '↘';
}


function _ssTrajSummary(data) {
    const at = data.all_time;
    if (!at || at.x == null) { return 'No mapped plays for this collection in range.'; }
    const n = (data.points || []).length;
    const unit = data.interval === 'month' ? 'month' : (data.interval === 'week' ? 'week' : 'day');
    const wt = data.weight_mode === 'count' ? ' · unweighted (no watch time)' : '';
    // Headline = plays the metrics actually use (those with a niche). Unmapped
    // plays (video not in the corpus) become a coverage caveat, so the count
    // always matches the H / centroid / trend denominators.
    const total = data.n_plays_total || 0;
    const mapped = total - (data.n_unmapped || 0);
    const pct = total > 0 ? Math.round(100 * mapped / total) : 0;
    const head = data.n_unmapped
        ? `${pct}% of ${total.toLocaleString()} plays are mapped`
        : `${total.toLocaleString()} plays`;
    let summary = head
        + (n ? ` · ${n} ${unit}${n === 1 ? '' : 's'}` : '')
        + ` · H=${at.niche_entropy} (Ĥ=${at.niche_entropy_norm})${wt}`;
    // Per-series trend arrows + path directness (tortuosity), when computed.
    const tr = data.trends || {};
    const bits = [];
    if (tr.niche_entropy) { bits.push(`entropy ${_ssTrendArrow(tr.niche_entropy)}`); }
    if (tr.novelty) { bits.push(`novelty ${_ssTrendArrow(tr.novelty)}`); }
    if (tr.mean_political_score) { bits.push(`political ${_ssTrendArrow(tr.mean_political_score)}`); }
    if (tr.mean_sensitivity_score) { bits.push(`sensitivity ${_ssTrendArrow(tr.mean_sensitivity_score)}`); }
    if (bits.length) { summary += ` · trend: ${bits.join(' ')}`; }
    if (data.tortuosity != null) { summary += ` · directness ${data.tortuosity}`; }
    return summary;
}


// Time gradient (t in [0,1], early→late): cool blue → warm orange.
function _ssLerp(a, b, t) { return Math.round(a + (b - a) * t); }


function _ssTimeColorRGB(t) {
    return [
        _ssLerp(_SS_TRAJ_T0[0], _SS_TRAJ_T1[0], t),
        _ssLerp(_SS_TRAJ_T0[1], _SS_TRAJ_T1[1], t),
        _ssLerp(_SS_TRAJ_T0[2], _SS_TRAJ_T1[2], t)
    ];
}


function _ssTopNichesStr(top) {
    return (top || []).map(t => `${t.name} ${Math.round(t.share * 100)}%`).join(' · ');
}


// Per-period change metrics for the hover/caption: shift (distributional
// velocity vs the previous period) and novelty (% of attention on niches never
// watched before). Both are null on the first period / when unavailable.
function _ssChangeBits(p) {
    const bits = [];
    if (p.js_from_prev != null) { bits.push(`shift ${p.js_from_prev}`); }
    if (p.novelty != null) { bits.push(`${Math.round(p.novelty * 100)}% new`); }
    return bits.join(' · ');
}


function _ssTrajHover(p) {
    const lo = p.low_volume ? ' (low volume)' : '';
    const ch = _ssChangeBits(p);
    return `<b>${p.date}</b><br>${p.n_mapped} plays · H=${p.niche_entropy}${lo}`
        + (ch ? `<br>${ch}` : '')
        + `<br>${_ssTopNichesStr(p.top_niches)}`;
}


// A rotated dispersion ellipse as a Plotly LAYOUT SHAPE (type:path). Shapes with
// layer:'above' draw above the WebGL scatter, so the cloud sits ON TOP of the
// dots (an SVG scatter trace would be hidden beneath the gl canvas). t in [0,1]
// drives the time gradient; the fill is translucent so overlapping clouds read.
function _ssEllipseShape(ell, t, alphaMul, emphasize) {
    alphaMul = (alphaMul == null) ? 1 : alphaMul;
    const steps = 40;
    const th = ell.theta * Math.PI / 180;
    const ct = Math.cos(th), st = Math.sin(th);
    const rx = ell.rx * _SS_ELLIPSE_SCALE, ry = ell.ry * _SS_ELLIPSE_SCALE;
    let d = '';
    for (let i = 0; i <= steps; i++) {
        const a = (i / steps) * 2 * Math.PI;
        const ex = rx * Math.cos(a), ey = ry * Math.sin(a);
        const x = ell.cx + ex * ct - ey * st;
        const y = ell.cy + ex * st + ey * ct;
        d += (i === 0 ? 'M' : 'L') + x.toFixed(3) + ',' + y.toFixed(3) + ' ';
    }
    d += 'Z';
    const c = _ssTimeColorRGB(t);
    const fillA = (emphasize ? 0.36 : 0.16) * alphaMul;
    const lineA = (emphasize ? 1.0 : 0.9) * alphaMul;
    return {
        type: 'path', path: d, layer: 'above',
        fillcolor: `rgba(${c[0]},${c[1]},${c[2]},${fillA.toFixed(3)})`,
        line: { color: `rgba(${c[0]},${c[1]},${c[2]},${lineA.toFixed(3)})`, width: emphasize ? 2.5 : 1.5 }
    };
}


// Fade factor for the period at index i given the continuous playback position
// t (a float). Static (t == null) → 1 (every period at full strength). During
// playback, future periods (i > t) are hidden and older ones decay
// geometrically by their *fractional* age (t - i), so the trail fades smoothly
// as the head glides between periods.
function _ssAnimFactor(i, t) {
    if (t == null) { return 1; }
    if (i > t) { return 0; }
    return Math.pow(_SS_ANIM_FADE, t - i);
}


// Linearly interpolate two dispersion ellipses (centre/axes lerp; theta along
// the shortest angular path, mod 180° since the ellipse is symmetric). Returns
// the non-null one if only one exists, or null if neither does.
function _ssLerpEllipse(a, b, frac) {
    if (!a && !b) { return null; }
    if (!a) { return b; }
    if (!b) { return a; }
    const lerp = (u, v) => u + (v - u) * frac;
    let dth = (((b.theta - a.theta + 90) % 180) + 180) % 180 - 90;
    return {
        cx: lerp(a.cx, b.cx), cy: lerp(a.cy, b.cy),
        rx: lerp(a.rx, b.rx), ry: lerp(a.ry, b.ry), theta: a.theta + dth * frac
    };
}


// Per-period dispersion ellipses as layout shapes (drawn above the WebGL dots).
// Static: one time-graded cloud per period. Playback: a fading trail of ghost
// clouds (faded by fractional age) plus one bright "head" ellipse interpolated
// between the current and next period — so it glides rather than jumps. The
// >_SS_TRAJ_MAX_ELLIPSES skip applies only to the static view (playback shows
// just a short trailing window, so it stays cheap even at daily granularity).
function _ssTrajectoryShapes() {
    if (!_ssTrajOn || !_ssTrajectory) { return []; }
    const T = _ssTrajectory;
    const all = T.points || [];
    if (all.length) {
        if (_ssAnimPos == null && all.length > _SS_TRAJ_MAX_ELLIPSES) { return []; }
        const n = all.length, t = _ssAnimPos;
        const shapes = [];
        all.forEach((p, i) => {
            if (!p.ellipse) { return; }
            const f = _ssAnimFactor(i, t);
            if (f < _SS_ANIM_MIN_ALPHA) { return; }
            shapes.push(_ssEllipseShape(p.ellipse, n === 1 ? 1 : i / (n - 1), f, false));
        });
        // Gliding head ellipse (interpolated between current & next period;
        // clamped so scrubbing to the final period lands the head on it).
        if (t != null && n >= 2) {
            const k = Math.min(Math.floor(t), n - 2), frac = t - k;
            const eh = _ssLerpEllipse(all[k].ellipse, all[k + 1].ellipse, frac);
            if (eh) { shapes.push(_ssEllipseShape(eh, n === 1 ? 1 : t / (n - 1), 1, true)); }
        }
        return shapes;
    }
    if (T.all_time && T.all_time.ellipse) { return [_ssEllipseShape(T.all_time.ellipse, 1, 1, false)]; }
    return [];
}


// Trajectory point/line traces appended after the base scatter (scattergl, same
// WebGL layer as the base, drawn after it → on top of the dots). Static: a dot
// per period + the full path. Playback: ghost dots fade by fractional age, and
// a bright head dot is interpolated between the current and next period; the
// path connects the ghosts up to that gliding head. Ellipses live in
// layout.shapes (see _ssTrajectoryShapes).
function _ssTrajectoryTraces() {
    if (!_ssTrajOn || !_ssTrajectory) { return []; }
    const traces = [];
    const T = _ssTrajectory;
    const at = T.all_time;
    const all = T.points || [];
    const t = _ssAnimPos, n = all.length;

    const mx = [], my = [], mcolor = [], msize = [], mtext = [];
    const lx = [], ly = [];
    all.forEach((p, i) => {
        if (p.x == null || p.y == null) { return; }
        const f = _ssAnimFactor(i, t);
        if (f < _SS_ANIM_MIN_ALPHA) { return; }
        const c = _ssTimeColorRGB(n === 1 ? 1 : i / (n - 1));
        const alpha = (t == null) ? 1 : f;
        mx.push(p.x); my.push(p.y);
        mcolor.push(`rgba(${c[0]},${c[1]},${c[2]},${alpha.toFixed(3)})`);
        msize.push(12);
        mtext.push(_ssTrajHover(p));
        lx.push(p.x); ly.push(p.y);
    });

    // Gliding head dot (interpolated position) during playback/scrub.
    if (t != null && n >= 2) {
        const k = Math.min(Math.floor(t), n - 2), frac = t - k, a = all[k], b = all[k + 1];
        if (a && b && a.x != null && b.x != null) {
            const hx = a.x + (b.x - a.x) * frac, hy = a.y + (b.y - a.y) * frac;
            const c = _ssTimeColorRGB(n === 1 ? 1 : t / (n - 1));
            mx.push(hx); my.push(hy);
            mcolor.push(`rgb(${c[0]},${c[1]},${c[2]})`);
            msize.push(20);
            mtext.push('');
            lx.push(hx); ly.push(hy);
        }
    }

    if (lx.length > 1) {
        traces.push({
            type: 'scattergl', mode: 'lines', x: lx, y: ly,
            line: { color: getCSSVar('--chart-text'), width: 1.5 },
            opacity: 0.5, hoverinfo: 'skip', showlegend: false
        });
    }
    if (mx.length) {
        traces.push({
            type: 'scattergl', mode: 'markers', x: mx, y: my,
            marker: { size: msize, color: mcolor, line: { width: 1.5, color: getCSSVar('--chart-bg') } },
            text: mtext, hoverinfo: 'text', showlegend: false
        });
    }

    // All-time centre of gravity as a labelled diamond (always on top).
    if (at && at.x != null && at.y != null) {
        traces.push({
            type: 'scattergl', mode: 'markers',
            x: [at.x], y: [at.y],
            marker: {
                size: 18, symbol: 'diamond', color: _SS_TRAJ_ACCENT,
                line: { width: 2, color: getCSSVar('--chart-bg') }
            },
            text: [`<b>All-time centre</b><br>${at.n_mapped} mapped plays · `
                + `H=${at.niche_entropy} (Ĥ=${at.niche_entropy_norm})`
                + `<br>${_ssTopNichesStr(at.top_niches)}`],
            hoverinfo: 'text', showlegend: false
        });
    }
    return traces;
}


// ---- Trajectory playback: step through periods, fading the trail ------------

function _ssAnimCaption(p) {
    const top = _ssTopNichesStr(p.top_niches);
    const ch = _ssChangeBits(p);
    return `▶ ${p.date} · ${p.n_mapped} plays · H=${p.niche_entropy}`
        + (ch ? ` · ${ch}` : '')
        + (p.low_volume ? ' (low)' : '') + (top ? ` · ${top}` : '');
}


function _ssAnimSetButton(playing) {
    const btn = document.getElementById('ss-anim-play');
    if (btn) { btn.textContent = playing ? '■ Stop' : '▶ Play'; }
}


// Update the caption to the period nearest the gliding head.
function _ssAnimRefreshCaption() {
    const pts = (_ssTrajectory && _ssTrajectory.points) || [];
    if (!pts.length || _ssAnimPos == null) { return; }
    const idx = Math.max(0, Math.min(pts.length - 1, Math.round(_ssAnimPos)));
    const el = document.getElementById('ss-traj-status');
    if (el && pts[idx]) { el.textContent = _ssAnimCaption(pts[idx]); }
}


// One animation frame: redraw the overlay (interpolated head + fading trail) at
// the current position. Reuses the already-rendered base scatter (div.data[0])
// so the 30k-point gl layer is not rebuilt — only the lightweight overlay
// changes, keeping each frame ~12 ms (smooth at ~60 fps).
function _ssAnimFrame() {
    const div = document.getElementById('semantic-space-plot');
    if (!div || !div.data || !div.data.length) { renderSemanticSpace(); return; }
    const base = div.data[0];
    const layout = Object.assign({}, div.layout, { shapes: _ssTrajectoryShapes() });
    Plotly.react(div, [base].concat(_ssTrajectoryTraces()), layout,
        { responsive: true, displayModeBar: true, scrollZoom: true });
}


// rAF tick: advance the continuous position by elapsed time, redraw, and pop
// back to the static all-periods view once the final period is reached.
function _ssAnimTick(now) {
    if (!_ssAnimPlaying) { return; }
    const N = ((_ssTrajectory && _ssTrajectory.points) || []).length;
    if (N < 2) { _ssAnimReset(); return; }
    if (_ssAnimLastTime == null) { _ssAnimLastTime = now; }
    _ssAnimPos += (now - _ssAnimLastTime) / _ssAnimStepMs;
    _ssAnimLastTime = now;
    if (_ssAnimPos >= N - 1) {
        _ssAnimPos = N - 1;
        _ssAnimFrame();
        _ssAnimRefreshCaption();
        _ssAnimReset();   // settle on the full static view
        return;
    }
    _ssAnimFrame();
    _ssAnimRefreshCaption();
    _ssAnimSyncScrub();
    _ssAnimRAF = requestAnimationFrame(_ssAnimTick);
}


// Keep the scrub slider thumb in step with the playback position.
function _ssAnimSyncScrub() {
    const scrub = document.getElementById('ss-scrub');
    if (scrub && !scrub.disabled && _ssAnimPos != null) { scrub.value = _ssAnimPos; }
}


function _ssAnimPlay() {
    if (!_ssTrajOn || !_ssTrajectory) { return; }
    const pts = _ssTrajectory.points || [];
    if (pts.length < 2) { return; }   // need at least two periods to morph between
    if (_ssAnimPos == null || _ssAnimPos >= pts.length - 1) { _ssAnimPos = 0; }   // (re)start
    _ssAnimPlaying = true;
    _ssAnimSetButton(true);
    // Per-period morph duration: slower with few periods, faster with many,
    // clamped to stay watchable.
    _ssAnimStepMs = Math.max(450, Math.min(1100, Math.round(9000 / pts.length)));
    _ssAnimLastTime = null;
    _ssAnimRAF = requestAnimationFrame(_ssAnimTick);
}


function _ssAnimStop() {
    if (_ssAnimRAF) { cancelAnimationFrame(_ssAnimRAF); _ssAnimRAF = null; }
    _ssAnimLastTime = null;
    _ssAnimPlaying = false;
    _ssAnimSetButton(false);
}


function _ssAnimToggle() {
    if (_ssAnimPlaying) { _ssAnimStop(); } else { _ssAnimPlay(); }
}


// Leave playback mode: stop, drop the frame index, redraw the static all-periods
// view, and restore the summary caption.
function _ssAnimReset() {
    _ssAnimStop();
    _ssAnimPos = null;
    const scrub = document.getElementById('ss-scrub');
    if (scrub) { scrub.value = 0; }
    renderSemanticSpace();
    const el = document.getElementById('ss-traj-status');
    if (el && _ssTrajectory) { el.textContent = _ssTrajSummary(_ssTrajectory); }
}


// ---------------------------------------------------------------------------
// Freshness banner — the map is global and rebuilt deliberately (not on every
// annotation), so it can lag the embedding store. We poll a light status
// endpoint while the tab is visible and surface one of four states without
// ever blocking the rendered map.
// ---------------------------------------------------------------------------

function _ssStartStatusPoll() {
    if (_ssStatusTimer) { return; }
    _ssPollStatus();
    _ssStatusTimer = setInterval(_ssPollStatus, 20000);
}


async function _ssPollStatus() {
    // Only poll while the tab is actually on screen (offsetParent is null when
    // the pane is display:none) and after the map has loaded.
    const pane = document.getElementById('semantic_space');
    if (!pane || pane.offsetParent === null || !_ssData) { return; }
    try {
        const res = await fetch('/api/semantic_space/status');
        if (!res.ok) { return; }
        _ssRenderBanner(await res.json());
    } catch (e) {
        // Transient (e.g. navigating away mid-fetch) — leave the banner as is.
    }
}


function _ssRenderBanner(s) {
    const banner = document.getElementById('ss-banner');
    const textEl = document.getElementById('ss-banner-text');
    const actionEl = document.getElementById('ss-banner-action');
    if (!banner || !textEl || !actionEl) { return; }

    const fresher = s.map_built_at != null && _ssLoadedMapBuiltAt != null
        && s.map_built_at > _ssLoadedMapBuiltAt;

    let text = '';
    let action = null;   // { label, fn }
    let warn = false;

    if (fresher && !s.map_rebuilding) {
        text = 'A new map has been calculated.';
        action = { label: 'Reload map', fn: _ssReloadMap };
    } else if (s.map_rebuilding) {
        text = '⟳ A new map is being calculated — showing the previous version…';
    } else if (s.model_mismatch) {
        const built = (s.map_meta && s.map_meta.embedding_model) || 'a different model';
        text = `This map was built with ${built}, but the active embedding backend is ` +
            `${s.active_embedding_model || 'different'} — run an embeddings refresh, then rebuild the map.`;
        warn = true;
        if (window.USER_IS_ADMIN) {
            action = { label: 'Rebuild map', fn: _ssRebuildMap };
        }
    } else if (s.map_stale) {
        const n = (s.behind || 0).toLocaleString();
        text = `This map is out of date — ${n} newer video${s.behind === 1 ? '' : 's'} embedded since it was built.`;
        warn = true;
        if (window.USER_IS_ADMIN) {
            action = { label: 'Rebuild map', fn: _ssRebuildMap };
        }
    } else if (s.embeddings_updating) {
        text = 'New videos are being added to the semantic data…';
    }

    if (!text) {
        banner.style.display = 'none';
        return;
    }
    textEl.textContent = text;
    textEl.style.color = warn ? 'var(--color-warning)' : 'var(--color-text-secondary)';
    if (action) {
        actionEl.style.display = '';
        actionEl.disabled = false;
        actionEl.textContent = action.label;
        actionEl.onclick = action.fn;
    } else {
        actionEl.style.display = 'none';
        actionEl.onclick = null;
    }
    banner.style.display = 'flex';
}


// Re-fetch the map after a rebuild, preserving the user's colour/focus
// selections where the rebuilt map still offers them (niche IDs can change).
async function _ssReloadMap() {
    const prevMode = (document.getElementById('ss-color-mode') || {}).value;
    const prevFocus = (document.getElementById('ss-niche-focus') || {}).value;
    const banner = document.getElementById('ss-banner');
    if (banner) { banner.style.display = 'none'; }

    await loadSemanticSpace();

    const cm = document.getElementById('ss-color-mode');
    if (cm && prevMode && Array.from(cm.options).some(o => o.value === prevMode)) {
        cm.value = prevMode;
    }
    const nf = document.getElementById('ss-niche-focus');
    if (nf && prevFocus && Array.from(nf.options).some(o => o.value === prevFocus)) {
        nf.value = prevFocus;
    }
    renderSemanticSpace();
}


// Admin-only: kick off a video_map_refresh. Embeddings are kept current by the
// consolidation cascade, so this rebuilds the 2D map/niches from the store.
// The backend defaults to auto_refresh, so a rebuild also re-recodes every
// study cache to propagate the new niche assignments into the analysis tabs.
async function _ssRebuildMap() {
    const actionEl = document.getElementById('ss-banner-action');
    if (actionEl) { actionEl.disabled = true; actionEl.textContent = 'Starting…'; }
    try {
        const meta = document.querySelector('meta[name="csrf-token"]');
        const res = await fetch('/api/start/video_map_refresh', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': meta ? meta.content : ''
            },
            body: '{}'
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || (data.status && data.status !== 'success')) {
            console.error('Rebuild map failed:', data.message || res.status);
        }
    } catch (e) {
        console.error('Rebuild map error:', e);
    } finally {
        if (actionEl) { actionEl.disabled = false; }
        _ssPollStatus();   // flip the banner to "being calculated…" promptly
    }
}
