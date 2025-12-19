// Video Viewer Logic

let viewerData = {
    metadata: null,
    filters: {},
    activeStudy: null,
    filteredIds: [],
    itemCount: 0,
    searchQuery: "",
    sortBy: null,
    sortOrder: 'asc',
    currentIndex: -1
};

// Initialization
document.addEventListener('DOMContentLoaded', function () {
    // Only init if tab exists
    if (document.getElementById('video_viewer')) {
        loadViewerStudies();

        // Search Input Listener
        const searchInput = document.getElementById('viewer-search-input');
        if (searchInput) {
            // Note: Unlike explorer, we might not auto-apply on input change if it's too heavy?
            // "applyViewerFilters" is manual. So we just update state.
            // But user might expect search to be live?
            // Explorer is live (debounced). Viewer requires "Apply". 
            // Let's stick to "Apply" paradigm for Viewer to match existing UI flow, OR make it live?
            // User did not specify. Explorer was live (auto updateStats).
            // Viewer has explicit "Apply Filters" button (line 27 html).
            // So we should just update the state, and let user click Apply.
            // OR we can make it auto-apply.
            // Given "Add it to the video viewer filter as well", and Viewer has explicit Apply, simply updating state is safest.

            searchInput.addEventListener('input', (e) => {
                viewerData.searchQuery = e.target.value;
            });
        }

        // Slider Listener
        const slider = document.getElementById('viewer-slider');
        if (slider) {
            // Live update of index text
            slider.addEventListener('input', (e) => {
                const val = parseInt(e.target.value) - 1;
                // Only update text, don't load yet
                const count = viewerData.itemCount;
                document.getElementById('viewer-index').innerText = `${val + 1} / ${count}`;
            });

            // Load on release (change)
            slider.addEventListener('change', (e) => {
                const val = parseInt(e.target.value) - 1;
                viewerData.currentIndex = val;
                loadViewerItem(val);
            });
        }
    }
});

async function loadViewerStudies() {
    const selector = document.getElementById('viewer-study-select');

    try {
        const res = await fetch('/api/studies/defined'); // Reuse endpoint
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

        if (studies.length > 0) {
            selector.value = studies[0];
            changeViewerStudy(studies[0]);
        }

    } catch (e) {
        console.error("Failed to load studies", e);
        selector.innerHTML = '<option disabled>Error loading studies</option>';
    }
}

function changeViewerStudy(val) {
    const selector = document.getElementById('viewer-study-select');
    const studyName = val || selector.value;

    if (!studyName) return;

    viewerData.activeStudy = studyName;
    viewerData.filters = {};
    viewerData.filteredIds = [];
    viewerData.currentIndex = -1;

    loadViewerMetadata();
    // Also clear player
    document.getElementById('viewer-video').src = "";
    document.getElementById('viewer-metadata').querySelector('tbody').innerHTML = "";
    document.getElementById('viewer-video-msg').style.display = "block";
    updateNavUI();
}

async function loadViewerMetadata() {
    if (!viewerData.activeStudy) return;

    const filterContainer = document.getElementById('viewer-filters');
    filterContainer.innerHTML = '<div style="text-align:center; margin-top:20px;">Loading filters...</div>';

    try {
        const res = await fetch(`/api/explorer/metadata?study=${encodeURIComponent(viewerData.activeStudy)}`);
        const data = await res.json();

        if (data.error) {
            filterContainer.innerHTML = `<div style="color:red; text-align:center;">${data.error}</div>`;
            return;
        }

        // Update File Info Display
        const infoSpan = document.getElementById('viewer-file-info');
        if (infoSpan) {
            if (data.source_file && data.source_file_modified) {
                infoSpan.innerText = `Using file: ${data.source_file} - saved ${data.source_file_modified}`;
            } else {
                infoSpan.innerText = "";
            }
        }

        viewerData.metadata = data;
        renderViewerFilters(data);

        // Initial fetch of IDs with empty filters
        applyViewerFilters();

    } catch (e) {
        console.error(e);
        filterContainer.innerHTML = '<div style="color:red; text-align:center;">Failed to load metadata</div>';
    }
}

function renderViewerFilters(metadata) {
    const container = document.getElementById('viewer-filters');
    container.innerHTML = '';

    // Use filter_priority if available
    const priority = metadata.filter_priority;
    let sortedCols = [];

    if (priority && priority.length > 0) {
        sortedCols = priority.filter(c => metadata[c]);
    } else {
        // Fallback to all (except special)
        sortedCols = Object.keys(metadata).sort().filter(c => c !== 'total_stats');
    }

    // Populate Sort Dropdown
    const sortSelect = document.getElementById('viewer-sort-select');
    if (sortSelect) {
        const currentVal = sortSelect.value || viewerData.sortBy;
        sortSelect.innerHTML = '<option value="">Default (Unsorted)</option>';

        sortedCols.forEach(col => {
            const opt = document.createElement('option');
            opt.value = col;
            opt.text = col;
            if (currentVal === col) opt.selected = true;
            sortSelect.appendChild(opt);
        });

        sortSelect.onchange = (e) => {
            viewerData.sortBy = e.target.value;
        };
    }

    // Init Sort Button
    updateSortBtnUI();

    sortedCols.forEach(col => {
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
            const inputRow = document.createElement('div');
            inputRow.style.display = 'flex';
            inputRow.style.gap = '5px';

            const minInput = document.createElement('input');
            minInput.type = 'number';
            minInput.placeholder = `Min (${info.min})`;
            minInput.style.width = '50%';
            minInput.onchange = (e) => setViewerFilter(col, 'number', 'min', e.target.value);

            const maxInput = document.createElement('input');
            maxInput.type = 'number';
            maxInput.placeholder = `Max (${info.max})`;
            maxInput.style.width = '50%';
            maxInput.onchange = (e) => setViewerFilter(col, 'number', 'max', e.target.value);

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

                // Handle new object format {value: "v", count: 123}
                if (typeof val === 'object' && val !== null && val.value !== undefined) {
                    actualValue = val.value;
                    displayValue = `${val.value} (${val.count.toLocaleString()})`;
                }

                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.value = actualValue;
                cb.dataset.rawValue = actualValue;
                cb.style.marginRight = '5px';

                // Restore checked state if filter exists
                if (viewerData.filters[col] && Array.isArray(viewerData.filters[col].value)) {
                    if (viewerData.filters[col].value.includes(actualValue)) {
                        cb.checked = true;
                    }
                }

                cb.onchange = () => {
                    const checked = Array.from(listContainer.querySelectorAll('input:checked')).map(c => c.dataset.rawValue);
                    console.log(`Viewer filtering ${col} with:`, checked);
                    setViewerFilter(col, info.type, 'list', checked);
                };

                const span = document.createElement('span');
                span.innerText = displayValue;
                span.style.fontSize = '0.9em';

                item.appendChild(cb);
                item.appendChild(span);
                listContainer.appendChild(item);
            });
            wrapper.appendChild(listContainer);
        }
        container.appendChild(wrapper);
    });
}

function setViewerFilter(col, type, subtype, value) {
    if (!viewerData.filters[col]) {
        viewerData.filters[col] = { type: type, value: (type === 'number' ? {} : []) };
    }

    if (type === 'number') {
        if (value === "") delete viewerData.filters[col].value[subtype];
        else viewerData.filters[col].value[subtype] = parseFloat(value);
        if (Object.keys(viewerData.filters[col].value).length === 0) delete viewerData.filters[col];
    } else {
        if (value.length === 0) delete viewerData.filters[col];
        else viewerData.filters[col].value = value;
    }
    // We do NOT auto-apply filters here to avoid constant reloading of ID list. 
    // User must click "Apply Filters".
}

function resetViewerFilters() {
    viewerData.filters = {};
    viewerData.searchQuery = "";

    const searchInput = document.getElementById('viewer-search-input');
    if (searchInput) searchInput.value = "";

    loadViewerMetadata(); // Re-render to clear inputs
}

function toggleViewerSort() {
    viewerData.sortOrder = viewerData.sortOrder === 'asc' ? 'desc' : 'asc';
    updateSortBtnUI();
    // Only apply if we have a sort key selected? 
    // Or just apply anyway (backend handles it)
    if (viewerData.sortBy) {
        applyViewerFilters();
    }
}

function updateSortBtnUI() {
    const btn = document.getElementById('viewer-sort-btn');
    if (btn) {
        btn.innerText = viewerData.sortOrder === 'asc' ? 'ASC' : 'DESC';
        // Optional: change color or icon
    }
}

async function applyViewerFilters() {
    if (!viewerData.activeStudy) return;

    try {
        const res = await fetch('/api/viewer/ids', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                study: viewerData.activeStudy,
                filters: viewerData.filters,
                search_query: viewerData.searchQuery,
                sort_by: viewerData.sortBy,
                sort_order: viewerData.sortOrder
            })
        });
        const data = await res.json();

        if (data.error) {
            alert(data.error);
            return;
        }

        viewerData.filteredIds = data.ids;
        viewerData.itemCount = data.count;

        // Reset to first item
        if (viewerData.itemCount > 0) {
            viewerData.currentIndex = 0;
            loadViewerItem(0);
        } else {
            viewerData.currentIndex = -1;
            updateNavUI();
            // Clear display
            document.getElementById('viewer-video').src = "";
            document.getElementById('viewer-metadata').querySelector('tbody').innerHTML = "<tr><td>No items found</td></tr>";
        }

    } catch (e) {
        console.error(e);
        alert("Failed to filter items");
    }
}

async function loadViewerItem(index) {
    if (index < 0 || index >= viewerData.itemCount) return;

    const itemId = viewerData.filteredIds[index];

    // Update UI Loading state?
    document.getElementById('viewer-status').innerText = `Loading ${itemId}...`;

    try {
        const res = await fetch(`/api/viewer/item/${encodeURIComponent(viewerData.activeStudy)}/${itemId}`);
        const item = await res.json();

        if (item.error) {
            document.getElementById('viewer-status').innerText = "Error loading item";
            return;
        }

        renderMetadata(item);

        // Load Video
        const videoEl = document.getElementById('viewer-video');
        const videoUrl = `/api/video/${encodeURIComponent(viewerData.activeStudy)}/${itemId}`;

        // Only change src if different to prevent flicker if we were just reloading meta? 
        // No, assuming distinct items always different.
        videoEl.src = videoUrl;
        document.getElementById('viewer-video-msg').style.display = "none";

        updateNavUI();

    } catch (e) {
        console.error(e);
        document.getElementById('viewer-status').innerText = "Error";
    }
}

function renderMetadata(item) {
    const tbody = document.getElementById('viewer-metadata').querySelector('tbody');
    tbody.innerHTML = '';

    const priorityList = viewerData.metadata && viewerData.metadata.display_priority ? viewerData.metadata.display_priority : [];
    const schemaMap = viewerData.metadata && viewerData.metadata.schema_map ? viewerData.metadata.schema_map : {};

    // Group items by Section
    const sections = {};
    const generalSection = "General";

    Object.keys(item).forEach(key => {
        let section = generalSection;
        if (schemaMap[key] && schemaMap[key].section) {
            section = schemaMap[key].section;
            if (!section || section.trim() === "") section = generalSection;
        }

        if (!sections[section]) sections[section] = [];
        sections[section].push(key);
    });

    // Sort Sections (General first? Or alphabetical? Let's do alphabetical, General last?)
    let sectionNames = Object.keys(sections).sort();

    // Sort variables within sections
    sectionNames.forEach(sec => {
        sections[sec].sort((a, b) => {
            const idxA = priorityList.indexOf(a);
            const idxB = priorityList.indexOf(b);

            // If both in list, sort by index (Priority)
            if (idxA !== -1 && idxB !== -1) return idxA - idxB;

            // If only A in list, A comes first
            if (idxA !== -1) return -1;

            // If only B in list, B comes first
            if (idxB !== -1) return 1;

            // Neither in list: alphabetical fallback
            return a.localeCompare(b);
        });
    });

    // Render
    sectionNames.forEach(sec => {
        const keys = sections[sec];
        if (keys.length === 0) return;

        // Section Header (Collapsible)
        // We put rows inside a tbody? No, we are INSIDE a tbody already (passed in selector).
        // HTML tables don't support nested divs easily for toggle.
        // Better: Use a single row for header, spanning 2 columns. 
        // AND toggle visibility of subsequent rows.

        // Alternatively, use multiple tbodies if parent allows?
        // document.getElementById('viewer-metadata') is a table.
        // We can append multiple tbodies to the TABLE, not inside a single tbody.
        // But the helper passed "tbody" as the container.
        // Let's change helper to clear table content and assume sections.

        // Wait, the DOM structure is static in HTML: 
        // <table id="viewer-metadata"> <tbody> <!-- Rows --> </tbody> </table>
        // It's cleaner to inject header rows.

        const headerRow = document.createElement('tr');
        headerRow.style.background = '#3e3e42';
        headerRow.style.cursor = 'pointer';

        const headerCell = document.createElement('td');
        headerCell.colSpan = 2;
        headerCell.style.padding = '8px';
        headerCell.style.fontWeight = 'bold';
        headerCell.style.color = '#fff';
        headerCell.innerHTML = `&#9662; ${sec}`; // Down arrow default
        headerRow.appendChild(headerCell);

        tbody.appendChild(headerRow);

        // Variables
        const rowGroups = [];
        keys.forEach(key => {
            const tr = document.createElement('tr');
            const tdKey = document.createElement('td');
            tdKey.innerText = key;

            // Tooltip
            if (schemaMap[key] && schemaMap[key].description) {
                // Remove title attribute to prevent native tooltip
                tdKey.removeAttribute('title');

                // Use custom CSS tooltip
                tdKey.classList.add('meta-tooltip');
                tdKey.setAttribute('data-tooltip', schemaMap[key].description);
            }

            const tdVal = document.createElement('td');

            let val = item[key];
            let displayVal = '';

            if (val === null || val === undefined) {
                displayVal = '';
            } else if (Array.isArray(val)) {
                displayVal = val.join(', ');
            } else if (typeof val === 'number') {
                if (key === 'item_id' || key === 'video_id' || key === 'G_id') {
                    displayVal = String(val);
                } else {
                    displayVal = val.toLocaleString();
                }
            } else if (typeof val === 'object') {
                displayVal = JSON.stringify(val);
            } else {
                displayVal = String(val);
            }

            tdVal.innerText = displayVal;

            tr.appendChild(tdKey);
            tr.appendChild(tdVal);
            tbody.appendChild(tr);
            rowGroups.push(tr);
        });

        // Click handler for collapse
        headerRow.onclick = () => {
            const isHidden = rowGroups[0].style.display === 'none';
            rowGroups.forEach(r => r.style.display = isHidden ? '' : 'none');
            headerCell.innerHTML = isHidden ? `&#9662; ${sec}` : `&#9656; ${sec}`; // Down vs Right arrow
        };
    });
}


function nextVideo() {
    if (viewerData.currentIndex < viewerData.itemCount - 1) {
        viewerData.currentIndex++;
        loadViewerItem(viewerData.currentIndex);
    }
}

function prevVideo() {
    if (viewerData.currentIndex > 0) {
        viewerData.currentIndex--;
        loadViewerItem(viewerData.currentIndex);
    }
}

function updateNavUI() {
    const indexStr = viewerData.currentIndex >= 0 ? (viewerData.currentIndex + 1) : 0;
    const count = viewerData.itemCount;
    document.getElementById('viewer-index').innerText = `${indexStr} / ${count}`;
    document.getElementById('viewer-status').innerText = "Ready";

    // Update Slider
    const slider = document.getElementById('viewer-slider');
    if (slider) {
        if (count > 0) {
            slider.disabled = false;
            slider.max = count;
            slider.value = indexStr; // 1-based usually for range if min=1
            // If currentIndex is -1 (empty), value 0? min is 1.
            if (viewerData.currentIndex === -1) {
                slider.value = 1;
                slider.disabled = true;
            }
        } else {
            slider.disabled = true;
            slider.max = 1;
            slider.value = 1;
        }
    }
}
