let pcaData = {
    activeStudy: null,
    metadata: null,
    filters: {},
    plotData: null,
    currentView: 'scatter'      // 'scatter' or 'heatmap'
};

let _lastScatterArgs = null;
let _lastHeatmapArgs = null;
let _correlationsFilterVisible = true;

// Renderer registry for the server-declared stat views (metadata.views).
// A new view = one manifest entry server-side + one renderer here.
const VIEW_RENDERERS = {
    scatter: () => updatePcaPlot(),
    heatmap: () => loadCorrelationHeatmap()
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


function mountCorrelationsVizGear() {
    if (!window.VariablePrefs) return;
    if (document.getElementById('correlations-var-gear')) return;
    const anchor = document.querySelector('#correlations .corr-view-toggle');
    if (!anchor) return;

    const gear = VariablePrefs.gearButton('viz', () => {
        const md = pcaData.metadata;
        if (!md || !md.all_variables_order) return;
        const covered = new Set(Object.values(md.numeric_col_bases || {}));
        VariablePrefs.openPanel({
            surface: 'viz',
            title: 'Customize variables',
            allOrder: md.all_variables_order,
            globalList: md.viz_priority || [],
            schemaMap: md.schema_map || {},
            sectionOrder: md.section_order || null,
            coveredSet: covered.size ? covered : null,
            onApply: () => {
                renderPcaControls(pcaData.metadata);
                refreshCurrentView();
            }
        });
    });
    gear.id = 'correlations-var-gear';
    anchor.insertAdjacentElement('afterend', gear);
}


// --- Banners + plain-language captions (the HASS readability layer) ---

function renderUnitBanner(unit) {
    const el = document.getElementById('corr-unit-banner');
    if (!el) return;
    if (!unit || !unit.grouping_display || !unit.grouping_display.length) {
        el.style.display = 'none';
        return;
    }
    const grouping = unit.grouping_display.join(' × ');
    el.textContent = `Each point/row is one ${grouping} group — the average of that group's annotated videos ` +
        `(${(unit.n_groups || 0).toLocaleString()} groups, each with at least ${unit.min_group_size} videos). ` +
        `All statistics on this tab describe these groups, not individual videos.`;
    el.style.display = 'block';
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


window.correlationsToggleSidebar = function () {
    _correlationsFilterVisible = !_correlationsFilterVisible;
    const panel = document.getElementById('correlations-filter-panel');
    if (panel) panel.style.display = _correlationsFilterVisible ? 'flex' : 'none';
    requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
};


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
    pcaData.filters = {};

    const plotDiv = document.getElementById('pca-plot');
    if (plotDiv && typeof Plotly !== 'undefined') {
        Plotly.purge(plotDiv);
    }

    if (studyName) {
        loadPcaMetadata();
    }
}


async function loadPcaMetadata() {
    document.getElementById('pca-status').innerText = "Loading metadata...";

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
        document.getElementById('pca-status').innerText = "Ready";

        renderViewToggle(data.views);
        mountCorrelationsVizGear();
        renderPcaControls(data);
        renderPcaFilters(data);
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
        document.getElementById('pca-status').innerText = "Error loading metadata";
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
    // variable's membership).
    const schemaMap = data.schema_map || {};
    const visibleCols = filterColsByPrefs(data.numeric_cols || []);

    visibleCols.forEach(col => {
        const variance = inter[col]?.explained_variance_pct;
        const displayName = (schemaMap[col] && schemaMap[col].display_name) ? schemaMap[col].display_name : col;
        const label = variance ? `${displayName} (${variance}%)` : displayName;

        const optX = document.createElement('option');
        optX.value = col;
        optX.text = label;
        xSelect.appendChild(optX);

        const optY = document.createElement('option');
        optY.value = col;
        optY.text = label;
        ySelect.appendChild(optY);
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


function renderPcaFilters(data) {
    const container = document.getElementById('pca-filters');
    container.innerHTML = '';

    const schemaMap = data.schema_map || {};
    const displayIds = data.display_ids || {};

    const truncatedFactors = data.truncated_factors || [];

    data.factor_cols.forEach(col => {
        const wrapper = document.createElement('div');
        wrapper.className = 'filter-group corr-filter-group';

        const label = document.createElement('div');
        // Use display_name from schema_map if available
        label.innerText = (schemaMap[col] && schemaMap[col].display_name) ? schemaMap[col].display_name : col;
        label.classList.add('font-bold', 'corr-filter-label');
        wrapper.appendChild(label);

        // Factors with too many distinct values are not filterable — say so
        // instead of rendering an empty checkbox list.
        if (truncatedFactors.includes(col)) {
            const note = document.createElement('div');
            note.classList.add('text-xs', 'corr-filter-note');
            note.innerText = 'Too many distinct values to filter';
            wrapper.appendChild(note);
            container.appendChild(wrapper);
            return;
        }

        const values = data.factor_values[col] || [];

        const listDiv = document.createElement('div');
        listDiv.className = 'corr-filter-values';

        values.forEach(val => {
            const row = document.createElement('div');
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.value = val;
            cb.classList.add('corr-filter-checkbox');

            if (pcaData.filters[col] && pcaData.filters[col].includes(val)) {
                cb.checked = true;
            }

            cb.onchange = () => {
                const checked = Array.from(listDiv.querySelectorAll('input:checked')).map(c => c.value);
                if (checked.length > 0) {
                    pcaData.filters[col] = checked;
                } else {
                    delete pcaData.filters[col];
                }
                refreshCurrentView();
            };

            const span = document.createElement('span');
            // For collection_id, show display_collection_id if available
            if (col === 'collection_id' && displayIds[val]) {
                span.innerText = displayIds[val];
            } else {
                span.innerText = val;
            }
            span.classList.add('text-sm');

            row.appendChild(cb);
            row.appendChild(span);
            listDiv.appendChild(row);
        });

        wrapper.appendChild(listDiv);
        container.appendChild(wrapper);
    });
}


function resetPcaFilters() {
    pcaData.filters = {};
    renderPcaFilters(pcaData.metadata);
    refreshCurrentView();
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

    refreshCurrentView();
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
                filters: pcaData.filters,
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
                ? `Showing ${shown.toLocaleString()} / ${total.toLocaleString()} points`
                : `${total.toLocaleString()} points`;
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
    if (countEl) countEl.innerText = 'Loading heatmap...';

    try {
        const res = await fetch('/api/correlations/correlation_matrix', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                study: pcaData.activeStudy,
                filters: pcaData.filters,
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
            countEl.innerText = `${data.count.toLocaleString()} observations`;
        }

        renderCorrelationHeatmap(data);

    } catch (e) {
        console.error(e);
        if (countEl) countEl.innerText = 'Error loading heatmap';
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

    // Optional significance masking: blank cells with q >= .05 (diagonal kept)
    let maskedCount = 0;
    const z = rM.map((row, i) => row.map((val, j) => {
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
    const rSymbol = (method === 'spearman') ? 'ρ' : 'r';
    const hoverText = rM.map((row, i) =>
        row.map((val, j) => {
            const head = `${displayColumns[i]} × ${displayColumns[j]}`;
            if (val === null || val === undefined) return `${head}<br>${rSymbol} undefined`;
            const parts = [`${rSymbol} = ${val.toFixed(3)}`];
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
        parts.push(`${maskedCount.toLocaleString()} non-significant cells (q ≥ .05) are blanked.`);
    }
    if (payload.centered) {
        parts.push('Values are centered within each collection (within-donor associations).');
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
            rows.push([
                dname(cols[i]), dname(cols[j]), fams[i], fams[j],
                payload.method,
                payload.matrix?.[i]?.[j],
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
