// Video Viewer Logic

let viewerData = {
    metadata: null,
    filters: {},
    activeStudy: null,
    filteredIds: [],
    itemCount: 0,
    currentIndex: -1
};

// Initialization
document.addEventListener('DOMContentLoaded', function () {
    // Only init if tab exists
    if (document.getElementById('video_viewer')) {
        loadViewerStudies();
    }
});

async function loadViewerStudies() {
    const selector = document.getElementById('viewer-study-select');

    try {
        const res = await fetch('/api/explorer/studies'); // Reuse endpoint
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

    const sortedCols = Object.keys(metadata).sort().filter(c => c !== 'total_stats');

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

                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.value = val;
                cb.style.marginRight = '5px';
                cb.onchange = () => {
                    const checked = Array.from(listContainer.querySelectorAll('input:checked')).map(c => c.value);
                    setViewerFilter(col, info.type, 'list', checked);
                };

                const span = document.createElement('span');
                span.innerText = val;
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
    loadViewerMetadata(); // Re-render to clear inputs
}

async function applyViewerFilters() {
    if (!viewerData.activeStudy) return;

    try {
        const res = await fetch('/api/viewer/ids', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                study: viewerData.activeStudy,
                filters: viewerData.filters
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

    Object.keys(item).sort().forEach(key => {
        const tr = document.createElement('tr');
        const tdKey = document.createElement('td');
        tdKey.innerText = key;
        const tdVal = document.createElement('td');

        let val = item[key];
        if (typeof val === 'object' && val !== null) val = JSON.stringify(val);
        tdVal.innerText = val !== null ? val : '';

        tr.appendChild(tdKey);
        tr.appendChild(tdVal);
        tbody.appendChild(tr);
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
    document.getElementById('viewer-index').innerText = `${indexStr} / ${viewerData.itemCount}`;
    document.getElementById('viewer-status').innerText = "Ready";
}
