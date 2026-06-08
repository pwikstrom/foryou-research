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
let _ssCollectionsLoaded = false; // collection selector populated once per load

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

// Trajectory overlay colours (chart-data literals, like _SS_PALETTE above —
// these encode an overlay, not UI chrome). The all-time centre of gravity uses
// a warm accent that reads on both themes; per-period centroids and their
// dispersion-ellipse clouds use a time gradient (early → late): cool blue →
// warm orange (see _ssTimeColorRGB).
const _SS_TRAJ_ACCENT = '#e08a2b';
const _SS_TRAJ_T0 = [44, 111, 214];
const _SS_TRAJ_T1 = [224, 138, 43];
const _SS_TRAJ_MAX_ELLIPSES = 52;   // above this many periods, skip clouds (path only)


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
        _ssLoadCollections();
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

    // Trajectory overlay controls. Selecting a collection (or changing the date
    // window / interval) refetches; the toggle just flips overlay visibility on
    // the already-loaded payload, so it never hits the network.
    const coll = document.getElementById('ss-collection');
    if (coll) { coll.addEventListener('change', _ssLoadTrajectory); }
    ['ss-traj-start', 'ss-traj-end', 'ss-traj-interval'].forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            el.addEventListener('change', () => {
                if ((document.getElementById('ss-collection') || {}).value) { _ssLoadTrajectory(); }
            });
        }
    });
    const tog = document.getElementById('ss-show-trajectory');
    if (tog) {
        tog.addEventListener('change', () => { _ssTrajOn = tog.checked; renderSemanticSpace(); });
    }
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
        colorArr = (P[overlay.field] || []).map(v => (v == null ? 0 : v));
        // Colour the points by the numeric scale, but DON'T draw Plotly's
        // in-figure colourbar — it resizes the plot box and (via the 1:1 aspect
        // lock) shifts the scatter when the scale changes. The scale is shown as
        // an HTML gradient legend below the plot instead (see _ssRenderLegend),
        // so the plot box never changes between colour modes.
        markerExtra = { colorscale: _SS_NUMERIC_COLORSCALE, showscale: false };
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
        const extra = ovField ? `<br>${overlay.label}: ${P[ovField][i]}` : '';
        hover[i] = `<b>${P.niche_name[i]}</b>${extra}<br>${P.story[i]}`;
    }

    const trace = {
        type: 'scattergl', mode: 'markers',
        x: P.x, y: P.y,
        customdata: P.item_id,
        text: hover, hoverinfo: 'text',
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
                window.open(`https://www.tiktok.com/@/video/${pt.customdata}/`, '_blank', 'noopener');
            }
        });
        div.on('plotly_relayout', _ssOnZoomRelayout);
    }
}


// Min/max of a numeric overlay, treating nulls as 0 to match the point colours
// (renderSemanticSpace maps null → 0), so the gradient endpoints line up.
function _ssNumericRange(field) {
    const arr = _ssData.points[field] || [];
    let lo = Infinity, hi = -Infinity;
    for (let i = 0; i < arr.length; i++) {
        const v = arr[i] == null ? 0 : arr[i];
        if (v < lo) { lo = v; }
        if (v > hi) { hi = v; }
    }
    return lo === Infinity ? [0, 0] : [lo, hi];
}


function _ssFmtNum(v) {
    return Number.isInteger(v) ? String(v) : String(+v.toFixed(1));
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
    const openHint = '<span style="margin-left:auto;white-space:nowrap;">click a point to open on TikTok</span>';
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
        const hint = '<span style="margin-left:auto;white-space:nowrap;">click a swatch to show/hide · click a point to open</span>';
        legend.innerHTML = swatches + hint;
    } else if (overlay && overlay.kind === 'numeric') {
        _ssLegendCats = null;
        const [lo, hi] = _ssNumericRange(overlay.field);
        const grad = `linear-gradient(to right, ${_SS_NUMERIC_SCALE.join(', ')})`;
        legend.innerHTML =
            `<span class="font-medium" style="white-space:nowrap;">${overlay.label}</span>`
            + `<span>${_ssFmtNum(lo)}</span>`
            + `<span style="width:140px;height:10px;border-radius:2px;background:${grad};display:inline-block;"></span>`
            + `<span>${_ssFmtNum(hi)}</span>` + openHint;
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

// Populate the collection selector once per map load (independent of the map
// payload — the list is access-scoped server-side).
async function _ssLoadCollections() {
    const sel = document.getElementById('ss-collection');
    if (!sel || _ssCollectionsLoaded) { return; }
    try {
        const res = await fetch('/api/semantic_space/collections');
        const data = await res.json();
        const ids = (data && data.collections) || [];
        sel.innerHTML = '<option value="">— select collection —</option>'
            + ids.map(id => `<option value="${id}">${id}</option>`).join('');
        _ssCollectionsLoaded = true;
    } catch (e) {
        console.error('Failed to load collections', e);
    }
}


// Fetch the selected collection's trajectory for the current date window /
// interval, then re-render. Deselecting clears the overlay.
async function _ssLoadTrajectory() {
    const sel = document.getElementById('ss-collection');
    const cid = sel ? sel.value : '';
    const statusEl = document.getElementById('ss-traj-status');
    if (!cid) {
        _ssTrajectory = null;
        if (statusEl) { statusEl.textContent = ''; }
        renderSemanticSpace();
        return;
    }
    const start = (document.getElementById('ss-traj-start') || {}).value || '';
    const end = (document.getElementById('ss-traj-end') || {}).value || '';
    const interval = (document.getElementById('ss-traj-interval') || {}).value || 'day';
    if (statusEl) { statusEl.textContent = 'Loading trajectory…'; }
    try {
        const qs = new URLSearchParams({ collection_id: cid, interval });
        if (start) { qs.set('start', start); }
        if (end) { qs.set('end', end); }
        const res = await fetch('/api/semantic_space/trajectory?' + qs.toString());
        const data = await res.json();
        if (!res.ok || data.error) {
            if (statusEl) { statusEl.textContent = data.error || `Error ${res.status}`; }
            return;
        }
        _ssTrajectory = data;
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


// One-line summary for the status span (plays · days · all-time entropy).
function _ssTrajSummary(data) {
    const at = data.all_time;
    if (!at || at.x == null) { return 'No mapped plays for this collection in range.'; }
    const n = (data.points || []).length;
    const unit = data.interval === 'month' ? 'month' : (data.interval === 'week' ? 'week' : 'day');
    const wt = data.weight_mode === 'count' ? ' · unweighted (no watch time)' : '';
    const cov = data.n_unmapped
        ? ` · ${data.n_unmapped.toLocaleString()} unmapped`
        : '';
    return `${data.n_plays_total.toLocaleString()} plays`
        + (n ? ` · ${n} ${unit}${n === 1 ? '' : 's'}` : '')
        + ` · entropy H=${at.niche_entropy} (Ĥ=${at.niche_entropy_norm})${cov}${wt}`;
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


function _ssTimeColor(t) { const c = _ssTimeColorRGB(t); return `rgb(${c[0]},${c[1]},${c[2]})`; }


function _ssTopNichesStr(top) {
    return (top || []).map(t => `${t.name} ${Math.round(t.share * 100)}%`).join(' · ');
}


function _ssTrajHover(p) {
    const lo = p.low_volume ? ' (low volume)' : '';
    return `<b>${p.date}</b><br>${p.n_plays} plays · H=${p.niche_entropy}${lo}`
        + `<br>${_ssTopNichesStr(p.top_niches)}`;
}


// A rotated dispersion ellipse as a Plotly LAYOUT SHAPE (type:path). Shapes with
// layer:'above' draw above the WebGL scatter, so the cloud sits ON TOP of the
// dots (an SVG scatter trace would be hidden beneath the gl canvas). t in [0,1]
// drives the time gradient; the fill is translucent so overlapping clouds read.
function _ssEllipseShape(ell, t) {
    const steps = 40;
    const th = ell.theta * Math.PI / 180;
    const ct = Math.cos(th), st = Math.sin(th);
    let d = '';
    for (let i = 0; i <= steps; i++) {
        const a = (i / steps) * 2 * Math.PI;
        const ex = ell.rx * Math.cos(a), ey = ell.ry * Math.sin(a);
        const x = ell.cx + ex * ct - ey * st;
        const y = ell.cy + ex * st + ey * ct;
        d += (i === 0 ? 'M' : 'L') + x.toFixed(3) + ',' + y.toFixed(3) + ' ';
    }
    d += 'Z';
    const c = _ssTimeColorRGB(t);
    return {
        type: 'path', path: d, layer: 'above',
        fillcolor: `rgba(${c[0]},${c[1]},${c[2]},0.13)`,
        line: { color: `rgba(${c[0]},${c[1]},${c[2]},0.9)`, width: 1.5 }
    };
}


// Per-period dispersion ellipses as layout shapes (above the dots). For day/
// week/month each period is a time-graded cloud; for "all-time only" it's the
// single all-time halo. Skipped above _SS_TRAJ_MAX_ELLIPSES periods (e.g. daily
// over a long span) to avoid clutter — the path+markers still convey the drift.
function _ssTrajectoryShapes() {
    if (!_ssTrajOn || !_ssTrajectory) { return []; }
    const T = _ssTrajectory;
    const pts = (T.points || []).filter(p => p.ellipse);
    if (pts.length) {
        if (pts.length > _SS_TRAJ_MAX_ELLIPSES) { return []; }
        const n = pts.length;
        return pts.map((p, i) => _ssEllipseShape(p.ellipse, n === 1 ? 1 : i / (n - 1)));
    }
    if (T.all_time && T.all_time.ellipse) { return [_ssEllipseShape(T.all_time.ellipse, 1)]; }
    return [];
}


// Trajectory point/line traces appended after the base scatter. These are
// scattergl (same WebGL layer as the base, drawn after it → on top of the
// dots), so the path and markers are never hidden. The ellipse clouds live in
// layout.shapes (see _ssTrajectoryShapes). Empty unless the overlay is loaded.
function _ssTrajectoryTraces() {
    if (!_ssTrajOn || !_ssTrajectory) { return []; }
    const traces = [];
    const T = _ssTrajectory;
    const at = T.all_time;
    const pts = (T.points || []).filter(p => p.x != null && p.y != null);

    if (pts.length) {
        const n = pts.length;
        // Connecting path (drawn first so the markers sit on top of it).
        traces.push({
            type: 'scattergl', mode: 'lines',
            x: pts.map(p => p.x), y: pts.map(p => p.y),
            line: { color: getCSSVar('--chart-text'), width: 1.5 },
            opacity: 0.7, hoverinfo: 'skip', showlegend: false
        });
        // Period centroids, time-graded, with hover.
        traces.push({
            type: 'scattergl', mode: 'markers',
            x: pts.map(p => p.x), y: pts.map(p => p.y),
            marker: {
                size: 12,
                color: pts.map((p, i) => _ssTimeColor(n === 1 ? 1 : i / (n - 1))),
                line: { width: 1.5, color: getCSSVar('--chart-bg') }
            },
            text: pts.map(_ssTrajHover), hoverinfo: 'text', showlegend: false
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
            text: [`<b>All-time centre</b><br>${at.n_plays} plays · `
                + `H=${at.niche_entropy} (Ĥ=${at.niche_entropy_norm})`
                + `<br>${_ssTopNichesStr(at.top_niches)}`],
            hoverinfo: 'text', showlegend: false
        });
    }
    return traces;
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
