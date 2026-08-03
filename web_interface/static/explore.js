// Explorer V2 Logic (Dual Slice Comparison)

let explorerDataV2 = {
    metadata: null,
    filters1: {},
    filters2: {},
    searchQuery1: "",
    searchQuery2: "",
    activeStudy: null,
    // Cache for stats
    stats1: null,
    count1: 0,
    stats2: null,
    count2: 0,
    sortMode: 'total',
    dualSliceMode: false,
    filterPanel1Visible: true,
    filterPanel2Visible: true
};


window.exploreToggleSidebar = function (which) {
    // which: 1 = left (slice 1), 2 = right (slice 2). Defaults to 1 for back-compat.
    const sliceIdx = (which === 2) ? 2 : 1;
    const key = sliceIdx === 2 ? 'filterPanel2Visible' : 'filterPanel1Visible';
    explorerDataV2[key] = !explorerDataV2[key];
    const visible = explorerDataV2[key];

    const panel = document.getElementById(sliceIdx === 2 ? 'explorer-v2-slice2-panel' : 'explorer-v2-slice1-panel');
    if (panel) {
        // Slice 2 panel is only ever visible when dual mode is on
        const allowed = (sliceIdx === 2) ? explorerDataV2.dualSliceMode : true;
        panel.style.display = (visible && allowed) ? 'flex' : 'none';
    }

    // Trigger Plotly resize so charts fill the new width
    requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
};

// Initialization
document.addEventListener('DOMContentLoaded', function () {
    // Only init if tab exists
    if (document.getElementById('explore')) {
        // Initial hydration: adopt the current global study once it's ready.
        if (window.studyState && window.studyState.ready) {
            window.studyState.ready.then(() => {
                if (window.studyState.current) {
                    applyExplorerActiveStudy(window.studyState.current, { reload: true });
                } else {
                    renderExplorerNoStudyState();
                }
            });
        }

        // Respond to global study changes.
        document.addEventListener('study:changed', (e) => {
            const next = e.detail && e.detail.study;
            applyExplorerActiveStudy(next, { reload: true });
            if (!next) renderExplorerNoStudyState();
        });

        // Search Input Listeners
        const searchInput1 = document.getElementById('explorer-v2-search-input-1');
        if (searchInput1) {
            let debounceTimer;
            searchInput1.addEventListener('input', (e) => {
                const val = e.target.value;
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(() => {
                    explorerDataV2.searchQuery1 = val;
                    updateExplorerV2Stats(1); // Trigger Slice 1
                }, 500);
            });
        }

        const searchInput2 = document.getElementById('explorer-v2-search-input-2');
        if (searchInput2) {
            let debounceTimer;
            searchInput2.addEventListener('input', (e) => {
                const val = e.target.value;
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(() => {
                    explorerDataV2.searchQuery2 = val;
                    updateExplorerV2Stats(2); // Trigger Slice 2
                }, 500);
            });
        }
    }
});

function setExplorerV2SliceMode(isDual) {
    if (explorerDataV2.dualSliceMode === isDual) return;
    explorerDataV2.dualSliceMode = isDual;

    // Update segmented control styling
    const singleBtn = document.getElementById('explorer-v2-toggle-single');
    const dualBtn = document.getElementById('explorer-v2-toggle-dual');
    if (singleBtn && dualBtn) {
        singleBtn.classList.toggle('active', !isDual);
        dualBtn.classList.toggle('active', isDual);
    }

    // Show/hide Slice 2 panel (respecting independent slice 2 visibility flag)
    const slice2Panel = document.getElementById('explorer-v2-slice2-panel');
    if (slice2Panel) slice2Panel.style.display = (isDual && explorerDataV2.filterPanel2Visible) ? 'flex' : 'none';

    // Show/hide the dedicated right-panel toggle button (desktop) and the
    // slice-2 "Customize variables" gear that sits beside it.
    const slice2ToggleBtn = document.getElementById('explore-sidebar-toggle-2');
    if (slice2ToggleBtn) slice2ToggleBtn.style.display = isDual ? 'flex' : 'none';
    const slice2Gear = document.getElementById('explorer-v2-filter-gear-2');
    if (slice2Gear) slice2Gear.style.display = isDual ? 'inline-flex' : 'none';

    // Show/hide the mobile "Filters 2" trigger; relabel the slice-1 trigger so the two are distinguishable in dual mode
    const mobile1 = document.getElementById('explorer-v2-mobile-filters-1');
    if (mobile1) mobile1.textContent = isDual ? 'Filters 1' : 'Filters';
    const mobile2 = document.getElementById('explorer-v2-mobile-filters-2');
    if (mobile2) mobile2.style.display = isDual ? '' : 'none';

    // Show/hide 'vs' label and Slice 2 count
    const vsLabel = document.getElementById('explorer-v2-vs-label');
    if (vsLabel) vsLabel.style.display = isDual ? '' : 'none';
    const count2 = document.getElementById('explorer-v2-count-2');
    if (count2) count2.style.display = isDual ? '' : 'none';

    // Show/hide Slice 2 sort option
    const sortSlice2 = document.getElementById('explorer-v2-sort-slice2');
    if (sortSlice2) sortSlice2.style.display = isDual ? '' : 'none';

    // If sort is currently 'slice2' and switching to single, reset to 'total'
    if (!isDual && explorerDataV2.sortMode === 'slice2') {
        explorerDataV2.sortMode = 'total';
        const sortSelect = document.getElementById('explorer-v2-sort');
        if (sortSelect) sortSelect.value = 'total';
    }

    // Re-fetch stats
    updateExplorerV2Stats();
}


// Honest empty state when the user has no accessible study: without this the
// filter panels keep their static "Loading filters..." placeholder forever.
function renderExplorerNoStudyState() {
    const msg = '<div style="text-align: center; margin-top: 20px; color: var(--color-text-muted);">'
        + 'No study selected. Studies shared with you appear in the study picker in the header — '
        + 'if it\'s empty, ask the researcher who invited you for access.</div>';
    const c1 = document.getElementById('explorer-v2-filters-1');
    const c2 = document.getElementById('explorer-v2-filters-2');
    if (c1) c1.innerHTML = msg;
    if (c2) c2.innerHTML = msg;
    const stats = document.getElementById('explorer-v2-stats');
    if (stats) stats.innerHTML = msg;
}


function applyExplorerActiveStudy(studyName, options = {}) {
    const { reload = true } = options;
    if (studyName === explorerDataV2.activeStudy && !reload) return;

    explorerDataV2.activeStudy = studyName || null;
    explorerDataV2.filters1 = {};
    explorerDataV2.filters2 = {};
    explorerDataV2.stats1 = null;
    explorerDataV2.count1 = 0;
    explorerDataV2.stats2 = null;
    explorerDataV2.count2 = 0;

    if (studyName) {
        loadExplorerV2Metadata();
    }
}

async function loadExplorerV2Metadata() {
    if (!explorerDataV2.activeStudy) return;

    const filterContainer1 = document.getElementById('explorer-v2-filters-1');
    const filterContainer2 = document.getElementById('explorer-v2-filters-2');

    const funLoader = '<div class="fun-loader-container"><div class="fun-loader"><div></div><div></div><div></div><div></div><div></div></div><div class="loading-text">Loading...</div></div>';
    filterContainer1.innerHTML = funLoader;
    filterContainer2.innerHTML = funLoader;

    const study = explorerDataV2.activeStudy;
    const studyParam = encodeURIComponent(study);

    // Kick off both requests in parallel. The base call hits a fast disk-cache
    // path that doesn't need to load the recoded parquet; the overlay call
    // pays the parquet load (User Tags, Has Annotation, Machine Annotations)
    // and resolves a moment later. We render filters as soon as base lands.
    const basePromise = fetch(`/api/explore/metadata/base?study=${studyParam}`).then(r => r.json());
    const overlayPromise = fetch(`/api/explore/metadata/overlay?study=${studyParam}`).then(r => r.json());

    let baseData;
    try {
        baseData = await basePromise;
    } catch (e) {
        console.error(e);
        filterContainer1.innerHTML = '<div style="color:var(--color-danger); text-align:center;">Failed to load metadata</div>';
        filterContainer2.innerHTML = '<div style="color:var(--color-danger); text-align:center;">Failed to load metadata</div>';
        return;
    }

    if (baseData.error) {
        const errHtml = `<div style="color:var(--color-danger); text-align:center;">${escapeHtml(baseData.error)}</div>`;
        filterContainer1.innerHTML = errHtml;
        filterContainer2.innerHTML = errHtml;
        return;
    }

    // Stop processing if the user has switched studies while we were waiting.
    if (explorerDataV2.activeStudy !== study) return;

    const infoSpan = document.getElementById('explorer-v2-file-info');
    if (infoSpan) {
        if (baseData.source_file && baseData.source_file_modified) {
            infoSpan.innerText = `Using file: ${baseData.source_file} - saved ${fypFmtDateTime(baseData.source_file_modified)}`;
        } else {
            infoSpan.innerText = "";
        }
    }

    explorerDataV2.metadata = baseData;
    renderFiltersV2(baseData, 1);
    renderFiltersV2(baseData, 2);

    updateExplorerV2Stats();

    // Merge in the user-specific overlay once it arrives.
    overlayPromise.then(overlay => {
        if (!overlay || overlay.error) return;
        if (explorerDataV2.activeStudy !== study) return;
        mergeOverlayIntoMetadataV2(overlay);
        renderFiltersV2(explorerDataV2.metadata, 1);
        renderFiltersV2(explorerDataV2.metadata, 2);
    }).catch(e => {
        console.error("Overlay metadata fetch failed:", e);
    });
}


function mergeOverlayIntoMetadataV2(overlay) {
    const m = explorerDataV2.metadata;
    if (!m || !overlay) return;

    Object.assign(m, overlay.columns || {});

    if (!m.schema_map) m.schema_map = {};
    Object.assign(m.schema_map, overlay.schema_map || {});

    if (!m.filter_priority) m.filter_priority = [];
    if (!m.display_priority) m.display_priority = [];

    // Prepend dynamic columns in reverse so the final order matches the
    // overlay's filter_priority_prepend order.
    (overlay.filter_priority_prepend || []).slice().reverse().forEach(col => {
        const idx = m.filter_priority.indexOf(col);
        if (idx > -1) m.filter_priority.splice(idx, 1);
        m.filter_priority.unshift(col);
    });
    (overlay.display_priority_prepend || []).slice().reverse().forEach(col => {
        const idx = m.display_priority.indexOf(col);
        if (idx > -1) m.display_priority.splice(idx, 1);
        m.display_priority.unshift(col);
    });

    if (overlay.stats_overlay && Object.keys(overlay.stats_overlay).length > 0) {
        if (!m.total_stats) m.total_stats = {};
        Object.assign(m.total_stats, overlay.stats_overlay);
    }
}

// Initialize expanded state (sections default to collapsed)
if (!explorerDataV2.expandedFilters1) {
    try {
        explorerDataV2.expandedFilters1 = JSON.parse(localStorage.getItem('explorer_expanded_filters_1') || '[]');
    } catch (e) { explorerDataV2.expandedFilters1 = []; }
}
if (!explorerDataV2.expandedFilters2) {
    try {
        explorerDataV2.expandedFilters2 = JSON.parse(localStorage.getItem('explorer_expanded_filters_2') || '[]');
    } catch (e) { explorerDataV2.expandedFilters2 = []; }
}

// Mount the "Customize variables" gear for a filter slice next to that slice's
// show/hide toggle in the control bar. Re-run safe (removes any prior gear for
// the slice so the customized-dot indicator stays fresh). The slice-2 gear
// tracks dual-slice visibility since its toggle only exists in dual mode.
function mountFilterGearV2(metadata, sliceId) {
    if (!(window.VariablePrefs && metadata && metadata.all_variables_order)) return;
    const toggle = document.getElementById(
        sliceId === 2 ? 'explore-sidebar-toggle-2' : 'explore-sidebar-toggle');
    if (!toggle) return;

    const gearId = `explorer-v2-filter-gear-${sliceId}`;
    const existing = document.getElementById(gearId);
    if (existing) existing.remove();

    const gear = VariablePrefs.gearButton('filter', () => {
        VariablePrefs.openPanel({
            surface: 'filter',
            title: 'Customize filter variables',
            allOrder: metadata.all_variables_order.filter(c => metadata[c]),
            globalList: metadata.filter_priority || [],
            schemaMap: metadata.schema_map || {},
            onApply: () => {
                renderFiltersV2(metadata, sliceId);
                if (explorerDataV2.dualSliceMode) renderFiltersV2(metadata, sliceId === 1 ? 2 : 1);
            },
        });
    });
    gear.id = gearId;
    // Restore the button's own display (must match gearButton's inline-flex) —
    // setting it to '' would clear it and let the block SVG wrap the dot below.
    if (sliceId === 2) gear.style.display = explorerDataV2.dualSliceMode ? 'inline-flex' : 'none';
    toggle.insertAdjacentElement('afterend', gear);
}


function renderFiltersV2(metadata, sliceId) {
    const container = document.getElementById(`explorer-v2-filters-${sliceId}`);
    container.innerHTML = '';

    // Per-user composition: (global ∪ include) − exclude over the canonical
    // candidate order. Dynamic prepends (user tags etc.) ride along as
    // non-schema extras inside filter_priority.
    const priority = (window.VariablePrefs && metadata.all_variables_order)
        ? VariablePrefs.effective('filter', metadata.all_variables_order, metadata.filter_priority || [])
        : metadata.filter_priority;
    let availableCols = [];

    if (priority && priority.length > 0) {
        availableCols = priority.filter(c => metadata[c]);
    } else {
        availableCols = Object.keys(metadata).sort().filter(c => c !== 'total_stats');
    }

    // "Customize variables" gear for this user's filter set — mounted next to
    // the panel show/hide toggle in the control bar (see mountFilterGearV2).
    mountFilterGearV2(metadata, sliceId);

    // Hide categorical/list filters with 0 or 1 unique values (no filtering possible)
    availableCols = availableCols.filter(col => {
        const info = metadata[col];
        if (info && (info.type === 'category' || info.type === 'list') && info.values && info.values.length <= 1) {
            return false;
        }
        return true;
    });

    const schemaMap = metadata.schema_map || {};

    // Group by Section
    const sections = {};
    const generalSection = "General";

    availableCols.forEach(col => {
        let section = generalSection;
        if (schemaMap[col] && schemaMap[col].section) {
            section = schemaMap[col].section;
            if (!section || section.trim() === "") section = generalSection;
        }
        if (!sections[section]) sections[section] = [];
        sections[section].push(col);
    });

    // Sort Sections by the backend's hard-coded section_order; sections not
    // listed fall after, alphabetically. Variables within a section already
    // arrive pre-sorted (categorical-before-numerical, then alphabetical).
    const sectionOrder = metadata.section_order || [];
    const sectionRank = (secName) => {
        const i = sectionOrder.indexOf(secName);
        return i === -1 ? sectionOrder.length : i;
    };
    let sectionNames = Object.keys(sections).sort((a, b) => {
        const ra = sectionRank(a), rb = sectionRank(b);
        if (ra !== rb) return ra - rb;
        return a.localeCompare(b);
    });

    // Move "Annotation Status" to the end
    const annIdx = sectionNames.indexOf('Annotation Status');
    if (annIdx > -1) {
        sectionNames.splice(annIdx, 1);
        sectionNames.push('Annotation Status');
    }

    // Render Sections (default collapsed, store expanded list)
    const expandedList = sliceId === 1 ? explorerDataV2.expandedFilters1 : explorerDataV2.expandedFilters2;
    const storageKey = `explorer_expanded_filters_${sliceId}`;
    const filters = sliceId === 1 ? explorerDataV2.filters1 : explorerDataV2.filters2;

    // Lazily build a section's per-column widgets the first time it's expanded.
    // Defers heavy DOM (sliders, checkbox lists) until the user actually opens
    // the section, which is the dominant cost when a study has many filters.
    const populateSectionBody = (body, vars) => {
        if (body.dataset.populated === '1') return;
        body.dataset.populated = '1';
        const inner = document.createDocumentFragment();
        vars.forEach(col => {
            const wrapper = renderFilterColumnV2(col, metadata, sliceId);
            if (wrapper) inner.appendChild(wrapper);
        });
        body.appendChild(inner);
    };

    const fragment = document.createDocumentFragment();

    sectionNames.forEach(sec => {
        const vars = sections[sec];
        if (vars.length === 0) return;

        // Check if any filter in this section is active
        const hasActiveFilter = vars.some(col => !!filters[col]);

        // Section Container
        const sectionDiv = document.createElement('div');
        sectionDiv.className = 'filter-section';
        sectionDiv.dataset.sliceId = sliceId;
        sectionDiv.dataset.columns = JSON.stringify(vars);
        sectionDiv.style.marginBottom = '10px';
        sectionDiv.style.border = '1px solid var(--color-border)';
        sectionDiv.style.borderRadius = '4px';
        sectionDiv.style.overflow = 'hidden';

        // Header
        const header = document.createElement('div');
        header.className = 'filter-section-header font-bold' + (hasActiveFilter ? ' has-active-filter' : '');
        header.style.padding = '8px 10px';
        header.style.cursor = 'pointer';
        header.style.color = 'var(--color-text-primary)';
        header.style.userSelect = 'none';
        header.style.display = 'flex';
        header.style.alignItems = 'center';

        const isExpanded = expandedList.includes(sec);
        const arrow = isExpanded ? '&#9662;' : '&#9656;'; // Down vs Right

        header.innerHTML = `<span style="margin-right:8px; width:15px; display:inline-block;">${arrow}</span> ${sec}`;

        // Body (Variables)
        const body = document.createElement('div');
        body.style.padding = '10px';
        body.style.background = 'var(--color-bg-surface)';
        body.style.display = isExpanded ? 'block' : 'none';

        // Toggle Logic
        header.onclick = () => {
            const currentlyHidden = body.style.display === 'none';
            if (currentlyHidden) {
                populateSectionBody(body, vars);
                body.style.display = 'block';
                header.innerHTML = `<span style="margin-right:8px; width:15px; display:inline-block;">&#9662;</span> ${sec}`;
                // Add to expanded list
                if (!expandedList.includes(sec)) expandedList.push(sec);
            } else {
                body.style.display = 'none';
                header.innerHTML = `<span style="margin-right:8px; width:15px; display:inline-block;">&#9656;</span> ${sec}`;
                // Remove from expanded list
                const idx = expandedList.indexOf(sec);
                if (idx > -1) expandedList.splice(idx, 1);
            }
            // Persist
            localStorage.setItem(storageKey, JSON.stringify(expandedList));
        };

        sectionDiv.appendChild(header);
        sectionDiv.appendChild(body);

        if (isExpanded) {
            populateSectionBody(body, vars);
        }

        fragment.appendChild(sectionDiv);
    });

    container.appendChild(fragment);
}


// Builds the DOM subtree for a single filter (slider / checkbox list) without
// inserting it. Extracted so renderFiltersV2 can defer construction until a
// section is actually expanded.
function renderFilterColumnV2(col, metadata, sliceId) {
    const info = metadata[col];
    if (!info) return null;

    const wrapper = document.createElement('div');
    wrapper.className = 'filter-group';
    wrapper.style.marginBottom = '15px';
    wrapper.style.borderBottom = '1px solid var(--color-border-subtle)';
    wrapper.style.paddingBottom = '10px';

    const label = document.createElement('label');

    let displayName = col;
    if (metadata.schema_map && metadata.schema_map[col] && metadata.schema_map[col].display_name) {
        displayName = metadata.schema_map[col].display_name;
    }

    label.innerText = displayName;
    label.classList.add('font-bold');
    label.style.display = 'block';
    label.style.marginBottom = '5px';
    label.style.color = 'var(--color-text-primary)';
    wrapper.appendChild(label);

            if (info.type === 'number') {
                const sliderDiv = document.createElement('div');
                sliderDiv.style.marginBottom = '10px';
                sliderDiv.style.marginLeft = '5px';
                sliderDiv.style.marginRight = '5px';
                wrapper.appendChild(sliderDiv);

                // Min/Max Labels
                const labelRow = document.createElement('div');
                labelRow.style.display = 'flex';
                labelRow.style.justifyContent = 'space-between';
                labelRow.classList.add('text-sm');
                labelRow.style.color = 'var(--color-text-muted)';
                labelRow.style.marginTop = '-5px';

                const minLabel = document.createElement('span');
                const maxLabel = document.createElement('span');

                labelRow.appendChild(minLabel);
                labelRow.appendChild(maxLabel);
                wrapper.appendChild(labelRow);

                // Frequency-scaled slider: backend-supplied percentile pivots
                // become non-linear noUiSlider range stops, so equal slider
                // travel covers equal data mass and all values stay in original
                // units. Falls back to log/linear scaling without quantiles.
                const qRange = buildQuantileSliderRange(info);
                const useLog = !qRange && info.log === true && info.min >= 0;
                const logOff = (info.log_offset > 0) ? info.log_offset : 1;
                const toLog = (v) => Math.log10(v + logOff);
                const fromLog = (v) => Math.max(0, Math.pow(10, v) - logOff);
                const sliderToValue = (raw) => qRange ? raw : (useLog ? fromLog(raw) : raw);

                // Current Values (linear space)
                let currentMin = info.min;
                let currentMax = info.max;

                const filters = sliceId === 1 ? explorerDataV2.filters1 : explorerDataV2.filters2;

                if (filters[col] && filters[col].value) {
                    if (filters[col].value.min !== undefined) currentMin = filters[col].value.min;
                    if (filters[col].value.max !== undefined) currentMax = filters[col].value.max;
                }

                // Formatting Helper. A capped top bound (extreme outliers above
                // the 99th percentile) renders open-ended: "0.056+".
                const fmt = (n) => formatMetricNumber(n);
                const fmtMax = (n) => (info.max_capped && n >= info.max) ? fmt(n) + '+' : fmt(n);

                minLabel.innerText = fmt(currentMin);
                maxLabel.innerText = fmtMax(currentMax);

                // Slider range and start values
                const sliderMin = useLog ? toLog(info.min) : info.min;
                const sliderMax = useLog ? toLog(info.max) : info.max;
                const sliderStartMin = useLog ? toLog(currentMin) : currentMin;
                const sliderStartMax = useLog ? toLog(currentMax) : currentMax;

                // Initialize Slider
                if (typeof noUiSlider !== 'undefined') {
                    if (info.min >= info.max) {
                        sliderDiv.style.display = 'none';
                    } else {
                        noUiSlider.create(sliderDiv, {
                            start: [sliderStartMin, sliderStartMax],
                            connect: true,
                            range: qRange || {
                                'min': sliderMin,
                                'max': sliderMax
                            },
                            step: useLog ? (sliderMax - sliderMin) / 200 : undefined,
                            // Full float precision: the default format rounds to
                            // 2 decimals, which destroys per-play ratio values.
                            format: { to: (v) => String(v), from: (v) => Number(v) },
                        });

                        sliderDiv.noUiSlider.on('update', function (values, handle) {
                            const display = sliderToValue(parseFloat(values[handle]));
                            if (handle === 0) {
                                minLabel.innerText = fmt(display);
                            } else {
                                maxLabel.innerText = fmtMax(display);
                            }
                        });

                        sliderDiv.noUiSlider.on('change', function (values, handle) {
                            const vMin = sliderToValue(parseFloat(values[0]));
                            const vMax = sliderToValue(parseFloat(values[1]));

                            const f = sliceId === 1 ? explorerDataV2.filters1 : explorerDataV2.filters2;

                            // Only apply bounds that actually bind. A handle at
                            // the (possibly capped) top applies no upper bound,
                            // so outliers above the cap stay included.
                            const atMin = vMin <= info.min;
                            const atMax = vMax >= info.max;
                            if (atMin && atMax) {
                                delete f[col];
                            } else {
                                if (!f[col]) f[col] = { type: 'number', value: {} };
                                f[col].value = {};
                                if (!atMin) f[col].value.min = vMin;
                                if (!atMax) f[col].value.max = vMax;
                            }

                            updateExplorerV2Stats(sliceId);
                            updateFilterSectionHighlights(sliceId);
                        });
                    }
                } else {
                    minLabel.innerText = "Error: Slider lib missing";
                }
            } else if (info.type === 'category' || info.type === 'list') {
                // Wrap label in header row with sort toggle
                const headerRow = document.createElement('div');
                headerRow.style.cssText = 'display:flex; align-items:center; justify-content:space-between;';
                label.style.marginBottom = '0';
                wrapper.removeChild(label);
                headerRow.appendChild(label);

                const sortBtn = document.createElement('button');
                sortBtn.className = 'filter-sort-toggle meta-tooltip';
                sortBtn.dataset.tooltip = 'Sort A\u2013Z';
                sortBtn.dataset.sortMode = 'freq';
                sortBtn.textContent = '#\u2193';
                headerRow.appendChild(sortBtn);
                wrapper.appendChild(headerRow);

                const listContainer = document.createElement('div');
                listContainer.style.maxHeight = '150px';
                listContainer.style.overflowY = 'auto';
                listContainer.style.background = 'var(--color-bg-surface)';
                listContainer.style.border = '1px solid var(--color-border)';
                listContainer.style.padding = '5px';

                info.values.forEach(val => {
                    const item = document.createElement('div');
                    item.style.display = 'flex';
                    item.style.alignItems = 'center';
                    item.className = 'filter-checkbox-item';

                    let actualValue = val;
                    let displayValue = val;
                    let sortLabel = String(val);
                    let sortCount = 0;

                    if (typeof val === 'object' && val !== null && val.value !== undefined) {
                        actualValue = val.value;
                        const lbl = val.label || val.value;
                        displayValue = `${lbl} (${val.count.toLocaleString()})`;
                        sortLabel = String(lbl);
                        sortCount = val.count;
                    }

                    item.dataset.sortLabel = sortLabel.toLowerCase();
                    item.dataset.sortCount = sortCount;

                    const cb = document.createElement('input');
                    cb.type = 'checkbox';
                    cb.value = actualValue;
                    cb.dataset.rawValue = actualValue;
                    cb.style.marginRight = '5px';

                    // Restore checked state
                    const filters = sliceId === 1 ? explorerDataV2.filters1 : explorerDataV2.filters2;
                    if (filters[col] && Array.isArray(filters[col].value)) {
                        if (filters[col].value.includes(actualValue)) {
                            cb.checked = true;
                        }
                    }

                    cb.onchange = () => {
                        const checked = Array.from(listContainer.querySelectorAll('input:checked')).map(c => c.dataset.rawValue);
                        setFilterV2(sliceId, col, info.type, 'list', checked);
                    };

                    const span = document.createElement('span');
                    span.innerText = displayValue;
                    span.classList.add('text-sm');

                    item.appendChild(cb);
                    item.appendChild(span);
                    listContainer.appendChild(item);
                });

                sortBtn.onclick = () => {
                    const items = Array.from(listContainer.querySelectorAll('.filter-checkbox-item'));
                    if (sortBtn.dataset.sortMode === 'freq') {
                        items.sort((a, b) => a.dataset.sortLabel.localeCompare(b.dataset.sortLabel));
                        sortBtn.dataset.sortMode = 'alpha';
                        sortBtn.textContent = 'A\u2193';
                        sortBtn.dataset.tooltip = 'Sort by frequency';
                    } else {
                        items.sort((a, b) => b.dataset.sortCount - a.dataset.sortCount);
                        sortBtn.dataset.sortMode = 'freq';
                        sortBtn.textContent = '#\u2193';
                        sortBtn.dataset.tooltip = 'Sort A\u2013Z';
                    }
                    items.forEach(el => listContainer.appendChild(el));
                };

                if (info.values.length === 0) {
                    listContainer.innerHTML = '<div class="text-xs" style="color:var(--color-text-faint);">No values</div>';
                }

                if (info.total_unique && info.total_unique > info.values.length) {
                    const notice = document.createElement('div');
                    notice.classList.add('text-xs');
                    notice.style.cssText = 'color: var(--color-text-faint); padding: 6px 4px 2px; font-style: italic;';
                    notice.textContent = `Showing top ${info.values.length} of ${info.total_unique.toLocaleString()} categories`;
                    listContainer.appendChild(notice);
                }

                wrapper.appendChild(listContainer);
            }

    return wrapper;
}

function setFilterV2(sliceId, col, type, subtype, value) {
    const filters = sliceId === 1 ? explorerDataV2.filters1 : explorerDataV2.filters2;

    if (!filters[col]) {
        filters[col] = { type: type, value: (type === 'number' ? {} : []) };
    }

    if (type === 'number') {
        if (value === "") delete filters[col].value[subtype];
        else filters[col].value[subtype] = parseFloat(value);
    } else {
        // subtype is 'list' usually
        if (value.length === 0) {
            filters[col].value = [];
        } else {
            filters[col].value = value;
        }
    }

    // Check cleanup
    const hasValue = (type === 'number') ?
        (filters[col].value && Object.keys(filters[col].value).length > 0) :
        (filters[col].value && filters[col].value.length > 0);

    if (!hasValue) {
        delete filters[col];
    }

    updateExplorerV2Stats(sliceId);
    updateFilterSectionHighlights(sliceId);
}

function resetFiltersV2(sliceId) {
    if (sliceId === 1) {
        explorerDataV2.filters1 = {};
        explorerDataV2.searchQuery1 = "";
        document.getElementById('explorer-v2-search-input-1').value = "";
    } else {
        explorerDataV2.filters2 = {};
        explorerDataV2.searchQuery2 = "";
        document.getElementById('explorer-v2-search-input-2').value = "";
    }

    // Clear UI widgets
    const container = document.getElementById(`explorer-v2-filters-${sliceId}`);
    const inputs = container.querySelectorAll('input[type="text"], input[type="number"]');
    inputs.forEach(i => i.value = '');

    const checkboxes = container.querySelectorAll('input[type="checkbox"]');
    checkboxes.forEach(cb => cb.checked = false);

    updateExplorerV2Stats(sliceId);
    updateFilterSectionHighlights(sliceId);
}


function updateFilterSectionHighlights(sliceId) {
    const filters = sliceId === 1 ? explorerDataV2.filters1 : explorerDataV2.filters2;
    const container = document.getElementById(`explorer-v2-filters-${sliceId}`);
    if (!container) return;

    container.querySelectorAll('.filter-section').forEach(sec => {
        const cols = JSON.parse(sec.dataset.columns || '[]');
        const header = sec.querySelector('.filter-section-header');
        if (!header) return;
        const active = cols.some(col => !!filters[col]);
        header.classList.toggle('has-active-filter', active);
    });
}


async function updateExplorerV2Stats(triggerSlice = null) {
    if (!explorerDataV2.activeStudy) return;

    const countEl1 = document.getElementById('explorer-v2-count-1');
    const countEl2 = document.getElementById('explorer-v2-count-2');

    // Show loading state for relevant slice
    if (triggerSlice === 1 || triggerSlice === null) countEl1.innerText = "Loading...";
    if (triggerSlice === 2 || triggerSlice === null) countEl2.innerText = "Loading...";

    const statsContainer = document.getElementById('explorer-v2-stats');
    const funLoader = '<div class="fun-loader-container"><div class="fun-loader"><div></div><div></div><div></div><div></div><div></div></div><div class="loading-text">Loading...</div></div>';
    statsContainer.innerHTML = funLoader;

    try {
        const payload = {
            study: explorerDataV2.activeStudy,
            filters: explorerDataV2.filters1,
            search_query: explorerDataV2.searchQuery1,
            trigger_slice: triggerSlice
        };

        // Only include Slice 2 data in dual-slice mode
        if (explorerDataV2.dualSliceMode) {
            payload.filters2 = explorerDataV2.filters2;
            payload.search_query2 = explorerDataV2.searchQuery2;
        }

        const res = await fetch('/api/explore/filter', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();

        if (data.error) {
            if (triggerSlice === 1 || triggerSlice === null) countEl1.innerText = "Error";
            if (triggerSlice === 2 || triggerSlice === null) countEl2.innerText = "Error";
            return;
        }

        // --- UPDATE STATE based on what returned ---

        // slice 1
        if (data.stats !== undefined) {
            explorerDataV2.stats1 = data.stats;
            explorerDataV2.count1 = data.count;
        }

        // slice 2
        // Make sure we handle if it exists in data
        if (data.stats2 !== undefined) {
            explorerDataV2.stats2 = data.stats2;
            explorerDataV2.count2 = data.count2;
        }

        // --- RENDER ---
        const isDual = explorerDataV2.dualSliceMode;
        countEl1.innerText = `${explorerDataV2.count1} items`;

        if (isDual) {
            if (explorerDataV2.stats2) {
                countEl2.innerText = `${explorerDataV2.count2} items`;
            } else {
                countEl2.innerText = `N/A`;
            }
        }

        renderStatsV2(explorerDataV2.stats1, isDual ? explorerDataV2.stats2 : null);

    } catch (e) {
        console.error(e);
        if (triggerSlice === 1 || triggerSlice === null) countEl1.innerText = "Error";
        if (triggerSlice === 2 || triggerSlice === null) countEl2.innerText = "Error";
    }
}

function changeExplorerV2Sort(mode) {
    explorerDataV2.sortMode = mode;
    renderStatsV2(explorerDataV2.stats1, explorerDataV2.stats2);
}

function renderStatsV2(stats1, stats2) {
    const container = document.getElementById('explorer-v2-stats');
    container.innerHTML = '';

    const metadata = explorerDataV2.metadata;
    if (!metadata) return;

    const priority = (window.VariablePrefs && metadata.all_variables_order)
        ? VariablePrefs.effective('viz', metadata.all_variables_order, metadata.viz_priority || [])
        : metadata.viz_priority;
    let colsToRender = [];

    // Use stats1 keys as base. If stats2 has more keys?
    // Ideally use metadata keys, but fallback to stats keys

    // We should compute union of keys from stats1 and stats2 in case
    // filtering somehow removed a column entirely? (Unlikely)
    // Safe bet: use stats1 keys
    const keys1 = stats1 ? Object.keys(stats1) : [];

    // "Customize variables" gear for this user's visualization set — mounted in
    // the center control row next to "Slices to explore" / "Sort by" to save
    // vertical space. Re-run safe (removes any prior gear first).
    if (window.VariablePrefs && metadata.all_variables_order) {
        const header = document.querySelector('.explorer-v2-center-header');
        if (header) {
            const existing = document.getElementById('explorer-v2-viz-gear');
            if (existing) existing.remove();
            const gear = VariablePrefs.gearButton('viz', () => {
                VariablePrefs.openPanel({
                    surface: 'viz',
                    title: 'Customize visualized variables',
                    allOrder: metadata.all_variables_order.filter(c => keys1.includes(c)),
                    globalList: metadata.viz_priority || [],
                    schemaMap: metadata.schema_map || {},
                    onApply: () => renderStatsV2(explorerDataV2.stats1, explorerDataV2.stats2),
                });
            });
            gear.id = 'explorer-v2-viz-gear';
            header.appendChild(gear);
        }
    }

    if (priority && priority.length > 0) {
        colsToRender = priority.filter(c => keys1.includes(c));
    } else {
        colsToRender = keys1.sort();
    }

    const isDual = explorerDataV2.dualSliceMode;

    colsToRender.forEach(col => {
        const s1 = stats1[col];
        const s2 = stats2 ? stats2[col] : null; // Slice 2 data
        const info = metadata[col];

        if (!s1) return;
        if (isDual && !s2) return;

        const card = document.createElement('div');
        card.className = 'stats-card';
        card.style.background = 'var(--color-bg-elevated)';
        card.style.padding = '10px';
        card.style.marginBottom = '10px';
        card.style.borderRadius = '4px';

        const title = document.createElement('h3');
        let titleText = col;
        if (metadata.schema_map && metadata.schema_map[col] && metadata.schema_map[col].display_name) {
            titleText = metadata.schema_map[col].display_name;
        }

        // Means
        let meanHtml = '';
        if (s1.mean !== undefined) {
            const m1 = formatMetricNumber(parseFloat(s1.mean));
            meanHtml += isDual
                ? `<span class="text-xs" style="margin-left:10px; color:var(--color-success);">S1: ${m1}</span>`
                : `<span class="text-xs" style="margin-left:10px; color:var(--color-text-tertiary);">Mean: ${m1}</span>`;
        }
        if (isDual && s2 && s2.mean !== undefined) {
            const m2 = formatMetricNumber(parseFloat(s2.mean));

            // Significance Test (Welch's t-test: Slice 1 vs Slice 2)
            let sigMarker = '';
            try {
                const mean1 = s1.mean;
                const std1 = s1.std || 0;
                const n1 = s1.count || 0;

                const mean2 = s2.mean;
                const std2 = s2.std || 0;
                const n2 = s2.count || 0;

                if (n1 > 1 && n2 > 1 && (std1 > 0 || std2 > 0)) {
                    const var1 = std1 * std1;
                    const var2 = std2 * std2;

                    // Welch's t-stat for two independent samples
                    const se = Math.sqrt((var1 / n1) + (var2 / n2));

                    if (se > 0) {
                        const tStat = Math.abs(mean1 - mean2) / se;

                        // Significance Thresholds (Two-tailed, large N)
                        if (tStat > 3.29) sigMarker = '***';
                        else if (tStat > 2.58) sigMarker = '**';
                        else if (tStat > 1.96) sigMarker = '*';

                        if (sigMarker) {
                            const pVal = tStat > 3.29 ? '< 0.001' : (tStat > 2.58 ? '< 0.01' : '< 0.05');
                            sigMarker = `<span class="font-bold" title="p ${pVal} (Slice 1 vs Slice 2)" style="cursor:help; margin-left:5px; color:var(--color-gold);">${sigMarker}</span>`;
                        }
                    }
                }
            } catch (e) { console.error("Sig test error", e); }

            meanHtml += `<span class="text-xs" style="margin-left:10px; color:var(--color-info);">S2: ${m2}${sigMarker}</span>`;
        }

        title.innerHTML = `${titleText} ${meanHtml}`;
        title.classList.add('text-body');
        title.style.marginTop = '0';
        title.style.marginBottom = '5px';
        title.style.color = 'var(--color-text-primary)';

        const drillHint = document.createElement('span');
        drillHint.className = 'drill-hint text-xs';
        drillHint.style.float = 'right';
        drillHint.textContent = 'Click to view videos';
        title.appendChild(drillHint);

        card.appendChild(title);

        const plotDiv = document.createElement('div');
        plotDiv.id = `plot-v2-${col.replace(/[^a-zA-Z0-9]/g, '')}`;
        plotDiv.style.width = '100%';
        const isCategorical = s1.type !== 'density';
        plotDiv.style.height = (!isDual && isCategorical) ? '60px' : '150px';
        card.appendChild(plotDiv);
        container.appendChild(card);

        if (s1.type === 'density') {
            // For log-transformed variables, compute original-scale hover labels
            const isLog = s1.transform === 'log10';
            const histLogOff = (s1.log_offset > 0) ? s1.log_offset : 1;
            const toOriginal = (logVals) => logVals.map(v => formatMetricNumber(Math.max(0, Math.pow(10, v) - histLogOff)));

            const trace1 = {
                x: s1.x,
                y: s1.y,
                type: 'bar',
                name: isDual ? 'Slice 1' : 'Count',
                marker: {
                    color: isDual ? 'rgba(76, 175, 80, 0.6)' : 'rgba(100, 180, 220, 0.7)',
                    line: { color: isDual ? 'rgba(76, 175, 80, 1.0)' : 'rgba(100, 180, 220, 1.0)', width: 1 }
                },
                ...(isLog
                    ? { customdata: toOriginal(s1.x), hovertemplate: '%{customdata}<extra></extra>' }
                    : { hoverinfo: 'x+y' })
            };

            const traces = [trace1];

            if (isDual && s2) {
                traces.push({
                    x: s2.x,
                    y: s2.y,
                    type: 'bar',
                    name: 'Slice 2',
                    marker: { color: 'rgba(33, 150, 243, 0.6)', line: { color: 'rgba(33, 150, 243, 1.0)', width: 1 } },
                    ...(isLog
                        ? { customdata: toOriginal(s2.x), hovertemplate: '%{customdata}<extra></extra>' }
                        : { hoverinfo: 'x+y' })
                });
            }

            const layout = {
                margin: { t: 0, b: 20, l: 30, r: 20 },
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0)',
                showlegend: isDual,
                legend: { x: 1, xanchor: 'right', y: 1 },
                barmode: 'overlay',
                bargap: 0,
                font: { family: getCSSVar('--font-sans'), color: getCSSVar('--color-text-primary') },
                xaxis: {
                    zeroline: false,
                    gridcolor: getCSSVar('--chart-grid'),
                    tickfont: { family: getCSSVar('--font-sans'), color: getCSSVar('--color-text-primary') }
                },
                yaxis: { showgrid: false, showticklabels: false }
            };

            // Sync Log Ticks from S1 if present (assuming same transform)
            if (s1.transform === 'log10' && s1.tick_vals) {
                layout.xaxis.tickmode = 'array';
                layout.xaxis.tickvals = s1.tick_vals;
                layout.xaxis.ticktext = s1.tick_text;
                layout.xaxis.title = { text: 'log scale', standoff: 5, font: { size: 10 } };
                layout.margin.b = 40;
            }

            Plotly.newPlot(plotDiv, traces, layout, { displayModeBar: false, responsive: true });

            // Drill-down: click a histogram bar → open in Video Analysis
            plotDiv.on('plotly_click', function(eventData) {
                const point = eventData.points[0];
                const xArr = point.data.x;
                let clickedX = point.x;
                let binWidth = xArr.length > 1 ? xArr[1] - xArr[0] : 1;
                let binMin = clickedX - binWidth / 2;
                let binMax = clickedX + binWidth / 2;
                if (s1.transform === 'log10') {
                    binMin = Math.max(0, Math.pow(10, binMin) - histLogOff);
                    binMax = Math.max(0, Math.pow(10, binMax) - histLogOff);
                }
                const sliceId = point.curveNumber === 0 ? 1 : 2;
                drillDownToViewer(col, { type: 'number', value: { min: binMin, max: binMax } }, sliceId);
            });

        } else {
            // Stacked Bar (Horizontal) normalized to %
            const count1 = Object.values(s1).reduce((a, b) => a + b, 0);
            const count2 = (isDual && s2) ? Object.values(s2).reduce((a, b) => a + b, 0) : 0;

            const allCats = isDual && s2
                ? new Set([...Object.keys(s1), ...Object.keys(s2)])
                : new Set(Object.keys(s1));
            const cats = Array.from(allCats).sort((a, b) => {
                const mode = explorerDataV2.sortMode || 'total';

                if (mode === 'name') {
                    const na = parseFloat(a);
                    const nb = parseFloat(b);
                    if (!isNaN(na) && !isNaN(nb)) return na - nb;
                    return a.localeCompare(b);
                }

                const v1a = s1[a] || 0;
                const v2a = (isDual && s2) ? (s2[a] || 0) : 0;
                const v1b = s1[b] || 0;
                const v2b = (isDual && s2) ? (s2[b] || 0) : 0;

                const p1a = v1a / Math.max(1, count1);
                const p2a = v2a / Math.max(1, count2 || 1);
                const p1b = v1b / Math.max(1, count1);
                const p2b = v2b / Math.max(1, count2 || 1);

                let scoreA = 0;
                let scoreB = 0;

                if (mode === 'slice1') {
                    scoreA = p1a;
                    scoreB = p1b;
                } else if (mode === 'slice2') {
                    scoreA = p2a;
                    scoreB = p2b;
                } else {
                    // Total (default) — in single-slice mode just use S1
                    scoreA = p1a + p2a;
                    scoreB = p1b + p2b;
                }

                // Descending score
                return scoreB - scoreA;
            });


            // Extended distinct palette (50 colors) to minimize adjacency collisions
            const palette = [
                '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
                '#aec7e8', '#ffbb78', '#98df8a', '#ff9896', '#c5b0d5', '#c49c94', '#f7b6d2', '#c7c7c7', '#dbdb8d', '#9edae5',
                '#393b79', '#637939', '#8c6d31', '#843c39', '#7b4173', '#5254a3', '#8ca252', '#bd9e39', '#d6616b', '#ce6dbd',
                '#65a620', '#c75a93', '#e7969c', '#7b4173', '#a55194', '#ce6dbd', '#de9ed6', '#ad494a', '#d6616b', '#e7969c',
                '#3182bd', '#e6550d', '#31a354', '#756bb1', '#636363', '#6baed6', '#fd8d3c', '#74c476', '#9e9ac8', '#969696'
            ];

            const traces = [];
            const yLabels = isDual ? ['Slice 1', 'Slice 2'] : ['Distribution'];

            cats.forEach((cat, idx) => {
                const val1 = s1[cat] || 0;
                const pct1 = (val1 / Math.max(1, count1)) * 100;

                if (isDual && s2) {
                    const val2 = s2[cat] || 0;
                    const pct2 = (val2 / Math.max(1, count2)) * 100;
                    traces.push({
                        x: [pct1, pct2],
                        y: yLabels,
                        name: cat,
                        marker: { color: palette[idx % palette.length] },
                        orientation: 'h',
                        type: 'bar',
                        text: [cat, cat],
                        textposition: 'inside',
                        customdata: [[cat, pct1.toFixed(1)], [cat, pct2.toFixed(1)]],
                        hovertemplate: '%{customdata[0]}<br>Share: %{customdata[1]}%<extra></extra>',
                    });
                } else {
                    traces.push({
                        x: [pct1],
                        y: yLabels,
                        name: cat,
                        marker: { color: palette[idx % palette.length] },
                        orientation: 'h',
                        type: 'bar',
                        text: [cat],
                        textposition: 'inside',
                        customdata: [[cat, pct1.toFixed(1)]],
                        hovertemplate: '%{customdata[0]}<br>Share: %{customdata[1]}%<extra></extra>',
                    });
                }
            });

            const layout = {
                barmode: 'stack',
                margin: { t: 0, b: 2, l: isDual ? 60 : 2, r: 2 },
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0)',
                showlegend: false,
                font: { family: getCSSVar('--font-sans'), color: getCSSVar('--color-text-primary') },
                xaxis: { range: [0, 100], showgrid: false, showticklabels: false },
                yaxis: { showticklabels: isDual, tickfont: { family: getCSSVar('--font-sans'), color: getCSSVar('--color-text-primary') } },
                height: isDual ? undefined : 60
            };

            Plotly.newPlot(plotDiv, traces, layout, { displayModeBar: false, responsive: true });

            // Drill-down: click a category segment → open in Video Analysis
            plotDiv.on('plotly_click', function(eventData) {
                const point = eventData.points[0];
                const categoryValue = point.customdata[0];
                const sliceId = (point.y === 'Slice 2') ? 2 : 1;
                drillDownToViewer(col, { type: 'category', value: [categoryValue] }, sliceId);
            });
        }
    });
}


// Drill-down: show confirmation then navigate to Video Analysis tab
function drillDownToViewer(col, clickedFilter, sliceId) {
    // Build a readable description of what was clicked
    const schemaMap = explorerDataV2.metadata?.schema_map || {};
    const displayName = schemaMap[col]?.display_name || col;

    let valueLabel;
    if (clickedFilter.type === 'category') {
        valueLabel = `"${clickedFilter.value[0]}"`;
    } else {
        const min = clickedFilter.value.min !== undefined ? formatMetricNumber(clickedFilter.value.min) : '0';
        const max = clickedFilter.value.max !== undefined ? formatMetricNumber(clickedFilter.value.max) : '∞';
        valueLabel = `${min} – ${max}`;
    }

    showDrillDownConfirm(displayName, valueLabel, () => {
        const sourceFilters = (sliceId === 2)
            ? JSON.parse(JSON.stringify(explorerDataV2.filters2))
            : JSON.parse(JSON.stringify(explorerDataV2.filters1));
        sourceFilters[col] = clickedFilter;

        window._pendingDrillDown = {
            filters: sourceFilters,
            searchQuery: (sliceId === 2) ? explorerDataV2.searchQuery2 : explorerDataV2.searchQuery1,
            timestamp: Date.now()
        };

        const tabBtn = document.querySelector('.tab-button[onclick*="video_analysis"]');
        if (tabBtn) tabBtn.click();
    });
}


function showDrillDownConfirm(variableName, valueLabel, onConfirm) {
    // Remove any existing popup
    const existing = document.getElementById('drilldown-confirm');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.id = 'drilldown-confirm';
    overlay.className = 'drilldown-overlay';

    const card = document.createElement('div');
    card.className = 'drilldown-card';

    card.innerHTML = `
        <div class="drilldown-header">
            <span class="drilldown-icon">&#x1F50D;</span>
            <span class="text-h3 font-semibold">View these videos?</span>
        </div>
        <p class="text-body" style="margin: 12px 0 6px; color: var(--color-text-secondary);">
            This will open <strong>Video Analysis</strong> filtered to:
        </p>
        <div class="drilldown-filter-preview">
            <span class="font-medium">${escapeHtml(variableName)}</span>
            <span style="color: var(--color-text-muted); margin: 0 6px;">→</span>
            <span class="font-semibold" style="color: var(--color-accent);">${escapeHtml(valueLabel)}</span>
        </div>
        <p class="text-sm" style="margin: 8px 0 16px; color: var(--color-text-muted);">
            Your current Explore filters will also be carried over.
        </p>
        <div class="drilldown-actions">
            <button class="btn btn-discreet drilldown-btn-cancel">Cancel</button>
            <button class="btn btn-primary drilldown-btn-go">View Videos</button>
        </div>
    `;

    overlay.appendChild(card);
    document.body.appendChild(overlay);

    // Animate in
    requestAnimationFrame(() => overlay.classList.add('visible'));

    const dismiss = () => {
        overlay.classList.remove('visible');
        setTimeout(() => overlay.remove(), 200);
    };

    card.querySelector('.drilldown-btn-cancel').onclick = dismiss;
    card.querySelector('.drilldown-btn-go').onclick = () => {
        dismiss();
        onConfirm();
    };
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) dismiss();
    });

    // Keyboard: Enter to confirm, Escape to cancel
    const keyHandler = (e) => {
        if (e.key === 'Escape') { dismiss(); document.removeEventListener('keydown', keyHandler); }
        if (e.key === 'Enter') { dismiss(); onConfirm(); document.removeEventListener('keydown', keyHandler); }
    };
    document.addEventListener('keydown', keyHandler);
}


window.addEventListener('theme-changed', () => {
    // Re-render charts (Plotly needs resolved color values, not CSS var() references)
    if (explorerDataV2.stats1) {
        renderStatsV2(explorerDataV2.stats1, explorerDataV2.dualSliceMode ? explorerDataV2.stats2 : null);
    }
});


// A "Customize variables" change made on another tab (the 'filter' surface is
// shared with Video Analysis) must be reflected here even while Explore is
// hidden — re-render the affected surface whenever prefs change.
window.addEventListener('fyp:variable-prefs-changed', (ev) => {
    if (!explorerDataV2 || !explorerDataV2.metadata) return;
    const surface = ev.detail && ev.detail.surface;
    if (!surface || surface === 'filter') {
        renderFiltersV2(explorerDataV2.metadata, 1);
        if (explorerDataV2.dualSliceMode) renderFiltersV2(explorerDataV2.metadata, 2);
    }
    if ((!surface || surface === 'viz') && explorerDataV2.stats1) {
        renderStatsV2(explorerDataV2.stats1, explorerDataV2.dualSliceMode ? explorerDataV2.stats2 : null);
    }
});
