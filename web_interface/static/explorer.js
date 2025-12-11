// Explorer Logic

let explorerData = {
    metadata: null,
    filters: {},
    searchQuery: "",
    activeStudy: null
};

// Initialization
document.addEventListener('DOMContentLoaded', function () {
    // Only init if tab exists
    if (document.getElementById('dataset_explorer')) {
        loadExplorerStudies(); // Start by loading studies

        // Search Input Listener
        const searchInput = document.getElementById('explorer-search-input');
        if (searchInput) {
            let debounceTimer;
            searchInput.addEventListener('input', (e) => {
                const val = e.target.value;
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(() => {
                    explorerData.searchQuery = val;
                    updateExplorerStats();
                }, 500); // 500ms debounce
            });
        }
    }
});

async function loadExplorerStudies() {
    const selector = document.getElementById('explorer-study-select');

    try {
        const res = await fetch('/api/explorer/studies');
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
            changeExplorerStudy(studies[0]);
        }

    } catch (e) {
        console.error("Failed to load studies", e);
        selector.innerHTML = '<option disabled>Error loading studies</option>';
    }
}

function changeExplorerStudy(val) {
    const selector = document.getElementById('explorer-study-select');
    const studyName = val || selector.value;

    if (!studyName) return;

    explorerData.activeStudy = studyName;
    explorerData.filters = {}; // Reset filters on study change
    loadExplorerMetadata();
}

async function loadExplorerMetadata() {
    if (!explorerData.activeStudy) return;

    const filterContainer = document.getElementById('explorer-filters');
    // Don't wipe filter container entirely, just the dynamic part?
    // Actually our layout has the select INSIDE the header, and filters in 'explorer-filters' div below.
    // So clearing 'explorer-filters' is safe.

    filterContainer.innerHTML = '<div style="text-align:center; margin-top:20px;">Loading metadata...</div>';

    try {
        const res = await fetch(`/api/explorer/metadata?study=${encodeURIComponent(explorerData.activeStudy)}`);
        const data = await res.json();

        if (data.error) {
            filterContainer.innerHTML = `<div style="color:red; text-align:center;">${data.error}</div>`;
            return;
        }

        explorerData.metadata = data;
        renderFilters(data);
        // Initial fetch of stats
        updateExplorerStats();
    } catch (e) {
        console.error(e);
        filterContainer.innerHTML = '<div style="color:red; text-align:center;">Failed to load metadata</div>';
    }
}

function renderFilters(metadata) {
    const container = document.getElementById('explorer-filters');
    container.innerHTML = '';

    const sortedCols = Object.keys(metadata).sort();

    // Skip 'total_stats' key if present in metadata (it's injected there)
    const colsToRender = sortedCols.filter(c => c !== 'total_stats');

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
            minInput.onchange = (e) => setFilter(col, 'number', 'min', e.target.value);

            const maxInput = document.createElement('input');
            maxInput.type = 'number';
            maxInput.placeholder = `Max (${info.max})`;
            maxInput.style.width = '50%';
            maxInput.style.background = '#3c3c3c';
            maxInput.style.border = '1px solid #555';
            maxInput.style.color = '#fff';
            maxInput.onchange = (e) => setFilter(col, 'number', 'max', e.target.value);

            inputRow.appendChild(minInput);
            inputRow.appendChild(maxInput);
            wrapper.appendChild(inputRow);

        } else if (info.type === 'category' || info.type === 'list') {
            // Multi-select / Checkbox list
            // For high cardinality, maybe a scrollable list of checkboxes?
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

                // Handle new object format {value: "v", count: 123}
                if (typeof val === 'object' && val !== null && val.value !== undefined) {
                    actualValue = val.value;
                    displayValue = `${val.value} (${val.count.toLocaleString()})`;
                }

                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.value = actualValue; // Keep for form submission if needed, but rely on dataset
                cb.dataset.rawValue = actualValue;
                cb.style.marginRight = '5px';

                // Restore checked state if filter exists
                if (explorerData.filters[col] && Array.isArray(explorerData.filters[col].value)) {
                    if (explorerData.filters[col].value.includes(actualValue)) {
                        cb.checked = true;
                    }
                }

                cb.onchange = () => {
                    const checked = Array.from(listContainer.querySelectorAll('input:checked')).map(c => c.dataset.rawValue);
                    console.log(`Filtering ${col} with:`, checked); // Debug log
                    setFilter(col, info.type, 'list', checked);
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

function setFilter(col, type, subtype, value) {
    if (!explorerData.filters[col]) {
        explorerData.filters[col] = { type: type, value: (type === 'number' ? {} : []) };
    }

    if (type === 'number') {
        if (value === "") delete explorerData.filters[col].value[subtype];
        else explorerData.filters[col].value[subtype] = parseFloat(value);

        // Cleanup if empty
        if (Object.keys(explorerData.filters[col].value).length === 0) delete explorerData.filters[col];

    } else {
        // Category or List (list of strings)
        if (value.length === 0) delete explorerData.filters[col];
        else explorerData.filters[col].value = value;
    }

    updateExplorerStats();
}

function resetFilters() {
    explorerData.filters = {};
    explorerData.searchQuery = "";

    const searchInput = document.getElementById('explorer-search-input');
    if (searchInput) searchInput.value = "";

    // Clear UI widgets
    const inputs = document.querySelectorAll('#explorer-filters input[type="text"], #explorer-filters input[type="number"]');
    inputs.forEach(i => i.value = '');

    // Uncheck categories? Actually renderFilters() rebuilds them from metadata usually?
    // The current renderStats logic re-renders charts.
    // The current logic doesn't seem to rebuild filter WIDGETS, it just clears data.
    // Let's assume re-rendering or clearing logic is robust.

    updateExplorerStats();
}

async function updateExplorerStats() {
    if (!explorerData.activeStudy) return;

    const statsContainer = document.getElementById('explorer-stats');
    const countEl = document.getElementById('explorer-count');

    // countEl.innerText = "Updating...";

    try {
        const res = await fetch('/api/explorer/filter', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                study: explorerData.activeStudy,
                filters: explorerData.filters,
                search_query: explorerData.searchQuery
            })
        });
        const data = await res.json();

        if (data.error) {
            countEl.innerText = "Error";
            return;
        }

        countEl.innerText = `${data.count} items selected`;
        renderStats(data.stats);

    } catch (e) {
        console.error(e);
    }
}

function renderStats(sliceStats) {
    const container = document.getElementById('explorer-stats');
    container.innerHTML = '';

    const metadata = explorerData.metadata;
    if (!metadata || !metadata.total_stats) return;

    const totalStats = metadata.total_stats;
    const sortedCols = Object.keys(sliceStats).sort();

    sortedCols.forEach(col => {
        const sSlice = sliceStats[col];
        const sTotal = totalStats[col];
        const info = metadata[col];

        if (!sSlice || !sTotal) return;

        const card = document.createElement('div');
        card.className = 'stats-card';
        card.style.background = '#2d2d30';
        card.style.padding = '10px';
        card.style.marginBottom = '10px';
        card.style.borderRadius = '4px';

        const title = document.createElement('h3');
        title.innerText = col;
        title.style.marginTop = '0';
        title.style.marginBottom = '5px';
        title.style.fontSize = '1em';
        title.style.color = '#eee';
        card.appendChild(title);

        const plotDiv = document.createElement('div');
        plotDiv.id = `plot-${col.replace(/[^a-zA-Z0-9]/g, '')}`;
        plotDiv.style.width = '100%';
        plotDiv.style.height = '150px'; // Compact
        card.appendChild(plotDiv);
        container.appendChild(card);

        if (sTotal.type === 'density') {
            // Density Plots (Area Charts)
            const traceTotal = {
                x: sTotal.x,
                y: sTotal.y,
                mode: 'lines',
                fill: 'tozeroy',
                type: 'scatter',
                name: 'Total',
                line: { color: '#888', width: 1 },
                fillcolor: 'rgba(120, 120, 120, 0.3)'
            };

            const traceSlice = {
                x: sSlice.x,
                y: sSlice.y,
                mode: 'lines',
                fill: 'tozeroy',
                type: 'scatter',
                name: 'Slice',
                line: { color: '#4CAF50', width: 1 },
                fillcolor: 'rgba(76, 175, 80, 0.5)'
            };

            // Determine Range consistent for Total vs Slice
            const isLog = sTotal.transform === 'log10';
            const xMin = isLog ? Math.log10(sTotal.min + 1) : sTotal.min;
            const xMax = isLog ? Math.log10(sTotal.max + 1) : sTotal.max;

            const layout = {
                margin: { t: 0, b: 20, l: 30, r: 20 },
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0)',
                showlegend: false,
                xaxis: {
                    range: [xMin, xMax],
                    zeroline: false,
                    gridcolor: '#444',
                    type: 'linear', // Always linear now (we manually transformed)
                    title: isLog ? 'Log10(x+1)' : ''
                },
                yaxis: {
                    showgrid: false,
                    showticklabels: false
                }
            };

            // If log, ensure range is positive? 
            // Backend ensures min_val > 0 for log.

            Plotly.newPlot(plotDiv, [traceTotal, traceSlice], layout, { displayModeBar: false });

        } else {
            // Stacked Bar (Horizontal) normalized to %
            // (Used for Category types OR Discrete Integers)

            // Need to merge keys from total and slice to handle missing cats in slice
            const allCats = new Set([...Object.keys(sTotal), ...Object.keys(sSlice)]);
            // Sort: if they look like numbers, numeric sort, else string sort
            const cats = Array.from(allCats).sort((a, b) => {
                const na = parseFloat(a);
                const nb = parseFloat(b);
                if (!isNaN(na) && !isNaN(nb)) return na - nb;
                return a.localeCompare(b);
            });

            // Total counts
            const totalCount = Object.values(sTotal).reduce((a, b) => a + b, 0);
            const sliceCount = Object.values(sSlice).reduce((a, b) => a + b, 0);

            const traces = [];

            cats.forEach(cat => {
                const valTotal = sTotal[cat] || 0;
                const valSlice = sSlice[cat] || 0;

                // Normalize to % to make the bars comparable width (distribution view)
                // Use Math.max(1, count) to avoid div by zero
                const pctTotal = (valTotal / Math.max(1, totalCount)) * 100;
                const pctSlice = (valSlice / Math.max(1, sliceCount)) * 100;

                traces.push({
                    x: [pctTotal, pctSlice],
                    y: ['Total', 'Slice'],
                    name: cat,
                    orientation: 'h',
                    type: 'bar',
                    text: [cat, cat], // Label on hover
                    hoverinfo: 'text+x+name',
                });
            });

            const layout = {
                barmode: 'stack',
                margin: { t: 0, b: 20, l: 50, r: 20 },
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0)',
                showlegend: false,
                xaxis: {
                    range: [0, 100],
                    showgrid: false,
                    zeroline: false
                },
                yaxis: {
                    tickfont: { color: '#ccc' }
                }
            };

            Plotly.newPlot(plotDiv, traces, layout, { displayModeBar: false });
        }
    });
}
