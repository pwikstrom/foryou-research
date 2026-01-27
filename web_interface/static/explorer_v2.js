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
    sortMode: 'total'
};

// Initialization
document.addEventListener('DOMContentLoaded', function () {
    // Only init if tab exists
    if (document.getElementById('data_explorer_v2')) {
        loadExplorerV2Studies(); // Start by loading studies

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

async function loadExplorerV2Studies() {
    const selector = document.getElementById('explorer-v2-study-select');

    try {
        const res = await fetch('/api/studies/defined');
        const studies = await res.json();

        selector.innerHTML = '<option value="" disabled selected>Select a study...</option>';

        if (studies.length === 0) {
            const opt = document.createElement('option');
            opt.disabled = true;
            opt.text = "No studies found";
            selector.appendChild(opt);
            return;
        }

        studies.forEach(study => {
            const opt = document.createElement('option');
            opt.value = study;
            opt.text = study;
            selector.appendChild(opt);
        });

        // Auto-select first study
        if (studies.length > 0) {
            selector.value = studies[0];
            changeExplorerV2Study(studies[0]);
        }


    } catch (e) {
        console.error("Failed to load studies", e);
        selector.innerHTML = '<option disabled>Error loading studies</option>';
    }
}

function changeExplorerV2Study(val) {
    const selector = document.getElementById('explorer-v2-study-select');
    const studyName = val || selector.value;

    if (!studyName) return;

    explorerDataV2.activeStudy = studyName;
    explorerDataV2.filters1 = {}; // Reset filters
    explorerDataV2.filters2 = {};
    // Reset Stats
    explorerDataV2.stats1 = null;
    explorerDataV2.count1 = 0;
    explorerDataV2.stats2 = null;
    explorerDataV2.count2 = 0;

    loadExplorerV2Metadata();
}

async function loadExplorerV2Metadata() {
    if (!explorerDataV2.activeStudy) return;

    const filterContainer1 = document.getElementById('explorer-v2-filters-1');
    const filterContainer2 = document.getElementById('explorer-v2-filters-2');

    filterContainer1.innerHTML = '<div style="text-align:center; margin-top:20px;">Loading metadata...</div>';
    filterContainer2.innerHTML = '<div style="text-align:center; margin-top:20px;">Loading metadata...</div>';

    try {
        const res = await fetch(`/api/explorer/metadata?study=${encodeURIComponent(explorerDataV2.activeStudy)}`);
        const data = await res.json();

        if (data.error) {
            const errHtml = `<div style="color:red; text-align:center;">${data.error}</div>`;
            filterContainer1.innerHTML = errHtml;
            filterContainer2.innerHTML = errHtml;
            return;
        }

        // Update File Info Display
        const infoSpan = document.getElementById('explorer-v2-file-info');
        if (infoSpan) {
            if (data.source_file && data.source_file_modified) {
                infoSpan.innerText = `Using file: ${data.source_file} - saved ${data.source_file_modified}`;
            } else {
                infoSpan.innerText = "";
            }
        }

        explorerDataV2.metadata = data;
        renderFiltersV2(data, 1);
        renderFiltersV2(data, 2);

        // Initial fetch of stats
        updateExplorerV2Stats();
    } catch (e) {
        console.error(e);
        filterContainer1.innerHTML = '<div style="color:red; text-align:center;">Failed to load metadata</div>';
    }
}

// Initialize collapsed state
if (!explorerDataV2.collapsedFilters1) {
    try {
        explorerDataV2.collapsedFilters1 = JSON.parse(localStorage.getItem('explorer_collapsed_filters_1') || '[]');
    } catch (e) { explorerDataV2.collapsedFilters1 = []; }
}
if (!explorerDataV2.collapsedFilters2) {
    try {
        explorerDataV2.collapsedFilters2 = JSON.parse(localStorage.getItem('explorer_collapsed_filters_2') || '[]');
    } catch (e) { explorerDataV2.collapsedFilters2 = []; }
}

function renderFiltersV2(metadata, sliceId) {
    const container = document.getElementById(`explorer-v2-filters-${sliceId}`);
    container.innerHTML = '';

    const priority = metadata.filter_priority;
    let availableCols = [];

    if (priority && priority.length > 0) {
        availableCols = priority.filter(c => metadata[c]);
    } else {
        availableCols = Object.keys(metadata).sort().filter(c => c !== 'total_stats');
    }

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

    // Sort Sections
    const sortPriority = metadata.display_priority && metadata.display_priority.length > 0 ? metadata.display_priority : priority;

    let sectionNames = Object.keys(sections).sort((a, b) => {
        const getSectionPrio = (secName) => {
            const vars = sections[secName] || [];
            let minPrio = 999999;
            vars.forEach(v => {
                const idx = sortPriority ? sortPriority.indexOf(v) : -1;
                const p = idx === -1 ? 999999 : idx;
                if (p < minPrio) minPrio = p;
            });
            return minPrio;
        };
        const prioA = getSectionPrio(a);
        const prioB = getSectionPrio(b);
        if (prioA !== prioB) return prioA - prioB;
        return a.localeCompare(b);
    });

    // Render Sections
    const collapsedList = sliceId === 1 ? explorerDataV2.collapsedFilters1 : explorerDataV2.collapsedFilters2;
    const storageKey = `explorer_collapsed_filters_${sliceId}`;

    sectionNames.forEach(sec => {
        const vars = sections[sec];
        if (vars.length === 0) return;

        // Section Container
        const sectionDiv = document.createElement('div');
        sectionDiv.className = 'filter-section';
        sectionDiv.style.marginBottom = '10px';
        sectionDiv.style.border = '1px solid #3e3e42';
        sectionDiv.style.borderRadius = '4px';
        sectionDiv.style.overflow = 'hidden';

        // Header
        const header = document.createElement('div');
        header.style.background = '#3e3e42';
        header.style.padding = '8px 10px';
        header.style.cursor = 'pointer';
        header.style.fontWeight = 'bold';
        header.style.color = '#eee';
        header.style.userSelect = 'none';
        header.style.display = 'flex';
        header.style.alignItems = 'center';

        const isCollapsed = collapsedList.includes(sec);
        const arrow = isCollapsed ? '&#9656;' : '&#9662;'; // Right vs Down

        header.innerHTML = `<span style="margin-right:8px; width:15px; display:inline-block;">${arrow}</span> ${sec}`;

        // Body (Variables)
        const body = document.createElement('div');
        body.style.padding = '10px';
        body.style.background = '#252526';
        body.style.display = isCollapsed ? 'none' : 'block';

        // Toggle Logic
        header.onclick = () => {
            const currentlyHidden = body.style.display === 'none';
            if (currentlyHidden) {
                body.style.display = 'block';
                header.innerHTML = `<span style="margin-right:8px; width:15px; display:inline-block;">&#9662;</span> ${sec}`;
                // Remove from collapsed list
                const idx = collapsedList.indexOf(sec);
                if (idx > -1) collapsedList.splice(idx, 1);
            } else {
                body.style.display = 'none';
                header.innerHTML = `<span style="margin-right:8px; width:15px; display:inline-block;">&#9656;</span> ${sec}`;
                // Add to collapsed list
                if (!collapsedList.includes(sec)) collapsedList.push(sec);
            }
            // Persist
            localStorage.setItem(storageKey, JSON.stringify(collapsedList));
        };

        sectionDiv.appendChild(header);

        // Render vars
        vars.forEach(col => {
            const info = metadata[col];
            const wrapper = document.createElement('div');
            wrapper.className = 'filter-group';
            wrapper.style.marginBottom = '15px';
            wrapper.style.borderBottom = '1px solid #333';
            wrapper.style.paddingBottom = '10px';

            const label = document.createElement('label');

            let displayName = col;
            if (metadata.schema_map && metadata.schema_map[col] && metadata.schema_map[col].display_name) {
                displayName = metadata.schema_map[col].display_name;
            }

            label.innerText = displayName;
            label.style.fontWeight = 'bold';
            label.style.display = 'block';
            label.style.marginBottom = '5px';
            label.style.color = '#d4d4d4';
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
                labelRow.style.fontSize = '0.85em';
                labelRow.style.color = '#888';
                labelRow.style.marginTop = '-5px';

                const minLabel = document.createElement('span');
                const maxLabel = document.createElement('span');

                labelRow.appendChild(minLabel);
                labelRow.appendChild(maxLabel);
                wrapper.appendChild(labelRow);

                // Current Values
                let currentMin = info.min;
                let currentMax = info.max;

                const filters = sliceId === 1 ? explorerDataV2.filters1 : explorerDataV2.filters2;

                if (filters[col] && filters[col].value) {
                    if (filters[col].value.min !== undefined) currentMin = filters[col].value.min;
                    if (filters[col].value.max !== undefined) currentMax = filters[col].value.max;
                }

                // Formatting Helper
                const fmt = (n) => Math.round(n).toLocaleString();

                minLabel.innerText = fmt(currentMin);
                maxLabel.innerText = fmt(currentMax);

                // Initialize Slider
                if (typeof noUiSlider !== 'undefined') {
                    if (info.min >= info.max) {
                        sliderDiv.style.display = 'none';
                    } else {
                        noUiSlider.create(sliderDiv, {
                            start: [currentMin, currentMax],
                            connect: true,
                            range: {
                                'min': info.min,
                                'max': info.max
                            },
                        });

                        sliderDiv.noUiSlider.on('update', function (values, handle) {
                            const value = parseFloat(values[handle]);
                            if (handle === 0) {
                                minLabel.innerText = fmt(value);
                            } else {
                                maxLabel.innerText = fmt(value);
                            }
                        });

                        sliderDiv.noUiSlider.on('change', function (values, handle) {
                            const vMin = parseFloat(values[0]);
                            const vMax = parseFloat(values[1]);

                            const f = sliceId === 1 ? explorerDataV2.filters1 : explorerDataV2.filters2;
                            if (!f[col]) f[col] = { type: 'number', value: {} };

                            f[col].value.min = vMin;
                            f[col].value.max = vMax;

                            updateExplorerV2Stats(sliceId);
                        });
                    }
                } else {
                    minLabel.innerText = "Error: Slider lib missing";
                }
            } else if (info.type === 'category' || info.type === 'list') {
                const listContainer = document.createElement('div');
                listContainer.style.maxHeight = '150px';
                listContainer.style.overflowY = 'auto';
                listContainer.style.background = '#252526';
                listContainer.style.border = '1px solid #3e3e42';
                listContainer.style.padding = '5px';

                info.values.forEach(val => {
                    const item = document.createElement('div');
                    item.style.display = 'flex';
                    item.style.alignItems = 'center';

                    let actualValue = val;
                    let displayValue = val;

                    if (typeof val === 'object' && val !== null && val.value !== undefined) {
                        actualValue = val.value;
                        displayValue = `${val.value} (${val.count.toLocaleString()})`;
                    }

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
                    span.style.fontSize = '0.9em';

                    item.appendChild(cb);
                    item.appendChild(span);
                    listContainer.appendChild(item);
                });

                if (info.values.length === 0) {
                    listContainer.innerHTML = '<div style="color:#777; font-size:0.8em;">No values</div>';
                }

                wrapper.appendChild(listContainer);
            }

            body.appendChild(wrapper);
        });

        sectionDiv.appendChild(body);
        container.appendChild(sectionDiv);
    });
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
}

async function updateExplorerV2Stats(triggerSlice = null) {
    if (!explorerDataV2.activeStudy) return;

    const countEl1 = document.getElementById('explorer-v2-count-1');
    const countEl2 = document.getElementById('explorer-v2-count-2');

    // Show loading state for relevant slice
    if (triggerSlice === 1 || triggerSlice === null) countEl1.innerText = "Loading...";
    if (triggerSlice === 2 || triggerSlice === null) countEl2.innerText = "Loading...";

    try {
        const payload = {
            study: explorerDataV2.activeStudy,
            filters: explorerDataV2.filters1,
            filters2: explorerDataV2.filters2,
            search_query: explorerDataV2.searchQuery1,
            search_query2: explorerDataV2.searchQuery2,
            trigger_slice: triggerSlice
        };

        const res = await fetch('/api/explorer/filter', {
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
        countEl1.innerText = `Slice 1: ${explorerDataV2.count1} items`;

        if (explorerDataV2.stats2) {
            countEl2.innerText = `Slice 2: ${explorerDataV2.count2} items`;
        } else {
            countEl2.innerText = `Slice 2: N/A`;
        }

        renderStatsV2(explorerDataV2.stats1, explorerDataV2.stats2);

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

    const priority = metadata.viz_priority;
    let colsToRender = [];

    // Use stats1 keys as base. If stats2 has more keys? 
    // Ideally use metadata keys, but fallback to stats keys

    // We should compute union of keys from stats1 and stats2 in case 
    // filtering somehow removed a column entirely? (Unlikely)
    // Safe bet: use stats1 keys
    const keys1 = stats1 ? Object.keys(stats1) : [];

    if (priority && priority.length > 0) {
        colsToRender = priority.filter(c => keys1.includes(c));
    } else {
        colsToRender = keys1.sort();
    }

    colsToRender.forEach(col => {
        const s1 = stats1[col];
        const s2 = stats2 ? stats2[col] : null; // Slice 2 data
        const info = metadata[col];

        if (!s1 || !s2) return;

        const card = document.createElement('div');
        card.className = 'stats-card';
        card.style.background = '#2d2d30';
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
            const m1 = parseFloat(s1.mean).toLocaleString(undefined, { maximumFractionDigits: 2 });
            meanHtml += `<span style="font-size:0.8em; margin-left:10px; color:#4CAF50;">S1: ${m1}</span>`;
        }
        if (s2.mean !== undefined) {
            const m2 = parseFloat(s2.mean).toLocaleString(undefined, { maximumFractionDigits: 2 });

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
                            sigMarker = `<span title="p ${pVal} (Slice 1 vs Slice 2)" style="cursor:help; margin-left:5px; font-weight:bold; color:#FFD700;">${sigMarker}</span>`;
                        }
                    }
                }
            } catch (e) { console.error("Sig test error", e); }

            meanHtml += `<span style="font-size:0.8em; margin-left:10px; color:#2196F3;">S2: ${m2}${sigMarker}</span>`;
        }

        title.innerHTML = `${titleText} ${meanHtml}`;
        title.style.marginTop = '0';
        title.style.marginBottom = '5px';
        title.style.fontSize = '1em';
        title.style.color = '#eee';
        card.appendChild(title);

        const plotDiv = document.createElement('div');
        plotDiv.id = `plot-v2-${col.replace(/[^a-zA-Z0-9]/g, '')}`;
        plotDiv.style.width = '100%';
        plotDiv.style.height = '150px';
        card.appendChild(plotDiv);
        container.appendChild(card);

        if (s1.type === 'density') {
            const trace1 = {
                x: s1.x,
                y: s1.y,
                type: 'bar',
                name: 'Slice 1',
                marker: { color: 'rgba(76, 175, 80, 0.6)', line: { color: 'rgba(76, 175, 80, 1.0)', width: 1 } },
                hoverinfo: 'x+y'
            };

            const trace2 = {
                x: s2.x,
                y: s2.y,
                type: 'bar',
                name: 'Slice 2',
                marker: { color: 'rgba(33, 150, 243, 0.6)', line: { color: 'rgba(33, 150, 243, 1.0)', width: 1 } },
                hoverinfo: 'x+y'
            };

            const traces = [trace1, trace2];

            // Determine combined range logic?
            // Actually Plotly handles it nicely if we just pass data.
            // But we need to handle Log vs Linear

            const layout = {
                margin: { t: 0, b: 20, l: 30, r: 20 },
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0)',
                showlegend: true,
                legend: { x: 1, xanchor: 'right', y: 1 },
                barmode: 'overlay',
                bargap: 0,
                font: { color: '#d4d4d4' },
                xaxis: {
                    zeroline: false,
                    gridcolor: '#444',
                    tickfont: { color: '#d4d4d4' }
                },
                yaxis: { showgrid: false, showticklabels: false }
            };

            // Sync Log Ticks from S1 if present (assuming same transform)
            if (s1.transform === 'log10' && s1.tick_vals) {
                layout.xaxis.tickmode = 'array';
                layout.xaxis.tickvals = s1.tick_vals;
                layout.xaxis.ticktext = s1.tick_text;
                layout.xaxis.title = 'Log';
            }

            Plotly.newPlot(plotDiv, traces, layout, { displayModeBar: false, responsive: true });

        } else {
            // Stacked Bar (Horizontal) normalized to %
            // Need to merge keys from s1 and s2

            // Counts
            const count1 = Object.values(s1).reduce((a, b) => a + b, 0);
            const count2 = Object.values(s2).reduce((a, b) => a + b, 0);

            const allCats = new Set([...Object.keys(s1), ...Object.keys(s2)]);
            const cats = Array.from(allCats).sort((a, b) => {
                const mode = explorerDataV2.sortMode || 'total';

                if (mode === 'name') {
                    const na = parseFloat(a);
                    const nb = parseFloat(b);
                    if (!isNaN(na) && !isNaN(nb)) return na - nb;
                    return a.localeCompare(b);
                }

                const v1a = s1[a] || 0;
                const v2a = s2[a] || 0;
                const v1b = s1[b] || 0;
                const v2b = s2[b] || 0;

                const p1a = v1a / Math.max(1, count1);
                const p2a = v2a / Math.max(1, count2);
                const p1b = v1b / Math.max(1, count1);
                const p2b = v2b / Math.max(1, count2);

                let scoreA = 0;
                let scoreB = 0;

                if (mode === 'slice1') {
                    scoreA = p1a;
                    scoreB = p1b;
                } else if (mode === 'slice2') {
                    scoreA = p2a;
                    scoreB = p2b;
                } else {
                    // Total (default)
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

            cats.forEach((cat, idx) => {
                const val1 = s1[cat] || 0;
                const val2 = s2[cat] || 0;

                const pct1 = (val1 / Math.max(1, count1)) * 100;
                const pct2 = (val2 / Math.max(1, count2)) * 100;

                traces.push({
                    x: [pct1, pct2],
                    y: ['Slice 1', 'Slice 2'],
                    name: cat,
                    marker: { color: palette[idx % palette.length] },
                    orientation: 'h',
                    type: 'bar',
                    text: [cat, cat],
                    hoverinfo: 'text+x+name',
                });
            });

            const layout = {
                barmode: 'stack',
                margin: { t: 0, b: 20, l: 60, r: 20 }, // Increased left margin for labels
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0)',
                showlegend: false,
                font: { color: '#d4d4d4' },
                xaxis: { range: [0, 100], showgrid: false },
                yaxis: { tickfont: { color: '#d4d4d4' } }
            };

            Plotly.newPlot(plotDiv, traces, layout, { displayModeBar: false, responsive: true });
        }
    });
}
