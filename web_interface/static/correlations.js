let pcaData = {
    activeStudy: null,
    metadata: null,
    // Per grouping factor: a list of values, or {min, max} for the date window.
    // Selects GROUPS (collection-days), not videos — see the Sample panel.
    filters: {},
    lastSample: null,           // group/video counts from the last payload
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
        pcaData.lastSample = data.sample || null;
        document.getElementById('pca-status').innerText = "Ready";

        renderViewToggle(data.views);
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

    renderSplitControl(data);
}


// "Split by" options come from the server (factors with 2..N levels, each
// flagged for whether it partitions collections). The whole control hides when
// this study has none.
function renderSplitControl(data) {
    const wrap = document.getElementById('pca-split-wrap');
    const select = document.getElementById('pca-split-select');
    if (!wrap || !select) return;

    const options = data.split_cols || [];
    const prev = select.value;
    select.innerHTML = '<option value="">— no split —</option>';

    options.forEach(o => {
        const opt = document.createElement('option');
        opt.value = o.col;
        // The dagger marks a split whose levels share donors, so the difference
        // between the two correlations cannot be formally tested.
        opt.text = o.independent ? o.display_name : `${o.display_name} †`;
        select.appendChild(opt);
    });

    wrap.style.display = options.length ? '' : 'none';
    if (options.some(o => o.col === prev)) select.value = prev;
}


function currentSplitCol() {
    return document.getElementById('pca-split-select')?.value || '';
}


// Display name for a split-level value: collection ids map through display_ids
// (the legend already does this; readouts and captions must match).
function splitLevelName(splitCol, value) {
    const displayIds = pcaData.metadata?.display_ids || {};
    return (splitCol === 'collection_id' && displayIds[value]) ? displayIds[value] : value;
}


function renderPcaFilters(data) {
    const container = document.getElementById('pca-filters');
    container.innerHTML = '';

    const schemaMap = data.schema_map || {};
    const displayIds = data.display_ids || {};

    const truncatedFactors = data.truncated_factors || [];
    const factorRanges = data.factor_ranges || {};

    data.factor_cols.forEach(col => {
        const wrapper = document.createElement('div');
        wrapper.className = 'filter-group corr-filter-group';

        const label = document.createElement('div');
        // Use display_name from schema_map if available
        label.innerText = (schemaMap[col] && schemaMap[col].display_name) ? schemaMap[col].display_name : col;
        label.classList.add('font-bold', 'corr-filter-label');
        wrapper.appendChild(label);

        // Date factors get a range, not a list: one row per day means a checkbox
        // list would run to hundreds of entries (and used to be suppressed
        // entirely, leaving the time window unfilterable).
        if (factorRanges[col]) {
            wrapper.appendChild(_buildDateRangeControl(col, factorRanges[col]));
            container.appendChild(wrapper);
            return;
        }

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

        const commit = () => {
            const checked = Array.from(listDiv.querySelectorAll('input:checked')).map(c => c.value);
            if (checked.length > 0) {
                pcaData.filters[col] = checked;
            } else {
                delete pcaData.filters[col];
            }
            refreshCurrentView();
        };

        values.forEach(val => {
            const row = document.createElement('div');
            row.className = 'corr-filter-value-row';
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.value = val;
            cb.classList.add('corr-filter-checkbox');

            if (pcaData.filters[col] && pcaData.filters[col].includes(val)) {
                cb.checked = true;
            }

            cb.onchange = commit;

            const span = document.createElement('span');
            // For collection_id, show display_collection_id if available
            if (col === 'collection_id' && displayIds[val]) {
                span.innerText = displayIds[val];
            } else {
                span.innerText = val;
            }
            span.classList.add('text-sm');
            row.dataset.search = `${val} ${span.innerText}`.toLowerCase();

            row.appendChild(cb);
            row.appendChild(span);
            listDiv.appendChild(row);
        });

        // Long value lists (collections, above all) get a search box and
        // select-all/none, so restricting to a handful of donors stops being a
        // scroll-and-click chore.
        if (values.length > _CORR_SEARCHABLE_FROM) {
            wrapper.appendChild(_buildValueListTools(listDiv, commit));
        }

        wrapper.appendChild(listDiv);
        container.appendChild(wrapper);
    });

    renderSampleSummary();
}


// Value lists longer than this get a search box + select all/none.
const _CORR_SEARCHABLE_FROM = 8;


function _buildValueListTools(listDiv, commit) {
    const tools = document.createElement('div');
    tools.className = 'corr-filter-tools';

    const search = document.createElement('input');
    search.type = 'search';
    search.className = 'control-input text-xs';
    search.placeholder = 'Search…';
    search.oninput = () => {
        const q = search.value.trim().toLowerCase();
        listDiv.querySelectorAll('.corr-filter-value-row').forEach(row => {
            row.style.display = (!q || (row.dataset.search || '').includes(q)) ? '' : 'none';
        });
    };
    tools.appendChild(search);

    // Both act on the rows currently visible, so "all" after a search means
    // "all the ones I just narrowed to".
    const setVisible = (checked) => {
        listDiv.querySelectorAll('.corr-filter-value-row').forEach(row => {
            if (row.style.display === 'none') return;
            const cb = row.querySelector('input[type="checkbox"]');
            if (cb) cb.checked = checked;
        });
        commit();
    };

    const all = document.createElement('button');
    all.className = 'btn-discreet text-xxs';
    all.textContent = 'All';
    all.onclick = () => setVisible(true);
    tools.appendChild(all);

    const none = document.createElement('button');
    none.className = 'btn-discreet text-xxs';
    none.textContent = 'None';
    none.onclick = () => setVisible(false);
    tools.appendChild(none);

    return tools;
}


function _buildDateRangeControl(col, range) {
    const box = document.createElement('div');
    box.className = 'corr-filter-range';

    const current = pcaData.filters[col] || {};
    const inputs = {};

    ['min', 'max'].forEach(bound => {
        const row = document.createElement('label');
        row.className = 'corr-filter-range-row text-xs';
        row.textContent = bound === 'min' ? 'From' : 'To';

        const input = document.createElement('input');
        input.type = 'date';
        input.className = 'control-input text-xs';
        input.min = range.min;
        input.max = range.max;
        input.value = current[bound] || '';
        inputs[bound] = input;
        row.appendChild(input);
        box.appendChild(row);
    });

    const commit = () => {
        const lo = inputs.min.value;
        const hi = inputs.max.value;
        // An untouched (or fully reopened) range is no filter at all — keeping
        // it out of pcaData.filters keeps "Reset" and the summary honest.
        if (!lo && !hi) {
            delete pcaData.filters[col];
        } else {
            pcaData.filters[col] = { min: lo || null, max: hi || null };
        }
        refreshCurrentView();
    };
    inputs.min.onchange = commit;
    inputs.max.onchange = commit;

    const hint = document.createElement('div');
    hint.className = 'text-xxs corr-filter-note';
    hint.textContent = `Data spans ${range.min} to ${range.max} (${(range.n_values || 0).toLocaleString()} days). Both ends included.`;
    box.appendChild(hint);

    return box;
}


// Panel header: what the current selection actually is, in the tab's own unit.
function renderSampleSummary(sample) {
    const el = document.getElementById('corr-sample-summary');
    if (!el) return;

    const meta = pcaData.metadata || {};
    const unit = meta.unit || {};
    const s = sample || pcaData.lastSample || meta.sample || {};
    const grouping = (unit.grouping_display || []).join(' × ') || 'group';
    const fmt = (n) => (n === null || n === undefined) ? '?' : Number(n).toLocaleString();

    const bits = [`1 row = one ${grouping}`];
    if (s.groups_total != null) {
        bits.push(s.groups_selected === s.groups_total
            ? `${fmt(s.groups_total)} groups`
            : `${fmt(s.groups_selected)} of ${fmt(s.groups_total)} groups`);
    }
    // Only present once the PCA parquet has been rebuilt with group_size.
    if (s.videos_selected != null) {
        bits.push(`covering ${fmt(s.videos_selected)} videos`);
    }
    if (unit.min_group_size != null) {
        bits.push(`≥${fmt(unit.min_group_size)} videos each`);
    }
    el.textContent = bits.join(' · ') + '.';
}


// Record the sample counts a payload came back with, so the panel header tracks
// the current selection. Filter-immune views never report one.
function noteSampleFromPayload(payload) {
    if (payload && payload.sample) {
        pcaData.lastSample = payload.sample;
        renderSampleSummary(payload.sample);
    }
}


// The "Group differences" view reads whole-study artifacts written by the
// pca_refresh worker, so the Sample panel does not apply to it. Disable it
// visibly rather than letting it silently do nothing.
function applySamplePanelAvailability() {
    const meta = pcaData.metadata || {};
    const immune = (meta.filter_immune_views || []).includes(pcaData.currentView);

    const list = document.getElementById('pca-filters');
    const note = document.getElementById('corr-sample-disabled');
    const panel = document.getElementById('correlations-filter-panel');
    if (list) {
        list.style.opacity = immune ? '0.4' : '';
        list.style.pointerEvents = immune ? 'none' : '';
    }
    if (panel) {
        const reset = panel.querySelector('.btn-save');
        if (reset) reset.disabled = immune;
    }
    if (note) {
        note.style.display = immune ? '' : 'none';
        note.textContent = immune
            ? 'This view is computed over the whole study when the caches are rebuilt, so the sample selection below does not apply to it.'
            : '';
    }
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
    applySamplePanelAvailability();

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
                split_col: currentSplitCol() || undefined,
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

        noteSampleFromPayload(data);

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

    // Split comparison: one regression line per level instead of one over
    // everything, so the question becomes "does this hold for both?" rather
    // than "what does the whole selection look like".
    const split = payload.split || null;
    if (split) {
        renderSplitRegressions(split, traces, layout, dataPoints, groups, groupsKeys, colors, displayIds, colorLabel);
        renderSplitCaption(split, xTitle, yTitle, isCentered);
        Plotly.newPlot('pca-plot', traces, layout, { responsive: true, displayModeBar: true });
        document.getElementById('pca-plot').on('plotly_click', function (eventData) {
            const point = eventData.points[0];
            if (!point || !point.customdata) return;
            const factors = point.customdata;
            if (!factors || Object.keys(factors).length === 0) return;
            drillDownFromCorrelations(factors);
        });
        return;
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


// One dashed regression line per compared level, in that level's own point
// colour, spanning only that level's observed x range.
function renderSplitRegressions(split, traces, layout, dataPoints, groups, groupsKeys, colors, displayIds, colorLabel) {
    (split.levels || []).forEach(level => {
        if (!level.stats) return;
        const gName = (colorLabel === 'collection_id' && displayIds[level.value])
            ? displayIds[level.value] : level.value;
        const gi = groupsKeys.indexOf(gName);
        const color = colors[(gi >= 0 ? gi : 0) % colors.length];

        const xs = (gi >= 0 ? groups[gName].x : dataPoints.map(d => d.x));
        let xMin = Infinity, xMax = -Infinity;
        xs.forEach(x => { if (x < xMin) xMin = x; if (x > xMax) xMax = x; });
        if (!isFinite(xMin) || !isFinite(xMax)) return;

        const s = level.stats;
        traces.push({
            x: [xMin, xMax],
            y: [xMin, xMax].map(x => s.slope * x + s.intercept),
            mode: 'lines',
            type: 'scatter',
            name: `${gName}: r = ${s.r.toFixed(2)}`,
            line: { color: color, width: 2, dash: 'dash' },
            hoverinfo: 'none'
        });
    });

    const rows = (split.levels || []).filter(l => l.stats).map(l => {
        const s = l.stats;
        return `${splitLevelName(split.col, l.value)}:  r = ${s.r.toFixed(2)}   slope = ${s.slope.toFixed(2)} `
            + `[${s.ci_low.toFixed(2)}, ${s.ci_high.toFixed(2)}]   n = ${s.n.toLocaleString()}`;
    });
    if (rows.length) {
        layout.annotations.push({
            xref: 'paper', yref: 'paper',
            x: 0.02, y: 0.98,
            xanchor: 'left', yanchor: 'top',
            text: rows.join('<br>'),
            showarrow: false,
            font: { family: getCSSVar('--font-sans'), size: 12, color: getCSSVar('--white') },
            bgcolor: getCSSVar('--chart-badge-bg'),
            bordercolor: getCSSVar('--color-text-faint'), borderwidth: 1,
            align: 'left'
        });
    }
}


// Plain-language reading of the split, including — deliberately prominently —
// whether the difference could be tested at all.
function renderSplitCaption(split, xTitle, yTitle, isCentered) {
    const schemaMap = pcaData.metadata?.schema_map || {};
    const splitName = schemaMap[split.col]?.display_name || split.col;
    const usable = (split.levels || []).filter(l => l.stats);

    if (usable.length < 2) {
        setCaption(`Not enough groups on both sides of ${escapeHtml(splitName)} to compare.`);
        return;
    }

    const parts = [];
    const described = usable.map(l => {
        const r = l.stats.r;
        const dir = r >= 0 ? 'positive' : 'negative';
        return `for <strong>${escapeHtml(splitLevelName(split.col, l.value))}</strong> the association is `
            + `${describeStrength(r)} and ${dir} (r = ${r.toFixed(2)}, n = ${l.stats.n.toLocaleString()})`;
    });
    parts.push(`Comparing <strong>${escapeHtml(xTitle)}</strong> and <strong>${escapeHtml(yTitle)}</strong> `
        + `across ${escapeHtml(splitName)}: ${described.join('; ')}.`);

    const c = split.comparison || {};
    if (c.p !== null && c.p !== undefined) {
        const verdict = c.p < 0.05
            ? 'The two associations differ significantly'
            : 'The two associations are not significantly different';
        parts.push(`${verdict} (Fisher r-to-z, ${formatP(c.p)}).`);
    } else if (c.r_difference !== null && c.r_difference !== undefined) {
        parts.push(`The correlations differ by ${Math.abs(c.r_difference).toFixed(2)}, `
            + 'reported descriptively only.');
    }
    if (c.note) parts.push(escapeHtml(c.note));

    if ((split.levels_omitted || []).length) {
        parts.push(`Only the two largest levels are compared; `
            + `${split.levels_omitted.map(v => escapeHtml(splitLevelName(split.col, v))).join(', ')} `
            + `${split.levels_omitted.length === 1 ? 'is' : 'are'} not shown in the comparison.`);
    }
    if (isCentered) {
        parts.push('Both axes are centred within collection, so this describes variation inside a donor’s feed.');
    }

    setCaption(parts.join(' '));
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
                split_col: currentSplitCol() || undefined,
                center: !!document.getElementById('pca-center-toggle')?.checked
            })
        });
        const data = await res.json();

        if (data.error) {
            console.error(data.error);
            if (countEl) countEl.innerText = `Error: ${data.error}`;
            return;
        }

        noteSampleFromPayload(data);

        if (countEl) {
            countEl.innerText = `${data.count.toLocaleString()} observations`;
        }

        renderCorrelationHeatmap(data);

    } catch (e) {
        console.error(e);
        if (countEl) countEl.innerText = 'Error loading heatmap';
    }
}


// "Correct for noise" needs reliability estimates (the worker-written
// artifact) and has no defined meaning for a split difference — disable the
// checkbox with an explanation instead of letting it silently do nothing.
let _disattenuateTooltipDefault = null;

function applyDisattenuateAvailability(payload) {
    const cb = document.getElementById('pca-disattenuate');
    if (!cb) return;
    const relAvailable = Object.keys(payload.reliability || {}).length > 0;
    cb.disabled = !relAvailable || !!payload.split;
    if (cb.disabled) cb.checked = false;
    const wrap = cb.closest('.corr-checkbox-group');
    if (!wrap) return;
    if (_disattenuateTooltipDefault === null) {
        _disattenuateTooltipDefault = wrap.dataset.tooltip || '';
    }
    if (!relAvailable) {
        wrap.dataset.tooltip = 'No reliability estimates are available yet — they come from ' +
            'repeat-annotation evaluation runs. Until then, noise-corrected correlations cannot be computed.';
    } else if (payload.split) {
        wrap.dataset.tooltip = 'Correcting for noise is undefined for a difference between two ' +
            'correlations, so it is unavailable while a split is active.';
    } else {
        wrap.dataset.tooltip = _disattenuateTooltipDefault;
    }
}


function rerenderHeatmapFromCache() {
    if (pcaData.currentView === 'heatmap' && _lastHeatmapArgs) {
        renderCorrelationHeatmap(_lastHeatmapArgs);
    }
}


function renderCorrelationHeatmap(payload) {
    _lastHeatmapArgs = payload;
    applyDisattenuateAvailability(payload);
    const schemaMap = pcaData.metadata?.schema_map || {};
    const method = payload.method;
    const rSym = (method === 'spearman') ? 'ρ' : 'r';

    // Split mode swaps in the cellwise difference between the two levels. The
    // p/q matrices then test the DIFFERENCE (Fisher r-to-z) and are all-null for
    // a within-donor split, where that test does not apply.
    const split = payload.split || null;
    const rLabel = split
        ? `Δ${rSym}`
        : ((method === 'spearman') ? 'Spearman ρ' : 'Pearson r');
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
    const rM = pick(split ? split.delta_matrix : payload.matrix);
    const pM = pick(split ? split.p_matrix : payload.p_matrix);
    const qM = pick(split ? split.q_matrix : payload.q_matrix);
    const nM = pick(split ? split.n_matrix : payload.n_matrix);
    // Per-level values, for the hover ("a: r = .62 · b: r = -.10").
    const levelMs = split ? (split.levels || []).map(l => (
        { value: splitLevelName(split.col, l.value), m: pick(l.matrix) })) : [];

    // Map columns to display names
    const displayColumns = columns.map(col => {
        return (schemaMap[col] && schemaMap[col].display_name) ? schemaMap[col].display_name : col;
    });

    // Optional Spearman disattenuation: r ÷ √(rel_i × rel_j), clipped to ±1.
    // Cells without a reliability estimate for BOTH columns are blanked so
    // corrected and uncorrected values are never mixed in one picture.
    // Disattenuation divides a correlation by its measurement reliability; that
    // has no defined meaning for a difference between two correlations, so it
    // is off in split mode rather than silently applied to the wrong quantity.
    const reliability = payload.reliability || {};
    const disattenuate = !split
        && !!document.getElementById('pca-disattenuate')?.checked
        && Object.keys(reliability).length > 0;
    const relOf = (i) => reliability[columns[i]]?.group_r;
    let noRelCount = 0;
    const correctedM = disattenuate ? rM.map((row, i) => row.map((val, j) => {
        if (i === j) return val;
        if (val === null || val === undefined) return null;
        const ri = relOf(i), rj = relOf(j);
        if (!ri || !rj) { noRelCount++; return null; }
        const corrected = val / Math.sqrt(ri * rj);
        return Math.max(-1, Math.min(1, corrected));
    })) : null;
    const shownM = correctedM || rM;

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
    const rSymbol = split ? `Δ${rSym}` : rSym;
    const hoverText = rM.map((row, i) =>
        row.map((val, j) => {
            const head = `${displayColumns[i]} × ${displayColumns[j]}`;
            if (val === null || val === undefined) return `${head}<br>${rSymbol} undefined`;
            const parts = [`${rSymbol} = ${val.toFixed(3)}`];
            // Show what the difference is made of, not just its size.
            levelMs.forEach(lv => {
                const v = lv.m?.[i]?.[j];
                if (v !== null && v !== undefined && i !== j) {
                    parts.push(`${lv.value}: ${rSym} = ${v.toFixed(3)}`);
                }
            });
            if (disattenuate && correctedM) {
                const corr = correctedM[i][j];
                if (i !== j) {
                    parts.push(corr === null
                        ? 'corrected: no reliability estimate'
                        : `corrected ${rSymbol} = ${corr.toFixed(3)}`);
                }
            }
            if (nM?.[i]?.[j] !== undefined) parts.push(`n = ${nM[i][j]}`);
            if (i !== j && pM?.[i]?.[j] !== null && pM?.[i]?.[j] !== undefined) {
                parts.push(formatP(pM[i][j]));
                const q = qM?.[i]?.[j];
                if (q !== null && q !== undefined) {
                    parts.push('q = ' + q.toFixed(3).replace(/^0/, ''));
                }
            }
            if (disattenuate && reliability[columns[i]] && i === j) {
                const rel = reliability[columns[i]];
                parts.push(`reliability ≈ ${rel.group_r} (${rel.source})`);
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
        // A difference between two correlations spans ±2, so the split scale is
        // widened rather than saturating every strong divergence at the ends.
        zmin: split ? -2 : -1,
        zmax: split ? 2 : 1,
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

    const splitTitle = split && split.levels && split.levels.length === 2
        ? `Correlation difference: ${splitLevelName(split.col, split.levels[0].value)} − `
            + `${splitLevelName(split.col, split.levels[1].value)}`
        : 'Correlation Matrix';
    const layout = {
        title: {
            text: splitTitle,
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

    renderHeatmapCaption(payload, maskNonSig, maskedCount, disattenuate, noRelCount);

    Plotly.newPlot('pca-plot', [trace], layout, { responsive: true, displayModeBar: true });
}


function renderHeatmapCaption(payload, maskNonSig, maskedCount, disattenuate, noRelCount) {
    const methodName = payload.method === 'spearman' ? 'Spearman (rank-based)' : 'Pearson (linear)';
    const parts = [];

    const split = payload.split || null;
    if (split && (split.levels || []).length === 2) {
        const schemaMap = pcaData.metadata?.schema_map || {};
        const splitName = schemaMap[split.col]?.display_name || split.col;
        const [a, b] = split.levels;
        const aName = splitLevelName(split.col, a.value);
        const bName = splitLevelName(split.col, b.value);
        parts.push(`Each cell is the difference between two ${methodName} correlations, ` +
            `split by ${escapeHtml(splitName)}: ` +
            `<strong>${escapeHtml(aName)}</strong> (${a.n_groups.toLocaleString()} groups) ` +
            `minus <strong>${escapeHtml(bName)}</strong> (${b.n_groups.toLocaleString()} groups). ` +
            `Red means the association is stronger for ${escapeHtml(aName)}, blue for ${escapeHtml(bName)}; ` +
            `hover a cell for both underlying correlations.`);
        parts.push(split.independent
            ? 'Each cell also reports a Fisher r-to-z test of the difference, with a Benjamini–Hochberg adjusted q across all pairs.'
            : '<span class="corr-caption-warning">No p-values are shown.</span>');
        if (split.note) parts.push(escapeHtml(split.note));
        if ((split.levels_omitted || []).length) {
            parts.push(`Only the two largest levels are compared; ` +
                `${split.levels_omitted.map(v => escapeHtml(splitLevelName(split.col, v))).join(', ')} not shown.`);
        }
        if (maskNonSig) {
            parts.push(split.independent
                ? `${maskedCount.toLocaleString()} cells whose difference is not significant (q ≥ .05) are blanked.`
                : 'Significance masking has nothing to act on here — no test of the difference is available.');
        }
        if (payload.centered) {
            parts.push('Values are centered within each collection (within-donor associations).');
        }
        setCaption(parts.join(' '));
        return;
    }

    parts.push(`${methodName} correlations across ${payload.count.toLocaleString()} groups; ` +
        `each cell also reports its pairwise n, p, and a Benjamini–Hochberg adjusted q ` +
        `(controls the share of false positives among all pairs tested).`);
    if (disattenuate) {
        const sources = [...new Set(Object.values(payload.reliability || {})
            .map(r => r.source).filter(Boolean))];
        parts.push(`Colours show correlations corrected for annotation noise ` +
            `(Spearman disattenuation; reliability from ${sources.join(' and ') || 'saved estimates'}, ` +
            `scaled to groups of ≥${payload.reliability_k || '?'} videos).` +
            (noRelCount ? ` ${noRelCount.toLocaleString()} cells lack a reliability estimate and are blanked.` : '') +
            ' <span class="corr-caption-warning">Corrected values are approximate upper bounds.</span>');
    }
    if (maskNonSig) {
        parts.push(`${maskedCount.toLocaleString()} non-significant cells (q ≥ .05, of the observed r) are blanked.`);
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

    const reliability = payload.reliability || {};
    const rows = [["variable_a", "variable_b", "family_a", "family_b",
                   "method", "r", "n", "p", "q", "r_disattenuated",
                   "reliability_a", "reliability_b", "centered"]];
    const cols = payload.columns;
    const fams = payload.families || cols;
    for (let i = 0; i < cols.length; i++) {
        for (let j = i + 1; j < cols.length; j++) {
            const r = payload.matrix?.[i]?.[j];
            const ra = reliability[cols[i]]?.group_r;
            const rb = reliability[cols[j]]?.group_r;
            let corrected = null;
            if (r !== null && r !== undefined && ra && rb) {
                corrected = Math.max(-1, Math.min(1, r / Math.sqrt(ra * rb))).toFixed(4);
            }
            rows.push([
                dname(cols[i]), dname(cols[j]), fams[i], fams[j],
                payload.method,
                r,
                payload.n_matrix?.[i]?.[j],
                payload.p_matrix?.[i]?.[j],
                payload.q_matrix?.[i]?.[j],
                corrected,
                ra ?? null,
                rb ?? null,
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
    if (countEl) countEl.innerText = 'Loading group statistics...';

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

        if (countEl) countEl.innerText = `${(data.n_groups || 0).toLocaleString()} groups`;
        renderGroupStats(data);

    } catch (e) {
        console.error(e);
        if (countEl) countEl.innerText = 'Error loading group statistics';
    }
}


function renderGroupStats(data) {
    const plotDiv = document.getElementById('pca-plot');
    const schemaMap = pcaData.metadata?.schema_map || {};
    const dname = (c) => (schemaMap[c] && schemaMap[c].display_name) ? schemaMap[c].display_name : c;
    const fmtP = (p) => (p === null || p === undefined) ? '—' : formatP(p).replace(/^p /, '');
    const fmtN = (v, d) => (v === null || v === undefined) ? '—' : Number(v).toFixed(d);

    const anova = [...(data.anova || [])].sort((a, b) => (b.eta2 || 0) - (a.eta2 || 0));
    const perma = [...(data.permanova || [])].sort((a, b) => (a.q ?? 1) - (b.q ?? 1));

    const generated = data.generated_at
        ? new Date(data.generated_at * 1000).toLocaleString() : 'unknown time';

    let html = `<div class="corr-table-wrap">`;
    html += `<p class="text-xs">Precomputed for the whole study (${generated}) — ` +
        `the filter panel, centering and variable preferences do <b>not</b> apply here. ` +
        `q-values are Benjamini–Hochberg adjusted across each table.</p>`;

    html += `<h3 class="text-h3">Which factors move single components? (one-way ANOVA)</h3>`;
    if (!anova.length) {
        html += `<p>No testable factor × component pairs in this study.</p>`;
    } else {
        html += `<table class="collection-table"><thead><tr>` +
            `<th>Factor</th><th>Component</th><th>η²</th><th>Effect</th><th>ω²</th>` +
            `<th>F</th><th>p</th><th>q</th><th>KW q</th><th>n</th><th>Levels</th>` +
            `</tr></thead><tbody>`;
        anova.forEach(row => {
            const sig = row.q !== null && row.q !== undefined && row.q < 0.05;
            html += `<tr${sig ? ' class="font-bold"' : ''}>` +
                `<td>${escapeHtml(dname(row.factor))}</td>` +
                `<td>${escapeHtml(dname(row.component))}</td>` +
                `<td>${fmtN(row.eta2, 3)}</td>` +
                `<td>${escapeHtml(row.magnitude || '—')}</td>` +
                `<td>${fmtN(row.omega2, 3)}</td>` +
                `<td>${fmtN(row.F, 1)}</td>` +
                `<td>${fmtP(row.p)}</td>` +
                `<td>${fmtP(row.q)}</td>` +
                `<td>${fmtP(row.kw_q)}</td>` +
                `<td>${row.n}</td><td>${row.levels}</td></tr>`;
        });
        html += `</tbody></table>`;
    }

    html += `<h3 class="text-h3">Do whole variable profiles differ by factor? (PERMANOVA)</h3>`;
    if (!perma.length) {
        html += `<p>No testable family × factor pairs in this study.</p>`;
    } else {
        html += `<table class="collection-table"><thead><tr>` +
            `<th>Variable family</th><th>Factor</th><th>pseudo-F</th><th>p</th><th>q</th>` +
            `<th>n</th><th>Levels</th><th>Components</th><th>Permutations</th>` +
            `</tr></thead><tbody>`;
        perma.forEach(row => {
            const sig = row.q !== null && row.q !== undefined && row.q < 0.05;
            html += `<tr${sig ? ' class="font-bold"' : ''}>` +
                `<td>${escapeHtml(dname(row.family))}</td>` +
                `<td>${escapeHtml(dname(row.factor))}</td>` +
                `<td>${fmtN(row.pseudo_F, 2)}</td>` +
                `<td>${fmtP(row.p)}</td>` +
                `<td>${fmtP(row.q)}</td>` +
                `<td>${row.n}</td><td>${row.levels}</td>` +
                `<td>${row.n_components}</td><td>${row.permutations}</td></tr>`;
        });
        html += `</tbody></table>`;
    }
    html += `</div>`;

    plotDiv.innerHTML = html;
    renderGroupStatsCaption(anova, perma, schemaMap);
}


function renderGroupStatsCaption(anova, perma, schemaMap) {
    const dname = (c) => (schemaMap[c] && schemaMap[c].display_name) ? schemaMap[c].display_name : c;
    const parts = [];
    const sigAnova = anova.filter(r => r.q !== null && r.q !== undefined && r.q < 0.05);
    if (anova.length) {
        parts.push(`${sigAnova.length} of ${anova.length} factor × component tests are ` +
            `significant after correction (q < .05).`);
        const top = sigAnova[0] || anova[0];
        if (top && top.eta2 !== null && top.eta2 !== undefined) {
            parts.push(`Largest effect: <b>${escapeHtml(dname(top.factor))}</b> explains ` +
                `${(top.eta2 * 100).toFixed(0)}% of the variation in ` +
                `<b>${escapeHtml(dname(top.component))}</b> (a ${top.magnitude} effect, ` +
                `${formatP(top.q)} after correction).`);
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
    parts.push('<span class="text-xs">η² = share of a component\'s variance explained by the factor ' +
        '(.01 small, .06 medium, .14 large). KW q = rank-based Kruskal–Wallis check — trust it ' +
        'over the ANOVA q when groups are small or skewed. PERMANOVA compares each variable\'s ' +
        'whole component profile, never mixing different variables\' PCA bases.</span>');
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
