// Semantic Space tab — global video embedding map.
// Loads recoded/video_map.parquet (via /api/semantic_space/map) once and
// renders a WebGL scatter of the 2D-projected videos. The map is corpus-wide
// (not study-scoped), so it loads independently of the active study.

let _ssData = null;
let _ssLoaded = false;
let _ssHandlersWired = false;
let _ssStatusTimer = null;
let _ssLoadedMapBuiltAt = null;   // mtime of the map currently rendered

// Categorical data palette (tab20-style). Niche colour = palette[niche % 20];
// category colours are assigned by index. Continuous "popularity" uses Viridis.
const _SS_PALETTE = [
    '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
    '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
    '#aec7e8', '#ffbb78', '#98df8a', '#ff9896', '#c5b0d5',
    '#c49c94', '#f7b6d2', '#c7c7c7', '#dbdb8d', '#9edae5'
];
const _SS_DIM = 'rgba(130,130,130,0.10)';
const _SS_MAX_LABELS = 30;


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

    // Per-point colour by the selected mode. Niche + categorical overlays use
    // the discrete palette; numeric overlays use a continuous Viridis scale.
    const overlay = mode === 'niche' ? null : _ssOverlay(mode);
    let colorArr;
    let markerExtra = {};
    let catColorMap = null;
    if (overlay && overlay.kind === 'numeric') {
        colorArr = (P[overlay.field] || []).map(v => (v == null ? 0 : v));
        markerExtra = {
            colorscale: 'Viridis', showscale: true,
            colorbar: {
                title: overlay.label, thickness: 12, len: 0.5,
                tickfont: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text') },
                titlefont: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text') }
            }
        };
    } else if (overlay && overlay.kind === 'categorical') {
        catColorMap = _ssCatColorMap(overlay.field);
        colorArr = (P[overlay.field] || []).map(v => catColorMap[v] || '#888');
    } else {
        colorArr = P.niche.map(nn => _SS_PALETTE[nn % _SS_PALETTE.length]);
    }

    // Focus: isolate one niche — highlight it, grey out the rest (uniform
    // across modes; the colourbar is suppressed while focusing).
    let sizeArr = 4;
    if (focusNiche !== null) {
        const fc = _SS_PALETTE[focusNiche % _SS_PALETTE.length];
        colorArr = new Array(n);
        sizeArr = new Array(n);
        for (let i = 0; i < n; i++) {
            const inFocus = P.niche[i] === focusNiche;
            colorArr[i] = inFocus ? fc : _SS_DIM;
            sizeArr[i] = inFocus ? 7 : 3;
        }
        markerExtra = {};
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
        marker: Object.assign({ size: sizeArr, color: colorArr, opacity: 0.75,
            line: { width: 0 } }, markerExtra)
    };

    // Centroid niche labels — only in niche mode or when a niche is focused.
    const annotations = [];
    if (showLabels && (mode === 'niche' || focusNiche !== null)) {
        let labelSet = _ssData._centroids.slice().sort((a, b) => b.size - a.size);
        labelSet = focusNiche !== null
            ? labelSet.filter(c => c.niche === focusNiche)
            : labelSet.slice(0, _SS_MAX_LABELS);
        labelSet.forEach(c => annotations.push({
            x: c.x, y: c.y, text: c.name, showarrow: false,
            font: { family: getCSSVar('--font-sans'), size: 10, color: getCSSVar('--white') },
            bgcolor: getCSSVar('--chart-badge-bg'), borderpad: 2, opacity: 0.92
        }));
    }

    const layout = {
        hovermode: 'closest', showlegend: false,
        margin: { l: 10, r: 10, t: 10, b: 10 },
        xaxis: { visible: false, fixedrange: false },
        yaxis: { visible: false, scaleanchor: 'x', scaleratio: 1 },
        paper_bgcolor: getCSSVar('--chart-bg'),
        plot_bgcolor: getCSSVar('--chart-bg'),
        font: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text') },
        annotations: annotations
    };

    Plotly.react(div, [trace], layout, { responsive: true, displayModeBar: true, scrollZoom: true });
    _ssRenderLegend(mode, overlay, catColorMap);

    if (!div._ssClickWired) {
        div._ssClickWired = true;
        div.on('plotly_click', function (ev) {
            const pt = ev.points && ev.points[0];
            if (pt && pt.customdata) {
                window.open(`https://www.tiktok.com/@/video/${pt.customdata}/`, '_blank', 'noopener');
            }
        });
    }
}


function _ssRenderLegend(mode, overlay, catColorMap) {
    const info = document.getElementById('ss-info');
    if (!info) { return; }
    if (overlay && overlay.kind === 'categorical' && catColorMap) {
        info.innerHTML = _ssDistinct(overlay.field).map(c =>
            `<span style="display:inline-flex;align-items:center;gap:4px;margin-right:10px;white-space:nowrap;">`
            + `<span style="width:9px;height:9px;border-radius:2px;background:${catColorMap[c]};display:inline-block;"></span>`
            + `${c}</span>`).join('');
    } else if (overlay && overlay.kind === 'numeric') {
        info.innerText = `${overlay.label}: brighter = higher · click a point to open on TikTok`;
    } else {
        info.innerText = 'Coloured by niche · click a point to open on TikTok';
    }
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
