
// data_management.js

let allStudies = [];
const savingStudies = new Set(); // Track studies currently being saved

function loadStudies() {
    fetch('/api/manage/studies')
        .then(response => response.json())
        .then(data => {
            allStudies = data;
            renderStudiesTable();
            if (typeof populateEnrichmentStudySelect === 'function') {
                populateEnrichmentStudySelect(data);
            }
        })
        .catch(err => console.error("Error loading studies:", err));
}

let availableCollections = [];

function loadAvailableCollections() {
    fetch('/api/manage/collections')
        .then(res => res.json())
        .then(data => {
            availableCollections = data;

            const ucEl = document.getElementById('ingest-unique-collections-count');
            if (ucEl) ucEl.textContent = availableCollections.length.toLocaleString();

            loadStudies();

            const editContainer = document.getElementById('edit-activity-list-container');
            if (editContainer) {
                renderEditActivityTable(editContainer);
                const searchInput = document.getElementById('edit-activity-search');
                if (searchInput && searchInput.value) {
                    filterEditActivityCollections(searchInput);
                }
            }
        })
        .catch(err => {
            console.error("Error loading collections list:", err);
            loadStudies();

            const editContainer = document.getElementById('edit-activity-list-container');
            if (editContainer) {
                renderEditActivityTable(editContainer);
                const searchInput = document.getElementById('edit-activity-search');
                if (searchInput && searchInput.value) {
                    filterEditActivityCollections(searchInput);
                }
            }
        });
}

// Global cache for roles
let systemRoles = [];

function loadSystemRoles() {
    fetch('/api/admin/roles')
        .then(res => res.json())
        .then(data => {
            systemRoles = data;
        })
        .catch(err => console.error("Error loading roles:", err));
}

// --------------------------------------------------------------------------
// Collection Selector Helper Logic
// --------------------------------------------------------------------------

function renderCollectionSelector(container, selectedList) {
    if (!container) return;

    container.innerHTML = '';
    const selectedSet = new Set(selectedList || []);

    if (availableCollections.length === 0) {
        container.innerHTML = '<div style="padding: 10px; color: var(--color-text-tertiary);">No collections available.</div>';
        return;
    }

    const table = document.createElement('table');
    table.className = 'collection-table';
    table.style.width = '100%';
    table.style.borderCollapse = 'collapse';
    table.style.color = 'var(--color-text-secondary)';
    table.classList.add('text-sm');

    // Create Header
    const thead = document.createElement('thead');
    thead.innerHTML = `
        <tr style="text-align: left;">
            <th style="padding: 8px 5px; width: 30px; position: sticky; top: 0; background: var(--color-border); z-index: 10; border-bottom: 2px solid var(--color-border-strong);"></th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid var(--color-border-strong);" onclick="sortCollectionTable(this)">Collection / Display ID</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid var(--color-border-strong);" onclick="sortCollectionTable(this)">Tags</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid var(--color-border-strong);" onclick="sortCollectionTable(this)">Email</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid var(--color-border-strong);" onclick="sortCollectionTable(this)">Name</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid var(--color-border-strong);" onclick="sortCollectionTable(this)">TikTok</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid var(--color-border-strong);" onclick="sortCollectionTable(this)">Age</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid var(--color-border-strong);" onclick="sortCollectionTable(this)">Country</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid var(--color-border-strong);" onclick="sortCollectionTable(this)">PostCode</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid var(--color-border-strong);" onclick="sortCollectionTable(this)">Active Days</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid var(--color-border-strong);" onclick="sortCollectionTable(this)">Total Events</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid var(--color-border-strong);" onclick="sortCollectionTable(this)">Last Event</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid var(--color-border-strong);" onclick="sortCollectionTable(this)">Added</th>
        </tr>
    `;
    table.appendChild(thead);

    const tbody = document.createElement('tbody');

    availableCollections.forEach(itemInfo => {
        const item = typeof itemInfo === 'string' ? itemInfo : itemInfo.id;

        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid var(--chart-grid)';
        tr.className = 'collection-item'; // Keep class for CSS/JS targeting

        let pEmail = '', pName = '', pTiktok = '', pAge = '', pCountry = '', pPostCode = '', pAdded = '', pDisplayId = '', pTags = '';
        let pActiveDays = '', pTotalEvents = '', pLastEvent = '';
        let searchString = item;

        if (typeof itemInfo === 'object') {
            if (itemInfo.displayId) pDisplayId = itemInfo.displayId;
            if (itemInfo.tags && Array.isArray(itemInfo.tags)) pTags = itemInfo.tags.join(', ');

            if (itemInfo.participants) {
                pEmail = itemInfo.participants.email || '';
                pName = itemInfo.participants.name || '';
                pTiktok = itemInfo.participants.tiktokHandle || '';
                pAge = itemInfo.participants.age || '';
                pCountry = itemInfo.participants.country || '';
                pPostCode = itemInfo.participants.postCode || '';
            }
            if (itemInfo.personas) {
                pActiveDays = itemInfo.personas.active_days ?? '';
                pTotalEvents = itemInfo.personas.total_events ?? '';
                if (itemInfo.personas.last_event_ts) {
                    pLastEvent = String(itemInfo.personas.last_event_ts).split('T')[0];
                }
            }
            if (itemInfo.other && itemInfo.other.ts_added_to_dataset) {
                pAdded = String(itemInfo.other.ts_added_to_dataset).split('T')[0];
            }
            searchString = `${item} ${pDisplayId} ${pTags} ${pEmail} ${pName} ${pTiktok} ${pAge} ${pCountry} ${pPostCode} ${pActiveDays} ${pTotalEvents} ${pLastEvent} ${pAdded}`;
        }

        tr.setAttribute('data-search', searchString.toLowerCase());

        // Omit hidden collections here
        if (itemInfo.hidden) {
            return;
        }

        // Checkbox Cell
        const tdCheck = document.createElement('td');
        tdCheck.style.padding = '5px';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = item;
        cb.checked = selectedSet.has(item);
        cb.style.cursor = 'pointer';
        cb.onchange = function () {
            updateCollectionSelection(container.parentElement);
        };
        tdCheck.appendChild(cb);

        const createCell = (text, isBold = false, tooltip = null) => {
            const td = document.createElement('td');
            td.style.padding = '5px';
            if (tooltip) {
                td.title = tooltip;
            }
            if (isBold) td.innerHTML = `<strong>${text}</strong>`;
            else td.textContent = text;
            return td;
        }

        tr.appendChild(tdCheck);
        const primaryId = pDisplayId ? pDisplayId : item;
        tr.appendChild(createCell(primaryId, true, item));
        tr.appendChild(createCell(pTags));
        tr.appendChild(createCell(pEmail));
        tr.appendChild(createCell(pName));
        tr.appendChild(createCell(pTiktok));
        tr.appendChild(createCell(pAge));
        tr.appendChild(createCell(pCountry));
        tr.appendChild(createCell(pPostCode));
        tr.appendChild(createCell(pActiveDays));
        tr.appendChild(createCell(pTotalEvents));
        tr.appendChild(createCell(pLastEvent));
        tr.appendChild(createCell(pAdded));

        tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    container.appendChild(table);

    // Initial count update
    updateCollectionSelection(container.parentElement);

    // Apply saved sort state
    const studyRow = container.closest('.detail-row');
    if (studyRow && studyRow.dataset.studyName) {
        const savedState = tableSortStates.get(`study-${studyRow.dataset.studyName}`);
        if (savedState) {
            const headers = Array.from(thead.querySelectorAll('th'));
            const targetHeader = headers.find(h => h.textContent.trim() === savedState.text);
            if (targetHeader) {
                window.sortCollectionTable(targetHeader, savedState.dir);
            }
        }
    }
}

function updateCollectionSelection(selectorDiv) {
    if (!selectorDiv) return;
    const container = selectorDiv.querySelector('.collection-checklist-container');

    // More robust way to find the hidden input within the same detail row instead of sibling logic
    const row = selectorDiv.closest('.detail-row') || document;
    const hiddenInput = row.querySelector('input[data-field="SELECTED_DONATIONS"]');

    const countSpan = selectorDiv.querySelector('.selected-count');
    const eventsSpan = selectorDiv.querySelector('.selected-events-count');

    const checked = container.querySelectorAll('input[type="checkbox"]:checked');
    const values = Array.from(checked).map(c => c.value);

    let totalEvents = 0;
    const valueSet = new Set(values);
    availableCollections.forEach(c => {
        const id = typeof c === 'string' ? c : c.id;
        if (valueSet.has(id) && typeof c === 'object' && c.personas && c.personas.total_events) {
            totalEvents += (Number(c.personas.total_events) || 0);
        }
    });

    if (countSpan) countSpan.textContent = values.length;
    if (eventsSpan) eventsSpan.textContent = totalEvents.toLocaleString();

    if (hiddenInput && hiddenInput.dataset.field === 'SELECTED_DONATIONS') {
        hiddenInput.value = JSON.stringify(values);
    }
}

let tableSortStates = new Map();

window.sortCollectionTable = function (th, forceDir = null) {
    const table = th.closest('table');
    const tbody = table.querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
    const headerRow = th.parentElement;
    const columnIndex = Array.from(headerRow.children).indexOf(th);

    let currentDir = th.dataset.sortDir || 'desc';
    let newDir = forceDir ? forceDir : (currentDir === 'asc' ? 'desc' : 'asc');

    headerRow.querySelectorAll('th').forEach(header => {
        header.dataset.sortDir = '';
        // Only strip sort arrows from text-only headers, skip headers with child elements (e.g. checkboxes)
        if (header.children.length === 0) {
            header.textContent = header.textContent.replace(/ [▼▲]$/, '');
        }
    });

    th.dataset.sortDir = newDir;
    if (th.children.length === 0) {
        th.textContent += newDir === 'asc' ? ' ▲' : ' ▼';
    }

    const textContent = th.textContent.replace(/ [▼▲]$/, '');

    if (!forceDir) {
        // Save sort state
        const editContainer = th.closest('#edit-activity-list-container');
        if (editContainer) {
            tableSortStates.set('edit-activity', { dir: newDir, text: textContent });
        } else {
            const studyRow = th.closest('.detail-row');
            if (studyRow && studyRow.dataset.studyName) {
                tableSortStates.set(`study-${studyRow.dataset.studyName}`, { dir: newDir, text: textContent });
            }
        }
    }

    const isNumeric = ['Age', 'Active Days', 'Total Events', 'Watch Time'].includes(textContent);

    rows.sort((a, b) => {
        let cellA = a.children[columnIndex].textContent.trim();
        let cellB = b.children[columnIndex].textContent.trim();

        if (isNumeric) {
            let numA = parseFloat(cellA);
            let numB = parseFloat(cellB);
            if (isNaN(numA)) numA = -Infinity;
            if (isNaN(numB)) numB = -Infinity;
            if (numA < numB) return newDir === 'asc' ? -1 : 1;
            if (numA > numB) return newDir === 'asc' ? 1 : -1;
            return 0;
        }

        const comp = cellA.localeCompare(cellB);
        return newDir === 'asc' ? comp : -comp;
    });

    rows.forEach(row => tbody.appendChild(row));
};

function filterCollections(inputElement) {
    const searchText = inputElement.value.toLowerCase();
    const selectorDiv = inputElement.closest('.collection-selector');
    const items = selectorDiv.querySelectorAll('.collection-item'); // these are now table rows (tr)

    items.forEach(item => {
        const text = item.getAttribute('data-search') || item.textContent.toLowerCase();
        if (text.includes(searchText)) {
            item.style.display = 'table-row';
        } else {
            item.style.display = 'none';
        }
    });
}

function selectAllCollections(btn, select) {
    const selectorDiv = btn.closest('.collection-selector');
    const container = selectorDiv.querySelector('.collection-checklist-container');
    const items = container.querySelectorAll('.collection-item');

    items.forEach(item => {
        if (item.style.display !== 'none') {
            const cb = item.querySelector('input[type="checkbox"]');
            cb.checked = select;
        }
    });

    updateCollectionSelection(selectorDiv);
}


function renderStudiesTable() {
    const tbody = document.getElementById('studies_table_body');
    tbody.innerHTML = '';

    allStudies.forEach((study, index) => {
        // Main Row
        const tr = document.createElement('tr');
        tr.className = 'study-row';
        tr.style.cursor = 'pointer';
        tr.onclick = (e) => toggleDetail(e, index);

        const stats = study.stats || {};
        const lastUpdated = study.last_updated ? new Date(study.last_updated).toLocaleString() : '-';

        // Format numbers with commas
        const formatNum = (num) => num !== undefined ? num.toLocaleString() : '-';

        tr.innerHTML = `
            <td style="padding: 10px;"><span class="expand-icon">▶</span></td>
            <td style="text-align: left; padding: 10px;"><strong>${study.STUDY_NAME}</strong></td>
            <td style="text-align: left; padding: 10px;">${study.START_DATE || '-'}</td>
            <td style="text-align: left; padding: 10px;">${study.END_DATE || '-'}</td>
            <td style="text-align: right; padding: 10px;">${formatNum(stats.unique_collections)}</td>
            <td style="text-align: right; padding: 10px;">${formatNum(stats.unique_videos)}</td>
            <td style="text-align: right; padding: 10px;">${formatNum(stats.scraped_videos)}</td>
            <td style="text-align: right; padding: 10px;">${formatNum(stats.annotated_videos)}</td>
            <td style="text-align: right; padding: 10px;">${lastUpdated}</td>
            <td style="padding: 10px;">
                ${savingStudies.has(study.STUDY_NAME) ? '<span class="font-bold" style="color: var(--color-success-light); text-shadow: 0 0 5px var(--color-success-light);">Saving...</span>' : ''}
            </td>
        `;

        tbody.appendChild(tr);

        // Detail Row (from template)
        const template = document.getElementById('study_detail_template');
        const detailRow = template.content.cloneNode(true).querySelector('tr');
        detailRow.id = `detail-${index}`;
        detailRow.dataset.studyName = study.STUDY_NAME; // Store ID

        // Populate inputs
        populateForm(detailRow, study);

        tbody.appendChild(detailRow);
    });
}

function toggleDetail(event, index) {
    // Prevent toggling when clicking inside inputs/buttons in the detail row itself (though click is on main row)
    // Actually the click is on the main row.

    // Find sibling detail row
    const tbody = document.getElementById('studies_table_body');
    // The structure is flat: tr, tr-detail, tr, tr-detail...
    // simpler:
    const detailRow = document.getElementById(`detail-${index}`);
    const row = detailRow.previousElementSibling;
    const icon = row.querySelector('.expand-icon');

    if (detailRow.style.display === 'none') {
        detailRow.style.display = 'table-row';
        icon.textContent = '▼';
        row.style.backgroundColor = 'var(--color-bg-primary)';
        row.style.borderLeft = '4px solid var(--color-info)';
    } else {
        detailRow.style.display = 'none';
        icon.textContent = '▶';
        row.style.backgroundColor = '';
        row.style.borderLeft = '';
    }
}

// Global function for conditional visibility
window.toggleSamplingOptions = function (selectElement) {
    // Find the container relative to the select
    // structure: div.form-group-row > select
    // container: #sampling-options-container is sibling of grandparent? No, structure is:
    // col 2 > div.form-group-row (select)
    // col 2 > div#sampling-options-container

    // safe way: find closest column (div) then find the container
    const columnDiv = selectElement.closest('.study-form > div');
    if (!columnDiv) return;

    const container = columnDiv.querySelector('#sampling-options-container');
    if (container) {
        if (selectElement.value === 'off') {
            container.style.display = 'none';
        } else {
            container.style.display = 'block';
        }
    }
}


function populateForm(row, study) {
    // 1. Standard Inputs
    const inputs = row.querySelectorAll('[data-field]');
    inputs.forEach(input => {
        const field = input.dataset.field;
        let value = study[field];

        // Handle Lists/JSON (Except USER_ACCESS which is now checkboxes)
        if (field === 'SELECTED_DONATIONS') {
            // Find the collection selector in this row
            // The row input[data-field="SELECTED_DONATIONS"] is now the HIDDEN one.
            // renderCollectionSelector needs the container.
            // Structure: input[hidden] is sibling of div.collection-selector

            // Wait, input iteration loop finds the HIDDEN input.
            // We can set its value (for reference) AND render the list.

            const selectorDiv = input.parentElement.querySelector('.collection-selector');
            if (selectorDiv) {
                const container = selectorDiv.querySelector('.collection-checklist-container');
                // Value is the array
                const selectedList = Array.isArray(value) ? value : [];
                input.value = JSON.stringify(selectedList); // Set hidden value

                // Render Checklist
                renderCollectionSelector(container, selectedList);
            } else {
                // Fallback (should not happen if HTML updated)
                if (Array.isArray(value)) {
                    input.value = JSON.stringify(value, null, 2);
                } else {
                    input.value = value || "[]";
                }
            }
        }
        // Handle Booleans (Selects)
        else if (input.tagName === 'SELECT') {
            if (field === 'DONATION_SAMPLE_FRAME') {
                input.value = value || "off";
                // Trigger visibility update
                toggleSamplingOptions(input);
            }
            else {
                if (value === true) input.value = "true";
                else if (value === false) input.value = "false";
                else input.value = value || "true";
            }
        }
        else {
            input.value = value !== undefined ? value : '';
        }
    });

    // 2. Checkbox Groups (USER_ACCESS)
    const groups = row.querySelectorAll('[data-field-group]');
    groups.forEach(group => {
        const field = group.dataset.fieldGroup; // USER_ACCESS
        const currentList = study[field] || []; // e.g. ["admin", "viewer"]
        const container = group.querySelector('.dynamic-roles-container');

        if (field === 'USER_ACCESS' && container) {
            // Render dynamic roles
            container.innerHTML = '';

            // Ensure admin is always present and handled even if not in systemRoles fetch yet (race condition)
            // But systemRoles should be loaded.
            const rolesToRender = systemRoles.length > 0 ? systemRoles : ['admin', 'researcher', 'viewer'];

            rolesToRender.forEach(role => {
                const label = document.createElement('label');
                label.style.display = 'block';
                label.style.marginBottom = '5px';

                const cb = document.createElement('input');
                cb.type = 'checkbox';
                cb.value = role;

                // Admin logic
                if (role === 'admin') {
                    cb.checked = true;
                    cb.disabled = true;
                    label.style.opacity = '0.6';
                    label.title = "Admin access is mandatory";
                } else {
                    if (currentList.includes('all')) {
                        cb.checked = true;
                    } else {
                        cb.checked = currentList.includes(role);
                    }
                }

                label.appendChild(cb);
                label.appendChild(document.createTextNode(" " + role.charAt(0).toUpperCase() + role.slice(1)));
                container.appendChild(label);
            });
        }
    });

    // 3. Stats Display
    const statsInput = row.querySelector('[data-field="stats"]');
    if (statsInput) {
        const stats = study.stats || {};
        const container = statsInput.parentElement;
        const uniqueVids = container.querySelector('.stat-unique-vids');
        const scrapedVids = container.querySelector('.stat-scraped-vids');
        const annotatedVids = container.querySelector('.stat-annotated-vids');

        if (uniqueVids) uniqueVids.textContent = stats.unique_videos !== undefined ? stats.unique_videos.toLocaleString() : '-';
        if (scrapedVids) scrapedVids.textContent = stats.scraped_videos !== undefined ? stats.scraped_videos.toLocaleString() : '-';
        if (annotatedVids) annotatedVids.textContent = stats.annotated_videos !== undefined ? stats.annotated_videos.toLocaleString() : '-';
    }
}


function collectFormData(row) {
    const data = {};

    // 1. Standard Inputs
    const inputs = row.querySelectorAll('[data-field]');
    inputs.forEach(input => {
        const field = input.dataset.field;
        let value = input.value;

        // Parse Types
        if (field === 'SELECTED_DONATIONS') {
            try {
                // For the new UI, the input.value is already a clean JSON string set by updateCollectionSelection.
                // But let's be robust.
                if (value && value.trim()) {
                    // It should be stringified array. 
                    // replace not strictly needed if set programmatically, but safe.
                    let safeVal = value.replace(/'/g, '"');

                    // If it's the Hidden Input, it holds the full array from check boxes.
                    data[field] = JSON.parse(safeVal);
                } else {
                    data[field] = [];
                }
            } catch (e) {
                // If parsing fails (e.g. empty), default to empty
                console.warn(`Failed to parse ${field}`, e);
                data[field] = [];
            }
        }
        else if (input.type === 'number') {
            data[field] = parseInt(value, 10);
            if (isNaN(data[field])) data[field] = 0;
        }
        else {
            data[field] = value;
        }
    });

    // 2. Checkbox Groups (USER_ACCESS)
    const groups = row.querySelectorAll('[data-field-group]');
    groups.forEach(group => {
        const field = group.dataset.fieldGroup; // USER_ACCESS
        const checkboxes = group.querySelectorAll('input[type="checkbox"]:checked');
        const selectedValues = Array.from(checkboxes).map(cb => cb.value);

        // Special Logic: If all options are checked, save as ["all"]? 
        // Or just save list. User prompt: "If the user checks all roles." -> maybe implies behaviour.
        // I'll stick to saving the list of roles to be explicit, unless "all" matches everything.
        // Actually, let's check if all available boxes are checked.
        const allCheckboxes = group.querySelectorAll('input[type="checkbox"]');
        if (checkboxes.length === allCheckboxes.length && allCheckboxes.length > 0) {
            data[field] = ["all"];
        } else {
            data[field] = selectedValues;
        }
    });

    return data;
}

// Always refresh both PCA and metadata when saving a study definition.
function collectSaveSettings(row) {
    return { REFRESH_PCA: true, REFRESH_METADATA: true };
}


function saveStudy(btn, event) {
    if (event) event.preventDefault();
    const detailRow = btn.closest('tr');
    const studyName = detailRow.dataset.studyName;

    try {
        const formData = collectFormData(detailRow);

        if (formData.SELECTED_DONATIONS && formData.SELECTED_DONATIONS.length === 0) {
            alert("Please select at least one collection to save the study definition.");
            return;
        }

        const saveSettings = collectSaveSettings(detailRow);

        // Merge settings into formData (backend will strip them)
        Object.assign(formData, saveSettings);

        formData.STUDY_NAME = studyName;
        //console.log("Saving study definition:", formData);

        // Mark as saving
        savingStudies.add(studyName);
        btn.textContent = "Saving...";
        btn.disabled = true;

        // Show Saving Indicator immediately (for instant feedback)
        const mainRow = detailRow.previousElementSibling;
        const actionCell = mainRow.cells[mainRow.cells.length - 1];
        actionCell.innerHTML = '<span class="font-bold" style="color: var(--color-success-light); text-shadow: 0 0 5px var(--color-success-light);">Saving...</span>';

        let isSuccess = false;

        fetch('/api/manage/studies/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify(formData)
        })
            .then(res => res.json())
            .then(data => {
                if (data.status === 'success') {
                    isSuccess = true;
                    // Update local model
                    const index = allStudies.findIndex(s => s.STUDY_NAME === studyName);
                    if (index !== -1) {
                        allStudies[index] = data.study;
                    }
                } else if (data.status === 'no_change') {
                    alert(data.message);
                } else {
                    alert("Error saving: " + data.error);
                }
            })
            .catch(err => {
                console.error(err);
                alert("Save failed.");
            })
            .finally(() => {
                // Done saving
                savingStudies.delete(studyName);

                // 1. Capture State BEFORE re-render
                // We need to find the specific study again in case index shifted
                const index = allStudies.findIndex(s => s.STUDY_NAME === studyName);
                let wasOpen = false;
                if (index !== -1) {
                    const currentDetailRow = document.getElementById(`detail-${index}`);
                    wasOpen = currentDetailRow && currentDetailRow.style.display !== 'none';
                }

                // 2. Re-render table (Updates "Last Updated" and clears "Saving..." indicator for THIS study)
                renderStudiesTable();

                // 3. Restore State & Show Feedback
                if (index !== -1) {
                    setTimeout(() => {
                        const newDetail = document.getElementById(`detail-${index}`);

                        // Restore Open State (if it was open OR we want it open? User said "stay collapsed if collapsed")
                        // Logic: IF wasOpen, re-open. ELSE stay closed.
                        if (wasOpen) {
                            toggleDetail(null, index);

                            // 4. Feedback (Green "Saved!" button) only if SUCCESS and was OPEN
                            if (isSuccess) {
                                const buttons = newDetail.querySelectorAll('button');
                                let saveBtn = null;
                                buttons.forEach(b => {
                                    if (b.textContent.includes('Save')) saveBtn = b;
                                });

                                if (saveBtn) {
                                    saveBtn.textContent = "Saved!";
                                    saveBtn.style.backgroundColor = 'var(--color-success)';

                                    setTimeout(() => {
                                        saveBtn.textContent = "Save Study Definition";
                                        saveBtn.style.backgroundColor = "";
                                    }, 2000);
                                }
                            }
                        }
                    }, 50);
                }

                // Reset button state? The button in the DOM was destroyed if re-rendered.
                // If the row was collapsed, the button is gone/hidden. 
                // We re-enable the *detached* button just in case, but it doesn't matter.
                btn.textContent = "Save Study Definition";
                btn.disabled = false;
            });

    } catch (e) {
        // Validation failed
    }
}

window.updateStudyEstimates = function (btn, event) {
    if (event) event.preventDefault();
    const detailRow = btn.closest('tr');
    const studyName = detailRow.dataset.studyName;

    try {
        const formData = collectFormData(detailRow);
        formData.STUDY_NAME = studyName;

        btn.textContent = "Updating...";
        btn.disabled = true;

        fetch('/api/manage/studies/calculate_stats', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify(formData)
        })
            .then(res => res.json())
            .then(data => {
                if (data.status === 'success') {
                    const stats = data.stats;

                    // Update specific elements in THIS row (not re-rendering whole table)
                    const container = btn.parentElement;
                    const uniqueVids = container.querySelector('.stat-unique-vids');
                    const scrapedVids = container.querySelector('.stat-scraped-vids');
                    const annotatedVids = container.querySelector('.stat-annotated-vids');

                    if (uniqueVids) uniqueVids.textContent = stats.unique_videos !== undefined ? stats.unique_videos.toLocaleString() : '0';
                    if (scrapedVids) scrapedVids.textContent = stats.scraped_videos !== undefined ? stats.scraped_videos.toLocaleString() : '0';
                    if (annotatedVids) annotatedVids.textContent = stats.annotated_videos !== undefined ? stats.annotated_videos.toLocaleString() : '0';

                } else {
                    alert("Error updating estimates: " + data.error);
                }
            })
            .catch(err => {
                console.error(err);
                alert("Update failed.");
            })
            .finally(() => {
                btn.textContent = "Update Estimates";
                btn.disabled = false;
            });

    } catch (e) {
        console.error("Failed to collect data for estimate update", e);
        btn.textContent = "Update Estimates";
        btn.disabled = false;
    }
}


function deleteStudy(btn, event) {
    if (event) event.preventDefault();
    const detailRow = btn.closest('tr');
    const studyName = detailRow.dataset.studyName;

    if (!confirm(`Are you sure you want to delete study '${studyName}'? This cannot be undone.`)) return;

    fetch('/api/manage/studies/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ STUDY_NAME: studyName })
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                alert("Study deleted.");
                loadStudies();
            } else {
                alert("Error: " + data.error);
            }
        })
        .catch(err => alert("Delete failed: " + err));
}

function populateEnrichmentStudySelect(studies) {
    const select = document.getElementById('enrichment-study-select');
    if (!select) return;

    // Keep the first default option
    select.innerHTML = '<option value="">-- Select Study --</option>';

    studies.forEach(study => {
        const opt = document.createElement('option');
        opt.value = study.STUDY_NAME;
        opt.textContent = study.STUDY_NAME;
        select.appendChild(opt);
    });
}

// --- Modal ---

function openNewStudyModal() {
    document.getElementById('newStudyModal').style.display = 'block';
    document.getElementById('new_study_name').value = '';
}

function closeNewStudyModal() {
    document.getElementById('newStudyModal').style.display = 'none';
}

function createStudy(event) {
    if (event) event.preventDefault();
    const name = document.getElementById('new_study_name').value.trim();
    if (!name) {
        alert("Please enter a name");
        return;
    }

    // Check overlapping
    if (allStudies.find(s => s.STUDY_NAME === name)) {
        alert("Study name already exists!");
        return;
    }

    // Default Template
    const newStudy = {
        STUDY_NAME: name,
        START_DATE: "2024-05-18",
        END_DATE: "2024-05-25",
        INCLUDE_ZEESCHUIMER_DATA: false,
        INCLUDE_DONATION_DATA: true,
        USER_ACCESS: ["all"],
        DONATION_SAMPLE_FRAME: "off",
        SELECTED_DONATIONS: []
    };

    // Use Save Endpoint to create
    fetch('/api/manage/studies/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify(newStudy)
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                closeNewStudyModal();
                loadStudies();
                alert("Study created.");
            } else {
                alert("Error: " + data.error);
            }
        });
}

// Init
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

// Load collections FIRST, then studies to ensure selector populates correctly
loadAvailableCollections();
loadSystemRoles();
loadIngestionSources();

// --- Enrichment Stats & Logic ---

function fetchEnrichmentStats() {
    fetch('/api/manage/enrichment/stats')
        .then(res => res.json())
        .then(data => {
            // Stats
            document.getElementById('enrich_total_videos').textContent = (data.total_videos !== undefined) ? data.total_videos.toLocaleString() : '-';
            document.getElementById('enrich_scraped').textContent = (data.scraped_videos !== undefined) ? data.scraped_videos.toLocaleString() : '-';
            document.getElementById('enrich_annotated').textContent = (data.annotated_videos !== undefined) ? data.annotated_videos.toLocaleString() : '-';

            // Queues
            if (data.scrape_queue_len !== undefined) {
                document.getElementById('enrich_scrape_targets').textContent = data.scrape_queue_len.toLocaleString();
                document.getElementById('enrich_scrape_targets').style.color = 'var(--color-success-light)';
            }
            if (data.annotate_queue_len !== undefined) {
                document.getElementById('enrich_annotate_targets').textContent = data.annotate_queue_len.toLocaleString();
                document.getElementById('enrich_annotate_targets').style.color = 'var(--color-success-light)';
            }

            // Consolidate button state
            const consolidateBtn = document.getElementById('btn-consolidate');
            if (consolidateBtn) {
                consolidateBtn.disabled = !data.has_unconsolidated;
            }
        })
        .catch(err => console.error("Error fetching enrichment stats:", err));
}

function queueVideosFromTargetStudy(btnElement) {
    const studyName = document.getElementById('enrichment-study-select').value;
    const scrapeTargetsDisplay = document.getElementById('enrich_scrape_targets');
    const annotateTargetsDisplay = document.getElementById('enrich_annotate_targets');

    if (!studyName) {
        alert("Please select a target study from the dropdown first.");
        return;
    }

    // UI Loading state
    const originalText = btnElement.textContent;
    btnElement.textContent = "Queueing...";
    btnElement.disabled = true;

    scrapeTargetsDisplay.textContent = "Calc...";
    scrapeTargetsDisplay.style.color = 'var(--color-text-tertiary)';
    annotateTargetsDisplay.textContent = "Calc...";
    annotateTargetsDisplay.style.color = 'var(--color-text-tertiary)';

    const fetchScrape = fetch('/api/manage/enrichment/calculate_to_scrape', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ study_name: studyName })
    }).then(res => res.json());

    const fetchAnnotate = fetch('/api/manage/enrichment/calculate_to_annotate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ study_name: studyName })
    }).then(res => res.json());

    Promise.all([fetchScrape, fetchAnnotate])
        .then(([scrapeData, annotateData]) => {
            // Restore button
            btnElement.textContent = originalText;
            btnElement.disabled = false;

            // Update scrape display
            if (scrapeData.status === 'success') {
                scrapeTargetsDisplay.textContent = scrapeData.videos_to_scrape.toLocaleString();
                scrapeTargetsDisplay.style.color = 'var(--color-success-light)';
            } else {
                scrapeTargetsDisplay.textContent = "Error";
                scrapeTargetsDisplay.style.color = 'var(--color-danger)';
                console.error("Scrape Error:", scrapeData.error);
            }

            // Update annotate display
            if (annotateData.status === 'success') {
                annotateTargetsDisplay.textContent = annotateData.videos_to_annotate.toLocaleString();
                annotateTargetsDisplay.style.color = 'var(--color-success-light)';
            } else {
                annotateTargetsDisplay.textContent = "Error";
                annotateTargetsDisplay.style.color = 'var(--color-danger)';
                console.error("Annotate Error:", annotateData.error);
            }

            // Refresh total stats
            fetchEnrichmentStats();
        })
        .catch(err => {
            btnElement.textContent = originalText;
            btnElement.disabled = false;
            scrapeTargetsDisplay.textContent = "Failed";
            scrapeTargetsDisplay.style.color = 'var(--color-danger)';
            annotateTargetsDisplay.textContent = "Failed";
            annotateTargetsDisplay.style.color = 'var(--color-danger)';
            console.error("Error queueing from target study:", err);
            alert("Error queueing videos from target study.");
        });
}

function emptyQueue(queueType) {
    if (!queueType) return;

    fetch(`/api/manage/enrichment/empty_queue/${queueType}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken }
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                fetchEnrichmentStats();
            } else {
                alert("Error: " + data.error);
            }
        })
        .catch(err => console.error("Failed to empty queue: " + err));
}

function consolidateEnrichmentData(btn) {
    const originalText = btn.textContent;
    const originalClass = btn.className;
    btn.textContent = "Consolidating...";
    btn.disabled = true;
    btn.className = 'btn-running';

    fetch('/api/manage/enrichment/consolidate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken }
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                fetchEnrichmentStats();
            } else {
                console.error("Consolidation error:", data.error);
            }
        })
        .catch(err => console.error("Failed to consolidate:", err))
        .finally(() => {
            btn.className = originalClass;
            btn.textContent = originalText;
            btn.disabled = false;
        });
}

// Call on load
fetchEnrichmentStats();

function toggleSection(contentId, arrowId) {
    const content = document.getElementById(contentId);
    const arrow = document.getElementById(arrowId);
    if (!content || !arrow) return;

    if (content.style.display === 'none') {
        content.style.display = 'block';
        arrow.innerHTML = '▼'; // Down arrow
    } else {
        content.style.display = 'none';
        arrow.innerHTML = '▶'; // Right arrow
    }
}

// --- Data Ingestion Logic ---

let ingestionMetadata = { collection_ids: [], tags: [] };
let uploadSelectedTags = [];
let uploadPendingFiles = null;

function loadIngestionMetadata() {
    return fetch('/api/manage/ingestion/metadata')
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                ingestionMetadata = data;
            }
        })
        .catch(err => console.error("Error loading ingestion metadata:", err));
}

function loadIngestionSources() {
    fetch('/api/manage/ingestion/sources')
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                renderIngestionSources(data.sources);
                updateProcessButton(data.total_pending || 0);
            } else {
                console.error("Failed to load ingestion sources:", data.error);
            }
        })
        .catch(err => console.error("Error loading ingestion sources:", err));
}

function updateProcessButton(totalPending) {
    const btn = document.getElementById('processRawFilesBtn');
    if (!btn) return;
    if (totalPending > 0) {
        btn.textContent = `Process New Collections (${totalPending} pending)`;
        btn.classList.add('btn-has-pending');
        btn.disabled = false;
    } else {
        btn.textContent = 'Process New Collections';
        btn.classList.remove('btn-has-pending');
        btn.disabled = true;
    }
}

function renderIngestionSources(sources) {
    const container = document.getElementById('ingestion-sources-container');
    if (!container) return;

    container.innerHTML = '';

    if (sources.length === 0) {
        container.innerHTML = '<div style="color: var(--color-text-tertiary); padding: 10px;">No collection subclasses registered.</div>';
        return;
    }

    sources.forEach(source => {
        const card = document.createElement('div');
        card.className = 'ingest-card';

        const pendingBadge = source.pending_files > 0
            ? `<span class="text-xs font-bold" style="color: var(--color-warning); margin-left: 8px;">${source.pending_files} pending</span>`
            : '';

        card.innerHTML = `
            <div class="font-bold text-body" style="margin-bottom: 5px;">${source.class_name}${pendingBadge}</div>
            <div class="text-sm" style="color: var(--color-text-tertiary); margin-bottom: 15px;">
                <strong>Platform:</strong> ${source.source_platform} | <strong>Source:</strong> ${source.data_source}
            </div>
            <div style="margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap;">
                <button type="button" class="action-btn" onclick="openUploadModal('${source.class_name}', '${source.raw_path}', 'files')">
                    Add Files
                </button>
                <button type="button" class="action-btn" onclick="openUploadModal('${source.class_name}', '${source.raw_path}', 'folder')">
                    Add Folder
                </button>
            </div>
        `;

        container.appendChild(card);
    });
}


// --- Upload Modal ---

let _uploadMode = 'files';

function openUploadModal(className, rawPath, mode) {
    // Reset state
    uploadSelectedTags = [];
    uploadPendingFiles = null;
    _uploadMode = mode;
    const listDiv = document.getElementById('uploadFilesList');
    listDiv.innerHTML = `<div style="text-align: center; padding: 16px; color: var(--color-text-tertiary); cursor: pointer;">Click here to select ${mode === 'folder' ? 'a folder' : 'files'}</div>`;
    document.getElementById('uploadSelectedTags').innerHTML = '';
    document.getElementById('uploadTagInput').value = '';
    document.getElementById('uploadTagSuggestions').style.display = 'none';
    document.getElementById('uploadRawPath').value = rawPath;
    document.getElementById('uploadClassName').value = className;
    document.getElementById('uploadStatus').style.display = 'none';
    document.getElementById('uploadSubmitBtn').disabled = false;
    document.getElementById('uploadNewCollectionId').value = '';
    document.getElementById('uploadNewCollectionId').style.display = 'none';
    document.getElementById('uploadExistingCollectionId').style.display = 'none';
    document.getElementById('uploadModalTitle').textContent = `Add to ${className}`;

    // Reset radio to default
    document.querySelector('input[name="collectionIdMode"][value="per_file"]').checked = true;

    // Fetch metadata then show modal
    loadIngestionMetadata().then(() => {
        // Populate existing collection IDs dropdown
        const sel = document.getElementById('uploadExistingCollectionId');
        sel.innerHTML = '';
        ingestionMetadata.collection_ids.forEach(id => {
            const opt = document.createElement('option');
            opt.value = id;
            opt.textContent = id;
            sel.appendChild(opt);
        });

        // Show modal
        document.getElementById('uploadModal').style.display = 'flex';
    });
}

function triggerFilePicker() {
    const existingInput = document.getElementById('uploadTempFileInput');
    if (existingInput) existingInput.remove();

    const input = document.createElement('input');
    input.type = 'file';
    input.id = 'uploadTempFileInput';
    input.style.display = 'none';

    if (_uploadMode === 'folder') {
        input.setAttribute('webkitdirectory', '');
    }
    input.setAttribute('multiple', '');

    input.addEventListener('change', () => {
        handleFilesSelected(input.files);
    });

    document.body.appendChild(input);
    input.click();
}

function handleFilesSelected(files) {
    if (!files || files.length === 0) return;
    uploadPendingFiles = files;
    const listDiv = document.getElementById('uploadFilesList');
    let filesHtml = '';
    if (files.length <= 10) {
        filesHtml = Array.from(files).map(f =>
            `<div class="text-xs" style="padding: 2px 0;">${f.name}</div>`
        ).join('');
    } else {
        filesHtml = `<div class="text-sm">${files.length} files selected</div>` +
            Array.from(files).slice(0, 5).map(f =>
                `<div class="text-xs" style="padding: 2px 0; color: var(--color-text-tertiary);">${f.name}</div>`
            ).join('') +
            `<div class="text-xs" style="color: var(--color-text-tertiary);">... and ${files.length - 5} more</div>`;
    }
    listDiv.innerHTML = filesHtml +
        `<div class="text-xs" style="margin-top: 6px; color: var(--color-accent); cursor: pointer;" onclick="triggerFilePicker()">Change selection...</div>`;
}

function closeUploadModal() {
    document.getElementById('uploadModal').style.display = 'none';
    uploadPendingFiles = null;
    uploadSelectedTags = [];
    const tempInput = document.getElementById('uploadTempFileInput');
    if (tempInput) tempInput.remove();
}


// --- Collection ID radio toggle ---

document.addEventListener('change', function (e) {
    if (e.target.name !== 'collectionIdMode') return;
    const val = e.target.value;
    document.getElementById('uploadExistingCollectionId').style.display = val === 'existing' ? 'block' : 'none';
    document.getElementById('uploadNewCollectionId').style.display = val === 'new' ? 'block' : 'none';
});


// --- Tag Management ---

document.addEventListener('input', function (e) {
    if (e.target.id !== 'uploadTagInput') return;
    const query = e.target.value.trim().toLowerCase();
    const sugDiv = document.getElementById('uploadTagSuggestions');
    if (!query) {
        sugDiv.style.display = 'none';
        return;
    }
    const matches = ingestionMetadata.tags.filter(t =>
        t.toLowerCase().includes(query) && !uploadSelectedTags.includes(t)
    );
    if (matches.length === 0) {
        sugDiv.style.display = 'none';
        return;
    }
    sugDiv.innerHTML = matches.slice(0, 8).map(t =>
        `<div class="tag-suggestion-item" onclick="addUploadTag('${t.replace(/'/g, "\\'")}')">${t}</div>`
    ).join('');
    sugDiv.style.display = 'block';
});

document.addEventListener('keydown', function (e) {
    if (e.target.id !== 'uploadTagInput') return;
    if (e.key === 'Enter') {
        e.preventDefault();
        const val = e.target.value.trim();
        if (val) addUploadTag(val);
    }
});

window.addUploadTag = function (tag) {
    tag = tag.trim();
    if (!tag || uploadSelectedTags.includes(tag)) return;
    uploadSelectedTags.push(tag);
    renderUploadTags();
    document.getElementById('uploadTagInput').value = '';
    document.getElementById('uploadTagSuggestions').style.display = 'none';
};

window.removeUploadTag = function (tag) {
    uploadSelectedTags = uploadSelectedTags.filter(t => t !== tag);
    renderUploadTags();
};

function renderUploadTags() {
    const container = document.getElementById('uploadSelectedTags');
    container.innerHTML = uploadSelectedTags.map(t =>
        `<span class="tag-chip">${t} <span class="remove-tag" onclick="removeUploadTag('${t.replace(/'/g, "\\'")}')">&times;</span></span>`
    ).join('');
}


// --- Submit Upload ---

function submitUpload() {
    if (!uploadPendingFiles || uploadPendingFiles.length === 0) {
        alert('Please select files first.');
        return;
    }

    const rawPath = document.getElementById('uploadRawPath').value;
    const modeRadio = document.querySelector('input[name="collectionIdMode"]:checked');
    const mode = modeRadio ? modeRadio.value : 'per_file';

    let collectionId = '';
    let collectionIdMode = 'per_file';

    if (mode === 'existing') {
        collectionId = document.getElementById('uploadExistingCollectionId').value;
        collectionIdMode = 'single';
        if (!collectionId) {
            alert('Please select an existing collection.');
            return;
        }
    } else if (mode === 'new') {
        collectionId = document.getElementById('uploadNewCollectionId').value.trim();
        collectionIdMode = 'single';
        if (!collectionId) {
            alert('Please enter a collection ID.');
            return;
        }
    }

    const formData = new FormData();
    for (const file of uploadPendingFiles) {
        formData.append('files', file);
    }
    formData.append('raw_path', rawPath);
    formData.append('collection_id', collectionId);
    formData.append('collection_id_mode', collectionIdMode);
    formData.append('tags', JSON.stringify(uploadSelectedTags));

    const statusDiv = document.getElementById('uploadStatus');
    const submitBtn = document.getElementById('uploadSubmitBtn');
    submitBtn.disabled = true;
    statusDiv.textContent = 'Uploading...';
    statusDiv.style.color = 'var(--color-text-tertiary)';
    statusDiv.style.display = 'block';

    fetch('/api/manage/ingestion/upload', {
        method: 'POST',
        headers: { 'X-CSRFToken': csrfToken },
        body: formData
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                statusDiv.textContent = data.message;
                statusDiv.style.color = 'var(--color-success-light)';
                loadIngestionSources();
                setTimeout(() => closeUploadModal(), 1500);
            } else {
                statusDiv.textContent = 'Error: ' + data.error;
                statusDiv.style.color = 'var(--color-danger)';
                submitBtn.disabled = false;
            }
        })
        .catch(err => {
            statusDiv.textContent = 'Upload failed.';
            statusDiv.style.color = 'var(--color-danger)';
            submitBtn.disabled = false;
        });
}


window.refreshIngestionCollection = function (btn) {
    const originalText = btn.textContent;
    const originalClass = btn.className;
    btn.textContent = "Processing...";
    btn.disabled = true;
    btn.className = 'btn-running';

    fetch('/api/manage/ingestion/refresh', {
        method: 'POST',
        headers: { 'X-CSRFToken': csrfToken }
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                loadAvailableCollections();
                loadIngestionSources();
            } else {
                console.error("Ingestion refresh error:", data.error);
            }
        })
        .catch(err => console.error("Error triggering refresh:", err))
        .finally(() => {
            btn.className = originalClass;
            btn.textContent = originalText;
            btn.disabled = false;
        });
}

// --- Edit Activity Data Modal Logic ---

function renderEditActivityTable(container) {
    if (!container) return;
    container.innerHTML = '';

    if (availableCollections.length === 0) {
        container.innerHTML = '<div style="padding: 10px; color: var(--color-text-tertiary);">No collections available.</div>';
        return;
    }

    const table = document.createElement('table');
    table.className = 'collection-table';

    const thStyle = 'padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid var(--color-border-strong);';
    const thead = document.createElement('thead');
    thead.innerHTML = `
        <tr style="text-align: left;">
            <th style="padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; border-bottom: 2px solid var(--color-border-strong); width: 40px; text-align: center;">
                <input type="checkbox" id="select-all-collections" onchange="toggleAllCollectionCheckboxes(this)" style="cursor: pointer;">
            </th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">Collection / Display ID</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">Tags</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">First Event</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">Last Event</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">Added</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">Total Events</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">Active Days</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">Timezone</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">Age</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">Country</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">PostCode</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">Name</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">Email</th>
        </tr>
    `;
    table.appendChild(thead);

    const tbody = document.createElement('tbody');

    availableCollections.forEach(itemInfo => {
        const item = typeof itemInfo === 'string' ? itemInfo : itemInfo.id;
        let pEmail = '', pName = '', pAge = '', pCountry = '', pPostCode = '', pAdded = '', pDisplayId = '', pTags = '';
        let pActiveDays = '', pTotalEvents = '', pLastEvent = '';
        let pTimezone = '', pFirstEvent = '';
        let searchString = item;

        if (typeof itemInfo === 'object') {
            if (itemInfo.displayId) pDisplayId = itemInfo.displayId;
            if (itemInfo.tags && Array.isArray(itemInfo.tags)) pTags = itemInfo.tags.join(', ');

            if (itemInfo.participants) {
                pEmail = itemInfo.participants.email || '';
                pName = itemInfo.participants.name || '';
                pAge = itemInfo.participants.age || '';
                pCountry = itemInfo.participants.country || '';
                pPostCode = itemInfo.participants.postCode || '';
            }
            if (itemInfo.personas) {
                pActiveDays = itemInfo.personas.active_days ?? '';
                pTotalEvents = itemInfo.personas.total_events ?? '';
                if (itemInfo.personas.last_event_ts) {
                    pLastEvent = String(itemInfo.personas.last_event_ts).split('T')[0];
                }
                if (itemInfo.personas.first_event_ts) {
                    pFirstEvent = String(itemInfo.personas.first_event_ts).split('T')[0];
                }
                const tz = itemInfo.personas.inferred_tz_offset;
                if (tz !== null && tz !== undefined) {
                    pTimezone = `UTC${tz >= 0 ? '+' : ''}${tz}`;
                }
            }
            if (itemInfo.other && itemInfo.other.ts_added_to_dataset) {
                pAdded = String(itemInfo.other.ts_added_to_dataset).split('T')[0];
            }
            searchString = `${item} ${pDisplayId} ${pTags} ${pEmail} ${pName} ${pAge} ${pCountry} ${pPostCode} ${pTimezone} ${pActiveDays} ${pTotalEvents} ${pFirstEvent} ${pLastEvent} ${pAdded}`;
        }

        const tr = document.createElement('tr');
        tr.className = 'edit-activity-item';
        tr.setAttribute('data-search', searchString.toLowerCase());
        tr.setAttribute('data-collection-id', item);

        // Apply distinct styling to hidden collections
        if (itemInfo.hidden) {
            tr.classList.add('collection-hidden');
        }

        tr.onmouseenter = () => {
            if (window.pe_selectedId !== item) tr.style.background = 'var(--color-bg-input)';
        };
        tr.onmouseleave = () => {
            tr.style.background = (window.pe_selectedId === item) ? 'var(--table-row-selected)' : 'transparent';
        };
        tr.onclick = () => {
            // Sync with bee swarm selection (scrollToRow=false since the row is already visible)
            if (typeof pe_selectDonation === 'function') {
                pe_selectDonation(item, false);
            }
        };
        const createCell = (text, isBold = false, tooltip = null) => {
            const td = document.createElement('td');
            td.style.padding = '5px';
            if (tooltip) {
                td.title = tooltip;
            }
            if (isBold) td.innerHTML = `<strong>${text}</strong>`;
            else td.textContent = text;
            return td;
        }

        // Select checkbox (first column)
        const checkTd = document.createElement('td');
        checkTd.style.padding = '5px';
        checkTd.style.textAlign = 'center';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'collection-row-checkbox';
        checkbox.dataset.collectionId = item;
        checkbox.checked = selectedCollectionIds.has(item);
        checkbox.style.cursor = 'pointer';
        checkbox.onclick = (e) => e.stopPropagation();
        checkbox.onchange = () => {
            toggleCollectionSelection(item, checkbox.checked);
        };
        checkTd.appendChild(checkbox);
        tr.appendChild(checkTd);

        const primaryId = pDisplayId ? pDisplayId : item;
        tr.appendChild(createCell(primaryId, true, item));
        tr.appendChild(createCell(pTags));
        tr.appendChild(createCell(pFirstEvent));
        tr.appendChild(createCell(pLastEvent));
        tr.appendChild(createCell(pAdded));
        tr.appendChild(createCell(pTotalEvents));
        tr.appendChild(createCell(pActiveDays));
        tr.appendChild(createCell(pTimezone));
        tr.appendChild(createCell(pAge));
        tr.appendChild(createCell(pCountry));
        tr.appendChild(createCell(pPostCode));
        tr.appendChild(createCell(pName));
        tr.appendChild(createCell(pEmail));

        tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    container.appendChild(table);

    // Apply saved sort state
    const savedState = tableSortStates.get('edit-activity');
    if (savedState) {
        const headers = Array.from(thead.querySelectorAll('th'));
        const targetHeader = headers.find(h => h.textContent.trim() === savedState.text);
        if (targetHeader) {
            window.sortCollectionTable(targetHeader, savedState.dir);
        }
    }
}

function filterEditActivityCollections(inputElement) {
    const searchText = inputElement.value.toLowerCase();
    const selectorDiv = inputElement.closest('.pe-edit-activity-section') || document.getElementById('edit-activity-list-container');
    const items = selectorDiv.querySelectorAll('.edit-activity-item');

    items.forEach(item => {
        const text = item.getAttribute('data-search') || item.textContent.toLowerCase();
        if (text.includes(searchText)) {
            item.style.display = 'table-row';
        } else {
            item.style.display = 'none';
        }
    });
}

let currentEditCollectionId = null;
let currentEditCollectionTags = [];
let selectedCollectionIds = new Set();
let bulkEditMode = false;
let bulkOriginalTagsMap = {};  // collectionId -> original tags array
let bulkPartialTags = new Set(); // tags present on some but not all selected collections
let hiddenUserTouched = false; // track if user explicitly changed hidden checkbox

function openEditCollectionModal(collectionObj) {
    if (typeof collectionObj === 'string') {
        const found = availableCollections.find(c => c.id === collectionObj);
        if (found) collectionObj = found;
        else collectionObj = { id: collectionObj };
    }

    bulkEditMode = false;
    bulkPartialTags = new Set();
    hiddenUserTouched = false;
    currentEditCollectionId = collectionObj.id;
    currentEditCollectionTags = Array.isArray(collectionObj.tags) ? [...collectionObj.tags] : [];

    document.getElementById('edit-collection-id-display').innerText = currentEditCollectionId;
    document.getElementById('edit-collection-id').value = currentEditCollectionId;

    const displayIdInput = document.getElementById('edit-collection-display-id');
    displayIdInput.value = collectionObj.displayId || currentEditCollectionId;
    displayIdInput.disabled = false;
    displayIdInput.placeholder = '';

    const hiddenCheckbox = document.getElementById('edit-collection-hidden');
    if (hiddenCheckbox) {
        hiddenCheckbox.checked = !!collectionObj.hidden;
        hiddenCheckbox.indeterminate = false;
        hiddenCheckbox.onchange = null;
    }

    dm_renderTags();
    document.getElementById('editCollectionModal').style.display = 'block';
}

function closeEditCollectionModal() {
    document.getElementById('editCollectionModal').style.display = 'none';
    currentEditCollectionId = null;
    bulkEditMode = false;
    hiddenUserTouched = false;
    const displayIdInput = document.getElementById('edit-collection-display-id');
    if (displayIdInput) {
        displayIdInput.disabled = false;
        displayIdInput.placeholder = '';
    }
    updateEditSelectedButton();
}

function dm_renderTags() {
    const container = document.getElementById('edit-collection-tags-container');
    if (!container) return;

    container.innerHTML = '';

    const allTagsSet = new Set();
    availableCollections.forEach(c => {
        if (typeof c === 'object' && c.tags && Array.isArray(c.tags)) {
            c.tags.forEach(t => allTagsSet.add(t));
        }
    });

    currentEditCollectionTags.forEach(t => allTagsSet.add(t));

    const allTags = Array.from(allTagsSet).sort();

    allTags.forEach(tag => {
        const isSelected = currentEditCollectionTags.includes(tag);
        const isPartial = bulkEditMode && !isSelected && bulkPartialTags.has(tag);
        const chip = document.createElement('label');

        chip.style.cssText = `
            background: var(--chip-bg);
            color: white;
            border: 1px solid var(--color-border-strong);
            padding: 2px 7px;
            border-radius: 10px;
            cursor: pointer;
            user-select: none;
            transition: all 0.1s;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        `;
        chip.classList.add('text-xs');

        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = isSelected;
        cb.indeterminate = isPartial;
        cb.style.cssText = 'margin: 0; cursor: pointer; width: auto;';
        cb.onchange = () => {
            if (bulkEditMode && bulkPartialTags.has(tag)) {
                bulkPartialTags.delete(tag);
            }
            dm_toggleTag(tag);
        };
        chip.appendChild(cb);

        const span = document.createElement('span');
        span.textContent = tag;
        chip.appendChild(span);

        chip.onclick = (e) => {
            if (e.target === cb) return;
            e.preventDefault();
            if (bulkEditMode && bulkPartialTags.has(tag)) {
                bulkPartialTags.delete(tag);
            }
            dm_toggleTag(tag);
        };

        container.appendChild(chip);
    });
}

function dm_toggleTag(tag) {
    if (!currentEditCollectionId && !bulkEditMode) return;
    const idx = currentEditCollectionTags.indexOf(tag);
    if (idx !== -1) {
        currentEditCollectionTags.splice(idx, 1);
    } else {
        currentEditCollectionTags.push(tag);
    }
    dm_renderTags();
}

function dm_addNewTag() {
    const input = document.getElementById('edit-collection-new-tag');
    if (!input) return;

    const val = input.value.trim();
    if (!val) return;

    const newTags = val.split(',').map(t => t.trim()).filter(t => t.length > 0);
    if (newTags.length > 0) {
        newTags.forEach(tag => {
            if (!currentEditCollectionTags.includes(tag)) {
                currentEditCollectionTags.push(tag);
            }
        });
        input.value = '';
        dm_renderTags();
    }
}

function dm_saveAnnotation() {
    if (!currentEditCollectionId && !bulkEditMode) return;

    const saveBtn = document.getElementById('save-collection-btn');
    if (saveBtn) saveBtn.disabled = true;

    if (!bulkEditMode) {
        // Single collection save
        const displayIdInput = document.getElementById('edit-collection-display-id');
        const displayId = displayIdInput.value;
        const hiddenCheckbox = document.getElementById('edit-collection-hidden');
        const isHidden = hiddenCheckbox ? hiddenCheckbox.checked : false;

        const payload = {
            collection_id: currentEditCollectionId,
            display_collection_id: displayId,
            tags: currentEditCollectionTags,
            hidden: isHidden
        };

        fetch('/api/manage/collection/save_annotation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify(payload)
        })
            .then(r => r.json())
            .then(data => {
                if (saveBtn) saveBtn.disabled = false;
                if (data.status === 'success') {
                    closeEditCollectionModal();
                    loadAvailableCollections();
                } else {
                    alert('Failed to save: ' + (data.error || 'Unknown error'));
                }
            })
            .catch(err => {
                if (saveBtn) saveBtn.disabled = false;
                console.error("Error saving annotation:", err);
                alert("Error saving annotation.");
            });
    } else {
        // Bulk save: compute per-collection tag diffs
        const hiddenCheckbox = document.getElementById('edit-collection-hidden');
        const selectedIds = [...selectedCollectionIds];

        // Compute tags added/removed relative to the original intersection
        const originalIntersection = new Set();
        const tagSets = selectedIds.map(id => new Set(bulkOriginalTagsMap[id] || []));
        if (tagSets.length > 0) {
            tagSets[0].forEach(tag => {
                if (tagSets.every(s => s.has(tag))) originalIntersection.add(tag);
            });
        }
        const currentTagSet = new Set(currentEditCollectionTags);
        const tagsToAdd = [...currentTagSet].filter(t => !originalIntersection.has(t));
        const tagsToRemove = [...originalIntersection].filter(t => !currentTagSet.has(t));

        // Build per-collection payloads and save sequentially to avoid file race conditions
        const payloads = selectedIds.map(id => {
            const obj = availableCollections.find(c => (typeof c === 'object' ? c.id : c) === id);
            const origTags = bulkOriginalTagsMap[id] || [];
            const finalTags = [...new Set([
                ...origTags.filter(t => !tagsToRemove.includes(t)),
                ...tagsToAdd
            ])];

            const payload = {
                collection_id: id,
                display_collection_id: obj ? (obj.displayId || id) : id,
                tags: finalTags
            };

            if (hiddenUserTouched && hiddenCheckbox) {
                payload.hidden = hiddenCheckbox.checked;
            } else if (obj) {
                payload.hidden = !!obj.hidden;
            }

            return payload;
        });

        (async () => {
            let failed = 0;
            for (const payload of payloads) {
                try {
                    const r = await fetch('/api/manage/collection/save_annotation', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                        body: JSON.stringify(payload)
                    });
                    const data = await r.json();
                    if (data.status !== 'success') failed++;
                } catch (e) {
                    console.error('Error saving:', payload.collection_id, e);
                    failed++;
                }
            }
            if (saveBtn) saveBtn.disabled = false;
            if (failed > 0) {
                alert(`Saved ${payloads.length - failed} of ${payloads.length} collections. ${failed} failed.`);
            }
            closeEditCollectionModal();
            loadAvailableCollections();
        })();
    }
}

function dm_closeEditModal() {
    document.getElementById('edit-collection-modal').style.display = 'none';
}


function toggleAllCollectionCheckboxes(masterCheckbox) {
    const checked = masterCheckbox.checked;
    document.querySelectorAll('#edit-activity-list-container .collection-row-checkbox').forEach(cb => {
        const row = cb.closest('.edit-activity-item');
        if (row && row.style.display !== 'none') {
            cb.checked = checked;
            const id = cb.dataset.collectionId;
            if (checked) selectedCollectionIds.add(id);
            else selectedCollectionIds.delete(id);
        }
    });
    updateEditSelectedButton();
}
window.toggleAllCollectionCheckboxes = toggleAllCollectionCheckboxes;


function toggleCollectionSelection(collectionId, isChecked) {
    if (isChecked) selectedCollectionIds.add(collectionId);
    else selectedCollectionIds.delete(collectionId);
    updateSelectAllCheckbox();
    updateEditSelectedButton();
}


function updateSelectAllCheckbox() {
    const master = document.getElementById('select-all-collections');
    if (!master) return;
    const visible = document.querySelectorAll('#edit-activity-list-container .collection-row-checkbox');
    const visibleArr = Array.from(visible).filter(cb => {
        const row = cb.closest('.edit-activity-item');
        return row && row.style.display !== 'none';
    });
    const checkedCount = visibleArr.filter(cb => cb.checked).length;
    master.checked = visibleArr.length > 0 && checkedCount === visibleArr.length;
    master.indeterminate = checkedCount > 0 && checkedCount < visibleArr.length;
}


function updateEditSelectedButton() {
    const btn = document.getElementById('edit-selected-collections-btn');
    if (!btn) return;
    const count = selectedCollectionIds.size;
    btn.disabled = count === 0;
    btn.textContent = count > 0 ? `Edit (${count})` : 'Edit';
}


function openEditSelectedCollections() {
    if (selectedCollectionIds.size === 0) return;

    const selectedIds = [...selectedCollectionIds];

    if (selectedIds.length === 1) {
        const found = availableCollections.find(c => (typeof c === 'object' ? c.id : c) === selectedIds[0]);
        if (found) openEditCollectionModal(found);
        return;
    }

    // Multi-select mode
    bulkEditMode = true;
    currentEditCollectionId = null;
    hiddenUserTouched = false;

    // Collect objects for selected collections
    const selectedObjs = selectedIds.map(id =>
        availableCollections.find(c => (typeof c === 'object' ? c.id : c) === id)
    ).filter(Boolean);

    // Store original tags per collection for diff-based save
    bulkOriginalTagsMap = {};
    selectedObjs.forEach(obj => {
        bulkOriginalTagsMap[obj.id] = Array.isArray(obj.tags) ? [...obj.tags] : [];
    });

    // Compute tag intersection (shared by all) and partial tags (shared by some)
    const tagSets = selectedObjs.map(obj => new Set(Array.isArray(obj.tags) ? obj.tags : []));
    const allUnion = new Set();
    tagSets.forEach(s => s.forEach(t => allUnion.add(t)));
    const intersection = [...tagSets[0]].filter(tag => tagSets.every(s => s.has(tag)));
    currentEditCollectionTags = [...intersection];
    bulkPartialTags = new Set([...allUnion].filter(tag => !intersection.includes(tag) && tagSets.some(s => s.has(tag))));

    // Modal header
    document.getElementById('edit-collection-id-display').innerText = `${selectedIds.length} collections`;
    document.getElementById('edit-collection-id').value = '';

    // Disable display ID
    const displayIdInput = document.getElementById('edit-collection-display-id');
    displayIdInput.value = '';
    displayIdInput.disabled = true;
    displayIdInput.placeholder = 'Multiple collections selected';

    // Hidden checkbox: check if all, none, or mixed
    const hiddenCheckbox = document.getElementById('edit-collection-hidden');
    if (hiddenCheckbox) {
        const hiddenCount = selectedObjs.filter(o => o.hidden).length;
        if (hiddenCount === selectedObjs.length) {
            hiddenCheckbox.checked = true;
            hiddenCheckbox.indeterminate = false;
        } else if (hiddenCount === 0) {
            hiddenCheckbox.checked = false;
            hiddenCheckbox.indeterminate = false;
        } else {
            hiddenCheckbox.checked = false;
            hiddenCheckbox.indeterminate = true;
        }
        hiddenCheckbox.onchange = () => { hiddenUserTouched = true; };
    }

    dm_renderTags();
    document.getElementById('editCollectionModal').style.display = 'block';
}
window.openEditSelectedCollections = openEditSelectedCollections;

function queueVotedVideos(btnElement) {
    if (!confirm("Are you sure you want to add all machine-voted videos to the scrape and annotation queues?")) {
        return;
    }

    const originalText = btnElement.textContent;
    btnElement.textContent = "Processing...";
    btnElement.disabled = true;

    fetch('/api/manage/enrichment/queue_voted', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        }
    })
        .then(response => response.json())
        .then(data => {
            btnElement.textContent = originalText;
            btnElement.disabled = false;

            if (data.status === 'success') {
                alert(`Success: Added ${data.added_to_scrape} to scrape queue and ${data.added_to_annotate} to annotate queue.`);
                fetchEnrichmentStats(); // Refresh the stats
            } else if (data.status === 'no_votes' || data.status === 'no_matches') {
                alert(data.message);
            } else {
                alert('Error queuing voted videos: ' + data.error);
            }
        })
        .catch(error => {
            btnElement.textContent = originalText;
            btnElement.disabled = false;
            console.error('Error queuing voted videos:', error);
            alert('Error queuing voted videos.');
        });
}

