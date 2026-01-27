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
    currentIndex: -1,
    userTags: {},
    activeModal: { item_id: null, variable: null, currentTags: [] }
};

// Initialization
document.addEventListener('DOMContentLoaded', function () {
    // Only init if tab exists
    if (document.getElementById('video_viewer')) {
        loadViewerStudies();
        loadUserTags();

        // Tagging Input Listener
        const tagInput = document.getElementById('tagging-input');
        if (tagInput) {
            tagInput.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') {
                    const val = tagInput.value.trim();
                    if (!val) return;

                    // Split by semicolon
                    const tags = val.split(';').map(t => t.trim()).filter(t => t.length > 0);

                    let added = false;
                    tags.forEach(tag => {
                        if (!viewerData.activeModal.currentTags.includes(tag)) {
                            viewerData.activeModal.currentTags.push(tag);
                            added = true;
                        }
                    });

                    if (added) {
                        renderModalChips();
                        tagInput.value = "";
                    }
                }
            });
        }

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

async function loadUserTags() {
    try {
        const res = await fetch('/api/viewer/tags');
        if (res.ok) {
            viewerData.userTags = await res.json();
        }
    } catch (e) { console.error("Failed to load tags", e); }
}

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
        const res = await fetch(`/api/explorer/metadata?study=${encodeURIComponent(viewerData.activeStudy)}&context=viewer`);
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

// Initialize collapsed state from localStorage
if (!viewerData.collapsedFilters) {
    try {
        viewerData.collapsedFilters = JSON.parse(localStorage.getItem('viewer_collapsed_filters') || '[]');
    } catch (e) {
        viewerData.collapsedFilters = [];
    }
}

function renderViewerFilters(metadata) {
    const container = document.getElementById('viewer-filters');
    container.innerHTML = '';

    // Use filter_priority if available
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

    // Sort Sections (Reusing logic from renderMetadata)
    // Use display_priority for section ordering to match Metadata Panel
    const sortPriority = metadata.display_priority && metadata.display_priority.length > 0 ? metadata.display_priority : priority;

    let sectionNames = Object.keys(sections).sort((a, b) => {
        const getSectionPrio = (secName) => {
            const vars = sections[secName] || [];
            let minPrio = 999999;
            vars.forEach(v => {
                const idx = sortPriority ? sortPriority.indexOf(v) : -1;
                // If not in priority list, treat as high number
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

    // Populate Sort Dropdown (only once or update?)
    const sortSelect = document.getElementById('viewer-sort-select');
    if (sortSelect) {
        const currentVal = sortSelect.value || viewerData.sortBy;
        sortSelect.innerHTML = '<option value="">Default (Unsorted)</option>';
        // Flatten sortedCols for dropdown? Or just all available?
        // Let's use the sorted section order -> sorted vars
        let dropdownCols = [];
        sectionNames.forEach(sec => {
            // Sort variables within section
            sections[sec].sort((a, b) => {
                const idxA = priority ? priority.indexOf(a) : -1;
                const idxB = priority ? priority.indexOf(b) : -1;
                if (idxA !== -1 && idxB !== -1) return idxA - idxB;
                if (idxA !== -1) return -1;
                if (idxB !== -1) return 1;
                return a.localeCompare(b);
            });
            dropdownCols.push(...sections[sec]);
        });

        dropdownCols.forEach(col => {
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
    updateSortBtnUI();

    // Render Sections
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

        const isCollapsed = viewerData.collapsedFilters.includes(sec);
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
                // Show
                body.style.display = 'block';
                header.innerHTML = `<span style="margin-right:8px; width:15px; display:inline-block;">&#9662;</span> ${sec}`;
                // Remove from collapsed list
                viewerData.collapsedFilters = viewerData.collapsedFilters.filter(s => s !== sec);
            } else {
                // Hide
                body.style.display = 'none';
                header.innerHTML = `<span style="margin-right:8px; width:15px; display:inline-block;">&#9656;</span> ${sec}`;
                // Add to collapsed list
                if (!viewerData.collapsedFilters.includes(sec)) {
                    viewerData.collapsedFilters.push(sec);
                }
            }
            // Persist
            localStorage.setItem('viewer_collapsed_filters', JSON.stringify(viewerData.collapsedFilters));
        };

        sectionDiv.appendChild(header);

        // Render variables inside
        vars.forEach(col => {
            const info = metadata[col];
            const wrapper = document.createElement('div');
            wrapper.className = 'filter-group';
            wrapper.style.marginBottom = '15px';
            wrapper.style.borderBottom = '1px solid #333';
            wrapper.style.paddingBottom = '10px';

            const label = document.createElement('label');

            let displayName = col;
            if (schemaMap && schemaMap[col] && schemaMap[col].display_name) {
                displayName = schemaMap[col].display_name;
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
                labelRow.style.marginTop = '-5px'; // Tweak spacing

                const minLabel = document.createElement('span');
                const maxLabel = document.createElement('span');

                labelRow.appendChild(minLabel);
                labelRow.appendChild(maxLabel);
                wrapper.appendChild(labelRow);

                // Current Values
                let currentMin = info.min;
                let currentMax = info.max;

                if (viewerData.filters[col] && viewerData.filters[col].value) {
                    if (viewerData.filters[col].value.min !== undefined) currentMin = viewerData.filters[col].value.min;
                    if (viewerData.filters[col].value.max !== undefined) currentMax = viewerData.filters[col].value.max;
                }

                // Helper format 
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
                            step: (info.max - info.min) > 100 ? 1 : ((info.max - info.min) / 100) // Adaptive step
                        });

                        // Debounce slider updates
                        let debounceTimer;
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

                            // Update Data
                            if (!viewerData.filters[col]) viewerData.filters[col] = { type: 'number', value: {} };
                            viewerData.filters[col].value.min = vMin;
                            viewerData.filters[col].value.max = vMax;

                            updateViewerStats();
                        });
                    }
                } else {
                    // Fallback for no slider lib (unlikely but safe)
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
            body.appendChild(wrapper);
        });

        sectionDiv.appendChild(body);
        container.appendChild(sectionDiv);
    });
}

function setViewerFilter(col, type, subtype, value) {
    if (!viewerData.filters[col]) {
        viewerData.filters[col] = { type: type, value: (type === 'number' ? {} : []) };
    }

    if (subtype === 'na') {
        viewerData.filters[col].na = value;
        if (!viewerData.filters[col].na) delete viewerData.filters[col].na;
    } else if (type === 'number') {
        if (value === "") delete viewerData.filters[col].value[subtype];
        else viewerData.filters[col].value[subtype] = parseFloat(value);
    } else {
        if (value.length === 0) viewerData.filters[col].value = [];
        else viewerData.filters[col].value = value;
    }

    // Cleanup
    const hasValue = (type === 'number') ?
        (viewerData.filters[col].value && Object.keys(viewerData.filters[col].value).length > 0) :
        (viewerData.filters[col].value && viewerData.filters[col].value.length > 0);

    const hasNa = !!viewerData.filters[col].na;

    if (!hasValue && !hasNa) {
        delete viewerData.filters[col];
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

    const hideDuplicates = document.getElementById('viewer-hide-duplicates')?.checked || false;

    try {
        const res = await fetch('/api/viewer/ids', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                study: viewerData.activeStudy,
                filters: viewerData.filters,
                search_query: viewerData.searchQuery,
                sort_by: viewerData.sortBy,
                sort_order: viewerData.sortOrder,
                hide_duplicates: hideDuplicates
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
        // Use POST to send context (filters) so backend picks the right row if duplicates exist
        const res = await fetch(`/api/viewer/item/${encodeURIComponent(viewerData.activeStudy)}/${itemId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                filters: viewerData.filters,
                search_query: viewerData.searchQuery
            })
        });
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

        // Check if tab is visible before playing
        const viewerTab = document.getElementById('video_viewer');
        if (viewerTab && viewerTab.classList.contains('active')) {
            // Check User Settings for Autostart
            // If undefined, default to false (as requested "default unchecked")
            if (window.userSettings && window.userSettings.video_autostart) {
                videoEl.play().catch(e => console.log("Auto-play blocked or failed:", e));
            }
        }

        updateNavUI();

    } catch (e) {
        console.error(e);
        document.getElementById('viewer-status').innerText = "Error";
    }
}

function linkify(text) {
    if (!text) return '';
    // Simple escape to prevent XSS before inserting as HTML
    const escaped = String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");

    // URL Regex
    const urlRegex = /(https?:\/\/[^\s]+)/g;
    return escaped.replace(urlRegex, function (url) {
        return `<a href="${url}" target="_blank" style="color: #4daafc; text-decoration: underline;">${url}</a>`;
    });
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
        // FILTER: Only show if BOTH display_name and web_display_prio are present
        const schema = schemaMap[key];
        if (!schema || !schema.display_name || schema.web_display_prio === undefined) {
            return;
        }

        let section = generalSection;
        if (schema.section) {
            section = schema.section;
            if (!section || section.trim() === "") section = generalSection;
        }

        if (!sections[section]) sections[section] = [];
        sections[section].push(key);
    });

    // Sort Sections based on the LOWEST (best) display_priority of any variable in that section
    let sectionNames = Object.keys(sections).sort((a, b) => {
        const getSectionPrio = (secName) => {
            const vars = sections[secName] || [];
            let minPrio = 999999;
            vars.forEach(v => {
                const idx = priorityList.indexOf(v);
                if (idx !== -1 && idx < minPrio) {
                    minPrio = idx;
                }
            });
            return minPrio;
        };

        const prioA = getSectionPrio(a);
        const prioB = getSectionPrio(b);

        if (prioA !== prioB) return prioA - prioB;
        return a.localeCompare(b);
    });

    // --- DEBUG LOGGING ---
    sectionNames.forEach(sec => {
        const vars = sections[sec];
        let minPrio = 999999;
        vars.forEach(v => {
            const idx = priorityList.indexOf(v);
            if (idx !== -1 && idx < minPrio) minPrio = idx;
        });
    });
    // ---------------------

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
            // --- Tagging & Tooltip ---
            tdKey.style.cursor = 'pointer';
            const itemIdStr = String(item['item_id']);
            const itemTags = (viewerData.userTags[itemIdStr]?.[key]) || [];

            // Shared Annotations
            const sharedData = item['shared_annotations']?.[key] || {};
            const hasShared = Object.keys(sharedData).length > 0;

            // Determine Display Name
            let displayName = key;
            if (schemaMap[key] && schemaMap[key].display_name) {
                displayName = schemaMap[key].display_name;
            }

            // Set Display Text Logic
            // Priorities:
            // 1. Own Tags -> Green
            // 2. Shared Tags -> Blue
            // 3. Both -> Green Text + Blue Icon/Indicator

            let displayText = displayName;
            let styleColor = '';
            let styleWeight = '';
            let decoration = '';
            let suffix = '';

            const myCount = itemTags.length;

            // Check for Notes/CC (My)
            const notesKey = `${key}__NOTES`;
            const itemNotes = viewerData.userTags[itemIdStr]?.[notesKey];
            const ccKey = `${key}__CLOSED_TAGGING`;
            const itemCC = viewerData.userTags[itemIdStr]?.[ccKey];
            const hasMyAnnotation = myCount > 0 || itemNotes || itemCC;

            if (hasMyAnnotation) {
                styleColor = '#4CAF50';
                styleWeight = 'bold';
                if (myCount > 0) displayText += ` [${myCount}]`;

                if (itemNotes || itemCC) {
                    decoration = 'underline dotted #4CAF50';
                }
            } else if (hasShared) {
                // Only shared
                styleColor = '#4daafc'; // Blue
                styleWeight = 'normal'; // Or bold? Let's keep normal but colored to distinguish from mine
                // Count total other tags?
                let otherTagCount = 0;
                Object.values(sharedData).forEach(u => {
                    if (u.tags) otherTagCount += u.tags.length;
                });
                if (otherTagCount > 0) displayText += ` [${otherTagCount}]`;

                decoration = 'underline dotted #4daafc'; // Shared implie annotation
            }

            if (styleColor) tdKey.style.color = styleColor;
            if (styleWeight) tdKey.style.fontWeight = styleWeight;
            if (decoration) tdKey.style.textDecoration = decoration;

            // If BOTH, add indicator for shared
            if (hasMyAnnotation && hasShared) {
                suffix = ' <span style="color:#4daafc" title="Annotated by others">👥</span>';
            } else if (hasShared && !hasMyAnnotation) {
                suffix = ' <span style="color:#4daafc" title="Annotated by others">👥</span>';
            }

            tdKey.innerHTML = displayText + suffix;


            // Construct Tooltip (Tags + Description)
            let tooltipParts = [];

            // 1. My Annotations
            if (hasMyAnnotation) {
                tooltipParts.push("--- MY ANNOTATIONS ---");
                if (myCount > 0) tooltipParts.push(`Tags: ${itemTags.join(', ')}`);
                if (itemCC) tooltipParts.push(`Closed Tagging: ${itemCC}`);
                if (itemNotes) tooltipParts.push(`Notes: ${itemNotes}`);
                tooltipParts.push(""); // Spacer
            }

            // 2. Shared Annotations
            if (hasShared) {
                tooltipParts.push("--- SHARED ANNOTATIONS ---");

                // Aggregate anonymously
                let allTags = new Set();
                let allNotes = [];
                let allClosed = new Set();

                // Iterate sharedData values (which are user objects)
                Object.values(sharedData).forEach(uData => {
                    if (uData.tags && Array.isArray(uData.tags)) {
                        uData.tags.forEach(t => allTags.add(t));
                    }
                    if (uData.notes) allNotes.push(uData.notes);
                    if (uData.closed) allClosed.add(uData.closed);
                });

                if (allTags.size > 0) {
                    tooltipParts.push(`Tags: ${Array.from(allTags).sort().join(', ')}`);
                }
                if (allClosed.size > 0) {
                    tooltipParts.push(`Closed: ${Array.from(allClosed).join(', ')}`);
                }
                if (allNotes.length > 0) {
                    tooltipParts.push("Notes:");
                    allNotes.forEach(n => tooltipParts.push(`- ${n}`));
                }

                tooltipParts.push("");
            }

            // 3. Description
            if (schemaMap[key] && schemaMap[key].description) {
                if (tooltipParts.length > 0) tooltipParts.push("--- DESCRIPTION ---");
                tooltipParts.push(schemaMap[key].description);
            }

            if (tooltipParts.length > 0) {
                tdKey.classList.add('meta-tooltip');
                // Join with newline. CSS handles whitespace: pre-wrap
                tdKey.setAttribute('data-tooltip', tooltipParts.join('\n'));
                tdKey.removeAttribute('title');
            } else {
                tdKey.title = "Click to add tags";
            }

            tdKey.onclick = (e) => {
                e.stopPropagation();
                openTaggingModal(itemIdStr, key, itemTags);
            };
            // ---------------

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
                } else if (key.includes('_timestamp')) {
                    // Try to parse timestamp
                    try {
                        let ts = val;
                        // Heuristic: if ts > 1e11 (100 billion), likely ms (valid after 1973).
                        // If < 1e11, likely seconds.
                        // Current time ~1.7e9 (seconds) or 1.7e12 (ms).
                        // So 1e11 is a safe divider.
                        if (ts < 1e11) ts *= 1000;

                        const date = new Date(ts);
                        if (!isNaN(date.getTime())) {
                            // Format dd/mm/yy hh:mm:ss
                            const day = String(date.getDate()).padStart(2, '0');
                            const month = String(date.getMonth() + 1).padStart(2, '0');
                            const year = String(date.getFullYear()).slice(-2);
                            const hours = String(date.getHours()).padStart(2, '0');
                            const minutes = String(date.getMinutes()).padStart(2, '0');
                            const seconds = String(date.getSeconds()).padStart(2, '0');
                            displayVal = `${day}/${month}/${year} ${hours}:${minutes}:${seconds}`;
                        } else {
                            displayVal = String(val);
                        }
                    } catch (e) {
                        displayVal = String(val);
                    }
                } else {
                    displayVal = val.toLocaleString();
                }
            } else if (typeof val === 'object') {
                displayVal = JSON.stringify(val);
            } else {
                displayVal = String(val);
            }

            tdVal.innerHTML = linkify(displayVal);

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

function playViewerVideo() {
    const video = document.getElementById('viewer-video');
    if (video && video.src && !video.paused) {
        // Already playing
    } else if (video && video.src) {
        video.play().catch(e => console.log("Play failed:", e));
    }
}

function pauseViewerVideo() {
    const video = document.getElementById('viewer-video');
    if (video && !video.paused) {
        video.pause();
    }
}

// --- Tagging Logic ---
function openTaggingModal(itemId, variable, currentTags) {
    viewerData.activeModal = { item_id: itemId, variable: variable, currentTags: [...currentTags] };

    // Determine Display Name for Title
    let displayName = variable;
    const schemaMap = viewerData.metadata && viewerData.metadata.schema_map ? viewerData.metadata.schema_map : {};
    if (schemaMap[variable] && schemaMap[variable].display_name) {
        displayName = schemaMap[variable].display_name;
    }

    document.getElementById('tagging-modal-title').innerText = `Tags and Notes for ${displayName}`;
    document.getElementById('tagging-input').value = "";

    // Load Notes
    const notesKey = `${variable}__NOTES`;
    const notes = viewerData.userTags[itemId]?.[notesKey] || "";
    const notesArea = document.getElementById('tagging-notes');
    if (notesArea) notesArea.value = notes;

    // Closed Tagging Setup
    const ccContainer = document.getElementById('tagging-closed-tagging-container');
    const ccSelect = document.getElementById('tagging-closed-tagging');

    if (ccContainer && ccSelect) {
        // Check metadata for accepted_labels
        const schema = viewerData.metadata?.schema_map?.[variable];
        const acceptedLabels = schema?.accepted_labels; // Array of strings if present

        if (acceptedLabels && Array.isArray(acceptedLabels) && acceptedLabels.length > 0) {
            ccContainer.style.display = 'block';
            ccSelect.innerHTML = '<option value="" disabled selected>Select an option...</option>'; // Reset

            acceptedLabels.forEach(label => {
                const opt = document.createElement('option');
                opt.value = label;
                opt.innerText = label;
                ccSelect.appendChild(opt);
            });

            // Set existing value if any
            const ccKey = `${variable}__CLOSED_TAGGING`;
            const existingCC = viewerData.userTags[itemId]?.[ccKey];
            if (existingCC) {
                ccSelect.value = existingCC;
            } else {
                ccSelect.selectedIndex = 0; // Select placeholder
            }

        } else {
            ccContainer.style.display = 'none';
        }
    }

    // Calculate global available tags
    let allTags = new Set();
    // userTags structure is now { item_id: { variable: [tags...] } }
    Object.values(viewerData.userTags || {}).forEach(itemVars => {
        Object.values(itemVars).forEach(tagList => {
            if (Array.isArray(tagList)) tagList.forEach(t => allTags.add(t));
        });
    });
    viewerData.activeModal.allTags = Array.from(allTags).sort();

    renderModalChips();
    document.getElementById('tagging-modal').style.display = "flex";
    document.getElementById('tagging-input').focus();
}

function closeTaggingModal() {
    document.getElementById('tagging-modal').style.display = "none";
}

function renderModalChips() {
    const container = document.getElementById('tagging-quick-select');
    container.innerHTML = "";

    const { currentTags, allTags } = viewerData.activeModal;

    // Merge current tags with all historical tags
    const displayTags = new Set([...currentTags, ...(allTags || [])]);
    const sortedTags = Array.from(displayTags).sort();

    sortedTags.forEach(tag => {
        const isSelected = currentTags.includes(tag);
        const chip = document.createElement('div');

        // Style: Blue if selected, Gray if available
        const bg = isSelected ? '#007acc' : '#444';
        const color = '#fff';
        const border = isSelected ? '1px solid #009ce6' : '1px solid #555';

        chip.style.cssText = `background:${bg};color:${color};border:${border};padding:4px 10px;border-radius:12px;display:flex;gap:5px;align-items:center;cursor:pointer;user-select:none;font-size:0.9em;transition:all 0.1s;`;

        // Chip content
        if (isSelected) {
            chip.innerHTML = `<span>${tag}</span><span style="font-weight:bold;margin-left:4px;">×</span>`;
            chip.onclick = () => removeTag(tag);
        } else {
            chip.innerHTML = `<span>${tag}</span>`;
            chip.onclick = () => addTag(tag);
        }

        container.appendChild(chip);
    });
}

function addTag(tag) {
    if (!viewerData.activeModal.currentTags.includes(tag)) {
        viewerData.activeModal.currentTags.push(tag);
        renderModalChips();
    }
}

function removeTag(tag) {
    viewerData.activeModal.currentTags = viewerData.activeModal.currentTags.filter(t => t !== tag);
    renderModalChips();
}

async function saveTaggingModal() {
    // Check for pending input and add it if present
    const tagInput = document.getElementById('tagging-input');
    if (tagInput && tagInput.value.trim()) {
        const val = tagInput.value.trim();
        const tags = val.split(';').map(t => t.trim()).filter(t => t.length > 0);

        tags.forEach(tag => {
            if (!viewerData.activeModal.currentTags.includes(tag)) {
                viewerData.activeModal.currentTags.push(tag);
            }
        });
    }

    // Get Notes
    const notesInput = document.getElementById('tagging-notes');
    const notesVal = notesInput ? notesInput.value : "";

    // Get Closed Tagging
    const ccSelect = document.getElementById('tagging-closed-tagging');
    const ccContainer = document.getElementById('tagging-closed-tagging-container');
    let ccVal = null;
    if (ccContainer && ccContainer.style.display !== 'none' && ccSelect) {
        if (ccSelect.value) ccVal = ccSelect.value;
    }

    const { item_id, variable, currentTags } = viewerData.activeModal;
    // console.log("Saving tags:", { study: viewerData.activeStudy, item_id, variable, tags: currentTags });

    if (!item_id) return; // Study not strictly required for key, but good to have active

    try {
        const res = await fetch('/api/viewer/tags/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                study: viewerData.activeStudy, // Optional now
                item_id: item_id,
                variable: variable,
                tags: currentTags,
                notes: notesVal,
                closed_tagging: ccVal
            })
        });

        if (res.ok) {
            // Update local state (Global structure)
            if (!viewerData.userTags[item_id]) viewerData.userTags[item_id] = {};
            viewerData.userTags[item_id][variable] = currentTags;

            // Update local Notes state
            const notesKey = `${variable}__NOTES`;
            if (notesVal && notesVal.trim()) {
                viewerData.userTags[item_id][notesKey] = notesVal.trim();
            } else {
                delete viewerData.userTags[item_id][notesKey];
            }

            // Update local Closed Tagging state
            const ccKey = `${variable}__CLOSED_TAGGING`;
            if (ccVal) {
                viewerData.userTags[item_id][ccKey] = ccVal;
            } else {
                delete viewerData.userTags[item_id][ccKey];
            }

            loadViewerItem(viewerData.currentIndex); // Re-render
            closeTaggingModal();
        }
    } catch (e) { console.error(e); alert("Error saving tags"); }
}
