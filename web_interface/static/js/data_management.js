
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
            availableCollections = data; // Array of objects containing metadata
            // After collections are loaded, assume studies can be rendered correctly?
            // If loadStudies was called before, we might need to re-render.
            // But we chained init order to call loadAvailableCollections first.
            // So we trigger loadStudies HERE.
            loadStudies();
        })
        .catch(err => {
            console.error("Error loading collections list:", err);
            // Even if collections fail, load studies
            loadStudies();
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
        container.innerHTML = '<div style="padding: 10px; color: #aaa;">No collections available.</div>';
        return;
    }

    const table = document.createElement('table');
    table.className = 'collection-table';
    table.style.width = '100%';
    table.style.borderCollapse = 'collapse';
    table.style.color = '#ddd';
    table.style.fontSize = '0.9em';

    // Create Header
    const thead = document.createElement('thead');
    thead.innerHTML = `
        <tr style="text-align: left;">
            <th style="padding: 8px 5px; width: 30px; position: sticky; top: 0; background: #3e3e42; z-index: 10; border-bottom: 2px solid #555;"></th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: #3e3e42; z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid #555;" onclick="sortCollectionTable(this)">Collection ID</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: #3e3e42; z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid #555;" onclick="sortCollectionTable(this)">Display ID</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: #3e3e42; z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid #555;" onclick="sortCollectionTable(this)">Tags</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: #3e3e42; z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid #555;" onclick="sortCollectionTable(this)">Email</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: #3e3e42; z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid #555;" onclick="sortCollectionTable(this)">Name</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: #3e3e42; z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid #555;" onclick="sortCollectionTable(this)">TikTok</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: #3e3e42; z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid #555;" onclick="sortCollectionTable(this)">Age</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: #3e3e42; z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid #555;" onclick="sortCollectionTable(this)">Country</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: #3e3e42; z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid #555;" onclick="sortCollectionTable(this)">PostCode</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: #3e3e42; z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid #555;" onclick="sortCollectionTable(this)">Active Days</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: #3e3e42; z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid #555;" onclick="sortCollectionTable(this)">Total Events</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: #3e3e42; z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid #555;" onclick="sortCollectionTable(this)">Last Event</th>
            <th style="padding: 8px 5px; position: sticky; top: 0; background: #3e3e42; z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid #555;" onclick="sortCollectionTable(this)">Added</th>
        </tr>
    `;
    table.appendChild(thead);

    const tbody = document.createElement('tbody');

    availableCollections.forEach(itemInfo => {
        const item = typeof itemInfo === 'string' ? itemInfo : itemInfo.id;

        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid #444';
        tr.className = 'donation-item'; // Keep class for CSS/JS targeting

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

        const createCell = (text, isBold = false) => {
            const td = document.createElement('td');
            td.style.padding = '5px';
            if (isBold) td.innerHTML = `<strong>${text}</strong>`;
            else td.textContent = text;
            return td;
        }

        tr.appendChild(tdCheck);
        tr.appendChild(createCell(item, true));
        tr.appendChild(createCell(pDisplayId));
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
}

function updateCollectionSelection(selectorDiv) {
    if (!selectorDiv) return;
    const container = selectorDiv.querySelector('.donation-checklist-container');

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

window.sortCollectionTable = function (th) {
    const table = th.closest('table');
    const tbody = table.querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr.donation-item'));
    const headerRow = th.parentElement;
    const columnIndex = Array.from(headerRow.children).indexOf(th);

    let currentDir = th.dataset.sortDir || 'desc';
    let newDir = currentDir === 'asc' ? 'desc' : 'asc';

    headerRow.querySelectorAll('th').forEach(header => {
        header.dataset.sortDir = '';
        header.textContent = header.textContent.replace(/ [▼▲]$/, '');
    });

    th.dataset.sortDir = newDir;
    th.textContent += newDir === 'asc' ? ' ▲' : ' ▼';

    const textContent = th.textContent.replace(/ [▼▲]$/, '');
    const isNumeric = ['Age', 'Active Days', 'Total Events'].includes(textContent);

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
    const selectorDiv = inputElement.closest('.donation-selector');
    const items = selectorDiv.querySelectorAll('.donation-item'); // these are now table rows (tr)

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
    const selectorDiv = btn.closest('.donation-selector');
    const container = selectorDiv.querySelector('.donation-checklist-container');
    const items = container.querySelectorAll('.donation-item');

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
            <td style="text-align: right; padding: 10px;">${formatNum(stats.unique_donations)}</td>
            <td style="text-align: right; padding: 10px;">${formatNum(stats.unique_videos)}</td>
            <td style="text-align: right; padding: 10px;">${formatNum(stats.scraped_videos)}</td>
            <td style="text-align: right; padding: 10px;">${formatNum(stats.annotated_videos)}</td>
            <td style="text-align: right; padding: 10px;">${lastUpdated}</td>
            <td style="padding: 10px;">
                ${savingStudies.has(study.STUDY_NAME) ? '<span style="color: #00ff00; font-weight: bold; text-shadow: 0 0 5px #00ff00;">Saving...</span>' : ''}
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
        row.style.backgroundColor = '#1e1e1e';
        row.style.borderLeft = '4px solid #3b82f6';
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
            // Find the donation selector in this row
            // The row input[data-field="SELECTED_DONATIONS"] is now the HIDDEN one.
            // renderCollectionSelector needs the container.
            // Structure: input[hidden] is sibling of div.donation-selector

            // Wait, input iteration loop finds the HIDDEN input.
            // We can set its value (for reference) AND render the list.

            const selectorDiv = input.parentElement.querySelector('.donation-selector');
            if (selectorDiv) {
                const container = selectorDiv.querySelector('.donation-checklist-container');
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

// Helper to collect save settings (flags that aren't part of the study definition)
// but are sent in the save payload.
function collectSaveSettings(row) {
    const settings = {};
    const pcaChk = row.querySelector('.save-setting-pca');
    const metaChk = row.querySelector('.save-setting-meta');

    if (pcaChk) settings['REFRESH_PCA'] = pcaChk.checked;
    if (metaChk) settings['REFRESH_METADATA'] = metaChk.checked;

    return settings;
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
        actionCell.innerHTML = '<span style="color: #00ff00; font-weight: bold; text-shadow: 0 0 5px #00ff00;">Saving...</span>';

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
                                    saveBtn.style.backgroundColor = "#28a745";

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

// --- Enrichment Stats & Logic ---

function fetchEnrichmentStats() {
    fetch('/api/manage/enrichment/stats')
        .then(res => res.json())
        .then(data => {
            // Stats
            document.getElementById('enrich_total_videos').textContent = (data.total_videos !== undefined) ? data.total_videos.toLocaleString() : '-';
            document.getElementById('enrich_scraped').textContent = (data.scraped_videos !== undefined) ? data.scraped_videos.toLocaleString() : '-';
            document.getElementById('enrich_annotated').textContent = (data.annotated_videos !== undefined) ? data.annotated_videos.toLocaleString() : '-';
            document.getElementById('enrich_unique_donations').textContent = (data.unique_donations !== undefined) ? data.unique_donations.toLocaleString() : '-';

            // Queues (If they still exist or are used for Annotations)
            // Legacy code removed, annotate queues are study-specific now.
        })
        .catch(err => console.error("Error fetching enrichment stats:", err));
}

function calculateVideosToScrape() {
    const studyName = document.getElementById('enrichment-study-select').value;
    const targetsDisplay = document.getElementById('enrich_scrape_targets');

    if (!studyName) {
        alert("Please select a study from the dropdown first.");
        return;
    }

    targetsDisplay.textContent = "Calculating...";
    targetsDisplay.style.color = "#aaa";

    fetch('/api/manage/enrichment/calculate_to_scrape', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ study_name: studyName })
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                targetsDisplay.textContent = data.videos_to_scrape.toLocaleString();
                targetsDisplay.style.color = "#4cd964";
            } else {
                targetsDisplay.textContent = "Error";
                targetsDisplay.style.color = "#ff4444";
                alert("Error: " + data.error);
            }
        })
        .catch(err => {
            targetsDisplay.textContent = "Failed";
            targetsDisplay.style.color = "#ff4444";
            console.error(err);
        });
}

function calculateVideosToAnnotate() {
    const studyName = document.getElementById('enrichment-study-select').value;
    const targetsDisplay = document.getElementById('enrich_annotate_targets');

    if (!studyName) {
        alert("Please select a study from the dropdown first.");
        return;
    }

    targetsDisplay.textContent = "Calculating...";
    targetsDisplay.style.color = "#aaa";

    fetch('/api/manage/enrichment/calculate_to_annotate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ study_name: studyName })
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                targetsDisplay.textContent = data.videos_to_annotate.toLocaleString();
                targetsDisplay.style.color = "#4cd964";
            } else {
                targetsDisplay.textContent = "Error";
                targetsDisplay.style.color = "#ff4444";
                alert("Error: " + data.error);
            }
        })
        .catch(err => {
            targetsDisplay.textContent = "Failed";
            targetsDisplay.style.color = "#ff4444";
            console.error(err);
        });
}

function emptyQueues() {
    if (!confirm("Are you sure you want to empty the Scrape and Annotation queues? This action cannot be undone.")) return;

    fetch('/api/manage/enrichment/empty_queues', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken }
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                alert(data.message);
                fetchEnrichmentStats();
            } else {
                alert("Error: " + data.error);
            }
        })
        .catch(err => alert("Failed to empty queues: " + err));
}

function consolidateEnrichmentData(btn) {
    if (!confirm("Are you sure you want to consolidate enrichment data? This may take a moment.")) return;

    const originalText = btn.textContent;
    btn.textContent = "Consolidating...";
    btn.disabled = true;

    fetch('/api/manage/enrichment/consolidate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken }
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                alert("Enrichment data consolidated successfully.");
                fetchEnrichmentStats();
            } else {
                alert("Error: " + data.error);
            }
        })
        .catch(err => alert("Failed to consolidate: " + err))
        .finally(() => {
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
