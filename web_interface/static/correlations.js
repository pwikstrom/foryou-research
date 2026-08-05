// There is deliberately no sample/filter state: the STUDY is the sample
// (exclusions and event windows belong in study definitions, where they are
// versioned and documented), so every view describes the whole study.
let pcaData = {
    activeStudy: null,
    metadata: null,
    plotData: null,
    currentView: 'scatter'      // 'scatter' or 'heatmap'
};

let _lastScatterArgs = null;
let _lastHeatmapArgs = null;
let _lastGroupStats = null;

// Renderer registry for the server-declared stat views (metadata.views).
// A new view = one manifest entry server-side + one renderer here.
const VIEW_RENDERERS = {
    scatter: () => updatePcaPlot(),
    heatmap: () => loadCorrelationHeatmap(),
    group_stats: () => loadGroupStats()
};

const DEFAULT_VIEWS = [
    { key: 'scatter', label: 'Scatter' },
    { key: 'heatmap', label: 'Heatmap' }
];


function refreshCurrentView() {
    const render = VIEW_RENDERERS[pcaData.currentView];
    if (render) render();
}


// --- Per-user variable preferences (shared "viz" surface) ---

function getEffectiveVizBases() {
    const md = pcaData.metadata;
    if (!md || !window.VariablePrefs || !md.all_variables_order) return null;
    const eff = VariablePrefs.effective('viz', md.all_variables_order, md.viz_priority || []);
    return new Set(eff);
}


// Filter derived numeric columns by their base variable's effective viz
// membership. Columns with no known base variable are always shown, and an
// empty result falls back to the full list so the tab never goes blank.
function filterColsByPrefs(cols) {
    const md = pcaData.metadata;
    if (!md) return cols;
    const bases = md.numeric_col_bases || {};
    const eff = getEffectiveVizBases();
    if (!eff || eff.size === 0) return cols;
    const out = cols.filter(c => {
        const b = bases[c];
        return !b || eff.has(b);
    });
    return out.length ? out : cols;
}


function getVisibleNumericCols() {
    const md = pcaData.metadata;
    if (!md) return [];
    return filterColsByPrefs(md.numeric_cols || []);
}


// (The per-tab viz gear moved to My Stuff -> Preferences; the
// 'fyp:variable-prefs-changed' listener below keeps this tab in sync.)


// --- Banners + plain-language captions (the HASS readability layer) ---

// The unit-of-analysis explainer lives behind the control bar's (i) icon: it
// is essential on first read and noise thereafter, so it stays one hover away
// instead of occupying a full-width banner above every plot.
function renderUnitBanner(unit) {
    const wrap = document.getElementById('corr-unit-info');
    const tip = document.getElementById('corr-unit-tooltip');
    if (!wrap || !tip) return;
    if (!unit || !unit.grouping_display || !unit.grouping_display.length) {
        wrap.style.display = 'none';
        return;
    }
    const grouping = unit.grouping_display.join(' × ');
    const videos = (unit.videos_total != null)
        ? ` covering ${Number(unit.videos_total).toLocaleString()} videos` : '';
    tip.textContent = `Each point/row is one ${grouping} group — the average of that group's annotated videos ` +
        `(${(unit.n_groups || 0).toLocaleString()} groups${videos}, each with at least ${unit.min_group_size} videos). ` +
        `All statistics on this tab describe the whole study's groups, not individual videos.`;
    wrap.style.display = '';
}


async function loadCorrelationsStatus() {
    const el = document.getElementById('corr-stale-banner');
    if (!el || !pcaData.activeStudy) return;
    el.style.display = 'none';
    try {
        const res = await fetch(`/api/correlations/status?study=${encodeURIComponent(pcaData.activeStudy)}`);
        const s = await res.json();
        if (s && s.stale) {
            el.textContent = "The PCA scores for this study are older than its latest data, so the statistics " +
                "below may not include recently added videos. An admin can rebuild them via " +
                "Data Pipeline → Refresh Caches (PCA / Correlations).";
            el.style.display = 'block';
        }
    } catch (e) {
        console.error(e);
    }
}


function formatP(p) {
    if (p === null || p === undefined || !isFinite(p)) return 'p n/a';
    if (p < 0.001) return 'p < .001';
    return 'p = ' + p.toFixed(3).replace(/^0/, '');
}


function describeStrength(r) {
    const a = Math.abs(r);
    if (a < 0.1) return 'negligible';
    if (a < 0.3) return 'weak';
    if (a < 0.5) return 'moderate';
    return 'strong';
}


function setCaption(html) {
    const el = document.getElementById('corr-caption');
    if (!el) return;
    if (!html) {
        el.style.display = 'none';
        el.innerHTML = '';
        return;
    }
    el.innerHTML = html;
    el.style.display = 'block';
}


function escapeHtml(s) {
    const div = document.createElement('div');
    div.innerText = String(s);
    return div.innerHTML;
}


function renderViewToggle(views) {
    const container = document.getElementById('pca-view-toggle');
    if (!container) return;
    container.innerHTML = '';
    (views && views.length ? views : DEFAULT_VIEWS).forEach(v => {
        if (!VIEW_RENDERERS[v.key]) return; // manifest entry without a renderer yet
        const btn = document.createElement('button');
        btn.className = 'toggle-option';
        btn.id = `pca-view-${v.key}`;
        btn.textContent = v.label;
        btn.onclick = () => setPcaView(v.key);
        container.appendChild(btn);
    });
}


// Initialize
document.addEventListener('DOMContentLoaded', function () {
    if (document.getElementById('correlations')) {
        if (window.studyState && window.studyState.ready) {
            window.studyState.ready.then(() => {
                if (window.studyState.current) {
                    applyCorrelationsActiveStudy(window.studyState.current);
                }
            });
        }

        document.addEventListener('study:changed', (e) => {
            const next = e.detail && e.detail.study;
            applyCorrelationsActiveStudy(next);
        });
    }
});


function applyCorrelationsActiveStudy(studyName) {
    pcaData.activeStudy = studyName || null;

    const plotDiv = document.getElementById('pca-plot');
    if (plotDiv && typeof Plotly !== 'undefined') {
        Plotly.purge(plotDiv);
    }

    if (studyName) {
        loadPcaMetadata();
    }
}


async function loadPcaMetadata() {
    document.getElementById('pca-status').innerText = "Loading…";

    try {
        const res = await fetch(`/api/correlations/metadata`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ study: pcaData.activeStudy })
        });
        const data = await res.json();

        if (data.error) {
            document.getElementById('pca-status').innerText = `Error: ${data.error}`;
            return;
        }

        pcaData.metadata = data;
        document.getElementById('pca-status').innerText = "";

        renderViewToggle(data.views);
        renderPcaControls(data);
        renderUnitBanner(data.unit);
        loadCorrelationsStatus();

        // Default heatmap method from the [correlations] config
        const methodSel = document.getElementById('pca-method-select');
        if (methodSel && data.default_method) methodSel.value = data.default_method;

        // Initial render based on current view (also applies active button styles)
        if (!VIEW_RENDERERS[pcaData.currentView]) pcaData.currentView = 'scatter';
        setPcaView(pcaData.currentView);

    } catch (e) {
        console.error(e);
        document.getElementById('pca-status').innerText = "Error";
    }
}


function renderPcaControls(data) {
    const xSelect = document.getElementById('pca-x-select');
    const ySelect = document.getElementById('pca-y-select');
    const colorSelect = document.getElementById('pca-color-select');

    // Preserve previously selected axes if the user had chosen them and they exist in the new study
    const prevX = xSelect.value;
    const prevY = ySelect.value;

    xSelect.innerHTML = '';
    ySelect.innerHTML = '';
    colorSelect.innerHTML = '';

    // Build display labels with explained variance for components
    const inter = data.interpretations || {};

    // X/Y Axis: Numeric Columns with variance info, filtered by the user's
    // effective "viz" variable preferences (components inherit their base
    // variable's membership). Columns are grouped into <optgroup>s by their
    // base variable so a variable's components/entropy read as one family;
    // standalone measures (no siblings) stay top-level.
    const schemaMap = data.schema_map || {};
    const visibleCols = filterColsByPrefs(data.numeric_cols || []);
    const bases = data.numeric_col_bases || {};

    const optionLabel = (col) => {
        const variance = inter[col]?.explained_variance_pct;
        const displayName = (schemaMap[col] && schemaMap[col].display_name) ? schemaMap[col].display_name : col;
        return variance ? `${displayName} (${variance}%)` : displayName;
    };

    const families = new Map();
    visibleCols.forEach(col => {
        const base = bases[col] || col;
        if (!families.has(base)) families.set(base, []);
        families.get(base).push(col);
    });

    const appendOption = (parentX, parentY, col) => {
        const label = optionLabel(col);
        const optX = document.createElement('option');
        optX.value = col;
        optX.text = label;
        parentX.appendChild(optX);
        const optY = document.createElement('option');
        optY.value = col;
        optY.text = label;
        parentY.appendChild(optY);
    };

    families.forEach((cols, base) => {
        if (cols.length === 1) {
            appendOption(xSelect, ySelect, cols[0]);
            return;
        }
        const groupName = (schemaMap[base] && schemaMap[base].display_name) ? schemaMap[base].display_name : base;
        const groupX = document.createElement('optgroup');
        groupX.label = groupName;
        const groupY = document.createElement('optgroup');
        groupY.label = groupName;
        cols.forEach(col => appendOption(groupX, groupY, col));
        xSelect.appendChild(groupX);
        ySelect.appendChild(groupY);
    });

    // Check if the previous selections are still valid options in this new study
    const hasPrevX = visibleCols.includes(prevX);
    const hasPrevY = visibleCols.includes(prevY);

    if (hasPrevX && hasPrevY) {
        // Carry over the existing user selections
        xSelect.value = prevX;
        ySelect.value = prevY;
    } else if (visibleCols.length > 0) {
        // Deterministic default: the two components with the highest explained variance
        const byVariance = [...visibleCols].sort((a, b) => {
            const va = parseFloat(inter[a]?.explained_variance_pct) || 0;
            const vb = parseFloat(inter[b]?.explained_variance_pct) || 0;
            return vb - va;
        });
        xSelect.value = byVariance[0];
        if (byVariance.length > 1) {
            ySelect.value = byVariance[1];
        }
    }

    // Colour: Factors — use display_name from schema_map if available
    data.factor_cols.forEach(col => {
        const opt = document.createElement('option');
        opt.value = col;
        opt.text = (schemaMap[col] && schemaMap[col].display_name) ? schemaMap[col].display_name : col;
        colorSelect.appendChild(opt);
    });

    if (data.factor_cols.length > 0) colorSelect.value = data.factor_cols[0];
}


// --- View Toggle ---

function setPcaView(view) {
    if (!VIEW_RENDERERS[view]) return;
    pcaData.currentView = view;

    // Highlight the active view button (buttons come from the manifest)
    const container = document.getElementById('pca-view-toggle');
    if (container) {
        [...container.children].forEach(btn => {
            btn.classList.toggle('active', btn.id === `pca-view-${view}`);
        });
    }

    // The axis/colour dropdowns only apply to the scatter view; the method /
    // significance controls only to the heatmap
    const scatterControls = document.getElementById('pca-scatter-controls');
    if (scatterControls) {
        scatterControls.style.display = (view === 'scatter') ? 'flex' : 'none';
    }
    const heatmapControls = document.getElementById('pca-heatmap-controls');
    if (heatmapControls) {
        heatmapControls.style.display = (view === 'heatmap') ? 'flex' : 'none';
    }
    setCaption('');
    applyCenteringAvailability();

    refreshCurrentView();
}


// Group differences is precomputed over the whole study by the pca_refresh
// worker, so centering cannot apply to it. Disable the control with an
// explanation rather than leaving a live-looking checkbox that does nothing.
let _centerTooltipDefault = null;

function applyCenteringAvailability() {
    const cb = document.getElementById('pca-center-toggle');
    const wrap = document.getElementById('pca-center-wrap');
    if (!cb || !wrap) return;
    if (_centerTooltipDefault === null) {
        _centerTooltipDefault = wrap.dataset.tooltip || '';
    }
    const precomputed = pcaData.currentView === 'group_stats';
    cb.disabled = precomputed;
    wrap.classList.toggle('corr-control-disabled', precomputed);
    wrap.dataset.tooltip = precomputed
        ? 'These tables are precomputed over the whole study when the caches are rebuilt, so within-collection centering does not apply to them.'
        : _centerTooltipDefault;
}


// --- Scatter Plot ---

async function updatePcaPlot() {
    if (!pcaData.activeStudy) return;

    const xCol = document.getElementById('pca-x-select').value;
    const yCol = document.getElementById('pca-y-select').value;
    const colorCol = document.getElementById('pca-color-select').value;

    if (!xCol || !yCol) return;

    try {
        const res = await fetch('/api/correlations/data', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                study: pcaData.activeStudy,
                x_col: xCol,
                y_col: yCol,
                color_col: colorCol,
                center: !!document.getElementById('pca-center-toggle')?.checked
            })
        });
        const data = await res.json();

        if (data.error) {
            console.error(data.error);
            const statusEl = document.getElementById('pca-status');
            if (statusEl) statusEl.innerText = `Error: ${data.error}`;
            return;
        }

        // Update point count
        const countEl = document.getElementById('pca-point-count');
        if (countEl) {
            const shown = data.data.length;
            const total = data.total_count || shown;
            countEl.innerText = shown < total
                ? `${shown.toLocaleString()} / ${total.toLocaleString()} obs`
                : `${total.toLocaleString()} obs`;
        }

        renderPlotlyChart(data, xCol, yCol, colorCol);

    } catch (e) {
        console.error(e);
    }
}


function renderPlotlyChart(payload, xLabel, yLabel, colorLabel) {
    _lastScatterArgs = { payload, xLabel, yLabel, colorLabel };
    const dataPoints = payload.data || [];
    const serverStats = payload.stats || null;
    const groupEllipses = payload.group_ellipses || [];
    const isCentered = !!payload.centered;
    const colors = [
        '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
        '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf'
    ];

    const displayIds = pcaData.metadata?.display_ids || {};

    // Group by color value
    const groups = {};
    dataPoints.forEach(d => {
        const rawColorVal = d.color_val || 'Undefined';
        // Map collection_id to display names for the legend
        const gName = (colorLabel === 'collection_id' && displayIds[rawColorVal]) ? displayIds[rawColorVal] : rawColorVal;

        if (!groups[gName]) groups[gName] = { x: [], y: [], text: [], factors: [], name: gName };
        groups[gName].x.push(d.x);
        groups[gName].y.push(d.y);
        groups[gName].text.push(d.text);
        groups[gName].factors.push(d.factors || {});
    });

    const groupsKeys = Object.keys(groups);
    let traces = [];

    // Confidence ellipses: true covariance-based ellipses at 95% coverage,
    // computed server-side on the FULL filtered set (not the display sample).
    const showEllipses = document.getElementById('pca-show-ellipses')?.checked;

    if (showEllipses && groupEllipses.length) {
        // chi-square quantile, 2 df, 95% coverage — matches ELLIPSE_COVERAGE
        const CHI2_2DF_95 = 5.991464547;

        groupEllipses.forEach(e => {
            // Match the server's raw group value to the display-keyed traces
            const gName = (colorLabel === 'collection_id' && displayIds[e.group])
                ? displayIds[e.group] : e.group;
            const gi = groupsKeys.indexOf(gName);
            const color = colors[(gi >= 0 ? gi : 0) % colors.length];

            // Closed-form eigendecomposition of the 2×2 covariance matrix
            const a = e.cov[0][0], b = e.cov[0][1], c = e.cov[1][1];
            const tr = a + c, det = a * c - b * b;
            const disc = Math.sqrt(Math.max(0, (tr * tr) / 4 - det));
            const l1 = tr / 2 + disc;
            const l2 = Math.max(0, tr / 2 - disc);
            const theta = (Math.abs(b) < 1e-12)
                ? (a >= c ? 0 : Math.PI / 2)
                : Math.atan2(l1 - a, b);
            const r1 = Math.sqrt(Math.max(0, l1) * CHI2_2DF_95);
            const r2 = Math.sqrt(l2 * CHI2_2DF_95);

            const numPoints = 60;
            const ellipseX = [], ellipseY = [];
            for (let j = 0; j <= numPoints; j++) {
                const t = (j / numPoints) * 2 * Math.PI;
                const ex = r1 * Math.cos(t), ey = r2 * Math.sin(t);
                ellipseX.push(e.mean_x + ex * Math.cos(theta) - ey * Math.sin(theta));
                ellipseY.push(e.mean_y + ex * Math.sin(theta) + ey * Math.cos(theta));
            }

            traces.push({
                x: ellipseX,
                y: ellipseY,
                mode: 'lines',
                name: `${gName} (95% ellipse, n=${e.n})`,
                showlegend: false,
                line: { width: 1, color: color },
                fill: 'toself',
                fillcolor: color,
                opacity: 0.15,
                hoverinfo: 'name'
            });
        });
    }

    // Scatter traces
    groupsKeys.forEach((g, i) => {
        const color = colors[i % colors.length];
        traces.push({
            x: groups[g].x,
            y: groups[g].y,
            mode: 'markers',
            type: 'scatter',
            name: g,
            text: groups[g].text,
            customdata: groups[g].factors,
            hoverinfo: 'text',
            marker: {
                size: (window.userSettings && window.userSettings.big_dots) ? 16 : 8,
                opacity: (window.userSettings && window.userSettings.big_dots) ? 0.45 : 0.8,
                color: color
            }
        });
    });

    // Build axis labels with variance info
    const inter = pcaData.metadata?.interpretations || {};
    const schemaMap = pcaData.metadata?.schema_map || {};

    const xDisplayName = (schemaMap[xLabel] && schemaMap[xLabel].display_name) ? schemaMap[xLabel].display_name : xLabel;
    const yDisplayName = (schemaMap[yLabel] && schemaMap[yLabel].display_name) ? schemaMap[yLabel].display_name : yLabel;

    const xVariance = inter[xLabel]?.explained_variance_pct;
    const yVariance = inter[yLabel]?.explained_variance_pct;
    const xTitle = xVariance ? `${xDisplayName} (${xVariance}% var.)` : xDisplayName;
    const yTitle = yVariance ? `${yDisplayName} (${yVariance}% var.)` : yDisplayName;

    // Axis Configuration — autoranged so no points are clipped
    const axisTitleFont = { family: getCSSVar('--font-sans'), size: 16, color: getCSSVar('--chart-text') };
    const axisConfig = {
        gridcolor: getCSSVar('--chart-grid'),
        zerolinecolor: getCSSVar('--chart-zeroline')
    };

    const layout = {
        xaxis: { ...axisConfig, title: { text: xTitle, font: axisTitleFont } },
        yaxis: { ...axisConfig, title: { text: yTitle, font: axisTitleFont } },
        hovermode: 'closest',
        paper_bgcolor: getCSSVar('--chart-bg'),
        plot_bgcolor: getCSSVar('--chart-bg'),
        font: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text') },
        annotations: [],
        margin: { t: 60, r: 40, b: 70, l: 70 }
    };

    // Interpretation annotations on axes
    if (inter) {
        const formatLabel = (txt) => {
            if (!txt) return '';
            const maxLen = 30;
            const words = txt.split(' ');
            let lines = [];
            let currentLine = words[0];
            for (let i = 1; i < words.length; i++) {
                if (currentLine.length + 1 + words[i].length <= maxLen) {
                    currentLine += ' ' + words[i];
                } else {
                    lines.push(currentLine);
                    currentLine = words[i];
                }
            }
            lines.push(currentLine);
            return lines.join('<br>');
        };

        const addLabel = (axis, direction, text) => {
            if (!text) return;
            const isX = axis === 'x';

            let ann = {
                xref: 'paper',
                yref: 'paper',
                text: formatLabel(text),
                showarrow: false,
                font: { family: getCSSVar('--font-sans'), size: 10, color: getCSSVar('--color-text-tertiary') },
                bgcolor: getCSSVar('--color-bg-surface'),
                bordercolor: getCSSVar('--chart-grid'),
                borderwidth: 1,
                opacity: 0.8
            };

            if (isX) {
                if (direction === 'pos') {
                    ann.x = 1; ann.y = 0.5;
                    ann.xanchor = 'right'; ann.yanchor = 'middle';
                    ann.xshift = -10;
                } else {
                    ann.x = 0; ann.y = 0.5;
                    ann.xanchor = 'left'; ann.yanchor = 'middle';
                    ann.xshift = 10;
                }
            } else {
                if (direction === 'pos') {
                    ann.x = 0.5; ann.y = 1;
                    ann.xanchor = 'center'; ann.yanchor = 'top';
                    ann.yshift = -10;
                } else {
                    ann.x = 0.5; ann.y = 0;
                    ann.xanchor = 'center'; ann.yanchor = 'bottom';
                    ann.yshift = 10;
                }
            }
            layout.annotations.push(ann);
        };

        if (inter[xLabel]) {
            addLabel('x', 'pos', inter[xLabel].top_positive);
            addLabel('x', 'neg', inter[xLabel].top_negative);
        }
        if (inter[yLabel]) {
            addLabel('y', 'pos', inter[yLabel].top_positive);
            addLabel('y', 'neg', inter[yLabel].top_negative);
        }
    }

    // Regression line + full readout (server-computed on all filtered groups)
    const showStats = document.getElementById('pca-show-stats')?.checked;
    if (showStats && serverStats) {
        // Span the line over the observed x range only
        let xMin = Infinity, xMax = -Infinity;
        dataPoints.forEach(d => {
            if (d.x < xMin) xMin = d.x;
            if (d.x > xMax) xMax = d.x;
        });
        if (isFinite(xMin) && isFinite(xMax)) {
            const lineX = [xMin, xMax];
            const lineY = lineX.map(x => serverStats.slope * x + serverStats.intercept);

            traces.push({
                x: lineX,
                y: lineY,
                mode: 'lines',
                type: 'scatter',
                name: 'Regression',
                line: { color: getCSSVar('--chart-regression-line'), width: 2, dash: 'dash' },
                hoverinfo: 'none'
            });
        }

        const s = serverStats;
        const readout = [
            `R² = ${s.r2.toFixed(2)}`,
            `slope = ${s.slope.toFixed(2)} [${s.ci_low.toFixed(2)}, ${s.ci_high.toFixed(2)}]`,
            formatP(s.p),
            `n = ${s.n.toLocaleString()}`
        ].join('   ');
        layout.annotations.push({
            xref: 'paper', yref: 'paper',
            x: 0.02, y: 0.98,
            xanchor: 'left', yanchor: 'top',
            text: readout,
            showarrow: false,
            font: { family: getCSSVar('--font-sans'), size: 12, color: getCSSVar('--white') },
            bgcolor: getCSSVar('--chart-badge-bg'),
            bordercolor: getCSSVar('--color-text-faint'), borderwidth: 1,
            align: 'left'
        });
    }

    renderScatterCaption(serverStats, xTitle, yTitle, isCentered, showStats);

    Plotly.newPlot('pca-plot', traces, layout, { responsive: true, displayModeBar: true });

    // Drill-down: click a dot to view matching videos
    document.getElementById('pca-plot').on('plotly_click', function(eventData) {
        const point = eventData.points[0];
        if (!point || !point.customdata) return;
        const factors = point.customdata;
        if (!factors || Object.keys(factors).length === 0) return;
        drillDownFromCorrelations(factors);
    });
}


function drillDownFromCorrelations(factors) {
    const schemaMap = pcaData.metadata?.schema_map || {};

    // Build filter set from factor values
    const filters = {};
    Object.entries(factors).forEach(([col, val]) => {
        filters[col] = { type: 'category', value: [val] };
    });

    // Build a readable label for the confirmation popup
    const labels = Object.entries(factors).map(([col, val]) => {
        const name = schemaMap[col]?.display_name || col;
        return `${name} = "${val}"`;
    });
    const variableName = 'Correlations';
    const valueLabel = labels.join(', ');

    showDrillDownConfirm(variableName, valueLabel, () => {
        window._pendingDrillDown = {
            filters: filters,
            searchQuery: "",
            timestamp: Date.now()
        };
        const tabBtn = document.querySelector('.tab-button[onclick*="video_analysis"]');
        if (tabBtn) tabBtn.click();
    });
}


// --- Correlation Heatmap ---

async function loadCorrelationHeatmap() {
    if (!pcaData.activeStudy) return;

    const countEl = document.getElementById('pca-point-count');
    if (countEl) countEl.innerText = 'Loading…';

    try {
        const res = await fetch('/api/correlations/correlation_matrix', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                study: pcaData.activeStudy,
                method: document.getElementById('pca-method-select')?.value || undefined,
                center: !!document.getElementById('pca-center-toggle')?.checked
            })
        });
        const data = await res.json();

        if (data.error) {
            console.error(data.error);
            if (countEl) countEl.innerText = `Error: ${data.error}`;
            return;
        }

        if (countEl) {
            countEl.innerText = `${data.count.toLocaleString()} obs`;
        }

        renderCorrelationHeatmap(data);

    } catch (e) {
        console.error(e);
        if (countEl) countEl.innerText = 'Error';
    }
}


function rerenderHeatmapFromCache() {
    if (pcaData.currentView === 'heatmap' && _lastHeatmapArgs) {
        renderCorrelationHeatmap(_lastHeatmapArgs);
    }
}


function renderCorrelationHeatmap(payload) {
    _lastHeatmapArgs = payload;
    const schemaMap = pcaData.metadata?.schema_map || {};
    const method = payload.method;
    const rSym = (method === 'spearman') ? 'ρ' : 'r';
    const rLabel = (method === 'spearman') ? 'Spearman ρ' : 'Pearson r';
    const maskNonSig = !!document.getElementById('pca-mask-nonsig')?.checked;

    // Apply the user's effective viz preferences (same base-variable rule as
    // the axis dropdowns) to the matrix rows/columns.
    const allCols = payload.columns;
    const visible = new Set(filterColsByPrefs(allCols));
    let keptIdx = allCols.map((c, i) => i).filter(i => visible.has(allCols[i]));
    if (keptIdx.length < 2) keptIdx = allCols.map((c, i) => i);

    const columns = keptIdx.map(i => allCols[i]);
    const families = (payload.families || allCols).filter((f, i) => keptIdx.includes(i));
    const pick = (mat) => (mat ? keptIdx.map(i => keptIdx.map(j => mat[i][j])) : null);
    const rM = pick(payload.matrix);
    const pM = pick(payload.p_matrix);
    const qM = pick(payload.q_matrix);
    const nM = pick(payload.n_matrix);

    // Map columns to display names
    const displayColumns = columns.map(col => {
        return (schemaMap[col] && schemaMap[col].display_name) ? schemaMap[col].display_name : col;
    });

    const shownM = rM;

    // Optional significance masking: blank cells with q >= .05 (diagonal kept)
    let maskedCount = 0;
    const z = shownM.map((row, i) => row.map((val, j) => {
        if (i === j) return val;
        if (maskNonSig) {
            const q = qM?.[i]?.[j];
            if (q === null || q === undefined || q >= 0.05) {
                if (val !== null && val !== undefined) maskedCount++;
                return null;
            }
        }
        return val;
    }));

    // Hover: r with pairwise n, p and BH q (null = undefined correlation)
    const hoverText = rM.map((row, i) =>
        row.map((val, j) => {
            const head = `${displayColumns[i]} × ${displayColumns[j]}`;
            if (val === null || val === undefined) return `${head}<br>${rSym} undefined`;
            const parts = [`${rSym} = ${val.toFixed(3)}`];
            if (nM?.[i]?.[j] !== undefined) parts.push(`n = ${nM[i][j]}`);
            if (i !== j && pM?.[i]?.[j] !== null && pM?.[i]?.[j] !== undefined) {
                parts.push(formatP(pM[i][j]));
                const q = qM?.[i]?.[j];
                if (q !== null && q !== undefined) {
                    parts.push('q = ' + q.toFixed(3).replace(/^0/, ''));
                }
            }
            if (families[i] !== families[j]) parts.push('(different PCA bases)');
            return `${head}<br>${parts.join(', ')}`;
        })
    );

    const trace = {
        z: z,
        x: displayColumns,
        y: displayColumns,
        type: 'heatmap',
        colorscale: [
            [0, '#2166ac'],
            [0.25, '#67a9cf'],
            [0.5, getCSSVar('--chart-heatmap-mid')],
            [0.75, '#ef8a62'],
            [1, '#b2182b']
        ],
        zmin: -1,
        zmax: 1,
        text: hoverText,
        hoverinfo: 'text',
        colorbar: {
            title: { text: rLabel, font: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text') } },
            tickfont: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text') },
            len: 0.8
        }
    };

    // Separator lines between variable families (columns arrive grouped)
    const shapes = [];
    for (let i = 1; i < families.length; i++) {
        if (families[i] !== families[i - 1]) {
            const pos = i - 0.5;
            const line = { color: getCSSVar('--chart-zeroline'), width: 1 };
            shapes.push({ type: 'line', xref: 'x', yref: 'paper', x0: pos, x1: pos, y0: 0, y1: 1, line });
            shapes.push({ type: 'line', yref: 'y', xref: 'paper', y0: pos, y1: pos, x0: 0, x1: 1, line });
        }
    }

    const layout = {
        title: {
            text: 'Correlation Matrix',
            font: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text') }
        },
        paper_bgcolor: getCSSVar('--chart-bg'),
        plot_bgcolor: getCSSVar('--chart-bg'),
        font: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text'), size: 10 },
        xaxis: {
            tickangle: -45,
            tickfont: { family: getCSSVar('--font-sans'), size: 9 },
            gridcolor: getCSSVar('--chart-grid')
        },
        yaxis: {
            autorange: 'reversed',
            tickfont: { family: getCSSVar('--font-sans'), size: 9 },
            gridcolor: getCSSVar('--chart-grid')
        },
        shapes: shapes,
        margin: { t: 50, r: 80, b: 120, l: 120 }
    };

    renderHeatmapCaption(payload, maskNonSig, maskedCount);

    Plotly.newPlot('pca-plot', [trace], layout, { responsive: true, displayModeBar: true });
}


function renderHeatmapCaption(payload, maskNonSig, maskedCount) {
    const methodName = payload.method === 'spearman' ? 'Spearman (rank-based)' : 'Pearson (linear)';
    const parts = [];

    parts.push(`${methodName} correlations across ${payload.count.toLocaleString()} groups; ` +
        `each cell also reports its pairwise n, p, and a Benjamini–Hochberg adjusted q ` +
        `(controls the share of false positives among all pairs tested).`);
    if (maskNonSig) {
        parts.push(`${maskedCount.toLocaleString()} non-significant cells (q ≥ .05, of the observed r) are blanked.`);
    }
    if (payload.centered) {
        parts.push('Values are centered within each collection (within-collection associations).');
    }
    parts.push('<span class="text-xs">Thin separators group components of the same variable; ' +
        'cells that cross a separator compare components from different per-variable PCA ' +
        'spaces — read them as associations between summary dimensions, not shared axes.</span>');
    setCaption(parts.join(' '));
}


function exportCorrelationCsv() {
    const payload = _lastHeatmapArgs;
    if (!payload || !payload.columns) return;
    const schemaMap = pcaData.metadata?.schema_map || {};
    const dname = (c) => (schemaMap[c] && schemaMap[c].display_name) ? schemaMap[c].display_name : c;
    const esc = (v) => {
        const s = String(v === null || v === undefined ? '' : v);
        return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    };

    const rows = [["variable_a", "variable_b", "family_a", "family_b",
                   "method", "r", "n", "p", "q", "centered"]];
    const cols = payload.columns;
    const fams = payload.families || cols;
    for (let i = 0; i < cols.length; i++) {
        for (let j = i + 1; j < cols.length; j++) {
            const r = payload.matrix?.[i]?.[j];
            rows.push([
                dname(cols[i]), dname(cols[j]), fams[i], fams[j],
                payload.method,
                r,
                payload.n_matrix?.[i]?.[j],
                payload.p_matrix?.[i]?.[j],
                payload.q_matrix?.[i]?.[j],
                payload.centered ? 'yes' : 'no',
            ]);
        }
    }
    const csv = rows.map(r => r.map(esc).join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    const centeredTag = payload.centered ? '_centered' : '';
    a.download = `${pcaData.activeStudy || 'study'}_correlations_${payload.method}${centeredTag}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
}


// --- Group differences view (worker-precomputed ANOVA/KW + PERMANOVA) ---

async function loadGroupStats() {
    if (!pcaData.activeStudy) return;
    const plotDiv = document.getElementById('pca-plot');
    const countEl = document.getElementById('pca-point-count');
    if (countEl) countEl.innerText = 'Loading…';

    try {
        const res = await fetch('/api/correlations/group_stats', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ study: pcaData.activeStudy })
        });
        const data = await res.json();

        if (typeof Plotly !== 'undefined' && plotDiv) Plotly.purge(plotDiv);

        if (data.error) {
            plotDiv.innerHTML = `<div class="corr-table-wrap"><p>${escapeHtml(data.error)}.` +
                (data.hint ? ` ${escapeHtml(data.hint)}` : '') + `</p></div>`;
            if (countEl) countEl.innerText = '';
            setCaption('');
            return;
        }

        if (countEl) countEl.innerText = `${(data.n_groups || 0).toLocaleString()} obs`;
        renderGroupStats(data);

    } catch (e) {
        console.error(e);
        if (countEl) countEl.innerText = 'Error';
    }
}


// Column specs for the two Group-differences tables: the label, the row key
// it reads, how to format it, and the plain-language explainer shown on hover.
// `num` marks columns sorted numerically; the rest sort as text.
const GROUP_STATS_TABLES = [
    {
        key: 'anova',
        title: 'Which factors move single components? (one-way ANOVA)',
        empty: 'No testable factor × component pairs in this study.',
        defaultSort: { col: 'eta2', dir: -1 },
        columns: [
            { key: 'factor', label: 'Factor', kind: 'name',
              tip: 'The grouping the test compares — e.g. do the collections, or the weeks, differ from each other?' },
            { key: 'component', label: 'Component', kind: 'name',
              tip: 'The variable being compared across those groups. "(C0)" is a variable\'s leading PCA dimension; "(entropy)" is how diverse the feed was on that variable.' },
            { key: 'eta2', label: 'η²', num: true, digits: 3,
              tip: 'Eta-squared: the share of this component\'s variation explained by the factor. The effect size — read this before the p-value. Conventions: .01 small, .06 medium, .14 large.' },
            { key: 'magnitude', label: 'Effect',
              tip: 'Plain-language label for η² using the conventional cutoffs (negligible / small / medium / large).' },
            { key: 'omega2', label: 'ω²', num: true, digits: 3,
              tip: 'Omega-squared: a less optimistic η². Trust it over η² when the factor has many levels or few groups per level.' },
            { key: 'F', label: 'F', num: true, digits: 1,
              tip: 'The ANOVA test statistic — between-group variance divided by within-group variance. Larger means the groups separate more cleanly.' },
            { key: 'p', label: 'p', num: true, p: true,
              tip: 'Uncorrected significance of this single test. With hundreds of tests in this table, use q instead.' },
            { key: 'q', label: 'q', num: true, p: true,
              tip: 'Benjamini–Hochberg adjusted p, across every test in this table. q < .05 means under 5% of the rows you call real are expected to be noise. This is the column to judge significance on.' },
            { key: 'kw_q', label: 'KW q', num: true, p: true,
              tip: 'The same comparison run as a rank-based Kruskal–Wallis test, BH-adjusted. Trust it over q when groups are small or skewed; if the two disagree, be sceptical of the result.' },
            { key: 'n', label: 'n', num: true,
              tip: 'Number of collection-day groups behind the test.' },
            { key: 'levels', label: 'Levels', num: true,
              tip: 'How many distinct values the factor takes (e.g. 2 collections, 26 weeks).' },
        ],
    },
    {
        key: 'permanova',
        title: 'Do whole variable profiles differ by factor? (PERMANOVA)',
        empty: 'No testable family × factor pairs in this study.',
        defaultSort: { col: 'q', dir: 1 },
        columns: [
            { key: 'family', label: 'Variable family', kind: 'name',
              tip: 'The variable whose components are tested together, as one profile.' },
            { key: 'factor', label: 'Factor', kind: 'name',
              tip: 'The grouping being compared.' },
            { key: 'pseudo_F', label: 'pseudo-F', num: true, digits: 2,
              tip: 'PERMANOVA test statistic: how much better the factor separates whole profiles than chance. Its significance comes from permutation, not a table.' },
            { key: 'p', label: 'p', num: true, p: true,
              tip: 'Permutation p-value: the share of random shuffles that separated the groups at least as well as the real labels.' },
            { key: 'q', label: 'q', num: true, p: true,
              tip: 'Benjamini–Hochberg adjusted p across this table — judge significance here rather than on p.' },
            { key: 'n', label: 'n', num: true,
              tip: 'Number of collection-day groups behind the test.' },
            { key: 'levels', label: 'Levels', num: true,
              tip: 'How many distinct values the factor takes.' },
            { key: 'n_components', label: 'Components', num: true,
              tip: 'How many of the variable\'s components were compared together as a profile.' },
            { key: 'permutations', label: 'Permutations', num: true,
              tip: 'How many random shuffles the p-value was computed from. More permutations = a finer-grained p.' },
        ],
    },
];

// Per-table sort state, keyed by table key. Persists across re-renders so a
// re-render (theme change, prefs change) keeps the reader's chosen order.
const _groupStatsSort = {};


function _groupStatsSortedRows(rows, spec, displayName) {
    const sort = _groupStatsSort[spec.key] || spec.defaultSort;
    if (!sort || !sort.col) return rows;
    const col = spec.columns.find(c => c.key === sort.col);
    // Name columns sort on what the reader sees, not the underlying key.
    const valueOf = (row) => (col && col.kind === 'name')
        ? displayName(row[sort.col]) : row[sort.col];
    const out = [...rows];
    out.sort((a, b) => {
        const va = valueOf(a);
        const vb = valueOf(b);
        // Missing values sort last whichever direction is active.
        const aMissing = va === null || va === undefined || va === '';
        const bMissing = vb === null || vb === undefined || vb === '';
        if (aMissing && bMissing) return 0;
        if (aMissing) return 1;
        if (bMissing) return -1;
        if (col && col.num) return (Number(va) - Number(vb)) * sort.dir;
        return String(va).localeCompare(String(vb), undefined, { sensitivity: 'base' }) * sort.dir;
    });
    return out;
}


// Clicking the sorted column flips its direction; clicking a new one sorts it
// biggest-first for numbers (the useful default for effect sizes and test
// statistics) and A-Z for names. There is no "unsorted" state — the table
// always has a defined, visible order.
function sortGroupStatsBy(tableKey, colKey) {
    const spec = GROUP_STATS_TABLES.find(s => s.key === tableKey);
    if (!spec) return;
    const cur = _groupStatsSort[tableKey] || spec.defaultSort;
    if (cur && cur.col === colKey) {
        _groupStatsSort[tableKey] = { col: colKey, dir: cur.dir === 1 ? -1 : 1 };
    } else {
        const col = spec.columns.find(c => c.key === colKey);
        _groupStatsSort[tableKey] = { col: colKey, dir: (col && col.num) ? -1 : 1 };
    }
    if (_lastGroupStats) renderGroupStats(_lastGroupStats);
}


function renderGroupStats(data) {
    _lastGroupStats = data;
    const plotDiv = document.getElementById('pca-plot');
    const schemaMap = pcaData.metadata?.schema_map || {};
    const dname = (c) => (schemaMap[c] && schemaMap[c].display_name) ? schemaMap[c].display_name : c;
    const fmtP = (p) => (p === null || p === undefined) ? '—' : formatP(p).replace(/^p /, '');
    const fmtN = (v, d) => (v === null || v === undefined) ? '—' : Number(v).toFixed(d);

    const renderTable = (spec) => {
        const rows = data[spec.key] || [];
        let out = `<h3 class="text-h3">${escapeHtml(spec.title)}</h3>`;
        if (!rows.length) return out + `<p class="text-xs">${escapeHtml(spec.empty)}</p>`;

        const sort = _groupStatsSort[spec.key] || spec.defaultSort;
        const mid = spec.columns.length / 2;
        out += `<table class="collection-table corr-stats-table"><thead><tr>`;
        spec.columns.forEach((col, i) => {
            const active = sort && sort.col === col.key;
            const arrow = active ? (sort.dir === 1 ? ' ▲' : ' ▼') : '';
            // Columns in the right half anchor their tooltip to the right edge
            // so it extends leftward and stays on screen.
            const anchor = i >= mid ? ' tooltip-right-anchored' : '';
            out += `<th class="corr-sortable meta-tooltip tooltip-below${anchor}${active ? ' corr-sorted' : ''}" ` +
                `data-tooltip="${escapeHtml(col.tip)}" ` +
                `onclick="sortGroupStatsBy('${spec.key}', '${col.key}')">` +
                `${escapeHtml(col.label)}${arrow}</th>`;
        });
        out += `</tr></thead><tbody>`;

        _groupStatsSortedRows(rows, spec, dname).forEach(row => {
            const sig = row.q !== null && row.q !== undefined && row.q < 0.05;
            out += `<tr${sig ? ' class="font-bold"' : ''}>`;
            spec.columns.forEach(col => {
                const raw = row[col.key];
                let cell;
                if (col.kind === 'name') cell = escapeHtml(dname(raw));
                else if (col.p) cell = fmtP(raw);
                else if (col.digits !== undefined) cell = fmtN(raw, col.digits);
                else cell = (raw === null || raw === undefined) ? '—' : escapeHtml(String(raw));
                out += `<td>${cell}</td>`;
            });
            out += `</tr>`;
        });
        return out + `</tbody></table>`;
    };

    plotDiv.innerHTML = `<div class="corr-table-wrap">` +
        GROUP_STATS_TABLES.map(renderTable).join('') + `</div>`;

    const anova = [...(data.anova || [])].sort((a, b) => (b.eta2 || 0) - (a.eta2 || 0));
    const perma = [...(data.permanova || [])].sort((a, b) => (a.q ?? 1) - (b.q ?? 1));
    renderGroupStatsCaption(anova, perma, schemaMap);
}


// Cohen bands, matching fyp/analysis/stats.py ETA2_THRESHOLDS. Applied to
// omega-squared here so the label always describes the number quoted beside it.
function varianceMagnitude(value) {
    if (value === null || value === undefined || !isFinite(value)) return null;
    if (value < 0.01) return 'negligible';
    if (value < 0.06) return 'small';
    if (value < 0.14) return 'medium';
    return 'large';
}


function renderGroupStatsCaption(anova, perma, schemaMap) {
    const dname = (c) => (schemaMap[c] && schemaMap[c].display_name) ? schemaMap[c].display_name : c;
    const parts = [];
    const sigAnova = anova.filter(r => r.q !== null && r.q !== undefined && r.q < 0.05);
    if (anova.length) {
        parts.push(`${sigAnova.length} of ${anova.length} factor × component tests are ` +
            `significant after correction (q < .05).`);
        // Headline on omega-squared, not eta-squared: eta-squared is inflated by
        // the number of factor levels, so a 26-week factor would otherwise
        // outrank a genuinely larger 2-level effect.
        const hasOmega = (r) => r && r.omega2 !== null && r.omega2 !== undefined && isFinite(r.omega2);
        const pool = (sigAnova.length ? sigAnova : anova).filter(hasOmega);
        const top = pool.length
            ? pool.reduce((best, r) => (r.omega2 > best.omega2 ? r : best))
            : null;
        if (top && top.omega2 > 0) {
            parts.push(`Largest effect: <b>${escapeHtml(dname(top.factor))}</b> explains ` +
                `${(top.omega2 * 100).toFixed(0)}% of the variation in ` +
                `<b>${escapeHtml(dname(top.component))}</b> ` +
                `(a ${varianceMagnitude(top.omega2)} effect, ${formatP(top.q)} after correction).`);
        }
    }
    const sigPerma = perma.filter(r => r.q !== null && r.q !== undefined && r.q < 0.05);
    if (sigPerma.length) {
        const fams = [...new Set(sigPerma.map(r => `${dname(r.family)} (by ${dname(r.factor)})`))].slice(0, 4);
        parts.push(`Whole-profile differences (PERMANOVA): ${fams.map(escapeHtml).join('; ')}` +
            `${sigPerma.length > 4 ? ' and more' : ''}.`);
    } else if (perma.length) {
        parts.push('No variable family shows a significant whole-profile difference after correction.');
    }
    parts.push('<span class="text-xs">Effects are quoted as ω², the share of a component\'s ' +
        'variance explained by the factor (.01 small, .06 medium, .14 large). ω² rather than η² ' +
        'because η² grows with the number of factor levels, which would flatter a 26-week factor ' +
        'over a 2-collection one. KW q = rank-based Kruskal–Wallis check — trust it over the ANOVA ' +
        'q when groups are small or skewed. PERMANOVA compares each variable\'s whole component ' +
        'profile, never mixing different variables\' PCA bases.</span>');
    setCaption(parts.join(' '));
}


// --- Scatter caption (plain-language summary of the server statistics) ---

function renderScatterCaption(stats, xTitle, yTitle, isCentered, showStats) {
    if (!showStats) {
        setCaption('');
        return;
    }
    if (!stats) {
        setCaption('Too few groups to compute a regression readout for this selection.');
        return;
    }
    const direction = stats.r >= 0 ? 'positive' : 'negative';
    const strength = describeStrength(stats.r);
    const rTxt = stats.r.toFixed(2).replace(/^(-?)0/, '$1');
    const parts = [];
    parts.push(`A ${strength} ${direction} association between ` +
        `<b>${escapeHtml(xTitle)}</b> and <b>${escapeHtml(yTitle)}</b> ` +
        `(r = ${rTxt}, ${formatP(stats.p)}, n = ${stats.n.toLocaleString()} groups).`);
    if (isCentered) {
        parts.push('Values are centered within each collection, so this describes ' +
            'variation <i>within</i> donors’ feeds, not differences between donors.');
    }
    if (stats.n < 30) {
        parts.push('<span class="corr-caption-warning">Small sample — fewer than 30 groups; ' +
            'interpret with caution.</span>');
    }
    parts.push('<span class="text-xs">The line assumes a straight-line relationship — ' +
        'check the scatter for curvature or outliers before relying on it.</span>');
    setCaption(parts.join(' '));
}

window.addEventListener('theme-changed', () => {
    // Re-render charts (Plotly needs resolved color values, not CSS var())
    if (pcaData.currentView === 'scatter' && _lastScatterArgs) {
        renderPlotlyChart(_lastScatterArgs.payload, _lastScatterArgs.xLabel,
                          _lastScatterArgs.yLabel, _lastScatterArgs.colorLabel);
    } else if (pcaData.currentView === 'heatmap' && _lastHeatmapArgs) {
        renderCorrelationHeatmap(_lastHeatmapArgs);
    }
});

// Re-apply when the shared "viz" prefs change (possibly edited on another
// tab; detail.surface is null when every surface was reset at once)
window.addEventListener('fyp:variable-prefs-changed', (e) => {
    const surface = e.detail ? e.detail.surface : undefined;
    if (surface !== 'viz' && surface !== null) return;
    if (!pcaData.metadata) return;
    renderPcaControls(pcaData.metadata);
    refreshCurrentView();
});
