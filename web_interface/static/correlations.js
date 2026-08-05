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
    const textEl = document.getElementById('corr-caption-text');
    if (!textEl) return;
    textEl.innerHTML = html || '';
    syncCaptionVisibility();
}


// --- Static per-view explainers ("What is this?") ---
//
// Longer plain-language copy for readers new to the tab, collapsed by default
// behind the toggle link that follows the highlight text. The copy is static
// on purpose: it describes the KINDS of variables and where the pipeline
// produces them (contracts -> ingest/scrape/annotate -> recode), never a
// specific variable name, so it stays true when the contracts change.
const VIEW_EXPLAINERS = {
    scatter:
        '<p>Each dot is one <b>collection-day</b>: all the videos that one donated feed ' +
        '(a “collection”) played on a single calendar day, averaged together. Days with too few ' +
        'annotated videos are dropped, so every dot summarises a reasonable sample of videos. ' +
        'Everything on this plot describes those day-averages — never an individual video.</p>' +
        '<p>The axis variables are built from the day-averages in four ways. <b>Components</b> ' +
        '(C0, C1, …) summarise how a day’s feed was spread across a categorical annotation: the ' +
        'percentage says how much of the day-to-day variation that summary captures, and the small ' +
        'labels at the ends of the axes name what each direction means — read them before ' +
        'interpreting. <b>(entropy)</b> measures how diverse the day’s feed was on that variable ' +
        '(low = concentrated, high = spread out). <b>(share of feed)</b> is the fraction of the ' +
        'day’s videos answered “yes” for a yes/no annotation. The rest are plain day averages of ' +
        'numeric properties. Most values are standardised (z-scores): 0 is an average day, ±1 is ' +
        'one standard deviation — hover a dot for the raw values.</p>' +
        '<p>The variables themselves come from the platform’s pipeline: what the account holder ' +
        'did comes from the donated activity data, properties of the served videos come from ' +
        'scraping, and content judgements come from AI annotation — each variable is declared in ' +
        'the study’s data contracts, so the dropdown lists follow whatever the contracts currently ' +
        'define.</p>' +
        '<p><b>Regression</b> fits one straight line over every group in the study and reports its ' +
        'strength (R²), slope and significance (p). <b>Ellipses</b> draw each colour group’s 95% ' +
        'region — separated ellipses mean the groups occupy different parts of the plane. ' +
        '<b>Within-collection</b> removes differences between collections first, so what remains ' +
        'is day-to-day movement inside each feed. A pattern here is an association between ' +
        'day-profiles — it does not show that one thing causes the other. Click any dot to ' +
        'inspect the actual videos behind it.</p>',
    heatmap:
        '<p>Each cell is the correlation between two of the study’s variables, computed over the ' +
        'same <b>collection-day</b> groups as the scatter plot (one donated feed’s videos on one ' +
        'calendar day, averaged). It runs from −1 (blue: when one is high the other is low) ' +
        'through 0 (no association) to +1 (red: they rise and fall together). Hover a cell for ' +
        'the exact value, the number of groups behind it (n), the p-value and the q-value.</p>' +
        '<p><b>Method:</b> Pearson measures straight-line association; Spearman ranks the values ' +
        'first and is safer when a variable is skewed or has outliers. <b>p vs q:</b> the matrix ' +
        'tests hundreds of pairs at once, so some would look “significant” by luck alone; the ' +
        'q-value corrects for this, and <b>Hide n.s.</b> blanks every cell that does not survive ' +
        'the correction — the cells left are the ones worth taking seriously.</p>' +
        '<p>The variables are the same day-level summaries as on the scatter plot — components, ' +
        'entropies, shares of feed and day averages — produced by the pipeline from donated ' +
        'activity data, scraped video metadata and AI annotation, as declared in the study’s ' +
        'data contracts. Thin separator lines group summaries of the same underlying variable: ' +
        'cells inside one block are partly related by construction, so the scientifically ' +
        'interesting cells usually cross a separator. Correlation here means day-profiles ' +
        'co-vary — it is not causal, and it says nothing about individual videos. ' +
        '<b>Download CSV</b> exports every pair for supplementary tables.</p>',
    group_stats:
        '<p>Both panels below compare the same <b>collection-day</b> groups as the scatter and ' +
        'heatmap: each row summarises how one variable’s day-level values differ between groups ' +
        'of days.</p>' +
        '<p><b>Where the variables come from.</b> Every variable on this tab is declared in the ' +
        'platform’s data contracts and produced by the pipeline: behavioural variables from the ' +
        'donated activity data, video properties from scraping, and content judgements from AI ' +
        'annotation. Each contract entry also assigns the variable a <i>role</i>: ' +
        '<b>measures</b> (per-video properties whose day-level average is meaningful) become the ' +
        'variable rows of these tables; <b>comparison</b> variables (categorical, constant across ' +
        'a whole collection-day, with a few meaningful levels) become the groupings tested in the ' +
        'second panel; and the collection identifier is the grouping key that defines the unit ' +
        'and drives the first panel. The tables update automatically when the contracts change — ' +
        'nothing on this page is a fixed variable list.</p>' +
        '<p><b>Changing what you see.</b> These tables always show every measure and comparison ' +
        'computed for the study (personal variable preferences and the Within-collection switch ' +
        'apply to the scatter and heatmap, not here). Adding or removing a variable means ' +
        'changing the contracts and re-running the pipeline — for example an admin adding an ' +
        'annotation field and refreshing the study — and a new comparison likewise needs a ' +
        'contract role, not a UI setting.</p>',
};

let _explainerOpen = false;


function toggleCorrExplainer() {
    _explainerOpen = !_explainerOpen;
    applyExplainerState();
    return false;
}


function applyExplainerState() {
    const body = document.getElementById('corr-view-explainer');
    const link = document.getElementById('corr-explainer-toggle');
    if (!body || !link) return;
    const copy = VIEW_EXPLAINERS[pcaData.currentView];
    if (!copy) {
        link.style.display = 'none';
        body.style.display = 'none';
    } else {
        link.style.display = '';
        link.textContent = _explainerOpen ? 'Hide explanation' : 'What is this?';
        body.innerHTML = copy;
        body.style.display = _explainerOpen ? 'block' : 'none';
    }
    syncCaptionVisibility();
}


// The strip shows whenever there is a highlight text OR the current view has
// explainer copy to offer (so the "What is this?" link is always reachable).
function syncCaptionVisibility() {
    const el = document.getElementById('corr-caption');
    const textEl = document.getElementById('corr-caption-text');
    if (!el || !textEl) return;
    const hasText = !!textEl.innerHTML;
    const hasCopy = !!VIEW_EXPLAINERS[pcaData.currentView];
    textEl.style.display = hasText ? '' : 'none';
    el.style.display = (hasText || hasCopy) ? 'block' : 'none';
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

    // Colour: comparison/descriptor variables — display_name from schema_map if available
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
    const viewChanged = pcaData.currentView !== view;
    pcaData.currentView = view;

    // Clear the plot area on a view change: Plotly.newPlot does not remove
    // foreign HTML (the Group-differences tables), so without this the old
    // view's headings stay visible under the next view's chart.
    const plotDiv = document.getElementById('pca-plot');
    if (viewChanged && plotDiv) {
        if (typeof Plotly !== 'undefined') Plotly.purge(plotDiv);
        plotDiv.innerHTML = '';
    }

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
    // Collapse the explainer when the view changes — its copy is per-view.
    if (viewChanged) _explainerOpen = false;
    setCaption('');
    applyExplainerState();
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

    // The scatter fetch is the tab's slowest request — show the same loading
    // signal the other views use, incl. on every X/Y/colour change.
    const loadingEl = document.getElementById('pca-point-count');
    if (loadingEl) loadingEl.innerText = 'Loading…';

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
            if (loadingEl) loadingEl.innerText = '';
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
        if (loadingEl) loadingEl.innerText = 'Error';
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
                legendgroup: gName,
                showlegend: false,
                line: { width: 1, color: color },
                fill: 'toself',
                fillcolor: color,
                opacity: 0.15,
                hoverinfo: 'name'
            });
        });
    }

    // Scatter traces. Each series is its own legendgroup, so its ellipse and
    // per-series regression line show/hide together with the dots.
    groupsKeys.forEach((g, i) => {
        const color = colors[i % colors.length];
        traces.push({
            x: groups[g].x,
            y: groups[g].y,
            mode: 'markers',
            type: 'scatter',
            name: g,
            legendgroup: g,
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

        // Per-series lines (server-fitted on the FULL data per colour group).
        // Sharing the series' legendgroup makes the legend toggle an honest
        // per-series filter: hiding a series hides its line, and no statistic
        // is ever re-fitted to the visible subset (the pooled line above
        // always describes the whole study — see the guide §3/§4.2).
        (payload.per_group_regressions || []).forEach(r => {
            const gName = (colorLabel === 'collection_id' && displayIds[r.group])
                ? displayIds[r.group] : r.group;
            const gi = groupsKeys.indexOf(gName);
            if (gi < 0) return; // series absent from the display sample
            const gx = groups[gName].x;
            const gMin = Math.min(...gx), gMax = Math.max(...gx);
            if (!isFinite(gMin) || !isFinite(gMax) || gMin === gMax) return;
            traces.push({
                x: [gMin, gMax],
                y: [r.slope * gMin + r.intercept, r.slope * gMax + r.intercept],
                mode: 'lines',
                type: 'scatter',
                name: `${gName}: slope ${r.slope.toFixed(2)} (n=${r.n})`,
                legendgroup: gName,
                showlegend: false,
                line: { color: colors[gi % colors.length], width: 1.5, dash: 'dot' },
                hoverinfo: 'name'
            });
        });

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

    renderScatterCaption(serverStats, xTitle, yTitle, isCentered, showStats, payload, colorLabel);

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
    const caveat = independenceCaveat();
    if (caveat) parts.push(caveat);
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


// The Group-differences view renders TWO panels because it answers two
// statistically different questions: personalization (variance BETWEEN
// collections — an effect-size reading with no p-values) and within-feed
// comparisons (ANOVA blocked on collection, with partial effect sizes).
const GROUP_STATS_PANELS = [
    {
        key: 'personalization',
        title: 'How personalized are the feeds?',
        intro: 'Share of each variable’s day-to-day variance that lies between ' +
            'collections. η² here reads as an intraclass correlation: 0 = the ' +
            'collections’ feeds are interchangeable, 1 = knowing the collection tells ' +
            'you everything. No p-values on purpose — with hundreds of dependent days ' +
            'per collection they are always ≈0 and say nothing; the effect size is the ' +
            'finding. The PERMANOVA table below asks the same question at ' +
            'whole-profile level (all of a variable’s components at once) — rank it ' +
            'by pseudo-F.',
    },
    {
        key: 'comparison',
        title: 'Within-feed comparisons (collection differences removed first)',
        intro: 'Do days differ by these variables <i>inside the same feed</i>? Because feeds ' +
            'differ so much from each other, each test first sets aside the differences ' +
            'between collections — every collection acts as its own control — and then asks ' +
            'whether the comparison explains any of the variation left <i>within</i> feeds. ' +
            'Effect sizes are therefore partial: the share of the within-feed variance the ' +
            'comparison explains, quoted as ω²ₚ (.01 small, .06 medium, .14 large). ' +
            '† rows are constant within each collection and cannot be separated from ' +
            'personalization — their p is descriptive only. KW q is a rank-based check ' +
            'on within-collection-centered values; trust it over the ANOVA q when ' +
            'groups are small or skewed. The PERMANOVA table runs on ' +
            'within-collection-centered profiles, never mixing different variables’ ' +
            'PCA bases; its permutation is not restricted within collections, so read ' +
            'its p as slightly optimistic.',
    },
];

// Marker + explainer for factors that are constant within every collection
// (e.g. platform when each collection donates from one platform).
const NESTED_TIP = 'This variable is constant within each collection, so its effect ' +
    'cannot be separated from personalization. The comparison has only as many ' +
    'independent units as there are collections — not day-groups — so no q is ' +
    'shown; treat its p as descriptive.';

// Column specs for the four Group-differences tables: the label, the row key
// it reads, how to format it, and the plain-language explainer shown on hover.
// `num` marks columns sorted numerically; the rest sort as text. `cell` is an
// optional custom renderer (row, formattedCell) -> html.
const GROUP_STATS_TABLES = [
    {
        key: 'personalization',
        panel: 'personalization',
        title: 'Variance between collections, per variable (ANOVA η² = ICC)',
        empty: 'Personalization needs at least 2 collections with enough days each.',
        defaultSort: { col: 'eta2', dir: -1 },
        explain:
            '<p>This table asks, one variable at a time: if you know which collection a day ' +
            'belongs to, how much do you know about that variable? Every day-level measure the ' +
            'pipeline produced for this study appears as a row — components, entropies, shares ' +
            'of feed and day averages alike (the row list follows the data contracts, so it ' +
            'changes when they do).</p>' +
            '<p><b>η²</b> is the share of the variable’s day-to-day variance that lies ' +
            '<i>between</i> collections rather than within them: 0 means the feeds are ' +
            'interchangeable on this variable; large values mean the variable mostly reflects ' +
            '<i>whose feed it is</i> — personalization made a number. <b>ω²</b> is the same idea ' +
            'with a small-sample correction; quote that one in a paper. There are deliberately ' +
            'no p-values: with hundreds of non-independent days per collection every test would ' +
            'come out “significant”, so the effect size is the finding.</p>',
        columns: [
            { key: 'component', label: 'Variable', kind: 'name',
              tip: 'The variable whose day-to-day variance is decomposed. "(C0)" is a variable\'s leading PCA dimension; "(entropy)" is feed diversity on that variable; "(share of feed)" is a yes/no variable\'s daily share.' },
            { key: 'eta2', label: 'η² (ICC)', num: true, digits: 3,
              tip: 'Share of this variable\'s variance lying between collections (the intraclass correlation). This is the personalization-strength number — .01 small, .06 medium, .14 large.' },
            { key: 'magnitude', label: 'Effect',
              tip: 'Plain-language label for η² using the conventional cutoffs (negligible / small / medium / large).' },
            { key: 'omega2', label: 'ω²', num: true, digits: 3,
              tip: 'A less optimistic η² (corrects small-sample inflation). Quote this one in a paper.' },
            { key: 'F', label: 'F', num: true, digits: 1,
              tip: 'Between-collection variance over within-collection variance. Descriptive here — no significance is attached (see the panel note).' },
            { key: 'n', label: 'n', num: true,
              tip: 'Number of collection-day groups behind the decomposition.' },
            { key: 'levels', label: 'Collections', num: true,
              tip: 'How many collections the variance is decomposed across.' },
        ],
    },
    {
        key: 'permanova_personalization',
        panel: 'personalization',
        title: 'Whole-profile personalization (PERMANOVA on collection)',
        empty: 'No multi-component variable families to test.',
        defaultSort: { col: 'pseudo_F', dir: -1 },
        explain:
            '<p>A categorical variable with many possible values is summarised by several ' +
            'components (C0, C1, …), and a difference between feeds can be spread across all of ' +
            'them at once — invisible to any single row of the table above. PERMANOVA therefore ' +
            'tests each <b>variable family</b> — all the components that came out of one ' +
            'variable’s PCA — as a single profile, asking whether whole day-profiles cluster by ' +
            'collection.</p>' +
            '<p><b>Why only families appear here:</b> a variable represented by one number (a ' +
            'day average, an entropy, a share of feed) — or whose PCA kept only one component — ' +
            'has no multi-dimensional profile to test; it is fully covered by the ' +
            'single-variable table above. So the rows are exactly the categorical variables ' +
            'whose PCA produced two or more components in this study — a list that follows the ' +
            'contracts and the data, not a fixed choice.</p>' +
            '<p>Rank rows by <b>pseudo-F</b> (larger = collection membership separates the ' +
            'profiles more strongly). The permutation p is nearly always tiny for the same ' +
            'non-independence reason as above — read it as descriptive.</p>',
        columns: [
            { key: 'family', label: 'Variable family', kind: 'name',
              tip: 'The variable whose components are tested together, as one profile.' },
            { key: 'pseudo_F', label: 'pseudo-F', num: true, digits: 2,
              tip: 'How much better collection membership separates whole day-profiles than chance. Rank on this — larger = stronger profile-level personalization.' },
            { key: 'p', label: 'p', num: true, p: true,
              tip: 'Permutation p. With many dependent days per collection it is nearly always tiny — read it as descriptive, and rank on pseudo-F.' },
            { key: 'q', label: 'q', num: true, p: true,
              tip: 'Benjamini–Hochberg adjusted p across this table.' },
            { key: 'n', label: 'n', num: true,
              tip: 'Number of collection-day groups behind the test.' },
            { key: 'levels', label: 'Collections', num: true,
              tip: 'How many collections are compared.' },
            { key: 'n_components', label: 'Components', num: true,
              tip: 'How many of the variable\'s components were compared together as a profile.' },
            { key: 'permutations', label: 'Permutations', num: true,
              tip: 'How many random shuffles the p-value was computed from.' },
        ],
    },
    {
        key: 'anova',
        panel: 'comparison',
        title: 'Which comparisons move single variables? (ANOVA, collection differences removed)',
        empty: 'No testable comparison × variable pairs in this study.',
        defaultSort: { col: 'eta2', dir: -1 },
        explain:
            '<p>This table asks whether a <b>comparison variable</b> — a categorical property ' +
            'that is constant across a whole collection-day and has a few meaningful levels — ' +
            'shifts a variable <i>inside the same feed</i>. Which comparisons appear is set by ' +
            'the role each variable is assigned in the data contracts, plus basic eligibility ' +
            '(enough days per level, not too many levels); a comparison missing here either has ' +
            'no qualifying contract role or too little data in this study. The variable rows are ' +
            'the same contract-driven measures as in the personalization table.</p>' +
            '<p>Because feeds differ so much from each other, a naive comparison would mostly ' +
            're-measure personalization. Each test here therefore first sets the collection ' +
            'differences aside — every collection acts as its own control (statisticians call ' +
            'this “blocking on collection”) — and then asks whether the comparison explains any ' +
            'of the variation that remains within feeds. <b>η²ₚ</b> (“partial”) is the share of ' +
            'that within-feed variation the comparison explains; <b>ω²ₚ</b> is its corrected ' +
            'twin — quote it. Judge significance on <b>q</b> (corrected for the number of tests ' +
            'in the table) and cross-check with <b>KW q</b>, a rank-based version that is more ' +
            'robust with small or skewed groups.</p>' +
            '<p>Rows marked <b>†</b> never vary within a collection, so their effect cannot be ' +
            'told apart from personalization: they are tested without the collection adjustment ' +
            'and their p is descriptive only.</p>',
        columns: [
            { key: 'factor', label: 'Comparison', kind: 'name',
              cell: (row, html) => row.nested_in_collection
                  ? `${html} <span class="meta-tooltip corr-nested-badge" data-tooltip="${escapeHtml(NESTED_TIP)}">†</span>`
                  : html,
              tip: 'The comparison variable the test contrasts — e.g. do weekend days differ from weekdays within a feed? † marks variables constant within each collection (hover the mark).' },
            { key: 'component', label: 'Variable', kind: 'name',
              tip: 'The variable being compared. "(C0)" is a leading PCA dimension; "(entropy)" is feed diversity; "(share of feed)" is a yes/no variable\'s daily share.' },
            { key: 'eta2', label: 'η²ₚ', num: true, digits: 3,
              tip: 'Partial eta-squared: the share of the WITHIN-feed variance this comparison explains, after removing collection differences (the blocking). Read this before the p-value. Conventions: .01 small, .06 medium, .14 large.' },
            { key: 'magnitude', label: 'Effect',
              tip: 'Plain-language label for the partial η² using the conventional cutoffs.' },
            { key: 'omega2', label: 'ω²ₚ', num: true, digits: 3,
              tip: 'Partial omega-squared: a less optimistic partial η². Quote this one in a paper. Slightly negative values just mean "indistinguishable from zero".' },
            { key: 'F', label: 'F', num: true, digits: 1,
              tip: 'The test statistic from the blocked model — comparison variance over residual (within-feed) variance.' },
            { key: 'p', label: 'p', num: true, p: true,
              tip: 'Uncorrected significance of this single test. With many tests in this table, use q instead. Days within a collection are not fully independent, so p runs optimistic.' },
            { key: 'q', label: 'q', num: true, p: true,
              tip: 'Benjamini–Hochberg adjusted p, across every non-† test in this table. q < .05 means under 5% of the rows you call real are expected to be noise. Judge significance here.' },
            { key: 'kw_q', label: 'KW q', num: true, p: true,
              tip: 'The same comparison as a rank-based Kruskal–Wallis test on within-collection-centered values (an approximation of the blocked test), BH-adjusted. Trust it over q when groups are small or skewed; if the two disagree, be sceptical.' },
            { key: 'n', label: 'n', num: true,
              tip: 'Number of collection-day groups behind the test.' },
            { key: 'levels', label: 'Levels', num: true,
              tip: 'How many distinct values the comparison takes (e.g. 2 for weekend vs weekday).' },
        ],
    },
    {
        key: 'permanova',
        panel: 'comparison',
        title: 'Do whole variable profiles differ? (PERMANOVA, within-collection)',
        empty: 'No testable family × comparison pairs in this study.',
        defaultSort: { col: 'q', dir: 1 },
        explain:
            '<p>The profile-level version of the table above: for each <b>variable family</b> ' +
            '(the components of one categorical variable, tested together as a single profile) ' +
            'it asks whether a comparison separates whole day-profiles inside feeds. Values are ' +
            'first centered within each collection — each collection’s own average profile is ' +
            'subtracted — so the test looks at day-to-day movement, not differences between ' +
            'feeds.</p>' +
            '<p>As in the other PERMANOVA table, only variables whose PCA produced two or more ' +
            'components appear as families; variables summarised by a single number are covered ' +
            'by the ANOVA table above. The comparisons are the same contract-declared comparison ' +
            'variables as above.</p>' +
            '<p>Rank on <b>pseudo-F</b> and judge significance on <b>q</b>. One honesty note: ' +
            'the permutation shuffles days without respecting which collection they belong to, ' +
            'so under strong day-to-day dependence its p runs optimistic. <b>†</b> rows are ' +
            'collection-constant comparisons, tested on raw (uncentered) profiles and reported ' +
            'without q.</p>',
        columns: [
            { key: 'family', label: 'Variable family', kind: 'name',
              tip: 'The variable whose components are tested together, as one profile.' },
            { key: 'factor', label: 'Comparison', kind: 'name',
              cell: (row, html) => row.nested_in_collection
                  ? `${html} <span class="meta-tooltip corr-nested-badge" data-tooltip="${escapeHtml(NESTED_TIP)}">†</span>`
                  : html,
              tip: 'The comparison being tested. Non-† rows run on within-collection-centered profiles (do days differ inside feeds?); † rows are collection-constant and run on raw profiles.' },
            { key: 'pseudo_F', label: 'pseudo-F', num: true, digits: 2,
              tip: 'PERMANOVA test statistic: how much better the comparison separates whole profiles than chance. Its significance comes from permutation, not a table.' },
            { key: 'p', label: 'p', num: true, p: true,
              tip: 'Permutation p-value. Permutation is free (not restricted within collections), so under collection dependence it runs optimistic — lean on q and effect direction.' },
            { key: 'q', label: 'q', num: true, p: true,
              tip: 'Benjamini–Hochberg adjusted p across the non-† rows of this table — judge significance here rather than on p.' },
            { key: 'n', label: 'n', num: true,
              tip: 'Number of collection-day groups behind the test.' },
            { key: 'levels', label: 'Levels', num: true,
              tip: 'How many distinct values the comparison takes.' },
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

// Per-table explainer open/closed state — persists across re-renders (sorting
// a column must not collapse an explanation the reader is mid-way through).
const _groupStatsExplainerOpen = {};


function toggleGroupTableExplainer(tableKey) {
    _groupStatsExplainerOpen[tableKey] = !_groupStatsExplainerOpen[tableKey];
    if (_lastGroupStats) renderGroupStats(_lastGroupStats);
    return false;
}


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

    // Version guard: an artifact from an older worker uses the retired
    // pooled-table layout whose numbers this overhaul deems misleading
    // (unblocked p-values, collection_id in the same BH family). Never
    // re-render them — ask for a refresh instead.
    if (data.version !== 2) {
        plotDiv.innerHTML = `<div class="corr-table-wrap"><p>These group statistics ` +
            `were computed by an older version of the analysis and use a layout this ` +
            `page no longer shows. Run Data Pipeline → Refresh Caches (PCA / ` +
            `Correlations), then reload.</p></div>`;
        setCaption('');
        return;
    }

    const renderTable = (spec) => {
        const rows = data[spec.key] || [];
        let out = `<h3 class="text-h3">${escapeHtml(spec.title)}</h3>`;
        if (spec.explain) {
            const open = !!_groupStatsExplainerOpen[spec.key];
            out += `<p class="corr-table-explainer-toggle"><a href="#" ` +
                `onclick="return toggleGroupTableExplainer('${spec.key}')">` +
                `${open ? 'Hide explanation' : 'What does this table show?'}</a></p>`;
            if (open) {
                out += `<div class="corr-explainer-body corr-table-explainer text-xs">${spec.explain}</div>`;
            }
        }
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
                if (col.cell) cell = col.cell(row, cell);
                out += `<td>${cell}</td>`;
            });
            out += `</tr>`;
        });
        return out + `</tbody></table>`;
    };

    // Each panel's result summary sits under its own heading, next to its
    // explanatory intro — not pooled into one caption below all the tables.
    const highlights = {
        personalization: personalizationHighlight(data, dname),
        comparison: comparisonHighlight(data, dname),
    };

    const renderPanel = (panel) => {
        const specs = GROUP_STATS_TABLES.filter(s => s.panel === panel.key);
        const highlight = highlights[panel.key];
        return `<h2 class="text-h2 corr-panel-title">${escapeHtml(panel.title)}</h2>` +
            `<p class="text-xs corr-panel-intro">${panel.intro}</p>` +
            (highlight ? `<p class="corr-panel-highlight">${highlight}</p>` : '') +
            specs.map(renderTable).join('');
    };

    plotDiv.innerHTML = `<div class="corr-table-wrap">` +
        GROUP_STATS_PANELS.map(renderPanel).join('') + `</div>`;

    setCaption('');
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


// Result summary for the personalization panel: the largest ICC.
function personalizationHighlight(data, dname) {
    const pers = data.personalization || [];
    const hasEta = (r) => r && r.eta2 !== null && r.eta2 !== undefined && isFinite(r.eta2);
    const topPers = pers.filter(hasEta).reduce(
        (best, r) => (best === null || r.eta2 > best.eta2 ? r : best), null);
    if (!topPers) return '';
    return `At its strongest, ` +
        `${(topPers.eta2 * 100).toFixed(0)}% of the day-to-day variation in ` +
        `<b>${escapeHtml(dname(topPers.component))}</b> lies between the ` +
        `${(data.n_collections || topPers.levels).toLocaleString()} collections ` +
        `(a ${varianceMagnitude(topPers.eta2)} effect).`;
}


// Result summary for the comparison panel: significant-test count, the
// largest partial omega², the PERMANOVA families, and — when the study has
// few collections — the standing non-independence caveat.
function comparisonHighlight(data, dname) {
    const parts = [];

    const anova = data.anova || [];
    const testable = anova.filter(r => !r.nested_in_collection);
    const sigAnova = testable.filter(r => r.q !== null && r.q !== undefined && r.q < 0.05);
    if (testable.length) {
        parts.push(`${sigAnova.length} of ${testable.length} comparison × ` +
            `variable tests are significant after correction (q < .05).`);
        const hasOmega = (r) => r && r.omega2 !== null && r.omega2 !== undefined && isFinite(r.omega2);
        const pool = (sigAnova.length ? sigAnova : testable).filter(hasOmega);
        const top = pool.length
            ? pool.reduce((best, r) => (r.omega2 > best.omega2 ? r : best))
            : null;
        if (top && top.omega2 > 0) {
            parts.push(`Largest effect: <b>${escapeHtml(dname(top.factor))}</b> explains ` +
                `${(top.omega2 * 100).toFixed(0)}% of the within-feed variation in ` +
                `<b>${escapeHtml(dname(top.component))}</b> ` +
                `(a ${varianceMagnitude(top.omega2)} effect, ${formatP(top.q)} after correction).`);
        }
    }

    const perma = (data.permanova || []).filter(r => !r.nested_in_collection);
    const sigPerma = perma.filter(r => r.q !== null && r.q !== undefined && r.q < 0.05);
    if (sigPerma.length) {
        const fams = [...new Set(sigPerma.map(r => `${dname(r.family)} (by ${dname(r.factor)})`))].slice(0, 4);
        parts.push(`Whole-profile differences (PERMANOVA): ` +
            `${fams.map(escapeHtml).join('; ')}${sigPerma.length > 4 ? ' and more' : ''}.`);
    } else if (perma.length) {
        parts.push('No variable family shows a significant whole-profile difference after correction.');
    }

    const caveat = independenceCaveat();
    if (caveat) parts.push(caveat);
    return parts.join(' ');
}


// Standing non-independence caveat for small collection counts, shared by the
// scatter, heatmap and group-differences captions. Returns '' when the study
// has enough collections (metadata.unit carries the config threshold).
function independenceCaveat() {
    const unit = pcaData.metadata?.unit || {};
    const n = unit.n_collections;
    const threshold = unit.independence_warning_collections;
    if (!threshold || n === null || n === undefined || n >= threshold) return '';
    return `<span class="corr-caption-warning">This study has only ` +
        `${n.toLocaleString()} collection${n === 1 ? '' : 's'}, and days within a ` +
        `collection are not independent — pooled p-values run optimistic. Treat ` +
        `results as descriptive of these collections.</span>`;
}


// --- Scatter caption (plain-language summary of the server statistics) ---

function renderScatterCaption(stats, xTitle, yTitle, isCentered, showStats, payload, colorLabel) {
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
    // Per-series slopes complement the pooled line: agreeing slopes back the
    // pooled claim, disagreeing ones expose it as a mixture. With a colour
    // split active, list every series (matching the drawn per-series lines);
    // otherwise fall back to per-collection slopes under the small-study
    // caveat. Display names come from the anonymised map.
    const caveat = independenceCaveat();
    const displayMap = pcaData.metadata?.display_ids || {};
    const seriesRegs = payload?.per_group_regressions || [];
    const collSlopes = payload?.per_collection_slopes || [];
    if (seriesRegs.length > 1) {
        const label = (g) => (colorLabel === 'collection_id' && displayMap[g]) ? displayMap[g] : g;
        const items = seriesRegs.map(r =>
            `${escapeHtml(label(r.group))} ${r.slope.toFixed(2)} (n=${r.n})`);
        parts.push(`Per-series slopes: ${items.join('; ')} — if these disagree, ` +
            `the pooled line mixes different relationships.`);
        parts.push('<span class="text-xs">Each series\' dotted line is fitted on the ' +
            'full data for that series; use the legend to show or hide series and ' +
            'their lines. The pooled line and readout always describe all groups, ' +
            'including any series hidden via the legend — they are never re-fitted ' +
            'to the visible subset.</span>');
    } else if (caveat && collSlopes.length > 1) {
        const items = collSlopes.map(s =>
            `${escapeHtml(displayMap[s.collection_id] || s.collection_id)} ${s.slope.toFixed(2)} (n=${s.n})`);
        parts.push(`Per-collection slopes: ${items.join('; ')} — if these disagree, ` +
            `the pooled line mixes different relationships.`);
    }
    if (caveat) parts.push(caveat);
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
