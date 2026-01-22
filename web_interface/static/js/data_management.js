
// data_management.js

let allStudies = [];

function loadStudies() {
    fetch('/api/manage/studies')
        .then(response => response.json())
        .then(data => {
            allStudies = data;
            renderStudiesTable();
        })
        .catch(err => console.error("Error loading studies:", err));
}

let availableDonations = [];

function loadAvailableDonations() {
    fetch('/api/manage/donations')
        .then(res => res.json())
        .then(data => {
            availableDonations = data; // Array of formatted strings
            // After donations are loaded, assume studies can be rendered correctly?
            // If loadStudies was called before, we might need to re-render.
            // But we chained init order to call loadAvailableDonations first.
            // So we trigger loadStudies HERE.
            loadStudies();
        })
        .catch(err => {
            console.error("Error loading donations list:", err);
            // Even if donations fail, load studies
            loadStudies();
        });
}

// --------------------------------------------------------------------------
// Donation Selector Helper Logic
// --------------------------------------------------------------------------

function renderDonationSelector(container, selectedList) {
    if (!container) return;

    container.innerHTML = '';
    const selectedSet = new Set(selectedList || []);

    if (availableDonations.length === 0) {
        container.innerHTML = '<div style="padding: 10px; color: #aaa;">No donations available.</div>';
        return;
    }

    const frag = document.createDocumentFragment();

    availableDonations.forEach(item => {
        const div = document.createElement('div');
        div.style.padding = '2px 5px';
        div.className = 'donation-item'; // for filtering

        // Hide if filtered out? (No, render is usually called on expand. Filtering is separate)

        const label = document.createElement('label');
        label.style.display = 'flex';
        label.style.alignItems = 'center';
        label.style.cursor = 'pointer';
        label.style.width = '100%';
        label.style.color = '#ddd';

        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = item;
        cb.checked = selectedSet.has(item);
        cb.style.marginRight = '8px';

        // Event listener to update count and hidden input
        cb.onchange = function () {
            updateDonationSelection(container.parentElement); // Pass parent .donation-selector
        };

        label.appendChild(cb);
        label.appendChild(document.createTextNode(item));

        div.appendChild(label);
        frag.appendChild(div);
    });

    container.appendChild(frag);

    // Initial count update
    updateDonationSelection(container.parentElement);
}

function updateDonationSelection(selectorDiv) {
    if (!selectorDiv) return;
    const container = selectorDiv.querySelector('.donation-checklist-container');
    // Finds the hidden input which is a sibling of selectorDiv in our HTML structure?
    // Structure: div.form-group > label, div.donation-selector, input[hidden]
    // So hidden input is next sibling of selectorDiv.
    const hiddenInput = selectorDiv.nextElementSibling;
    const countSpan = selectorDiv.querySelector('.selected-count');

    const checked = container.querySelectorAll('input[type="checkbox"]:checked');
    const values = Array.from(checked).map(c => c.value);

    if (countSpan) countSpan.textContent = values.length;

    if (hiddenInput && hiddenInput.dataset.field === 'SELECTED_DONATIONS') {
        hiddenInput.value = JSON.stringify(values);
    }
}

function filterDonations(inputElement) {
    const searchText = inputElement.value.toLowerCase();
    const selectorDiv = inputElement.closest('.donation-selector');
    const items = selectorDiv.querySelectorAll('.donation-item');

    items.forEach(item => {
        const text = item.textContent.toLowerCase();
        if (text.includes(searchText)) {
            item.style.display = 'block';
        } else {
            item.style.display = 'none';
        }
    });
}

function selectAllDonations(btn, select) {
    const selectorDiv = btn.closest('.donation-selector');
    const container = selectorDiv.querySelector('.donation-checklist-container');
    const items = container.querySelectorAll('.donation-item');

    items.forEach(item => {
        if (item.style.display !== 'none') {
            const cb = item.querySelector('input[type="checkbox"]');
            cb.checked = select;
        }
    });

    updateDonationSelection(selectorDiv);
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
                <!-- Actions provided in detail view mostly -->
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
    } else {
        detailRow.style.display = 'none';
        icon.textContent = '▶';
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
            // renderDonationSelector needs the container.
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
                renderDonationSelector(container, selectedList);
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
        const field = group.dataset.fieldGroup;
        const currentList = study[field] || []; // e.g. ["admin", "researcher"] or ["all"]

        const checkboxes = group.querySelectorAll('input[type="checkbox"]');
        checkboxes.forEach(chk => {
            // Logic: if currentList has 'all', check everything (or specific logic)
            // If chk.value is in currentList, check it.

            if (currentList.includes('all')) {
                chk.checked = true;
            } else {
                chk.checked = currentList.includes(chk.value);
            }
        });
    });
}


function collectFormData(row) {
    const data = {};

    // 1. Standard Inputs
    const inputs = row.querySelectorAll('[data-field]');
    inputs.forEach(input => {
        const field = input.dataset.field;
        let value = input.value;

        // Parse Types
        if (field === 'INCLUDE_ZEESCHUIMER_DATA' || field === 'INCLUDE_DONATION_DATA') {
            data[field] = (value === 'true');
        }
        else if (field === 'SELECTED_DONATIONS') {
            try {
                // For the new UI, the input.value is already a clean JSON string set by updateDonationSelection.
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


function saveStudy(btn, event) {
    if (event) event.preventDefault();
    const detailRow = btn.closest('tr');
    const studyName = detailRow.dataset.studyName;

    try {
        const formData = collectFormData(detailRow);
        formData.STUDY_NAME = studyName;
        //console.log("Saving study definition:", formData);

        btn.textContent = "Saving...";
        btn.disabled = true;

        fetch('/api/manage/studies/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify(formData)
        })
            .then(res => res.json())
            .then(data => {
                if (data.status === 'success') {
                    // Update local model
                    const index = allStudies.findIndex(s => s.STUDY_NAME === studyName);
                    if (index !== -1) {
                        allStudies[index] = data.study;
                    }
                    renderStudiesTable(); // Re-render to show updated stats/dates
                    // Re-open the row that was open?
                    // render closes everything.
                    // ideally we stay open.
                    // Quick hack:
                    setTimeout(() => {
                        const newDetail = document.getElementById(`detail-${index}`);
                        const newRow = newDetail.previousElementSibling;
                        toggleDetail(null, index); // Open it
                        alert("Study saved successfully!");
                    }, 100);

                } else {
                    alert("Error saving: " + data.error);
                }
            })
            .catch(err => {
                console.error(err);
                alert("Save failed.");
            })
            .finally(() => {
                btn.textContent = "Save Changes";
                btn.disabled = false;
            });

    } catch (e) {
        // Validation failed
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
        INCLUDE_ZEESCHUIMER_DATA: true,
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
// Assuming csrfToken is available globally (it usually is in main layout or main.js)
// If not, we might need to get it from meta tag.
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

// Hook into tab open?
// Or just load when file loaded? 
// Better to expose init function or run loadStudies if tab is active?
// main.js usually handles tab switching. We can add to the tab onclick or just load once.
// Let's rely on explicit call or just run it:
// Load donations FIRST, then studies to ensure selector populates correctly
loadAvailableDonations();

