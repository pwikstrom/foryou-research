// Semantic Space tab — global video embedding map.
// Loads recoded/video_map.parquet (via /api/semantic_space/map) once and
// renders a WebGL scatter of the 2D-projected videos. The map is corpus-wide
// (not study-scoped), so it loads independently of the active study.

let _ssData = null;
let _ssLoaded = false;
let _ssHandlersWired = false;

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
        _ssComputeCentroids();
        _ssPopulateNicheFocus();
        if (status) {
            status.innerText =
                `${data.total_mapped.toLocaleString()} videos shown · `
                + `${data.n_niches} niches · ${data.total_videos.toLocaleString()} embedded`;
        }
        _ssWireControls();
        renderSemanticSpace();
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


function _ssCategoryColorMap() {
    const map = {};
    _ssData.categories.forEach((c, i) => { map[c] = _SS_PALETTE[i % _SS_PALETTE.length]; });
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

    // Per-point colour by mode.
    const catColors = _ssCategoryColorMap();
    let colorArr;
    let markerExtra = {};
    if (mode === 'popularity') {
        colorArr = P.log_plays;
        markerExtra = {
            colorscale: 'Viridis', showscale: true,
            colorbar: {
                title: 'log₁₀ plays', thickness: 12, len: 0.5,
                tickfont: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text') },
                titlefont: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text') }
            }
        };
    } else if (mode === 'category') {
        colorArr = P.category.map(c => catColors[c] || '#888');
    } else {
        colorArr = P.niche.map(nn => _SS_PALETTE[nn % _SS_PALETTE.length]);
    }

    // Focus dimming: grey out everything outside the focused niche.
    let sizeArr = 4;
    if (focusNiche !== null) {
        sizeArr = new Array(n);
        colorArr = (mode === 'popularity' ? P.log_plays.slice() : colorArr.slice());
        for (let i = 0; i < n; i++) {
            const inFocus = P.niche[i] === focusNiche;
            sizeArr[i] = inFocus ? 7 : 3;
            if (!inFocus) { colorArr[i] = _SS_DIM; }
        }
        if (mode === 'popularity') { markerExtra.showscale = false; }
    }

    const hover = new Array(n);
    for (let i = 0; i < n; i++) {
        hover[i] = `<b>${P.niche_name[i]}</b><br>${P.category[i]}<br>${P.story[i]}`;
    }

    const trace = {
        type: 'scattergl', mode: 'markers',
        x: P.x, y: P.y,
        customdata: P.item_id,
        text: hover, hoverinfo: 'text',
        marker: Object.assign({ size: sizeArr, color: colorArr, opacity: 0.75,
            line: { width: 0 } }, markerExtra)
    };

    // Centroid labels (niche or focus modes).
    const annotations = [];
    if (showLabels && mode !== 'category') {
        let labelSet = _ssData._centroids.slice().sort((a, b) => b.size - a.size);
        labelSet = focusNiche !== null
            ? labelSet.filter(c => c.niche === focusNiche)
            : labelSet.slice(0, _SS_MAX_LABELS);
        labelSet.forEach(c => annotations.push({
            x: c.x, y: c.y, text: c.name, showarrow: false,
            font: { family: getCSSVar('--font-sans'), size: 10, color: getCSSVar('--chart-text') },
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
    _ssRenderLegend(mode, catColors);

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


function _ssRenderLegend(mode, catColors) {
    const info = document.getElementById('ss-info');
    if (!info) { return; }
    if (mode === 'category') {
        info.innerHTML = _ssData.categories.map(c =>
            `<span style="display:inline-flex;align-items:center;gap:4px;margin-right:10px;white-space:nowrap;">`
            + `<span style="width:9px;height:9px;border-radius:2px;background:${catColors[c]};display:inline-block;"></span>`
            + `${c}</span>`).join('');
    } else if (mode === 'popularity') {
        info.innerText = 'Brighter = more plays · click a point to open on TikTok';
    } else {
        info.innerText = 'Coloured by niche · click a point to open on TikTok';
    }
}
