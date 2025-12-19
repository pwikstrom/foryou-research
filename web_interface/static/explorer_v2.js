// Explorer V2 Logic (Dual Slice Comparison)

let explorerDataV2 = {
    metadata: null,
    filters1: {},
    filters2: {},
    searchQuery1: "",
    searchQuery2: "",
    activeStudy: null
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
                    updateExplorerV2Stats();
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
                    updateExplorerV2Stats();
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

        // Auto-select first if available
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

function renderFiltersV2(metadata, sliceId) {
    const container = document.getElementById(`explorer-v2-filters-${sliceId}`);
    container.innerHTML = '';

    const priority = metadata.filter_priority;
    let colsToRender = [];

    if (priority && priority.length > 0) {
        colsToRender = priority.filter(c => metadata[c]);
    } else {
        colsToRender = Object.keys(metadata).sort().filter(c => c !== 'total_stats');
    }

    colsToRender.forEach(col => {
        const info = metadata[col];
        const wrapper = document.createElement('div');
        wrapper.className = 'filter-group';
        wrapper.style.marginBottom = '15px';
        wrapper.style.borderBottom = '1px solid #333';
        wrapper.style.paddingBottom = '10px';

        const label = document.createElement('label');
        label.innerText = col;
        label.style.fontWeight = 'bold';
        label.style.display = 'block';
        label.style.marginBottom = '5px';
        label.style.color = '#d4d4d4';
        wrapper.appendChild(label);

        if (info.type === 'number') {
            // Min/Max Inputs
            const inputRow = document.createElement('div');
            inputRow.style.display = 'flex';
            inputRow.style.gap = '5px';

            const minInput = document.createElement('input');
            minInput.type = 'number';
            minInput.placeholder = `Min (${info.min})`;
            minInput.style.width = '50%';
            minInput.style.background = '#3c3c3c';
            minInput.style.border = '1px solid #555';
            minInput.style.color = '#fff';
            minInput.onchange = (e) => setFilterV2(sliceId, col, 'number', 'min', e.target.value);

            const maxInput = document.createElement('input');
            maxInput.type = 'number';
            maxInput.placeholder = `Max (${info.max})`;
            maxInput.style.width = '50%';
            maxInput.style.background = '#3c3c3c';
            maxInput.style.border = '1px solid #555';
            maxInput.style.color = '#fff';
            maxInput.onchange = (e) => setFilterV2(sliceId, col, 'number', 'max', e.target.value);

            inputRow.appendChild(minInput);
            inputRow.appendChild(maxInput);
            wrapper.appendChild(inputRow);

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

        container.appendChild(wrapper);
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

        if (Object.keys(filters[col].value).length === 0) delete filters[col];

    } else {
        if (value.length === 0) delete filters[col];
        else filters[col].value = value;
    }

    updateExplorerV2Stats();
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

    updateExplorerV2Stats();
}

async function updateExplorerV2Stats() {
    if (!explorerDataV2.activeStudy) return;

    const statsContainer = document.getElementById('explorer-v2-stats');
    const countEl1 = document.getElementById('explorer-v2-count-1');
    const countEl2 = document.getElementById('explorer-v2-count-2');

    try {
        const res = await fetch('/api/explorer/filter', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                study: explorerDataV2.activeStudy,
                filters: explorerDataV2.filters1,
                filters2: explorerDataV2.filters2,
                search_query: explorerDataV2.searchQuery1,
                search_query2: explorerDataV2.searchQuery2
            })
        });
        const data = await res.json();

        if (data.error) {
            countEl1.innerText = "Error";
            return;
        }

        countEl1.innerText = `Slice 1: ${data.count} items`;

        // Handle logic where if filters2 is empty, it might return count2 as total? 
        // Backend logic: if filters2 is None, it won't be in data? 
        // Or we should update backend to ALWAYS return stats2 if we ask for it?
        // We will update backend to return stats2 if we pass filters2 key (even if empty).

        let count2 = 0;
        let stats2 = null;

        if (data.stats2) {
            count2 = data.count2;
            stats2 = data.stats2;
            countEl2.innerText = `Slice 2: ${count2} items`;
        } else {
            // Fallback if backend doesn't support v2 yet or error
            countEl2.innerText = `Slice 2: N/A`;
        }

        renderStatsV2(data.stats, stats2);

    } catch (e) {
        console.error(e);
    }
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

            Plotly.newPlot(plotDiv, traces, layout, { displayModeBar: false });

        } else {
            // Stacked Bar (Horizontal) normalized to %
            // Need to merge keys from s1 and s2

            const allCats = new Set([...Object.keys(s1), ...Object.keys(s2)]);
            const cats = Array.from(allCats).sort((a, b) => {
                const na = parseFloat(a);
                const nb = parseFloat(b);
                if (!isNaN(na) && !isNaN(nb)) return na - nb;
                return a.localeCompare(b);
            });

            // Counts
            const count1 = Object.values(s1).reduce((a, b) => a + b, 0);
            const count2 = Object.values(s2).reduce((a, b) => a + b, 0);

            const traces = [];

            cats.forEach(cat => {
                const val1 = s1[cat] || 0;
                const val2 = s2[cat] || 0;

                const pct1 = (val1 / Math.max(1, count1)) * 100;
                const pct2 = (val2 / Math.max(1, count2)) * 100;

                traces.push({
                    x: [pct1, pct2],
                    y: ['Slice 1', 'Slice 2'],
                    name: cat,
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

            Plotly.newPlot(plotDiv, traces, layout, { displayModeBar: false });
        }
    });
}
