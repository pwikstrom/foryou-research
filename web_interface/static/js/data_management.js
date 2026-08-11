
// data_management.js

let allStudies = [];
const savingStudies = new Set(); // Track studies currently being saved
const refreshingStudies = new Map(); // Track studies being refreshed: name → {message, percent}

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

function loadSystemRoles(callback) {
    fetch('/api/admin/roles')
        .then(res => res.json())
        .then(data => {
            // /api/admin/roles now returns [{name, permissions}]; downstream
            // code (access dropdown etc.) expects a list of role-name strings.
            systemRoles = Array.isArray(data)
                ? data.map(r => (typeof r === 'string' ? r : r.name)).filter(Boolean)
                : [];
            if (callback) callback();
        })
        .catch(err => {
            // Non-admins can't read /api/admin/roles (it returns an HTML
            // login/403 page, not JSON). Roles only drive the admin-only
            // access dropdown, so fall back to an empty list and still run
            // the callback — otherwise the study modal never opens for them.
            console.error("Error loading roles:", err);
            systemRoles = [];
            if (callback) callback();
        });
}

// --------------------------------------------------------------------------
// Collection Selector Helper Logic
// --------------------------------------------------------------------------

function renderCollectionSelector(container, selectedList, readOnly = false) {
    if (!container) return;

    container.innerHTML = '';
    const selectedSet = new Set(selectedList || []);

    if (readOnly) {
        _renderReadOnlyCollectionList(container, selectedList || []);
        return;
    }

    if (availableCollections.length === 0) {
        container.innerHTML = '<div style="padding: 10px; color: var(--color-text-tertiary);">No collections available.</div>';
        return;
    }

    const table = document.createElement('table');
    table.className = 'collection-table';
    table.style.cssText = 'width: 100% !important; max-width: 100%; border-collapse: collapse; color: var(--color-text-secondary);';
    table.classList.add('text-sm');

    // Create Header (column order matches Edit Collections table)
    const thead = document.createElement('thead');
    const sThStyle = 'padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid var(--color-border-strong);';
    thead.innerHTML = `
        <tr style="text-align: left;">
            <th style="padding: 8px 5px; width: 30px; position: sticky; top: 0; background: var(--color-border); z-index: 10; border-bottom: 2px solid var(--color-border-strong);"><input type="checkbox" class="select-all-collections" title="Select / deselect all" style="cursor: pointer;"></th>
            <th style="${sThStyle} max-width: 160px;" onclick="sortCollectionTable(this)">Collection</th>
            <th style="${sThStyle}" onclick="sortCollectionTable(this)">Tags</th>
            <th style="${sThStyle}" onclick="sortCollectionTable(this)">Last Event</th>
            <th style="${sThStyle}" onclick="sortCollectionTable(this)">Added</th>
            <th style="${sThStyle}" onclick="sortCollectionTable(this)">Activities</th>
            <th style="${sThStyle}" onclick="sortCollectionTable(this)">Active Days</th>
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
                    pLastEvent = fypWallDate(itemInfo.personas.last_event_ts);
                }
            }
            if (itemInfo.other && itemInfo.other.ts_added_to_dataset) {
                pAdded = fypFmtDate(itemInfo.other.ts_added_to_dataset);
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
        const idCell = createCell(primaryId, true, item);
        idCell.style.maxWidth = '160px';
        idCell.style.overflow = 'hidden';
        idCell.style.textOverflow = 'ellipsis';
        tr.appendChild(idCell);
        tr.appendChild(createCell(pTags));
        tr.appendChild(createCell(pLastEvent));
        tr.appendChild(createCell(pAdded));
        tr.appendChild(createCell(pTotalEvents));
        tr.appendChild(createCell(pActiveDays));

        tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    container.appendChild(table);

    // Wire up select-all checkbox in header
    const selectAllCb = thead.querySelector('.select-all-collections');
    if (selectAllCb) {
        selectAllCb.onchange = function () {
            const selectorDiv = container.parentElement;
            const items = container.querySelectorAll('.collection-item');
            items.forEach(item => {
                if (item.style.display !== 'none') {
                    item.querySelector('input[type="checkbox"]').checked = selectAllCb.checked;
                }
            });
            updateCollectionSelection(selectorDiv);
        };
    }

    // Initial count update
    updateCollectionSelection(container.parentElement);

    // Apply saved sort state
    const studyRow = container.closest('.study-edit-form');
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

// Read-only collection list for the My Studies modal. Built from the study's own
// SELECTED_COLLECTIONS rather than the global `availableCollections`, which comes
// from an endpoint a plain viewer is refused — and which would expose every donor
// in the system, not just this study's.
function _renderReadOnlyCollectionList(container, selectedList) {
    const ids = Array.from(selectedList).sort();
    if (!ids.length) {
        container.innerHTML = '<div style="padding: 10px; color: var(--color-text-tertiary);">This study has no collections.</div>';
        return;
    }

    const header = document.createElement('div');
    header.className = 'text-xs';
    header.style.cssText = 'padding: 6px 5px; color: var(--color-text-secondary); border-bottom: 1px solid var(--color-border-strong);';
    header.textContent = `${ids.length.toLocaleString()} collection${ids.length === 1 ? '' : 's'} in this study`;
    container.appendChild(header);

    const list = document.createElement('div');
    list.className = 'text-sm';
    list.style.cssText = 'display: flex; flex-wrap: wrap; gap: 4px 10px; padding: 8px 5px; color: var(--color-text-secondary);';
    ids.forEach(id => {
        const span = document.createElement('span');
        span.style.cssText = 'font-family: var(--font-mono); white-space: nowrap;';
        span.textContent = id;
        list.appendChild(span);
    });
    container.appendChild(list);
}

const _LARGE_STUDY_THRESHOLD = 500000;

function _sumSelectedActivities(formContainer) {
    const hiddenInput = formContainer.querySelector('input[data-field="SELECTED_COLLECTIONS"]');
    let selectedIds = [];
    try {
        selectedIds = JSON.parse((hiddenInput ? hiddenInput.value : '[]').replace(/'/g, '"'));
    } catch (e) { return 0; }
    let total = 0;
    selectedIds.forEach(id => {
        const col = availableCollections.find(c => c.id === id);
        if (col && col.personas) total += (col.personas.total_events || 0);
    });
    return total;
}

function _checkLargeStudy(formContainer) {
    const sampleSelect = formContainer.querySelector('[data-field="SAMPLE_FRAME"]');
    const sampleValue = sampleSelect ? sampleSelect.value : 'off';
    if (sampleValue !== 'off') return Promise.resolve(true);

    // The daily chart holds a per-day count for exactly the selected collections,
    // so summing it across the study window is the size the study will actually
    // have. Only when the chart has no data (fetch failed / not yet returned) do
    // we fall back to the whole-collection totals, which ignore the window.
    const windowed = _windowActivityCount(formContainer);
    const totalActivities = windowed != null ? windowed : _sumSelectedActivities(formContainer);
    if (totalActivities <= _LARGE_STUDY_THRESHOLD) return Promise.resolve(true);

    const dateInputs = _dateInputs(formContainer);
    const rangeNote = (windowed != null && dateInputs.start?.value && dateInputs.end?.value)
        ? ` between ${dateInputs.start.value} and ${dateInputs.end.value}`
        : '';

    return new Promise(resolve => {
        const overlay = document.getElementById('large-study-warning');
        const textEl = document.getElementById('large-study-warning-text');
        textEl.innerHTML = `This study covers approximately <strong>${totalActivities.toLocaleString()}</strong> activities${rangeNote} with no sampling. ` +
            `To avoid impacting the hub's performance, consider limiting the time period, enabling sampling, or reducing the number of collections.`;
        overlay.classList.add('visible');
        document.getElementById('large-study-warning-back').onclick = () => {
            overlay.classList.remove('visible');
            resolve(false);
        };
        document.getElementById('large-study-warning-continue').onclick = () => {
            overlay.classList.remove('visible');
            resolve(true);
        };
    });
}

function updateCollectionSelection(selectorDiv) {
    if (!selectorDiv) return;
    const container = selectorDiv.querySelector('.collection-checklist-container');

    // More robust way to find the hidden input within the same detail row instead of sibling logic
    const row = selectorDiv.closest('.study-edit-form') || document;
    const hiddenInput = row.querySelector('input[data-field="SELECTED_COLLECTIONS"]');

    const formContainer = selectorDiv.closest('.study-edit-form') || selectorDiv.closest('.form-group') || selectorDiv;

    const checked = container.querySelectorAll('input[type="checkbox"]:checked:not(.select-all-collections)');
    const values = Array.from(checked).map(c => c.value);

    // Collections potential updates instantly; the mosaic + stats are recomputed for
    // the new collection set (which may rebuild the cache), so show a loading message
    // instead of wiping to the empty placeholder. The estimate fires from the daily-
    // chart refetch below once the date window has snapped to the new collections.
    _updateCollectionsHeader({ resetActual: true, potential: values.length });
    if (values.length) {
        _showStudyVizLoading(formContainer, 'Loading new collection(s)…');
    } else {
        _resetStudySetViz(formContainer, 'empty');
    }

    if (hiddenInput && hiddenInput.dataset.field === 'SELECTED_COLLECTIONS') {
        hiddenInput.value = JSON.stringify(values);
    }

    // Clear previous issues & included-per-day overlay; refetch chart totals (debounced),
    // which re-estimates the mosaic once the new date window is known.
    _clearStudyIssues(formContainer);
    _invalidateDailyChartOverlay(formContainer);
    _debouncedRefetchDailyChart(formContainer);

    // Sync select-all checkbox state
    const selectAllCb = container.querySelector('.select-all-collections');
    if (selectAllCb) {
        const visibleItems = container.querySelectorAll('.collection-item:not([style*="display: none"])');
        const visibleChecked = Array.from(visibleItems).filter(item => item.querySelector('input[type="checkbox"]').checked);
        selectAllCb.checked = visibleItems.length > 0 && visibleChecked.length === visibleItems.length;
        selectAllCb.indeterminate = visibleChecked.length > 0 && visibleChecked.length < visibleItems.length;
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
            const studyRow = th.closest('.study-edit-form');
            if (studyRow && studyRow.dataset.studyName) {
                tableSortStates.set(`study-${studyRow.dataset.studyName}`, { dir: newDir, text: textContent });
            }
        }
    }

    const isNumeric = ['Age', 'Active Days', 'Activities', 'Watch Time'].includes(textContent);

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

function renderStudiesTable() {
    // The studies partial is included once per tab that wants to show it
    // (Data Management's Studies sub-page + My Studies). Populate every copy.
    const tbodies = document.querySelectorAll('.studies-table-body');
    if (tbodies.length === 0) return;

    const buildRow = (study, index, allowEdit) => {
        const tr = document.createElement('tr');
        tr.className = 'study-row';
        tr.style.borderBottom = '1px solid var(--chart-grid)';

        const isRefreshing = refreshingStudies.has(study.STUDY_NAME);
        const isSaving = savingStudies.has(study.STUDY_NAME);

        if (isRefreshing || isSaving) {
            tr.style.cursor = 'default';
            tr.style.opacity = '0.45';
        } else {
            // My Studies opens the same modal read-only: every field disabled,
            // no Save/Delete/Access, and rendered without the Data-Management
            // endpoints a plain viewer would be refused.
            tr.style.cursor = 'pointer';
            tr.style.opacity = '1';
            tr.onclick = () => openStudyModal(index, !allowEdit);
        }

        const stats = study.stats || {};
        const formatNum = (num) => num !== undefined ? num.toLocaleString() : '-';

        let actionHtml = '';
        if (isSaving) {
            actionHtml = '<span class="text-sm font-semibold" style="color: var(--color-warning);">Saving...</span>';
        } else if (isRefreshing) {
            const info = refreshingStudies.get(study.STUDY_NAME);
            const pct = info.percent !== undefined ? info.percent : 0;
            const msg = info.message || 'Refreshing...';
            actionHtml = `
                <div style="display: flex; flex-direction: column; gap: 4px;">
                    <span class="text-sm font-semibold" style="color: var(--color-warning);">${msg}</span>
                    <div class="progress-bar" style="height: 5px; border-radius: 3px;">
                        <div style="width: ${pct}%; height: 100%; background: var(--color-warning); border-radius: 3px; transition: width 0.3s;"></div>
                    </div>
                </div>`;
        } else if (!allowEdit) {
            // Per-study provenance note (My Studies only — it lists every study
            // the user can see, so the note is offered per row rather than for
            // one "active" study the way Explore used to). Wired below rather
            // than inline, so a study name containing a quote is safe.
            actionHtml = '<button class="btn-discreet text-xs js-study-methods-btn">Methods</button>';
        }

        tr.innerHTML = `
            <td style="padding: 5px;"><strong>${study.STUDY_NAME}</strong></td>
            <td style="padding: 5px;">${study.START_DATE || '-'}</td>
            <td style="padding: 5px;">${study.END_DATE || '-'}</td>
            <td style="padding: 5px;">${(study.SAMPLE_FRAME === 'events' ? 'activities' : study.SAMPLE_FRAME) || '-'}</td>
            <td style="text-align: right; padding: 5px;">${formatNum(stats.unique_collections)}</td>
            <td style="text-align: right; padding: 5px;">${formatNum(stats.total_activities)}</td>
            <td style="text-align: right; padding: 5px;">${formatNum(stats.unique_videos)}</td>
            <td style="text-align: right; padding: 5px;">${formatNum(stats.scraped_videos)}</td>
            <td style="text-align: right; padding: 5px;">${formatNum(stats.annotated_videos)}</td>
            <td style="padding: 5px;">${actionHtml}</td>
        `;

        const methodsBtn = tr.querySelector('.js-study-methods-btn');
        if (methodsBtn) {
            methodsBtn.title = "How this study's dataset was built";
            methodsBtn.onclick = (ev) => {
                ev.stopPropagation();   // don't also open the read-only definition
                if (typeof openStudyMethodsModal === 'function') {
                    openStudyMethodsModal(study.STUDY_NAME);
                }
            };
        }
        return tr;
    };

    tbodies.forEach(tbody => {
        // Rows inside the My Studies tab are read-only — they list studies but
        // do not open the edit modal. The DM "Define Studies" sub-page keeps
        // the click-to-edit behaviour.
        const allowEdit = !tbody.closest('#my-stuff-page-studies');
        tbody.innerHTML = '';
        if (allStudies.length === 0) {
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.colSpan = 10;
            td.className = 'text-sm';
            td.style.cssText = 'padding: 16px; text-align: center; color: var(--color-text-muted);';
            td.textContent = allowEdit
                ? 'No studies defined yet — use "New Study" to create one.'
                : 'No studies listed here yet. Studies shared with you appear in the study picker in the header; ask the researcher who invited you if you expected one.';
            tr.appendChild(td);
            tbody.appendChild(tr);
            return;
        }
        allStudies.forEach((study, index) => {
            tbody.appendChild(buildRow(study, index, allowEdit));
        });
    });
}

function openStudyModal(index, readOnly = false) {
    const study = allStudies[index];
    if (!study) return;

    // Block opening if study is currently refreshing
    if (refreshingStudies.has(study.STUDY_NAME)) return;

    // Read-only opens straight away: roles only feed the admin-only access
    // dropdown, which is not rendered in that mode.
    if (readOnly) {
        _showStudyModal(study, false, true);
        return;
    }

    // Refresh roles before populating to pick up any newly defined roles
    loadSystemRoles(() => _showStudyModal(study));
}

function _showStudyModal(study, isNew = false, readOnly = false) {
    const modal = document.getElementById('editStudyModal');
    const title = document.getElementById('editStudyModalTitle');
    const body = document.getElementById('editStudyModalBody');

    body.innerHTML = '';

    // The access dropdown is admin-only markup, but an admin browsing My
    // Studies would otherwise see an editing control on a read-only view.
    const accessDropdown = document.getElementById('studyAccessDropdown');
    if (accessDropdown) accessDropdown.style.display = readOnly ? 'none' : '';

    if (isNew) {
        title.textContent = 'New Study';
        // Add name input row before the form
        const nameRow = document.createElement('div');
        nameRow.style.cssText = 'display: flex; align-items: center; gap: 10px; margin-bottom: 16px;';
        nameRow.innerHTML = '<label class="font-semibold" style="white-space: nowrap;">Study Name:</label>' +
            '<input type="text" id="newStudyNameInput" class="control-input" placeholder="Enter a unique name..." style="flex: 1;">';
        body.appendChild(nameRow);
    } else {
        title.textContent = study.STUDY_NAME;
    }

    const template = document.getElementById('study_detail_template');
    const formClone = template.content.cloneNode(true).querySelector('.study-edit-form');
    formClone.dataset.studyName = study.STUDY_NAME;
    if (isNew) {
        formClone.dataset.isNew = 'true';
        // Rename/Duplicate act on a saved study — meaningless before first save.
        formClone.querySelectorAll('.js-study-existing-action').forEach(b => { b.style.display = 'none'; });
    }
    // Consulted by populateForm and the chart/collection renderers. Read-only
    // renders entirely from the /api/manage/studies payload — it never calls the
    // Data-Management-only endpoints a viewer would be refused.
    if (readOnly) formClone.dataset.readOnly = '1';

    // Set last updated text in modal header
    const lastUpdatedEl = document.getElementById('editStudyModalLastUpdated');
    if (lastUpdatedEl) {
        lastUpdatedEl.textContent = study.last_updated
            ? 'Last updated: ' + formatShortDate(study.last_updated)
            : '';
    }

    body.appendChild(formClone);
    populateForm(formClone, study);

    if (readOnly) _lockStudyForm(formClone);

    modal.classList.add('visible');

    if (isNew) {
        document.getElementById('newStudyNameInput')?.focus();
    }
}


// Belt-and-braces over the template's own `{% if not current_user.is_admin() %}
// disabled{% endif %}`: that only covers non-admins, and an admin can reach this
// modal from My Studies too, where nothing is editable.
function _lockStudyForm(row) {
    row.querySelectorAll('input, select, textarea, button').forEach(el => {
        el.disabled = true;
    });
    row.querySelectorAll('.sampling-input').forEach(el => { el.style.opacity = ''; });
}

function closeStudyModal() {
    const modal = document.getElementById('editStudyModal');
    modal.classList.remove('visible');
    const accessPanel = document.getElementById('studyAccessPanel');
    if (accessPanel) accessPanel.style.display = 'none';

    // Tear down the Plotly chart so its interaction layers (drag cover, hover)
    // don't keep catching pointer events beneath the closed modal. Also clear
    // the body entirely so no stale DOM lingers.
    const chartDiv = modal.querySelector('.study-daily-chart');
    if (chartDiv && chartDiv._plotlyInited && window.Plotly) {
        window.Plotly.purge(chartDiv);
        chartDiv._plotlyInited = false;
    }
    const body = document.getElementById('editStudyModalBody');
    if (body) body.innerHTML = '';
}

// Kept as no-op for backwards compatibility if something still calls it.
window.toggleSamplingOptions = function () { };


function populateForm(row, study) {
    const readOnly = row.dataset.readOnly === '1';

    // 1. Standard Inputs
    const inputs = row.querySelectorAll('[data-field]');
    inputs.forEach(input => {
        const field = input.dataset.field;
        let value = study[field];

        // Never assign a non-string/number to an input's value — it would
        // coerce via toString() and round-trip garbage (e.g. "[object Object]")
        // back to the server on the next save. Skip fields whose shape we
        // manage via dedicated renderers (stats, SELECTED_COLLECTIONS handled
        // below, USER_ACCESS is a checkbox group, not a scalar input).
        if (field === 'stats') return;

        // Handle Lists/JSON (Except USER_ACCESS which is now checkboxes)
        if (field === 'SELECTED_COLLECTIONS') {
            // Find the collection selector in this row
            // The row input[data-field="SELECTED_COLLECTIONS"] is now the HIDDEN one.
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
                renderCollectionSelector(container, selectedList, readOnly);
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
            if (field === 'SAMPLE_FRAME') {
                input.value = (value === 'events' ? 'activities' : value) || "activities";
            }
            else {
                if (value === true) input.value = "true";
                else if (value === false) input.value = "false";
                else input.value = value || "true";
            }
        }
        else {
            // Defaults for a NEW study (field undefined). Max fields default to blank,
            // which the backend reads as "no cap". An explicitly blank value on an
            // existing study is preserved (also "no cap") rather than re-defaulted.
            const samplingDefaults = {
                'MIN_ACTIVITY_COUNT_PER_GROUP': 10,
                'MAX_ACTIVITY_COUNT_PER_GROUP': '',
                'MIN_GROUP_COUNT_PER_COLLECTION': 0,
                'MAX_GROUP_COUNT_PER_COLLECTION': ''
            };
            if (value !== undefined && value !== null) {
                input.value = value;
            } else if (field in samplingDefaults) {
                input.value = samplingDefaults[field];
            } else {
                input.value = '';
            }
        }
    });

    // Keep sampling-matrix inputs in sync with SAMPLE_FRAME value — gray out when 'off'.
    const sampleSelect = row.querySelector('[data-field="SAMPLE_FRAME"]');
    if (sampleSelect) {
        const syncSamplingInputs = () => {
            const isOff = sampleSelect.value === 'off';
            row.querySelectorAll('.sampling-input').forEach(inp => {
                inp.disabled = isOff;
                inp.style.opacity = isOff ? '0.4' : '';
            });
            const matrix = row.querySelector('.sampling-matrix');
            if (matrix) matrix.style.opacity = isOff ? '0.6' : '';
        };
        sampleSelect.addEventListener('change', syncSamplingInputs);
        // Run once after values are populated (end of populateForm).
        setTimeout(syncSamplingInputs, 0);
    }

    // 2. Checkbox Groups (USER_ACCESS) — now lives in the modal header dropdown
    // rather than the cloned template, so look it up via the modal scope.
    if (!readOnly) _renderAccessDropdown(study);

    // 3. Stats Display (seed from saved study; potentials fill on chart fetch).
    // Collections shows in the header; the mosaic viz needs the date-range universe
    // counts (only from /calculate_stats), so it stays on its placeholder until the
    // auto-estimate runs.
    const stats = study.stats || {};
    const seededPotentialCols = Array.isArray(study.SELECTED_COLLECTIONS) ? study.SELECTED_COLLECTIONS.length : undefined;
    if (stats.unique_collections != null) {
        _updateCollectionsHeader({ actual: stats.unique_collections, potential: seededPotentialCols });
    } else {
        _updateCollectionsHeader({ resetActual: true, potential: seededPotentialCols });
    }
    // Seed the mosaic from the last persisted check so it is present on open.
    // Opening triggers a daily-activities fetch that snaps the date window and can
    // reset the viz; _fetchDailyChart re-applies this seed once that settles, gated
    // on the initialFetch flag so later user edits still clear the viz.
    row.dataset.initialFetch = '1';
    if (stats.universe && Number(stats.universe.activities) > 0) {
        _renderStudySetViz(row, { universe: stats.universe, included: stats, frame: study.SAMPLE_FRAME, seeded: true });
    } else {
        _resetStudySetViz(row, 'empty');
    }

    // Read-only stops here: everything below either edits the form or calls a
    // Data-Management-only endpoint (/calculate_stats, /daily_activities,
    // /prewarm_check). The chart is seeded from the study's own
    // cached_daily_activities below instead of being refetched.
    if (readOnly) {
        const cached = study.cached_daily_activities;
        const state = _getChartState(row);
        if (cached && Array.isArray(cached.total_per_day) && cached.total_per_day.length) {
            state.totalPerDay = cached.total_per_day;
            if (cached.potentials && cached.potentials.collections != null) {
                _updateCollectionsHeader({ potential: cached.potentials.collections });
            }
        } else {
            state.totalPerDay = [];
        }
        state.loading = false;
        _renderDailyChart(row);
        return;
    }

    // Auto-update the mosaic / issues / overlay when sampling or the date window
    // changes — no button. Sampling commits on 'change' (release/blur); the hidden
    // date fields are driven by the chart selection via 'input'. Both funnel through
    // one debounced, sequenced estimate (_scheduleStudyEstimate), which dims the
    // current mosaic while in flight rather than wiping it.
    const samplingFields = ['SAMPLE_FRAME',
        'MIN_ACTIVITY_COUNT_PER_GROUP', 'MAX_ACTIVITY_COUNT_PER_GROUP',
        'MIN_GROUP_COUNT_PER_COLLECTION', 'MAX_GROUP_COUNT_PER_COLLECTION'];
    samplingFields.forEach(field => {
        const el = row.querySelector(`[data-field="${field}"]`);
        if (el) el.addEventListener('change', () => _scheduleStudyEstimate(row));
    });

    // Date range: typed dates, day steppers, "Full range", and (wired on first
    // chart render) the chart's per-endpoint drag handles. The window key marks
    // the saved window as belonging to the saved collection set, so the chart
    // fetches below leave it alone until the user changes collections.
    row.dataset.windowKey = _collectionsKey(study.SELECTED_COLLECTIONS);
    _wireDateRangeControls(row);

    // Seed the chart from the cached snapshot saved on the study so it
    // renders instantly on modal open. The backend only persists this cache
    // when the hash matches the study's saved SELECTED_COLLECTIONS, so we
    // can trust it here. The async fetch below refreshes it regardless.
    const chartState = _getChartState(row);
    const cache = study.cached_daily_activities;
    if (cache && Array.isArray(cache.total_per_day) && cache.total_per_day.length) {
        chartState.totalPerDay = cache.total_per_day;
        if (cache.potentials && cache.potentials.collections != null) {
            _updateCollectionsHeader({ potential: cache.potentials.collections });
        }
        _renderDailyChart(row);
    }

    // Kick off initial chart fetch for the selected collections. This prewarms the
    // preview frame, snaps the date window, and (in its callback) triggers the initial
    // mosaic estimate.
    _fetchDailyChart(row);
}


function collectFormData(row) {
    const data = {};

    // 1. Standard Inputs
    const inputs = row.querySelectorAll('[data-field]');
    inputs.forEach(input => {
        const field = input.dataset.field;
        let value = input.value;

        // stats is a server-computed object; never send it from the client.
        if (field === 'stats') return;

        // Parse Types
        if (field === 'SELECTED_COLLECTIONS') {
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
            // Preserve a blank number field as '' (the backend reads it as no minimum
            // for a min threshold, or no cap for a max threshold) — never coerce to 0,
            // which on a max would cap every cell to zero rows.
            const raw = (value ?? '').trim();
            if (raw === '') {
                data[field] = '';
            } else {
                const n = parseInt(raw, 10);
                data[field] = isNaN(n) ? '' : n;
            }
        }
        else {
            data[field] = value;
        }
    });

    // 2. Checkbox Groups (USER_ACCESS) — the panel now lives in the modal
    // header, so search the modal, not just the cloned form.
    const modal = row.closest('#editStudyModal') || document.getElementById('editStudyModal') || document;
    const groups = modal.querySelectorAll('[data-field-group]');
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


function _showSaveStatusMsg(btn, msg) {
    // Show a temporary message alongside the button that triggered the action.
    const row = btn.closest('div');
    let span = row.querySelector('.save-status-msg');
    if (!span) {
        span = document.createElement('span');
        span.className = 'save-status-msg text-xs';
        span.style.cssText = 'color: var(--color-text-tertiary); margin-left: 4px;';
        row.appendChild(span);
    }
    span.textContent = msg;
    setTimeout(() => { span.textContent = ''; }, 4000);
}

function _validateStudyForm(formData, btn) {
    // Date format check
    const dateRegex = /^\d{4}-\d{2}-\d{2}$/;
    if (formData.START_DATE && !dateRegex.test(formData.START_DATE)) {
        _showSaveStatusMsg(btn, 'Start date must be yyyy-mm-dd');
        return false;
    }
    if (formData.END_DATE && !dateRegex.test(formData.END_DATE)) {
        _showSaveStatusMsg(btn, 'End date must be yyyy-mm-dd');
        return false;
    }
    if (formData.START_DATE && formData.END_DATE && formData.START_DATE > formData.END_DATE) {
        _showSaveStatusMsg(btn, 'Start date must be before end date');
        return false;
    }
    // Sampling limits sanity
    const minAct = formData.MIN_ACTIVITY_COUNT_PER_GROUP;
    const maxAct = formData.MAX_ACTIVITY_COUNT_PER_GROUP;
    if (minAct !== undefined && maxAct !== undefined && maxAct > 0 && minAct > maxAct) {
        _showSaveStatusMsg(btn, 'Min activity count cannot exceed max');
        return false;
    }
    const minGrp = formData.MIN_GROUP_COUNT_PER_COLLECTION;
    const maxGrp = formData.MAX_GROUP_COUNT_PER_COLLECTION;
    if (minGrp !== undefined && maxGrp !== undefined && maxGrp > 0 && minGrp > maxGrp) {
        _showSaveStatusMsg(btn, 'Min group count cannot exceed max');
        return false;
    }
    // Collections
    if (!formData.SELECTED_COLLECTIONS || formData.SELECTED_COLLECTIONS.length === 0) {
        _showSaveStatusMsg(btn, 'Select at least one collection');
        return false;
    }
    return true;
}

async function saveStudy(btn, event) {
    if (event) event.preventDefault();
    const formContainer = btn.closest('.study-edit-form');
    const isNew = formContainer.dataset.isNew === 'true';

    let studyName = formContainer.dataset.studyName;
    if (isNew) {
        const nameInput = document.getElementById('newStudyNameInput');
        const name = nameInput ? nameInput.value.trim() : '';
        if (!name) {
            _showSaveStatusMsg(btn, 'Enter a study name');
            nameInput?.focus();
            return;
        }
        if (allStudies.find(s => s.STUDY_NAME === name)) {
            _showSaveStatusMsg(btn, 'Study name already exists');
            nameInput?.focus();
            return;
        }
        studyName = name;
        formContainer.dataset.studyName = name;
    }

    try {
        const formData = collectFormData(formContainer);
        if (!_validateStudyForm(formData, btn)) return;

        const proceed = await _checkLargeStudy(formContainer);
        if (!proceed) return;

        const saveSettings = collectSaveSettings(formContainer);
        Object.assign(formData, saveSettings);
        formData.STUDY_NAME = studyName;

        savingStudies.add(studyName);
        btn.className = 'btn-running';
        btn.textContent = "Saving...";
        btn.disabled = true;
        renderStudiesTable();

        fetch('/api/manage/studies/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify(formData)
        })
            .then(res => res.json())
            .then(data => {
                savingStudies.delete(studyName);

                if (data.status === 'success') {
                    const index = allStudies.findIndex(s => s.STUDY_NAME === studyName);
                    if (index !== -1) {
                        allStudies[index] = data.study;
                    } else {
                        allStudies.push(data.study);
                    }

                    // Refresh study dropdowns across all tabs (the new study may
                    // not appear yet — it needs the background refresh to finish
                    // producing its recoded parquet — but rename/edit changes show
                    // up here and we'll refresh again when the refresh completes).
                    refreshStudyDropdowns();

                    if (data.refresh_status === 'dispatched') {
                        // Close modal and track progress in the table
                        closeStudyModal();
                        _pollStudyRefresh(studyName);
                    } else {
                        // Local/sync save — show success briefly then close
                        btn.className = 'btn-save';
                        btn.textContent = "Saved!";
                        btn.style.backgroundColor = 'var(--color-success)';
                        btn.disabled = false;
                        renderStudiesTable();
                        setTimeout(() => {
                            closeStudyModal();
                            btn.textContent = "Save/Refresh Study";
                            btn.style.backgroundColor = "";
                        }, 1500);
                    }
                } else if (data.status === 'no_change') {
                    _showSaveStatusMsg(btn, 'No changes to save');
                    btn.className = 'btn-save';
                    btn.textContent = "Save/Refresh Study";
                    btn.disabled = false;
                    renderStudiesTable();
                } else {
                    showAppAlert("Error saving: " + (data.error || "Unknown error"));
                    btn.className = 'btn-save';
                    btn.textContent = "Save/Refresh Study";
                    btn.disabled = false;
                    renderStudiesTable();
                }
            })
            .catch(err => {
                console.error(err);
                showAppAlert("Save failed.");
                savingStudies.delete(studyName);
                btn.className = 'btn-save';
                btn.textContent = "Save/Refresh Study";
                btn.disabled = false;
                renderStudiesTable();
            });

    } catch (e) {
        // Validation failed
    }
}


function _pollStudyRefresh(studyName) {
    refreshingStudies.set(studyName, { message: 'Starting...', percent: 0 });
    renderStudiesTable();

    const interval = setInterval(() => {
        fetch(`/api/status/study_refresh/${encodeURIComponent(studyName)}`)
            .then(res => res.json())
            .then(proc => {
                if (!proc || proc.state === 'unknown') return;

                if (proc.state === 'running') {
                    const progress = proc.progress || {};
                    refreshingStudies.set(studyName, {
                        message: progress.message || 'Refreshing...',
                        percent: progress.percent !== undefined ? progress.percent : 0,
                    });
                    renderStudiesTable();
                } else {
                    // Task finished
                    clearInterval(interval);
                    refreshingStudies.delete(studyName);

                    // Reload study data to get updated stats, then refresh
                    // the study dropdowns across all tabs — the new study's
                    // recoded parquet now exists, so /api/studies/defined will
                    // finally include it.
                    fetch('/api/manage/studies')
                        .then(r => r.json())
                        .then(studiesData => {
                            if (Array.isArray(studiesData)) {
                                allStudies = studiesData;
                            } else if (studiesData.studies) {
                                allStudies = studiesData.studies;
                            }
                            renderStudiesTable();
                            refreshStudyDropdowns();
                        })
                        .catch(() => renderStudiesTable());
                }
            })
            .catch(() => {
                clearInterval(interval);
                refreshingStudies.delete(studyName);
                renderStudiesTable();
            });
    }, 3000);
}


// Live preview: auto-update the mosaic/issues/overlay when sampling or the date
// window changes. Replaces the old "Check study design" button. Debounced so a burst
// of changes coalesces into one call, and sequenced so an out-of-order response from a
// superseded request can't overwrite the latest numbers.
const _studyEstimateDebounce = new WeakMap();
const _studyEstimateSeq = new WeakMap();

function _setStudyVizLoading(row, on) {
    const viz = row.querySelector('.study-set-viz');
    if (!viz) return;
    // Dim the existing mosaic while recomputing rather than wiping it.
    viz.style.transition = 'opacity 0.15s ease';
    viz.style.opacity = on ? '0.45' : '1';
    let badge = row.querySelector('.study-viz-updating');
    if (on) {
        if (!badge) {
            badge = document.createElement('div');
            badge.className = 'study-viz-updating text-xxs';
            badge.style.cssText = 'color: var(--color-text-tertiary); margin-top: 2px;';
            badge.textContent = 'updating…';
            viz.insertAdjacentElement('afterend', badge);
        }
        badge.style.display = '';
    } else if (badge) {
        badge.style.display = 'none';
    }
}

// Replace the mosaic with a spinner + message. Used when there's no current mosaic to
// dim — e.g. after a collection change, where the cache may be (re)built from scratch.
function _showStudyVizLoading(row, message) {
    const viz = row.querySelector('.study-set-viz');
    if (!viz) return;
    viz.style.opacity = '1';
    viz.dataset.state = 'loading';
    viz.innerHTML =
        '<div class="study-set-viz-empty text-xs" style="display:flex; align-items:center; gap:8px; color: var(--color-text-tertiary); padding: 12px; border: 1px dashed var(--color-border); border-radius: 4px; background: var(--color-bg-surface);">'
        + '<span class="global-tasks-spinner"></span><span>' + message + '</span></div>';
    const badge = row.querySelector('.study-viz-updating');
    if (badge) badge.style.display = 'none';
}

function _scheduleStudyEstimate(row, delay = 400) {
    const prev = _studyEstimateDebounce.get(row);
    if (prev) clearTimeout(prev);
    _studyEstimateDebounce.set(row, setTimeout(() => _runStudyEstimate(row), delay));
}

function _runStudyEstimate(row) {
    const selected = _getSelectedCollections(row);
    if (!selected.length) {
        _resetStudySetViz(row, 'empty');
        _clearStudyIssues(row);
        _invalidateDailyChartOverlay(row);
        return;
    }

    let formData;
    try { formData = collectFormData(row); }
    catch (e) { console.error('estimate: collectFormData failed', e); return; }
    // Name is only needed for the request contract; previews never persist, so a
    // placeholder is fine for an unsaved study.
    formData.STUDY_NAME = row.dataset.studyName
        || (document.getElementById('newStudyNameInput')?.value || '').trim()
        || '__preview__';
    formData.PREVIEW_ONLY = true;

    const seq = (_studyEstimateSeq.get(row) || 0) + 1;
    _studyEstimateSeq.set(row, seq);
    // If a mosaic is already shown (sampling/date tweak), dim it in place. Otherwise
    // (collection change → cache rebuild, or first load) show a spinner + message.
    const viz = row.querySelector('.study-set-viz');
    const hasMosaic = viz && (viz.dataset.state === 'ready' || viz.dataset.state === 'seeded');
    if (hasMosaic) _setStudyVizLoading(row, true);
    else if (!viz || viz.dataset.state !== 'loading') _showStudyVizLoading(row, 'Loading…');

    fetch('/api/manage/studies/calculate_stats', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify(formData)
    })
        .then(res => res.json())
        .then(data => {
            if (_studyEstimateSeq.get(row) !== seq) return;   // superseded by a newer request
            if (data.status === 'success') {
                const stats = data.stats || {};
                const potentials = data.potentials || {};
                _updateCollectionsHeader({ actual: stats.unique_collections, potential: potentials.collections });
                _renderStudySetViz(row, { universe: data.universe, included: stats, frame: formData.SAMPLE_FRAME });
                _setDailyChartOverlay(row, data.included_per_day || []);
                _renderStudyIssues(row, data.issues || []);
                const cached = (typeof allStudies !== 'undefined') ? allStudies.find(s => s.STUDY_NAME === row.dataset.studyName) : null;
                if (cached) cached.stats = stats;   // keep client cache fresh so reopen seeds instantly
            } else if (data.error) {
                console.error('estimate error:', data.error);
            }
        })
        .catch(err => { console.error('estimate request failed', err); })
        .finally(() => {
            if (_studyEstimateSeq.get(row) === seq) _setStudyVizLoading(row, false);
        });
}


async function deleteStudy(btn, event) {
    if (event) event.preventDefault();
    const formContainer = btn.closest('.study-edit-form');
    const studyName = formContainer.dataset.studyName;

    if (!studyName || formContainer.dataset.isNew === 'true') {
        closeStudyModal();
        return;
    }

    if (!(await showAppConfirm(`Are you sure you want to delete study '${studyName}'? This cannot be undone.`,
        { title: 'Delete study', okLabel: 'Delete', danger: true }))) return;

    fetch('/api/manage/studies/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ STUDY_NAME: studyName })
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                closeStudyModal();
                showAppAlert("Study deleted.");
                loadStudies();
            } else {
                showAppAlert("Error: " + data.error);
            }
        })
        .catch(err => showAppAlert("Delete failed: " + err));
}

// --- Duplicate / Rename (grouped with Delete in the modal footer) ---

// Suggest a unique name for a duplicated study.
function _suggestCopyName(sourceName) {
    let candidate = `${sourceName}_copy`;
    let n = 2;
    while (allStudies.some(s => s.STUDY_NAME === candidate)) {
        candidate = `${sourceName}_copy${n}`;
        n += 1;
    }
    return candidate;
}

// Duplicate = reopen the New Study modal pre-filled with this study's saved
// definition. Saving goes through the normal new-study flow (name-uniqueness
// check, validation, background stats/PCA refresh), so the user can adjust
// the copy before committing the expensive rebuild.
function duplicateStudy(btn, event) {
    if (event) event.preventDefault();
    const formContainer = btn.closest('.study-edit-form');
    const sourceName = formContainer.dataset.studyName;
    const source = allStudies.find(s => s.STUDY_NAME === sourceName);
    if (!source) return;

    // Copy the definition only — stats, timestamps and the cached daily chart
    // belong to the source study and are recomputed when the copy is saved.
    const copy = JSON.parse(JSON.stringify(source));
    delete copy.stats;
    delete copy.last_updated;
    delete copy.cached_daily_activities;
    copy.STUDY_NAME = '';

    closeStudyModal();
    loadSystemRoles(() => {
        _showStudyModal(copy, true);
        const title = document.getElementById('editStudyModalTitle');
        if (title) title.textContent = `New study — copy of ${sourceName}`;
        const nameInput = document.getElementById('newStudyNameInput');
        if (nameInput) {
            nameInput.value = _suggestCopyName(sourceName);
            nameInput.select();
        }
    });
}

// Small input prompt on top of the study modal. Resolves to the entered name,
// or null on cancel/Escape/backdrop.
function _promptStudyRename(oldName) {
    return new Promise(resolve => {
        const overlay = document.getElementById('study-rename-overlay');
        if (!overlay) { resolve(window.prompt(`New name for '${oldName}':`, oldName)); return; }

        const input = document.getElementById('study-rename-input');
        const hint = document.getElementById('study-rename-hint');
        hint.textContent = `Enter a new name for '${oldName}'. Its dataset, stats and access are kept — no rebuild needed.`;
        input.value = oldName;

        let done = false;
        const close = (val) => {
            if (done) return;
            done = true;
            overlay.classList.remove('visible');
            resolve(val);
        };
        document.getElementById('study-rename-ok-btn').onclick = () => close(input.value.trim());
        document.getElementById('study-rename-cancel-btn').onclick = () => close(null);
        overlay.onclick = (e) => { if (e.target === overlay) close(null); };
        input.onkeydown = (e) => {
            if (e.key === 'Enter') close(input.value.trim());
            else if (e.key === 'Escape') close(null);
            e.stopPropagation();
        };

        overlay.classList.add('visible');
        setTimeout(() => { input.focus(); input.select(); }, 50);
    });
}

async function renameStudy(btn, event) {
    if (event) event.preventDefault();
    const formContainer = btn.closest('.study-edit-form');
    const oldName = formContainer.dataset.studyName;
    if (!oldName || formContainer.dataset.isNew === 'true') return;

    const newName = await _promptStudyRename(oldName);
    if (!newName || newName === oldName) return;
    if (allStudies.some(s => s.STUDY_NAME === newName)) {
        showAppAlert(`A study named '${newName}' already exists.`);
        return;
    }

    fetch('/api/manage/studies/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ OLD_NAME: oldName, NEW_NAME: newName })
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                // Update in place — the modal stays open under the new name.
                formContainer.dataset.studyName = newName;
                const title = document.getElementById('editStudyModalTitle');
                if (title) title.textContent = newName;
                const cached = allStudies.find(s => s.STUDY_NAME === oldName);
                if (cached) cached.STUDY_NAME = newName;
                renderStudiesTable();
                refreshStudyDropdowns();
            } else {
                showAppAlert('Rename failed: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(err => showAppAlert('Rename failed: ' + err));
}

function populateEnrichmentStudySelect(studies) {
    // Both the Scrape page's and the Annotation page's study selects (either
    // may be absent depending on the user's permissions).
    for (const selectId of ['enrichment-study-select', 'annotation-study-select']) {
        const select = document.getElementById(selectId);
        if (!select) continue;

        // Preserve current selection so a refresh from another action doesn't wipe it
        const currentValue = select.value;

        // Keep the first default option
        select.innerHTML = '<option value="">-- Select Study --</option>';

        studies.forEach(study => {
            const opt = document.createElement('option');
            opt.value = study.STUDY_NAME;
            opt.textContent = study.STUDY_NAME;
            select.appendChild(opt);
        });

        if (currentValue && studies.some(s => s.STUDY_NAME === currentValue)) {
            select.value = currentValue;
        }
    }
}

// Refresh every study dropdown across the app without triggering any tab
// navigation. Used after a study is saved or finishes its background refresh.
function refreshStudyDropdowns() {
    if (typeof loadDefinedStudies === 'function') loadDefinedStudies();
    if (window.studyState && typeof window.studyState.reload === 'function') {
        window.studyState.reload();
    }
    if (Array.isArray(allStudies)) populateEnrichmentStudySelect(allStudies);
}

// --- Study daily-activities chart ---

const _studyChartState = new WeakMap();   // formContainer -> {totalPerDay, includedPerDay}
const _studyChartDebounce = new WeakMap();

function _getChartState(row) {
    let s = _studyChartState.get(row);
    if (!s) { s = { totalPerDay: [], includedPerDay: null }; _studyChartState.set(row, s); }
    return s;
}

function _invalidateDailyChartOverlay(row) {
    const s = _getChartState(row);
    if (s.includedPerDay) {
        s.includedPerDay = null;
        _renderDailyChart(row);
    }
}

function _setDailyChartOverlay(row, includedPerDay) {
    const s = _getChartState(row);
    s.includedPerDay = Array.isArray(includedPerDay) ? includedPerDay : [];
    _renderDailyChart(row);
}

function _getSelectedCollections(row) {
    const hidden = row.querySelector('input[data-field="SELECTED_COLLECTIONS"]');
    try { return JSON.parse((hidden?.value || '[]').replace(/'/g, '"')); }
    catch (e) { return []; }
}

// Warm the server-side study-estimate frame for the current collection set so the
// first auto-estimate is fast. Fire-and-forget: the modal calls this on open and on
// every collection-selection change (via _fetchDailyChart), so the (possibly slow)
// build / disk-load happens during the user's think-time rather than on the first estimate.
function _prewarmStudyCheck(selected, studyName) {
    if (!Array.isArray(selected) || selected.length === 0) return;
    fetch('/api/manage/studies/prewarm_check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ SELECTED_COLLECTIONS: selected, STUDY_NAME: studyName })
    }).catch(() => { /* best-effort warming */ });
}

function _fetchDailyChart(row) {
    const selected = _getSelectedCollections(row);
    const s = _getChartState(row);
    s.includedPerDay = null;

    if (!selected.length) {
        s.totalPerDay = [];
        s.loading = false;
        _renderDailyChart(row);
        return;
    }

    // Mark loading so the empty/loading placeholder can render correctly
    // until the response comes back.
    s.loading = true;
    _renderDailyChart(row);

    const studyName = row.dataset.studyName || null;
    _prewarmStudyCheck(selected, studyName);
    fetch('/api/manage/studies/daily_activities', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ SELECTED_COLLECTIONS: selected, STUDY_NAME: studyName })
    })
        .then(r => r.json())
        .then(data => {
            s.loading = false;
            if (data.status !== 'success') { _renderDailyChart(row); return; }
            const isInitial = row.dataset.initialFetch === '1';
            s.totalPerDay = data.total_per_day || [];
            _syncDateRangeToCollections(row, s.totalPerDay);
            _renderDailyChart(row);
            if (data.potentials && data.potentials.collections != null) {
                _updateCollectionsHeader({ potential: data.potentials.collections });
            }
            // On the first fetch after opening, the date snap above may have reset
            // the seeded mosaic — re-apply the last persisted check so it persists.
            if (isInitial) {
                row.dataset.initialFetch = '';
                const sd = (typeof allStudies !== 'undefined') ? allStudies.find(x => x.STUDY_NAME === studyName) : null;
                const st = sd && sd.stats;
                if (st && st.universe && Number(st.universe.activities) > 0) {
                    _renderStudySetViz(row, { universe: st.universe, included: st, frame: sd.SAMPLE_FRAME, seeded: true });
                    if (st.unique_collections != null) _updateCollectionsHeader({ actual: st.unique_collections });
                }
            }
            // Re-estimate now that the date window has snapped to the collections.
            // This is the canonical trigger on collection change — it fires even when
            // the snapped dates are unchanged (so the date 'input' event wouldn't).
            _scheduleStudyEstimate(row);
        })
        .catch(err => {
            s.loading = false;
            _renderDailyChart(row);
            console.error('daily_activities fetch failed', err);
        });
}

// Identity of a collection set, order-independent — the window is snapped when
// this changes and left alone when it doesn't.
function _collectionsKey(list) {
    return JSON.stringify((Array.isArray(list) ? list : []).map(String).sort());
}

// Snap START_DATE/END_DATE to cover the full span of the currently selected
// collections, so a changed collection set starts with everything included.
//
// An existing window is kept while the collection set is unchanged — a study
// opens on its saved window (and keeps it verbatim, so a window that reaches
// past the last day with data survives), instead of being silently widened back
// to the full range and saved that way. Every daily-activities response lands
// here, including the debounced refetch the collection selector kicks off on
// open, so the decision is keyed on the collections rather than on "first fetch".
function _syncDateRangeToCollections(row, totalPerDay) {
    const startInput = row.querySelector('[data-field="START_DATE"]');
    const endInput = row.querySelector('[data-field="END_DATE"]');
    if (!startInput || !endInput) return;

    const key = _collectionsKey(_getSelectedCollections(row));
    const sameCollections = row.dataset.windowKey === key;
    row.dataset.windowKey = key;

    // Only fire input (which invalidates the seeded mosaic + actuals) when the
    // snapped value actually changes, so re-opening a study whose window already
    // spans the full range doesn't spuriously wipe the seeded viz.
    const setIfChanged = (inp, val) => {
        if (inp.value === val) return;
        inp.value = val;
        inp.dispatchEvent(new Event('input', { bubbles: true }));
    };
    if (!Array.isArray(totalPerDay) || !totalPerDay.length) {
        setIfChanged(startInput, '');
        setIfChanged(endInput, '');
        return;
    }
    if (sameCollections && (startInput.value || '').trim() && (endInput.value || '').trim()) return;

    const dates = totalPerDay.map(d => d.date).filter(Boolean).sort();
    setIfChanged(startInput, dates[0]);
    setIfChanged(endInput, dates[dates.length - 1]);
}

function _debouncedRefetchDailyChart(row) {
    const prev = _studyChartDebounce.get(row);
    if (prev) clearTimeout(prev);
    _studyChartDebounce.set(row, setTimeout(() => _fetchDailyChart(row), 400));
}

function _toIsoDate(v) {
    // Plotly date axes return zone-less strings like "2026-04-20 00:00:00.0000";
    // fypWallIsoDate() reads the calendar day straight out of those rather than
    // round-tripping through UTC, which would shift the date back a day here.
    return fypWallIsoDate(v);
}


// --- Study date window: endpoints move independently -----------------------
//
// The window lives in the two date inputs (START_DATE / END_DATE). Four things
// write to them — typing, the -/+ day steppers, the chart's edge handles, and a
// drag across the chart — and they all funnel through _setDateWindow so the
// chart shading, the summary line and the estimate stay in sync.

function _dateInputs(row) {
    return {
        start: row.querySelector('[data-field="START_DATE"]'),
        end: row.querySelector('[data-field="END_DATE"]'),
    };
}

// First/last day covered by the currently selected collections, from the chart data.
function _chartSpan(row) {
    const dates = (_getChartState(row).totalPerDay || []).map(d => d.date).filter(Boolean).sort();
    if (!dates.length) return null;
    return { lo: dates[0], hi: dates[dates.length - 1] };
}

function _isoDayMs(iso) {
    if (!iso || iso.length < 10) return NaN;
    return Date.UTC(
        parseInt(iso.slice(0, 4), 10),
        parseInt(iso.slice(5, 7), 10) - 1,
        parseInt(iso.slice(8, 10), 10),
    );
}

// Calendar arithmetic on a yyyy-mm-dd string. Both the input and the output are
// wall-clock days, so the shift runs against UTC midnight and reads the parts
// back out in UTC — no instant is ever formatted in a local timezone.
function _shiftIsoDate(iso, days) {
    const ms = _isoDayMs(iso);
    if (isNaN(ms)) return iso;
    const d = new Date(ms + days * 86400000);
    const pad = n => String(n).padStart(2, '0');
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}

function _clampIso(iso, lo, hi) {
    if (!iso) return iso;
    if (lo && iso < lo) return lo;
    if (hi && iso > hi) return hi;
    return iso;
}

// Activities inside the current window, summed from the chart's per-day counts.
// Returns null when there is no chart data to sum.
function _windowActivityCount(row) {
    const total = _getChartState(row).totalPerDay || [];
    if (!total.length) return null;
    const { start, end } = _dateInputs(row);
    const s = (start?.value || '').trim();
    const e = (end?.value || '').trim();
    let n = 0;
    total.forEach(d => {
        if (s && d.date < s) return;
        if (e && d.date > e) return;
        n += (d.count || 0);
    });
    return n;
}

// Move one or both endpoints. An endpoint only ever moves the other one when the
// two would cross, so each end can be adjusted on its own.
//
// opts.fire  — dispatch the input event (redraw + re-estimate). A live drag passes
//              false per frame and fires once on release.
// opts.clamp — hold the value inside the collections' span. On for pointer moves
//              (a handle can't leave the chart anyway); off for typed dates and the
//              day steppers, so a window may deliberately reach past the last day
//              with data.
function _setDateWindow(row, { start, end }, opts = {}) {
    const { fire = true, clamp = false } = opts;
    const inputs = _dateInputs(row);
    if (!inputs.start || !inputs.end) return false;
    const span = clamp ? _chartSpan(row) : null;
    const lo = span ? span.lo : null;
    const hi = span ? span.hi : null;

    let nextStart = start != null ? _clampIso(start, lo, hi) : (inputs.start.value || '').trim();
    let nextEnd = end != null ? _clampIso(end, lo, hi) : (inputs.end.value || '').trim();
    if (nextStart && nextEnd && nextStart > nextEnd) {
        // The moved end wins; the other one is pushed to meet it.
        if (start != null && end == null) nextEnd = nextStart;
        else if (end != null && start == null) nextStart = nextEnd;
        else nextEnd = nextStart;
    }

    const changed = [];
    if (inputs.start.value !== nextStart) { inputs.start.value = nextStart; changed.push(inputs.start); }
    if (inputs.end.value !== nextEnd) { inputs.end.value = nextEnd; changed.push(inputs.end); }
    if (!changed.length) return false;

    if (fire) changed.forEach(el => el.dispatchEvent(new Event('input', { bubbles: true })));
    else _renderDailyChart(row);
    return true;
}

// Keep the summary line and the handle positions in step with the inputs. Called
// at the end of every chart render, so every path that touches the window
// refreshes it.
function _updateDateRangeUI(row) {
    const span = _chartSpan(row);
    const { start, end } = _dateInputs(row);

    const summary = row.querySelector('.study-date-summary');
    if (summary) {
        const s = (start?.value || '').trim();
        const e = (end?.value || '').trim();
        if (!span || !s || !e) {
            summary.textContent = '';
        } else {
            const days = Math.round((_isoDayMs(e) - _isoDayMs(s)) / 86400000) + 1;
            const n = _windowActivityCount(row);
            const parts = [`${days.toLocaleString()} day${days === 1 ? '' : 's'}`];
            if (n != null) parts.push(`${n.toLocaleString()} activities`);
            if (s <= span.lo && e >= span.hi) parts.push('full range');
            summary.textContent = parts.join(' · ');
        }
    }

    _positionRangeHandles(row);
}

// Wire the typed inputs, the day steppers and the "Full range" button. The chart's
// own drag handles are wired lazily in _ensureRangeHandles.
function _wireDateRangeControls(row) {
    const { start, end } = _dateInputs(row);
    [start, end].forEach(el => {
        if (!el) return;
        // Live redraw while typing/picking; the estimate is debounced downstream.
        el.addEventListener('input', () => { _renderDailyChart(row); _scheduleStudyEstimate(row); });
        // On commit, repair a half-typed or crossed-over value rather than saving
        // an unbounded window by accident.
        el.addEventListener('change', () => {
            const span = _chartSpan(row);
            if (!span) return;
            const isStart = el === start;
            const val = (el.value || '').trim();
            if (!val) { _setDateWindow(row, isStart ? { start: span.lo } : { end: span.hi }); return; }
            _setDateWindow(row, isStart ? { start: val } : { end: val });
        });
    });

    row.querySelectorAll('.study-date-step').forEach(btn => {
        btn.addEventListener('click', () => {
            const edge = btn.dataset.edge;
            const step = parseInt(btn.dataset.step, 10) || 0;
            const el = edge === 'start' ? start : end;
            const span = _chartSpan(row);
            const current = (el?.value || '').trim() || (span ? (edge === 'start' ? span.lo : span.hi) : '');
            if (!current) return;
            const moved = _shiftIsoDate(current, step);
            _setDateWindow(row, edge === 'start' ? { start: moved } : { end: moved });
        });
    });

    const reset = row.querySelector('.study-date-reset');
    if (reset) {
        reset.addEventListener('click', () => {
            const span = _chartSpan(row);
            if (span) _setDateWindow(row, { start: span.lo, end: span.hi });
        });
    }
}


// --- Chart edge handles ----------------------------------------------------

// Two draggable rules over the chart, one per endpoint. They live in the chart
// wrap (not inside the Plotly div, which Plotly owns) and are repositioned from
// the axis on every render.
function _ensureRangeHandles(row) {
    const wrap = row.querySelector('.study-daily-chart-wrap');
    if (!wrap) return null;
    if (wrap.querySelectorAll('.study-range-handle').length === 2) return wrap;

    ['start', 'end'].forEach(edge => {
        const handle = document.createElement('div');
        handle.className = 'study-range-handle';
        handle.dataset.edge = edge;
        handle.style.display = 'none';
        handle.title = edge === 'start'
            ? 'Drag to move the start of the window'
            : 'Drag to move the end of the window';
        handle.innerHTML = '<span class="study-range-handle__rule"></span><span class="study-range-handle__grip"></span>';
        handle.addEventListener('mousedown', ev => _beginRangeHandleDrag(row, edge, ev));
        wrap.appendChild(handle);
    });
    return wrap;
}

function _positionRangeHandles(row) {
    const wrap = row.querySelector('.study-daily-chart-wrap');
    if (!wrap) return;
    const handles = wrap.querySelectorAll('.study-range-handle');
    if (!handles.length) return;

    const chartDiv = row.querySelector('.study-daily-chart');
    const fullLayout = chartDiv && chartDiv._fullLayout;
    const span = _chartSpan(row);
    const hidden = row.dataset.readOnly === '1' || !span || !fullLayout || !fullLayout.xaxis
        || !chartDiv._plotlyInited || chartDiv.style.display === 'none';
    if (hidden) {
        handles.forEach(h => { h.style.display = 'none'; });
        return;
    }

    const xa = fullLayout.xaxis;
    const ya = fullLayout.yaxis;
    const { start, end } = _dateInputs(row);
    const values = {
        start: (start?.value || '').trim() || span.lo,
        end: (end?.value || '').trim() || span.hi,
    };
    const top = chartDiv.offsetTop + (ya._offset || 0);
    const height = ya._length || chartDiv.clientHeight;

    handles.forEach(h => {
        // Bars are anchored at noon, so the handle sits on the centre of the day
        // it selects — the same day the drag maths reads back out.
        const px = xa._offset + xa.d2p(_isoDayMs(values[h.dataset.edge]) + 43200000);
        if (!isFinite(px)) { h.style.display = 'none'; return; }
        const clamped = Math.max(xa._offset, Math.min(xa._offset + xa._length, px));
        h.style.display = '';
        h.style.top = `${top}px`;
        h.style.height = `${height}px`;
        h.style.left = `${clamped}px`;
    });
}

// Pixel (relative to the chart div) -> the calendar day under the cursor, clamped
// to the collections' span.
function _isoAtChartPixel(row, xa, xPx) {
    const inAxis = Math.max(xa._offset, Math.min(xa._offset + xa._length, xPx)) - xa._offset;
    const iso = _toIsoDate(xa.p2d(inAxis));
    if (!iso) return null;
    const span = _chartSpan(row);
    return span ? _clampIso(iso, span.lo, span.hi) : iso;
}

function _beginRangeHandleDrag(row, edge, ev) {
    if (ev.button !== 0) return;
    const chartDiv = row.querySelector('.study-daily-chart');
    const fullLayout = chartDiv && chartDiv._fullLayout;
    if (!fullLayout || !fullLayout.xaxis) return;
    const { start, end } = _dateInputs(row);
    const moving = edge === 'start' ? start : end;
    if (!moving || moving.disabled) return;

    // Keep the mousedown away from Plotly's drag layer, which would otherwise
    // start a range selection under the handle.
    ev.preventDefault();
    ev.stopPropagation();

    const xa = fullLayout.xaxis;
    const rect = chartDiv.getBoundingClientRect();
    const wrap = row.querySelector('.study-daily-chart-wrap');
    wrap?.classList.add('dragging-range');
    document.body.classList.add('study-range-dragging');

    let pending = null;
    let frame = null;
    let moved = false;

    const apply = () => {
        frame = null;
        if (pending == null) return;
        const iso = pending;
        pending = null;
        // fire=false: redraw only. One input event goes out on release so the
        // estimate runs once for the whole drag rather than once per day crossed.
        const patch = edge === 'start' ? { start: iso } : { end: iso };
        if (_setDateWindow(row, patch, { fire: false, clamp: true })) moved = true;
    };

    const onMove = (e) => {
        const iso = _isoAtChartPixel(row, xa, e.clientX - rect.left);
        if (!iso) return;
        pending = iso;
        if (frame == null) frame = requestAnimationFrame(apply);
    };

    const onUp = () => {
        window.removeEventListener('mousemove', onMove, true);
        window.removeEventListener('mouseup', onUp, true);
        if (frame != null) { cancelAnimationFrame(frame); apply(); }
        wrap?.classList.remove('dragging-range');
        document.body.classList.remove('study-range-dragging');
        if (moved) moving.dispatchEvent(new Event('input', { bubbles: true }));
    };

    window.addEventListener('mousemove', onMove, true);
    window.addEventListener('mouseup', onUp, true);
}

function _renderDailyChart(row) {
    const chartDiv = row.querySelector('.study-daily-chart');
    const emptyDiv = row.querySelector('.study-daily-chart-empty');
    const hintDiv = row.querySelector('.study-daily-chart-hint');
    if (!chartDiv) return;

    const s = _getChartState(row);
    const readOnly = row.dataset.readOnly === '1';
    const total = s.totalPerDay || [];
    const included = s.includedPerDay;
    const selected = _getSelectedCollections(row);

    if (!total.length) {
        chartDiv.style.display = 'none';
        if (hintDiv) hintDiv.style.display = 'none';
        if (emptyDiv) {
            emptyDiv.style.display = '';
            if (s.loading && selected.length) {
                emptyDiv.textContent = 'Loading daily activities\u2026';
            } else if (readOnly) {
                // Read-only never fetches; the chart only appears when the study
                // carries a cached snapshot from its last save.
                emptyDiv.textContent = 'No activity chart has been computed for this study yet.';
            } else {
                emptyDiv.textContent = 'Select one or more collections to see activities per day.';
            }
        }
        if (chartDiv._plotlyInited && window.Plotly) {
            window.Plotly.purge(chartDiv);
            chartDiv._plotlyInited = false;
        }
        _updateDateRangeUI(row);
        return;
    }

    if (emptyDiv) emptyDiv.style.display = 'none';
    chartDiv.style.display = '';
    // The hint explains drag-to-set-window, which read-only does not offer.
    if (hintDiv) hintDiv.style.display = readOnly ? 'none' : '';

    const startInput = row.querySelector('[data-field="START_DATE"]');
    const endInput = row.querySelector('[data-field="END_DATE"]');
    const startVal = (startInput?.value || '').trim();
    const endVal = (endInput?.value || '').trim();

    const xs = total.map(d => d.date);
    const ys = total.map(d => d.count);
    // Plotly places "2026-04-20" at UTC midnight, which straddles the boundary
    // between the 2026-04-19 and 2026-04-20 tick labels. A narrow drag on the
    // "left" half of the bar then yields start=end=2026-04-19 and excludes the
    // data. Anchor each bar at noon UTC of its date instead so the bar sits
    // cleanly inside a single day label.
    const xsPlot = xs.map(d => d + 'T12:00:00Z');

    const mutedColor = getCSSVar('--color-text-tertiary') || 'rgba(150,150,150,0.4)';
    const baseColor = getCSSVar('--color-text-secondary') || 'rgba(100,100,100,0.8)';
    // Match the "sampled into study" fill in the coverage mosaic.
    const accentColor = getCSSVar('--study-viz-included') || '#6A9B7E';

    const inRange = (d) => {
        if (startVal && d < startVal) return false;
        if (endVal && d > endVal) return false;
        return true;
    };

    const baseColors = xs.map(d => inRange(d) ? baseColor : mutedColor);
    const baseOpacities = xs.map(d => inRange(d) ? 0.9 : 0.35);

    const hasIncluded = Array.isArray(included) && included.length;
    const inclMap = hasIncluded ? new Map(included.map(d => [d.date, d.count])) : null;
    const inclY = hasIncluded ? xs.map(d => inclMap.get(d) || 0) : null;

    // With a single-day collection Plotly has no span to infer a default bar
    // width from, so the bar collapses to a hairline. Pin the width to 80% of
    // one day so the bar renders at a comparable size to multi-day charts.
    const singleBarWidth = xs.length === 1 ? 86400000 * 0.8 : undefined;

    const traces = [{
        type: 'bar',
        name: '',
        x: xsPlot,
        y: ys,
        width: singleBarWidth,
        customdata: hasIncluded ? inclY : undefined,
        marker: { color: baseColors, opacity: baseOpacities },
        hovertemplate: hasIncluded
            ? '%{customdata:,}/%{y:,} activities<extra></extra>'
            : '%{y:,} activities<extra></extra>',
    }];

    if (hasIncluded) {
        traces.push({
            type: 'bar',
            name: '',
            x: xsPlot,
            y: inclY,
            width: singleBarWidth,
            marker: { color: accentColor },
            hoverinfo: 'skip',
        });
    }

    // Date-range caption for the top-right — "Date range: yyyy-mm-dd – yyyy-mm-dd".
    const startInputVal = (startInput?.value || '').trim();
    const endInputVal = (endInput?.value || '').trim();
    const fmtIsoDate = (iso) => _toIsoDate(iso) || '';
    const rangeFirst = fmtIsoDate(startInputVal || xs[0]);
    const rangeLast = fmtIsoDate(endInputVal || xs[xs.length - 1]);
    const dateRangeCaption = `Selected date range: ${rangeFirst} \u2013 ${rangeLast}`;

    const layout = {
        barmode: 'overlay',
        margin: { l: 32, r: 8, t: 18, b: 32 },
        paper_bgcolor: getCSSVar('--chart-bg'),
        plot_bgcolor: getCSSVar('--chart-bg'),
        font: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text'), size: 10 },
        xaxis: {
            type: 'date',
            gridcolor: getCSSVar('--chart-grid'),
            tickfont: { size: 9 },
            tickformat: '%Y-%m-%d',
            hoverformat: '%Y-%m-%d',
            fixedrange: false,
        },
        yaxis: {
            gridcolor: getCSSVar('--chart-grid'),
            tickfont: { size: 9 },
            fixedrange: true,
            rangemode: 'tozero',
        },
        annotations: [{
            text: dateRangeCaption,
            xref: 'paper', yref: 'paper',
            x: 1, y: 1.0,
            xanchor: 'right', yanchor: 'bottom',
            showarrow: false,
            font: { size: 10, color: getCSSVar('--color-text-tertiary') },
        }],
        showlegend: false,
        // Read-only has no editable date window, so drag-to-select is off.
        dragmode: readOnly ? false : 'select',
        selectdirection: 'h',
        hovermode: 'x',
    };

    // Single-day collections have no natural span for Plotly to auto-range
    // against, so it picks an arbitrary sub-day view that places the bar near
    // an edge. Pad the range by half a day on each side of the noon anchor to
    // center the bar with exactly one date label visible.
    if (xs.length === 1) {
        const dayMs = 86400000;
        const noonMs = Date.UTC(
            parseInt(xs[0].slice(0, 4), 10),
            parseInt(xs[0].slice(5, 7), 10) - 1,
            parseInt(xs[0].slice(8, 10), 10),
            12, 0, 0,
        );
        layout.xaxis.range = [
            new Date(noonMs - dayMs / 2).toISOString(),
            new Date(noonMs + dayMs / 2).toISOString(),
        ];
    }

    // For narrow spans Plotly's auto-ticks fall on sub-day intervals and the
    // '%Y-%m-%d' tickformat then shows the same date repeatedly. Pin ticks to
    // the noon bar anchor on a per-day cadence. Skip for longer spans — daily
    // ticks across months/years stack into an unreadable band.
    if (xs.length > 0 && xs.length <= 14) {
        layout.xaxis.tick0 = xsPlot[0];
        layout.xaxis.dtick = 86400000;
    }

    // Turn off Plotly's own double-click reset so our handler runs instead.
    const config = { displayModeBar: false, responsive: true, doubleClick: false };

    if (!window.Plotly) return;
    window.Plotly.react(chartDiv, traces, layout, config);

    if (!chartDiv._plotlyInited && !readOnly) {
        chartDiv._plotlyInited = true;
        chartDiv.on('plotly_selected', (ev) => {
            if (!ev || !ev.range || !ev.range.x) return;
            const [minX, maxX] = ev.range.x;
            const s1 = _toIsoDate(minX);
            const e1 = _toIsoDate(maxX);
            if (!s1 || !e1) return;
            // A drag that overshoots the data would otherwise persist a window
            // wider than any activity; clamp it back to the span.
            _setDateWindow(row, { start: s1, end: e1 }, { clamp: true });
            window.Plotly.relayout(chartDiv, { selections: [] });
        });
        // Plotly's built-in plotly_doubleclick only fires in the plot interior
        // when dragmode isn't swallowing the event — with 'select' active the
        // selection overlay eats it everywhere except below the x-axis. Listen
        // to the native dblclick on the container (capture phase, so it runs
        // even if Plotly stops propagation) and reset the date range to the
        // full span of the current selection.
        chartDiv.addEventListener('dblclick', () => {
            const xs2 = (_getChartState(row).totalPerDay || []).map(d => d.date);
            if (!xs2.length) return;
            startInput.value = xs2[0];
            endInput.value = xs2[xs2.length - 1];
            startInput.dispatchEvent(new Event('input', { bubbles: true }));
            endInput.dispatchEvent(new Event('input', { bubbles: true }));
            if (window.Plotly) window.Plotly.relayout(chartDiv, { selections: [] });
        }, true);
        // Responsive resizes and autorange changes move the axis under the
        // handles; reposition them whenever Plotly finishes drawing.
        chartDiv.on('plotly_afterplot', () => _positionRangeHandles(row));
    }

    if (!readOnly) _ensureRangeHandles(row);
    _updateDateRangeUI(row);
}

// --- Access dropdown in modal header ---

function _updateAccessToggleLabel() {
    const countEl = document.getElementById('studyAccessCount');
    const panel = document.getElementById('studyAccessPanel');
    if (!countEl || !panel) return;
    // Count visible (non-admin) checked roles. Admin is always implicit.
    const checked = panel.querySelectorAll('div:not([style*="display: none"]) input[type="checkbox"]:checked');
    countEl.textContent = String(checked.length);
}

function _renderAccessDropdown(study) {
    const panel = document.getElementById('studyAccessPanel');
    if (!panel) return;
    const container = panel.querySelector('.dynamic-roles-container');
    if (!container) return;

    const currentList = study.USER_ACCESS || [];
    container.innerHTML = '';

    const rolesToRender = systemRoles.length > 0 ? systemRoles : ['admin', 'researcher', 'viewer'];
    rolesToRender.forEach(role => {
        const item = document.createElement('div');
        item.style.display = 'flex';
        item.style.alignItems = 'center';
        item.style.padding = '1px 0';

        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = role;
        cb.style.marginRight = '5px';

        if (role === 'admin') {
            cb.checked = true;
            item.style.display = 'none';
        } else if (currentList.includes('all')) {
            cb.checked = true;
        } else {
            cb.checked = currentList.includes(role);
        }
        cb.addEventListener('change', _updateAccessToggleLabel);

        const span = document.createElement('span');
        span.classList.add('text-sm');
        span.textContent = role.charAt(0).toUpperCase() + role.slice(1);

        item.appendChild(cb);
        item.appendChild(span);
        container.appendChild(item);
    });

    _updateAccessToggleLabel();
}

document.addEventListener('click', (ev) => {
    const dropdown = document.getElementById('studyAccessDropdown');
    if (!dropdown) return;
    const panel = document.getElementById('studyAccessPanel');
    const toggle = document.getElementById('studyAccessToggle');
    if (!panel || !toggle) return;
    if (toggle.contains(ev.target)) {
        panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
    } else if (!panel.contains(ev.target)) {
        panel.style.display = 'none';
    }
});


// Collections count lives in the modal header (next to "Last updated"). It carries
// the included (after sampling/date filter) and potential (selected) counts, stored
// on the element's dataset so partial updates (only actual, or only potential) work.
function _updateCollectionsHeader({ actual, potential, resetActual } = {}) {
    const el = document.getElementById('editStudyModalCollections');
    if (!el) return;
    if (resetActual) el.dataset.actual = '';
    else if (actual !== undefined && actual !== null) el.dataset.actual = String(actual);
    if (potential !== undefined && potential !== null) el.dataset.potential = String(potential);
    const a = el.dataset.actual ? Number(el.dataset.actual).toLocaleString() : '\u2013';
    const p = el.dataset.potential ? Number(el.dataset.potential).toLocaleString() : '\u2013';
    el.textContent = `${a} / ${p} collections`;
}

const _SET_FRAME_ELIGIBLE = {
    activities: ['annotated', 'scrapedOnly', 'notScraped'],
    off: ['annotated', 'scrapedOnly', 'notScraped'],
    scraped: ['annotated', 'scrapedOnly'],
    annotated: ['annotated'],
};

// Help copy for the mosaic. \n becomes a line break (tooltip uses white-space: pre-wrap).
// Keep these free of double-quotes, < , > and & so they stay valid inside data-tooltip="…".
const _VIZ_TIPS = {
    overview: 'This box is every activity (a play or observe event) in your selected collections and date range.\n\n'
        + 'The columns split those activities by how enriched each video currently is. The shaded band is the share the sampling keeps for the study.\n\n'
        + 'Key point: enrichment status is the current state, not a limit. You can scrape and annotate the videos you include here afterwards. Hover over the areas in the plot for details.',
    annotated: 'Activities on videos that are scraped AND annotated by the LLM (captions, on-screen text, themes, language, country…). This is the richest data for analysis.',
    scrapedOnly: 'Activities on videos that are scraped (metadata and video downloaded) but not yet annotated.\n\n'
        + 'Including them is fine: you can annotate these videos later (Scrape and Annotate tab) and they move into the annotated column.',
    notScraped: 'Activities on videos with no enrichment yet — only the raw on-device capture.\n\n'
        + 'Including them is fine: you can scrape them later, then annotate them, moving them across the columns.',
    headline: 'How many activities the sampling actually kept, out of all activities in your collections and date range.\n\n'
        + 'The shaded band shows this share; the number inside each band is how many kept activities are of that enrichment type.',
};

function _fmtInt(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n.toLocaleString() : '0';
}

function _resetStudySetViz(row, state) {
    const viz = row.querySelector('.study-set-viz');
    if (!viz) return;
    viz.dataset.state = state || 'stale';
    viz.innerHTML = '<div class="study-set-viz-empty text-xs">Select collections and a date range to see activity coverage and the sampled share.</div>';
}

function _renderStudySetViz(row, { universe, included, frame, seeded } = {}) {
    const viz = row.querySelector('.study-set-viz');
    if (!viz) return;

    const all = Math.max(0, Number(universe && universe.activities) || 0);
    if (!all) { _resetStudySetViz(row, 'empty'); return; }

    const uScraped = Math.max(0, Number(universe && universe.scraped) || 0);
    const uAnnotated = Math.max(0, Number(universe && universe.annotated) || 0);
    const incActivities = Math.max(0, Number(included && included.total_activities) || 0);

    // Included activities split by the enrichment status of their video.
    const incAnnotated = Math.max(0, Number(included && included.activities_annotated) || 0);
    const incScraped = Math.max(0, Number(included && included.activities_scraped) || 0);
    const incScrapedOnly = Math.max(incScraped - incAnnotated, 0);
    const incNotScraped = Math.max(incActivities - incScraped, 0);

    // Column counts (clamp so nesting holds: annotated <= scraped <= all).
    const colAnnotated = Math.min(uAnnotated, all);
    const colScrapedOnly = Math.max(Math.min(uScraped, all) - colAnnotated, 0);
    const colNotScraped = Math.max(all - colAnnotated - colScrapedOnly, 0);

    const cols = [
        { key: 'annotated', label: 'annotated', count: colAnnotated, inc: incAnnotated },
        { key: 'scrapedOnly', label: 'scraped only', count: colScrapedOnly, inc: incScrapedOnly },
        { key: 'notScraped', label: 'not scraped', count: colNotScraped, inc: incNotScraped },
    ];

    const frameKey = frame || 'activities';
    const eligibleKeys = _SET_FRAME_ELIGIBLE[frameKey] || _SET_FRAME_ELIGIBLE.activities;

    // Uniform fill height across the eligible columns: sampling treats the frame as
    // one pool, so the band height is included activities / (universe within the frame).
    const frameUniverse = cols
        .filter(c => eligibleKeys.indexOf(c.key) !== -1)
        .reduce((s, c) => s + c.count, 0);
    let fillPct = frameUniverse > 0 ? (incActivities / frameUniverse) * 100 : 0;
    fillPct = Math.max(0, Math.min(100, fillPct));

    const samplePct = all > 0 ? Math.round((incActivities / all) * 100) : 0;

    // Each column shows only its label; the counts live in a dynamic tooltip so
    // they stay legible even when a column is too narrow to fit a number.
    const boxHtml = cols.map(c => {
        const widthPct = (c.count / all) * 100;
        const eligible = eligibleKeys.indexOf(c.key) !== -1;
        const countLine = eligible
            ? `${c.label}: ${_fmtInt(c.count)} activities in frame, ${_fmtInt(c.inc)} sampled into study.`
            : `${c.label}: ${_fmtInt(c.count)} activities, outside the sampling frame.`;
        const tip = `${countLine}\n\n${_VIZ_TIPS[c.key] || ''}`;
        const anchor = c.key === 'notScraped' ? ' tooltip-right-anchored' : '';
        const fill = eligible ? `<div class="study-viz__fill" style="height: ${fillPct}%;"></div>` : '';
        const clsExtra = eligible ? '' : ' study-viz__col--outframe';
        return `<div class="study-viz__col meta-tooltip tooltip-below${clsExtra}${anchor}" data-tooltip="${tip}" style="flex: 0 0 ${widthPct}%;">` +
            fill +
            `<span class="study-viz__collabel-in text-xxs">${c.label}</span>` +
            `</div>`;
    }).join('');

    viz.innerHTML =
        `<div class="study-viz__main">` +
            `<div class="study-viz__box">${boxHtml}</div>` +
            `<span class="study-viz__help study-viz__help--side meta-tooltip tooltip-below tooltip-right-anchored text-xxs" data-tooltip="${_VIZ_TIPS.overview}">what is this?</span>` +
        `</div>` +
        `<div class="study-viz__headline text-xs">sampled into study &middot; ${_fmtInt(incActivities)} of ${_fmtInt(all)} activities (${samplePct}%)` +
            `<span class="study-viz__help meta-tooltip tooltip-right-anchored" data-tooltip="${_VIZ_TIPS.headline}">&#9432;</span>` +
        `</div>` +
        `<div class="study-viz__legend text-xxs">` +
        `<span class="study-viz__legend-item"><span class="study-viz__swatch study-viz__swatch--included"></span>sampled into study</span>` +
        `<span class="study-viz__legend-item"><span class="study-viz__swatch study-viz__swatch--eligible"></span>inside frame, not sampled</span>` +
        `<span class="study-viz__legend-item"><span class="study-viz__swatch study-viz__swatch--outframe"></span>outside frame</span>` +
        `</div>` +
        (seeded ? `<div class="study-viz__seeded-note text-xxs">Showing the last saved result; adjust the sampling or date range to refresh.</div>` : '');
    viz.dataset.state = seeded ? 'seeded' : 'ready';
    requestAnimationFrame(() => _fitMosaicLabels(viz));
}

// Rotate a column label to vertical (and shrink it) when it cannot fit
// horizontally — mirrors how Plotly lays out cramped bar labels.
function _fitMosaicLabels(viz) {
    const cols = viz.querySelectorAll('.study-viz__col');
    cols.forEach((col) => {
        const label = col.querySelector('.study-viz__collabel-in');
        if (!label) return;
        col.classList.remove('study-viz__col--vlabel', 'study-viz__col--tinylabel');
        const naturalWidth = label.scrollWidth;
        if (naturalWidth <= col.clientWidth - 4) return;
        col.classList.add('study-viz__col--vlabel');
        if (naturalWidth > col.clientHeight - 6) {
            col.classList.add('study-viz__col--tinylabel');
        }
    });
}

function _clearStudyIssues(row) {
    const container = row.querySelector('.study-issues-list');
    if (!container) return;
    container.innerHTML = '';
    container.style.display = 'none';
}

function _renderStudyIssues(row, issues) {
    const container = row.querySelector('.study-issues-list');
    if (!container) return;
    if (!issues || !issues.length) {
        _clearStudyIssues(row);
        return;
    }
    const colorMap = {
        ok: 'var(--color-success)',
        warn: 'var(--color-warning)',
        error: 'var(--color-danger)',
    };
    const esc = (s) => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    container.innerHTML = issues.map(i => {
        const color = colorMap[i.severity] || colorMap.warn;
        return '<div style="display: flex; align-items: center; gap: 6px;">' +
            `<span style="display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: ${color}; flex: 0 0 auto;"></span>` +
            `<span style="color: var(--color-text-secondary);">${esc(i.message)}</span>` +
            '</div>';
    }).join('');
    container.style.display = 'flex';
}

// --- Modal ---

function createNewStudy() {
    const newStudy = {
        STUDY_NAME: '',
        START_DATE: "",
        END_DATE: "",
        USER_ACCESS: [],
        SAMPLE_FRAME: "activities",
        SELECTED_COLLECTIONS: []
    };

    // Open the edit modal with a name input — user will save when ready
    loadSystemRoles(() => _showStudyModal(newStudy, true));
}

// Init
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

// This file is also loaded for My-Studies-only users, who have none of the Data
// Management permissions. Skipping the three admin bootstraps for them avoids a
// row of 403s in the console; the My Studies table only needs loadStudies().
function _dmCan(perm) {
    return !Array.isArray(window.USER_PERMS) || window.USER_PERMS.includes(perm);
}

if (_dmCan('tab.data_management.edit_collections')) {
    // Load collections FIRST, then studies to ensure selector populates correctly
    loadAvailableCollections();
} else {
    loadStudies();
}
// Roles only drive the admin-only access dropdown in the study modal.
if (window.USER_IS_ADMIN) loadSystemRoles();
// Self-guards on the ingestion sub-page being present.
loadIngestionSources();

// --- Enrichment Stats & Logic ---

function formatShortDate(isoStr) {
    return fypFmtDateTimeShort(isoStr);
}

function renderConsolidateStatus(stats) {
    const statusEl = document.getElementById('consolidate-status');
    if (!statusEl || !stats) return;
    const lines = [];
    if (stats.last_consolidation) {
        const dt = formatShortDate(stats.last_consolidation);
        lines.push(`Last consolidation ${dt}: ${stats.new_scrape_files ?? 0} new scrape file(s) and ${stats.new_annotation_files ?? 0} new annotation file(s).`);
    }
    if (stats.last_status_refresh) {
        const dt = formatShortDate(stats.last_status_refresh);
        lines.push(`Last enrichment status refresh: ${dt}`);
    }
    // Persistent pipeline outcome — written by the orchestrator at the end
    // of a consolidate+refresh run (or by the consolidate worker itself when
    // no downstream refresh was needed). Shown in success green so the user
    // has an explicit statement of what happened.
    if (stats.last_pipeline_summary) {
        const esc = escapeHtml(stats.last_pipeline_summary);
        // A partial/aborted pipeline is surfaced as an amber warning, not a
        // green ✓ — otherwise an aborted refresh reads as a success.
        const partial = !!stats.last_pipeline_partial;
        const icon = partial ? '⚠' : '✓';
        const color = partial ? 'var(--color-warning)' : 'var(--color-success-light)';
        lines.push(`<span style="color: ${color}; font-weight: var(--weight-medium);">${icon} ${esc}</span>`);
    }
    if (lines.length) {
        statusEl.innerHTML = lines.join('<br>');
        statusEl.style.color = 'var(--color-success-light)';
    }
}

function escapeHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function checkConsolidationNeeded(data) {
    const warningEl = document.getElementById('consolidate-warning');
    if (!warningEl) return;

    const consolidateBtn = document.getElementById('btn-consolidate');
    const setNeedsAction = (needs) => {
        // Mirror the state on the "Refresh Caches" sidebar item, so the
        // stale signal is visible without opening the page.
        const staleDot = document.getElementById('refresh-caches-stale-dot');
        if (staleDot) staleDot.style.display = needs ? 'inline-block' : 'none';
        if (!consolidateBtn) return;
        if (needs) {
            consolidateBtn.classList.add('btn-has-pending');
        } else {
            consolidateBtn.classList.remove('btn-has-pending');
        }
    };

    // Suppress the "scraper/annotator completed after last consolidation"
    // warning whenever the consolidate pipeline is actively running (or the
    // local-dev poll loop is active) — the pipeline IS the response to that
    // condition, so showing the warning during it is misleading.
    if (data.consolidate_pipeline_active || _consolidatePollActive) {
        warningEl.style.display = 'none';
        setNeedsAction(false);
        return;
    }

    const lastConsolidation = data.consolidate_stats?.last_consolidation;
    const scraperSuccess = data.scraper_last_success;
    const annotatorSuccess = data.annotator_last_success;

    if (!lastConsolidation) {
        // Never consolidated — warn if any process has run
        if (scraperSuccess || annotatorSuccess) {
            warningEl.textContent = 'New enrichment data has not been consolidated yet. Click "Consolidate & Refresh" to update.';
            warningEl.style.display = '';
            setNeedsAction(true);
        } else {
            setNeedsAction(false);
        }
        return;
    }

    // Parsed through the shared helper so a zone-less legacy value is read as
    // UTC rather than as the viewer's local time, which would skew the compare.
    const consolTs = fypParseInstant(lastConsolidation)?.getTime();
    const isNewer = (ts) => {
        const d = fypParseInstant(ts);
        return !!(d && consolTs != null && d.getTime() > consolTs);
    };
    const scraperNewer = isNewer(scraperSuccess);
    const annotatorNewer = isNewer(annotatorSuccess);

    if (scraperNewer || annotatorNewer) {
        const parts = [];
        if (scraperNewer) parts.push('scraper');
        if (annotatorNewer) parts.push('annotator');
        warningEl.textContent = `The ${parts.join(' and ')} completed after the last consolidation. Click "Consolidate & Refresh" to incorporate new data.`;
        warningEl.style.display = '';
        setNeedsAction(true);
    } else {
        warningEl.style.display = 'none';
        setNeedsAction(false);
    }
}

// --- Cascade Refresh State ---
// Tracks the active cascade refresh so that:
//   1. main.js can chain meta refreshes after study refresh completes
//   2. Refresh Caches page buttons are disabled while a cascade is running
let _cascadeRefresh = null;

function renderConsolidationImpact(impact, partial = null) {
    const panel = document.getElementById('consolidate-impact');
    const details = document.getElementById('impact-details');
    const actions = document.getElementById('impact-actions');
    const note = document.getElementById('impact-partial-note');
    if (!panel || !details || !actions) return;

    if (!impact || !impact.changed_item_count) {
        panel.style.display = 'none';
        return;
    }

    // When the last auto-refresh aborted partway, explain why the impact is
    // still here so the panel doesn't read as "nothing happened".
    if (note) {
        if (partial && partial.partial) {
            const where = partial.failedAt
                ? ` at "${escapeHtml(_humanizePipelineSteps(partial.failedAt))}"` : '';
            note.textContent = `⚠ The auto-refresh stopped${where}; the items below were not fully refreshed. `
                + `Click "Refresh All Affected" to complete.`;
            note.style.display = '';
        } else {
            note.style.display = 'none';
        }
    }

    const parts = [];
    if (impact.new_scrape_item_count) parts.push(`${impact.new_scrape_item_count.toLocaleString()} newly scraped`);
    if (impact.new_annotation_item_count) parts.push(`${impact.new_annotation_item_count.toLocaleString()} newly annotated`);
    const itemSummary = parts.length ? parts.join(', ') : `${impact.changed_item_count.toLocaleString()} changed`;

    const collCount = impact.affected_collection_ids ? impact.affected_collection_ids.length : 0;
    const studyNames = impact.affected_study_names || [];

    let html = `${itemSummary} item(s) across <strong>${collCount}</strong> collection(s)`;
    if (studyNames.length) {
        html += ` in <strong>${studyNames.length}</strong> study/studies: ${studyNames.join(', ')}`;
    }
    details.innerHTML = html;

    // Single cascade button
    actions.innerHTML = '';
    const btn = document.createElement('button');
    btn.className = 'action-btn text-xs';
    btn.id = 'btn-cascade-refresh';
    btn.style.padding = '4px 8px';
    btn.textContent = 'Refresh All Affected';
    btn.onclick = () => startCascadeRefresh(impact, btn);
    // Disable if cascade is already running
    if (_cascadeRefresh) {
        btn.disabled = true;
        btn.textContent = _cascadeRefresh.statusText || 'Refreshing...';
        btn.className = 'btn-running text-xs';
    } else {
        btn.classList.add('btn-has-pending');
    }
    actions.appendChild(btn);

    panel.style.display = '';
}

function startCascadeRefresh(impact, btn) {
    // Run the SAME downstream pipeline the consolidate auto-refresh uses
    // (embeddings → video_map → recode → {meta ‖ pca ‖ timelines}) against the
    // stored impact. The backend builds + dispatches it and records the
    // pipeline_plan; we then poll the shared pipeline status — so the step list
    // and the niche steps (embeddings/video_map) that the old per-button
    // cascade skipped are now both covered.
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Starting…';
        btn.className = 'btn-running text-xs';
    }
    fetch('/api/manage/enrichment/refresh-downstream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: '{}',
    })
        .then(res => res.json())
        .then(resp => {
            if (resp.status === 'started') {
                // Hide the impact card while the pipeline runs; the step list +
                // status line take over. It only re-appears if the run was partial.
                renderConsolidationImpact(null);
                pollConsolidationStatus();
            } else {
                const statusEl = document.getElementById('consolidate-status');
                if (statusEl) {
                    statusEl.textContent = resp.message || 'Could not start refresh.';
                    statusEl.style.color = (resp.status === 'noop')
                        ? 'var(--color-text-secondary)' : 'var(--color-danger)';
                }
                if (btn) { btn.disabled = false; btn.textContent = 'Refresh All Affected'; btn.className = 'action-btn text-xs'; }
            }
        })
        .catch(err => {
            console.error('Failed to start downstream refresh:', err);
            if (btn) { btn.disabled = false; btn.textContent = 'Refresh All Affected'; btn.className = 'action-btn text-xs'; }
        });
}

function onCascadeStudiesComplete() {
    if (!_cascadeRefresh || _cascadeRefresh.phase !== 'waiting_for_studies') return;
    _cascadeRefresh.phase = 'waiting_for_meta';
    _cascadeRefresh.statusText = 'Refreshing metadata...';
    updateCascadeButton();
    startMetaRefreshes();
}

function startMetaRefreshes() {
    const studyFilter = _cascadeRefresh.studyNames.length
        ? { studies: _cascadeRefresh.studyNames.join(',') } : {};
    const promises = [];
    promises.push(
        startTargetedRefresh('meta_refresh_groups', {})
            .then(() => { _cascadeRefresh.startedMetaGroups = true; })
    );
    promises.push(
        startTargetedRefresh('pca_refresh', studyFilter)
            .then(() => { _cascadeRefresh.startedPca = true; })
    );
    Promise.allSettled(promises).then(() => {
        // Now waiting for meta + PCA processes to finish — detected by main.js
    });
}

function onCascadeRefreshComplete() {
    // Called when all cascade processes have finished
    _cascadeRefresh = null;
    updateCascadeButton();
    updateCascadeRefreshPageLock(false);
    // Call staleness FIRST so the backend clears `consolidation_impact` from
    // process_stats when all downstream is fresh. Only then refresh enrichment
    // stats — otherwise the stats endpoint still returns the stale impact and
    // the panel re-appears.
    const stalePromise = (typeof fetchStalenessStatus === 'function')
        ? fetchStalenessStatus()
        : Promise.resolve();
    Promise.resolve(stalePromise).finally(() => fetchEnrichmentStats());
}

function updateCascadeButton() {
    const btn = document.getElementById('btn-cascade-refresh');
    if (!btn) return;
    if (_cascadeRefresh) {
        btn.disabled = true;
        btn.textContent = _cascadeRefresh.statusText || 'Refreshing...';
        btn.className = 'btn-running text-xs';
    } else {
        btn.disabled = false;
        btn.textContent = 'Refresh All Affected';
        btn.className = 'action-btn text-xs';
    }
}

function updateCascadeRefreshPageLock(locked) {
    // Disable/enable the toggle buttons on the Refresh Caches page
    const processNames = ['recode_refresh_studies', 'meta_refresh_groups', 'timelines_refresh', 'pca_refresh'];
    processNames.forEach(name => {
        const toggleBtn = document.getElementById(`${name}-toggle`);
        if (toggleBtn) {
            toggleBtn.disabled = locked;
            if (locked) {
                toggleBtn.title = 'Cascade refresh in progress';
            } else {
                toggleBtn.title = '';
            }
        }
    });
}

function startTargetedRefresh(processName, params) {
    return fetch(`/api/start/${processName}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify(params)
    })
        .then(res => res.json())
        .then(data => {
            if (data.status !== 'success') {
                console.error(`Failed to start ${processName}: ${data.message}`);
            }
            return data;
        })
        .catch(err => {
            console.error(`Failed to start ${processName}:`, err);
        });
}

// Map a system-health chip status → { label, cls } for the per-card pill. The
// class selects a semantic-token color in style.css (no hardcoded colors here).
const _HEALTH_CHIP_META = {
    ok: { label: 'OK', cls: 'ok' },
    warn: { label: 'Warning', cls: 'warn' },
    fail: { label: 'Failing', cls: 'bad' },
    unknown: { label: 'Unknown', cls: 'unknown' },
};

// Compact relative time for a chip tooltip ("just now", "5m ago", "3h ago").
function _healthRelativeTime(iso) {
    return fypFmtRelative(iso);
}

// Paint one health pill from a { status, summary, checked_at } entry. The
// summary carries the combined scrape/media/cookie (or Gemini) detail; the
// tooltip appends when the underlying check last ran.
function _renderHealthChip(el, prefix, entry) {
    if (!el || !entry) return;
    const meta = _HEALTH_CHIP_META[entry.status] || _HEALTH_CHIP_META.unknown;
    el.className = `cookie-pill cookie-pill--${meta.cls} meta-tooltip`;
    el.textContent = `${prefix}: ${meta.label}`;

    const parts = [];
    if (entry.summary) parts.push(entry.summary);
    const rel = _healthRelativeTime(entry.checked_at);
    parts.push(rel ? `Checked ${rel}` : 'Health check has not run yet');
    el.setAttribute('data-tooltip', parts.join(' • '));
}

// Render the per-platform scraper chips and the annotation chip from the
// enrichment-stats payload's derived card_health (see system_health.derive_card_health).
function renderCardHealth(cardHealth) {
    if (!cardHealth) return;
    for (const [platform, entry] of Object.entries(cardHealth.platforms || {})) {
        _renderHealthChip(document.getElementById('cookie-health-' + platform), 'Scraper', entry);
    }
    // Label follows the active backend (renderAnnotationConfigNotice keeps
    // window._annotationBackend current from the same stats payload).
    const backendLabel = (window._annotationBackend && window._annotationBackend !== 'gemini')
        ? window._annotationBackend : 'Gemini';
    _renderHealthChip(document.getElementById('annotation-health'), backendLabel, cardHealth.annotation);
}

// Show/hide the per-platform scraper alert banners from the enrichment-stats
// payload's scraper_alerts (raised by the worker on a systematic failure such
// as a permanent-failure storm; cleared on the next healthy batch or by the
// Dismiss button).
function renderScraperAlerts(alerts) {
    document.querySelectorAll('[id^="scraper-alert-"]').forEach(banner => {
        const platform = banner.id.replace('scraper-alert-', '');
        const alert = (alerts || {})[platform];
        if (!alert) {
            banner.style.display = 'none';
            return;
        }
        const raised = _healthRelativeTime(alert.raised_at);
        const seen = alert.occurrences > 1 ? ` (seen ${alert.occurrences}× since ${raised || '?'})`
                                           : (raised ? ` (${raised})` : '');
        banner.querySelector('.scraper-alert-text').textContent =
            `⚠ Scraper needs attention${seen}: ${alert.message || alert.kind}`;
        banner.style.display = 'flex';
    });
}

// Show the "Gemini not configured" notice from the enrichment-stats payload,
// so the state is visible on the card rather than only on a refused Start.
// Not dismissable: unlike a scraper alert it is a standing setup state, and it
// disappears by itself once Gemini is configured.
function renderAnnotationConfigNotice(stats) {
    // Backend badge: which engine the annotator card will run (Gemini vs a
    // local model, selected in Admin → Backends).
    const backend = (stats && stats.annotation_backend) || 'gemini';
    window._annotationBackend = backend;
    const badge = document.getElementById('annotation-backend-badge');
    if (badge) {
        badge.textContent = backend === 'gemini' ? 'Gemini' : backend;
        badge.style.display = 'inline-block';
    }

    const notice = document.getElementById('annotation-config-notice');
    if (!notice) return;
    if (!stats || stats.annotation_configured !== false) {
        notice.style.display = 'none';
        return;
    }
    const reason = stats.annotation_config_reason || 'Machine annotation is not configured.';
    const localAnchors = {
        qwen_local: 'docs/installation.md#enabling-local-qwen-annotation',
        minicpm_local: 'docs/installation.md#enabling-local-minicpm-annotation',
    };
    const docsAnchor = localAnchors[stats.annotation_backend]
        || 'docs/installation.md#enabling-gemini-later';
    notice.querySelector('.config-notice-text').textContent = `⚙ ${reason} See ${docsAnchor}`;
    notice.style.display = 'block';
}

function dismissScraperAlert(platform) {
    const csrfToken = document.querySelector('meta[name="csrf-token"]').getAttribute('content');
    fetch('/api/manage/enrichment/scraper_alert/dismiss', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ platform: platform }),
    })
        .then(res => res.json())
        .then(() => fetchEnrichmentStats())
        .catch(err => console.error('Failed to dismiss scraper alert:', err));
}

function fetchEnrichmentStats() {
    // The Scrape / Annotation / Refresh sub-pages only render for users with
    // the matching 'tab.data_management.*' permission. Without any of them the
    // endpoint aborts 403 (an HTML page that breaks res.json()), so skip the
    // call entirely. Every DOM write below is null-guarded because the target
    // elements are spread across those three pages and any of them may be
    // absent for the current role.
    if (!document.getElementById('dm-page-scrape')
        && !document.getElementById('dm-page-annotation')
        && !document.getElementById('dm-page-refresh')) return;
    const setText = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    };
    fetch('/api/manage/enrichment/stats')
        .then(res => res.json())
        .then(data => {
            // Stats (scrape-page header + annotation-page header)
            setText('enrich_total_videos', (data.total_videos !== undefined) ? data.total_videos.toLocaleString() : '-');
            setText('enrich_scraped', (data.scraped_videos !== undefined) ? data.scraped_videos.toLocaleString() : '-');
            setText('enrich_annotated', (data.annotated_videos !== undefined) ? data.annotated_videos.toLocaleString() : '-');
            setText('annot_scraped', (data.scraped_videos !== undefined) ? data.scraped_videos.toLocaleString() : '-');
            setText('annot_annotated', (data.annotated_videos !== undefined) ? data.annotated_videos.toLocaleString() : '-');

            // Queues (per-platform scrape counters)
            if (data.scrape_queues) {
                for (const [platform, len] of Object.entries(data.scrape_queues)) {
                    const el = document.getElementById('enrich_scrape_targets_' + platform);
                    if (el) {
                        el.textContent = len.toLocaleString();
                        el.style.color = 'var(--color-success-light)';
                    }
                }
            }

            // Per-card health pills (scrapers + annotation), combining the last
            // system-health check with the fresh cookie status. Also cached
            // globally so main.js's startProcess can warn before starting a
            // scraper/annotator whose health is degraded.
            if (data.card_health) {
                window._cardHealth = data.card_health;
                renderCardHealth(data.card_health);
            }
            renderScraperAlerts(data.scraper_alerts);
            renderAnnotationConfigNotice(data);
            const annotateTargets = document.getElementById('enrich_annotate_targets');
            if (annotateTargets && data.annotate_queue_len !== undefined) {
                annotateTargets.textContent = data.annotate_queue_len.toLocaleString();
                annotateTargets.style.color = 'var(--color-success-light)';
            }
            if (typeof updateAnnotateInflight === 'function') updateAnnotateInflight(data.annotate_claimed_len);

            // Consolidation status from process_stats (only when not actively polling a run)
            if (!_consolidatePollActive && data.consolidate_stats) {
                renderConsolidateStatus(data.consolidate_stats);
                // Suppress the impact panel while the consolidate pipeline is
                // running (auto-pipeline or manual cascade) — those flows are
                // already refreshing the same downstream caches the panel's
                // button would invoke, so showing it is misleading.
                const pipelineActive = !!data.consolidate_pipeline_active || !!_cascadeRefresh;
                renderConsolidationImpact(
                    pipelineActive ? null : data.consolidate_stats.consolidation_impact,
                    { partial: !!data.last_pipeline_partial, failedAt: data.last_pipeline_failed_at }
                );
            }

            // Persistent + live per-step pipeline list (updates every tick).
            renderPipelineSteps(data.pipeline_steps);

            // Button state (armed / workers-running / idle)
            applyConsolidateButtonState(data);

            // If a pipeline step is running (e.g. after page reload mid-run),
            // kick off the poll so the UI shows live stage progress.
            if (!_consolidatePollActive && data.consolidate_pipeline_active) {
                pollConsolidationStatus();
            }

            // Auto-fire: flag is armed AND workers now idle → POST consolidate.
            // Race-safe because the server rejects a double-dispatch.
            if (data.consolidate_auto_armed
                && (data.workers_blocking_consolidate || []).length === 0
                && !_consolidatePollActive) {
                const autoRefresh = !!data.consolidate_auto_armed_auto_refresh;
                fetch('/api/manage/enrichment/consolidate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                    body: JSON.stringify({ auto_refresh: autoRefresh }),
                })
                    .then(res => res.json())
                    .then(resp => {
                        if (resp.status === 'started') {
                            pollConsolidationStatus();
                        }
                    })
                    .catch(err => console.error('Auto-fire consolidate failed:', err));
            }

            // Check if consolidation is needed
            checkConsolidationNeeded(data);
        })
        .catch(err => console.error("Error fetching enrichment stats:", err));
}

// All per-platform scrape-queue counter elements (one per scraper block).
function scrapeTargetEls() {
    return Array.from(document.querySelectorAll('[id^="enrich_scrape_targets_"]'));
}

// Ask the server what a queue build WOULD do, then confirm with the user.
// Returns true when the caller should proceed with the real request.
// A dry-run failure is not fatal — we fall through to the normal request
// rather than blocking the user on an estimate.
async function confirmQueueBuild(endpoint, payload, noun) {
    let est;
    try {
        const res = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify({ ...payload, dry_run: true })
        });
        est = await res.json();
    } catch (err) {
        console.error('Queue dry-run failed:', err);
        return true;
    }
    if (!est || est.status !== 'success' || est.would_queue === undefined) return true;

    if (est.would_queue === 0) {
        showAppAlert(`Nothing to queue — no ${noun} matched the current selection.`);
        return false;
    }

    const lines = [`Queue ${est.would_queue.toLocaleString()} ${noun}?`];
    if (est.cost_estimate && est.cost_estimate.est_cost_usd) {
        lines.push(`Estimated cost: ~$${est.cost_estimate.est_cost_usd.toLocaleString()} `
            + `(${est.cost_estimate.backend}).`);
    }
    if (est.capped) {
        lines.push(`${est.requested.toLocaleString()} matched, but a per-request cap `
            + `of ${est.cap.toLocaleString()} applies to your account.`);
    }
    return showAppConfirm(lines.join('\n'));
}

async function queueVideosForScraping(btnElement) {
    const studyName = document.getElementById('enrichment-study-select').value;
    const scrapeTargets = scrapeTargetEls();

    if (!studyName) {
        showAppAlert("Please select a target study from the dropdown first.");
        return;
    }

    const retryEl = document.getElementById('retry-failed-attempts');
    const retryFailed = !!(retryEl && retryEl.checked);
    const retryMediaEl = document.getElementById('retry-missing-media');
    const retryMissingMedia = !!(retryMediaEl && retryMediaEl.checked);

    const scrapePayload = {
        study_name: studyName,
        retry_failed: retryFailed,
        retry_missing_media: retryMissingMedia,
    };
    if (!(await confirmQueueBuild('/api/manage/enrichment/calculate_to_scrape',
        scrapePayload, 'video(s) for scraping'))) return;

    // UI Loading state
    const originalText = btnElement.textContent;
    btnElement.textContent = "Queueing...";
    btnElement.disabled = true;

    scrapeTargets.forEach(el => {
        el.textContent = "Calc...";
        el.style.color = 'var(--color-text-tertiary)';
    });

    fetch('/api/manage/enrichment/calculate_to_scrape', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify(scrapePayload)
    })
        .then(res => res.json())
        .then(scrapeData => {
            btnElement.textContent = originalText;
            btnElement.disabled = false;

            // Update scrape display (per-platform queue lengths)
            if (scrapeData.status === 'success') {
                const byPlatform = scrapeData.videos_to_scrape_by_platform || {};
                scrapeTargets.forEach(el => {
                    const platform = el.id.slice('enrich_scrape_targets_'.length);
                    const len = byPlatform[platform];
                    el.textContent = (len !== undefined) ? len.toLocaleString() : '0';
                    el.style.color = 'var(--color-success-light)';
                });
            } else {
                scrapeTargets.forEach(el => {
                    el.textContent = "Error";
                    el.style.color = 'var(--color-danger)';
                });
                console.error("Scrape Error:", scrapeData.error);
            }

            // Refresh total stats
            fetchEnrichmentStats();
        })
        .catch(err => {
            btnElement.textContent = originalText;
            btnElement.disabled = false;
            scrapeTargets.forEach(el => {
                el.textContent = "Failed";
                el.style.color = 'var(--color-danger)';
            });
            console.error("Error queueing videos for scraping:", err);
            showAppAlert("Error queueing videos for scraping.");
        });
}

// Selected annotation-queue selection mode on the Annotation page.
function _annotationSelectionMode() {
    const checked = document.querySelector('input[name="annot-selection-mode"]:checked');
    return checked ? checked.value : 'study';
}

// Enable/disable the per-mode controls when the selection-mode radio changes.
function updateAnnotationModeControls() {
    const mode = _annotationSelectionMode();
    const versionSelect = document.getElementById('annot-version-select');
    const fromDate = document.getElementById('annot-from-date');
    const toDate = document.getElementById('annot-to-date');
    const retryLabel = document.getElementById('annot-retry-failed-label');
    if (versionSelect) versionSelect.disabled = mode !== 'version';
    if (fromDate) fromDate.disabled = mode !== 'timeframe';
    if (toDate) toDate.disabled = mode !== 'timeframe';
    // "Include previously failed attempts" only applies to the study mode —
    // the other modes select successfully-annotated videos by definition.
    if (retryLabel) retryLabel.style.display = (mode === 'study') ? 'flex' : 'none';
}

// Populate the annotation-version dropdown from the enrichment-scoped version
// list (versions that actually occur in the annotation archive).
let _annotVersionsLoaded = false;
function loadAnnotationVersionOptions(force = false) {
    const select = document.getElementById('annot-version-select');
    if (!select || (_annotVersionsLoaded && !force)) return;
    fetch('/api/manage/enrichment/annotation_versions')
        .then(res => res.json())
        .then(data => {
            const versions = data.versions || [];
            const current = select.value;
            select.innerHTML = '<option value="">-- Select annotation version --</option>';
            versions.forEach(v => {
                const opt = document.createElement('option');
                opt.value = v.annotation_version;
                const label = v.label || v.model || v.annotation_version;
                const shortHash = String(v.annotation_version).slice(0, 11);
                opt.textContent = `${label} (${shortHash}…)${v.active ? ' — preferred' : ''}`;
                select.appendChild(opt);
            });
            if (current && versions.some(v => v.annotation_version === current)) {
                select.value = current;
            }
            _annotVersionsLoaded = true;
        })
        .catch(err => console.error("Error loading annotation versions:", err));
}

async function queueVideosForAnnotation(btnElement) {
    const mode = _annotationSelectionMode();
    const studySelect = document.getElementById('annotation-study-select');
    const studyName = studySelect ? studySelect.value : '';
    const annotateTargetsDisplay = document.getElementById('enrich_annotate_targets');
    const resultEl = document.getElementById('annot-queue-result');

    // Every selection mode operates within a target study.
    if (!studyName) {
        showAppAlert("Please select a target study from the dropdown first.");
        return;
    }

    const payload = { selection_mode: mode, study_name: studyName };

    if (mode === 'study') {
        const retryEl = document.getElementById('annot-retry-failed');
        payload.retry_failed = !!(retryEl && retryEl.checked);
    } else if (mode === 'version') {
        const versionSelect = document.getElementById('annot-version-select');
        payload.annotation_version = versionSelect ? versionSelect.value : '';
        if (!payload.annotation_version) {
            showAppAlert("Please select an annotation version first.");
            return;
        }
    } else if (mode === 'timeframe') {
        const fromEl = document.getElementById('annot-from-date');
        const toEl = document.getElementById('annot-to-date');
        payload.annotated_from = fromEl ? fromEl.value : '';
        payload.annotated_to = toEl ? toEl.value : '';
        if (!payload.annotated_from && !payload.annotated_to) {
            showAppAlert("Please set at least one of the timeframe dates.");
            return;
        }
    }

    if (!(await confirmQueueBuild('/api/manage/enrichment/calculate_to_annotate',
        payload, 'video(s) for annotation'))) return;

    // UI Loading state
    const originalText = btnElement.textContent;
    btnElement.textContent = "Queueing...";
    btnElement.disabled = true;
    if (resultEl) resultEl.style.display = 'none';

    if (annotateTargetsDisplay) {
        annotateTargetsDisplay.textContent = "Calc...";
        annotateTargetsDisplay.style.color = 'var(--color-text-tertiary)';
    }

    fetch('/api/manage/enrichment/calculate_to_annotate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify(payload)
    })
        .then(res => res.json())
        .then(annotateData => {
            btnElement.textContent = originalText;
            btnElement.disabled = false;

            if (annotateData.status === 'success') {
                if (annotateTargetsDisplay) {
                    annotateTargetsDisplay.textContent = annotateData.videos_to_annotate.toLocaleString();
                    annotateTargetsDisplay.style.color = 'var(--color-success-light)';
                }
                if (resultEl) {
                    const parts = [`Queued ${(annotateData.newly_queued ?? annotateData.videos_to_annotate).toLocaleString()} video(s)`];
                    if (annotateData.selected !== undefined) parts.push(`${annotateData.selected.toLocaleString()} matched the selection`);
                    if (annotateData.skipped_no_media) parts.push(`${annotateData.skipped_no_media.toLocaleString()} skipped (media not downloaded)`);
                    if (annotateData.skipped_no_inference_ts) parts.push(`${annotateData.skipped_no_inference_ts.toLocaleString()} skipped (no stored annotation timestamp — run the inference_ts backfill to include them)`);
                    if (annotateData.capped) parts.push(`capped at ${annotateData.cap.toLocaleString()} of ${annotateData.requested.toLocaleString()} matched`);
                    resultEl.textContent = parts.join(' · ');
                    resultEl.style.display = '';
                }
            } else {
                if (annotateTargetsDisplay) {
                    annotateTargetsDisplay.textContent = "Error";
                    annotateTargetsDisplay.style.color = 'var(--color-danger)';
                }
                console.error("Annotate Error:", annotateData.error);
                showAppAlert("Error queueing videos for annotation: " + (annotateData.error || 'unknown error'));
            }

            // Refresh total stats
            fetchEnrichmentStats();
        })
        .catch(err => {
            btnElement.textContent = originalText;
            btnElement.disabled = false;
            if (annotateTargetsDisplay) {
                annotateTargetsDisplay.textContent = "Failed";
                annotateTargetsDisplay.style.color = 'var(--color-danger)';
            }
            console.error("Error queueing videos for annotation:", err);
            showAppAlert("Error queueing videos for annotation.");
        });
}

function emptyQueue(queueType, platform) {
    if (!queueType) return;

    fetch(`/api/manage/enrichment/empty_queue/${queueType}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify(platform ? { platform: platform } : {})
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                fetchEnrichmentStats();
            } else {
                showAppAlert("Error: " + data.error);
            }
        })
        .catch(err => console.error("Failed to empty queue: " + err));
}

// Tracks whether the consolidation pipeline is currently being polled so that
// periodic fetchEnrichmentStats refreshes don't start a second polling loop.
let _consolidatePollActive = false;

// Downstream pipeline steps in dispatch order; used to identify the
// currently-running step during the consolidate pipeline. Must include the
// embeddings/video_map spine steps or the live poll can't surface progress for
// the slowest (and most failure-prone) part of the chain.
const _PIPELINE_STEPS = [
    "consolidate_enrichment",
    "embeddings_refresh",
    "video_map_refresh",
    "recode_refresh_studies",
    "meta_refresh_groups",
    "pca_refresh",
    "timelines_refresh",
];

// Short human labels for pipeline steps — mirrors _PIPELINE_STAGE_LABELS on the
// backend. Used to humanize the failed-at step name in the impact panel note.
const _PIPELINE_STEP_LABELS = {
    consolidate_enrichment: "Consolidate enrichment data",
    embeddings_refresh: "Semantic embeddings",
    video_map_refresh: "Semantic map",
    recode_refresh_studies: "Study definitions",
    meta_refresh_groups: "Explore metadata",
    pca_refresh: "Correlations",
    timelines_refresh: "Timelines",
};

function _humanizePipelineSteps(csv) {
    // failed_at may be a single step name or a comma-separated list of leaf
    // names. Return a readable, comma-joined label list.
    if (!csv) return '';
    return String(csv).split(',')
        .map(s => _PIPELINE_STEP_LABELS[s.trim()] || s.trim())
        .filter(Boolean)
        .join(', ');
}

function renderPipelineSteps(steps) {
    // Render the persistent + live per-step pipeline list below the consolidate
    // buttons. Hidden when no plan has been recorded (e.g. after a no-refresh
    // consolidation). Each step shows a status dot, label, state, and — for the
    // active step — an inline progress message/percent.
    const container = document.getElementById('consolidate-pipeline-steps');
    const list = document.getElementById('pipeline-steps-list');
    if (!container || !list) return;
    if (!steps || !steps.length) {
        container.style.display = 'none';
        list.innerHTML = '';
        return;
    }
    list.innerHTML = steps.map(s => {
        const state = s.state || 'pending';
        let detail = '';
        if (state === 'running' && (s.message || s.percent != null)) {
            const pct = (s.percent != null) ? ` ${Math.round(s.percent)}%` : '';
            detail = `<span class="pipeline-step-detail text-xxs">${escapeHtml(s.message || '')}${pct}</span>`;
        }
        return `<div class="pipeline-step pipeline-step--${state}">`
            + `<span class="pipeline-step-dot" aria-hidden="true"></span>`
            + `<span class="pipeline-step-label text-xs">${escapeHtml(s.label || s.step)}</span>`
            + `<span class="pipeline-step-state text-xxs">${escapeHtml(state)}</span>`
            + detail
            + `</div>`;
    }).join('');
    container.style.display = '';
}

function _activePipelineStep(statusData) {
    // Return the {name, state_obj} of the currently-running pipeline step, or
    // null if none is running. Only counts steps whose state is 'running'.
    for (const name of _PIPELINE_STEPS) {
        const p = statusData[name];
        if (p && p.state === 'running') return { name, state: p };
    }
    return null;
}

function _renderStageText(statusEl, stepName, progress) {
    if (!statusEl) return;
    const msg = progress && progress.message ? progress.message : '';
    const idx = progress && progress.stage_index;
    const total = progress && progress.stage_total;
    if (idx && total) {
        statusEl.textContent = `Stage ${idx}/${total} — ${msg || stepName}`;
    } else {
        statusEl.textContent = msg || `${stepName} running...`;
    }
    statusEl.style.color = 'var(--color-text-secondary)';
}

function consolidateEnrichmentData(btn, force = false, skipRefresh = false) {
    const statusEl = document.getElementById('consolidate-status');
    const btnC = document.getElementById('btn-consolidate');
    const btnI = document.getElementById('btn-consolidate-incremental');
    const btnF = document.getElementById('btn-consolidate-force');

    // Three modes map to two flags:
    //   default     → {}                  (incremental consolidate + refresh)
    //   force       → {force:true}        (full rebuild, no refresh)
    //   skipRefresh → {auto_refresh:false}(incremental consolidate, no refresh)
    const body = force ? { force: true } : (skipRefresh ? { auto_refresh: false } : {});

    // Hide the impact panel up-front so the old run's summary doesn't linger
    // while the new run is in flight. It will re-render on completion.
    renderConsolidationImpact(null);

    // If the button is already armed, a click disarms. Both non-force buttons
    // can be the armed one (dataset.armed is routed by applyConsolidateButtonState).
    if (!force && btn.dataset.armed === '1') {
        fetch('/api/manage/enrichment/consolidate/disarm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        })
            .then(res => res.json())
            .then(() => {
                statusEl.textContent = 'Auto-consolidation cancelled.';
                statusEl.style.color = 'var(--color-text-secondary)';
                // Button state will reconcile on next fetchEnrichmentStats tick.
                fetchEnrichmentStats();
            })
            .catch(err => console.error('Failed to disarm:', err));
        return;
    }

    // Optimistic UI: mark the clicked button as busy. The server response
    // tells us whether we fired or armed; on armed, the button is restyled
    // by applyConsolidateButtonState() when stats refresh.
    const originalText = btn.textContent;
    const originalClass = btn.className;
    btn.disabled = true;

    fetch('/api/manage/enrichment/consolidate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify(body)
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'started') {
                btn.textContent = 'Consolidating...';
                btn.className = 'btn-running';
                [btnC, btnI, btnF].forEach(b => { if (b && b !== btn) b.disabled = true; });
                statusEl.textContent = 'Consolidation running...';
                statusEl.style.color = 'var(--color-text-secondary)';
                pollConsolidationStatus();
            } else if (data.status === 'armed') {
                // Auto-arm accepted. fetchEnrichmentStats will re-render the
                // button; keep a short confirmation in the status line.
                statusEl.textContent = data.message || 'Armed — will run when scraper/annotator finish.';
                statusEl.style.color = 'var(--color-warning)';
                btn.disabled = false;
                fetchEnrichmentStats();
            } else {
                statusEl.textContent = 'Error: ' + (data.message || data.error || 'Unknown error');
                statusEl.style.color = 'var(--color-danger)';
                btn.className = originalClass;
                btn.textContent = originalText;
                btn.disabled = false;
            }
        })
        .catch(err => {
            console.error('Failed to start consolidation:', err);
            statusEl.textContent = 'Failed to start consolidation.';
            statusEl.style.color = 'var(--color-danger)';
            btn.className = originalClass;
            btn.textContent = originalText;
            btn.disabled = false;
        });
}

function _applyArmableButton(btn, idleLabel, isArmed, blocking, workersRunning) {
    // Render a non-force consolidate button's idle/armed state. Leaves the
    // btn-has-pending class alone in the idle branch — it is owned by
    // checkConsolidationNeeded() — but clears it when armed.
    if (!btn) return;
    if (isArmed) {
        btn.dataset.armed = '1';
        btn.textContent = '⏳ Armed — click to cancel';
        btn.classList.add('action-btn', 'btn-armed-pulse');
        btn.classList.remove('btn-running', 'btn-has-pending');
        btn.title = blocking.length
            ? `Runs when ${blocking.join(', ')} finish.`
            : 'Runs when scraper/annotator finish.';
    } else {
        btn.dataset.armed = '';
        btn.textContent = idleLabel;
        btn.classList.add('action-btn');
        btn.classList.remove('btn-running', 'btn-armed-pulse');
        btn.title = workersRunning
            ? 'Click to arm — will run when scraper/annotator finish.'
            : '';
    }
    btn.disabled = false;
}

function applyConsolidateButtonState(data) {
    // Drive button styling off the latest enrichment-stats response.
    // Called from fetchEnrichmentStats every poll tick.
    const btnC = document.getElementById('btn-consolidate');
    const btnI = document.getElementById('btn-consolidate-incremental');
    const btnF = document.getElementById('btn-consolidate-force');
    if (!btnC || !btnF) return;

    const blocking = data.workers_blocking_consolidate || [];
    const armed = !!data.consolidate_auto_armed;
    // Both non-force buttons share the single armed slot. The saved refresh
    // preference tells us WHICH button is the armed one.
    const armedRefresh = !!data.consolidate_auto_armed_auto_refresh;
    const workersRunning = blocking.length > 0;
    const pipelineActive = !!data.consolidate_pipeline_active;

    // Force button: disabled while any worker runs OR the pipeline is in
    // flight (so users can't kick off a force rebuild while studies/timelines
    // are still refreshing). Reenables only when everything is idle.
    if (workersRunning || pipelineActive || _consolidatePollActive) {
        btnF.disabled = true;
        if (workersRunning) {
            btnF.title = `Wait for ${blocking.join(', ')} to finish.`;
        } else if (pipelineActive || _consolidatePollActive) {
            btnF.title = 'Wait for consolidation pipeline to finish.';
        }
    } else {
        btnF.disabled = false;
        btnF.title = '';
    }

    // Non-force buttons: tri-state (idle, armed, running).
    if (_consolidatePollActive) {
        // Polling loop owns the button text/state during an active run.
        return;
    }

    // Route the armed indicator to whichever button's mode is armed; the other
    // shows its normal idle label.
    _applyArmableButton(btnC, 'Consolidate & Refresh', armed && armedRefresh, blocking, workersRunning);
    _applyArmableButton(btnI, 'Consolidate Only', armed && !armedRefresh, blocking, workersRunning);
}

function pollConsolidationStatus() {
    // Poll for the consolidate pipeline. Considers consolidate_enrichment and
    // all downstream refresh steps as part of the same logical operation —
    // as long as any one of them is running, the UI stays in the "running"
    // state. Only exits when none are running AND the consolidate step has a
    // completion outcome.
    if (_consolidatePollActive) return;
    _consolidatePollActive = true;

    const statusEl = document.getElementById('consolidate-status');
    const btnC = document.getElementById('btn-consolidate');
    const btnI = document.getElementById('btn-consolidate-incremental');
    const btnF = document.getElementById('btn-consolidate-force');
    if (btnC) {
        btnC.disabled = true;
        btnC.textContent = 'Consolidating...';
        btnC.classList.add('action-btn', 'btn-running');
        btnC.classList.remove('btn-armed-pulse', 'btn-has-pending');
    }
    if (btnI) { btnI.disabled = true; btnI.classList.remove('btn-armed-pulse'); }
    if (btnF) btnF.disabled = true;

    // Hide the "scraper/annotator completed after last consolidation" warning
    // as soon as the run starts. fetchEnrichmentStats isn't called every tick
    // while polling, so without this the warning lingers through the run.
    const warningEl = document.getElementById('consolidate-warning');
    if (warningEl) warningEl.style.display = 'none';

    const interval = setInterval(() => {
        // Fetch both /api/status (for live step progress) and the enrichment
        // stats (for pipeline_in_flight across the gap between steps) each
        // tick. Light endpoints; we run this loop at 2s cadence only during
        // an active pipeline.
        Promise.all([
            fetch('/api/status').then(r => r.json()),
            fetch('/api/manage/enrichment/stats').then(r => r.json()),
        ])
            .then(([data, estats]) => {
                // Keep the per-step list live during the run.
                if (estats) renderPipelineSteps(estats.pipeline_steps);

                const active = _activePipelineStep(data);
                if (active) {
                    _renderStageText(statusEl, active.name, active.state.progress || {});
                    return;
                }

                // No step is currently "running". If the pipeline is still
                // flagged as in-flight, we're in the gap between steps —
                // keep polling (render a neutral placeholder).
                if (estats && estats.consolidate_pipeline_active) {
                    if (statusEl) {
                        statusEl.textContent = 'Advancing to next stage...';
                        statusEl.style.color = 'var(--color-text-secondary)';
                    }
                    return;
                }

                // Pipeline fully settled. Check the consolidate step's outcome.
                const consolidate = data.consolidate_enrichment;
                const outcome = consolidate && consolidate.last_run_outcome;
                if (!outcome) {
                    // Dispatcher may still be in flight — keep polling briefly.
                    return;
                }

                clearInterval(interval);
                _consolidatePollActive = false;

                if (btnC) {
                    btnC.classList.remove('btn-running', 'btn-armed-pulse');
                    btnC.classList.add('action-btn');
                    btnC.textContent = 'Consolidate & Refresh';
                    btnC.disabled = false;
                    btnC.dataset.armed = '';
                }
                if (btnI) {
                    btnI.classList.remove('btn-running', 'btn-armed-pulse');
                    btnI.classList.add('action-btn');
                    btnI.textContent = 'Consolidate Only';
                    btnI.disabled = false;
                    btnI.dataset.armed = '';
                }
                if (btnF) btnF.disabled = false;

                if (outcome === 'Success') {
                    // The persistent summary now lives in consolidate_stats
                    // (written by the orchestrator at pipeline end), so just
                    // refetching stats is enough — renderConsolidateStatus
                    // will render the "✓ Refreshed ..." line.
                    fetchEnrichmentStats();
                    if (typeof fetchStalenessStatus === 'function') fetchStalenessStatus();
                } else {
                    statusEl.textContent = 'Consolidation failed. Check logs.';
                    statusEl.style.color = 'var(--color-danger)';
                    renderConsolidationImpact(null);
                }
            })
            .catch(err => {
                console.error('Error polling consolidation status:', err);
                clearInterval(interval);
                _consolidatePollActive = false;
                if (btnC) {
                    btnC.className = 'action-btn';
                    btnC.textContent = 'Consolidate & Refresh';
                    btnC.disabled = false;
                }
                if (btnI) {
                    btnI.className = 'action-btn btn-discreet';
                    btnI.textContent = 'Consolidate Only';
                    btnI.disabled = false;
                }
                if (btnF) btnF.disabled = false;
            });
    }, 2000);
}

function fetchStalenessStatus() {
    return fetch('/api/manage/refresh/staleness')
        .then(res => res.json())
        .then(data => {
            if (!data.has_impact) {
                ['recode_refresh_studies', 'meta_refresh_groups', 'timelines_refresh', 'pca_refresh'].forEach(name => {
                    const el = document.getElementById(`${name}-stale`);
                    if (el) el.style.display = 'none';
                });
                // Authoritative signal that impact is gone — hide the panel
                // immediately so it doesn't linger while enrichment stats reload.
                if (typeof renderConsolidationImpact === 'function') {
                    renderConsolidationImpact(null);
                }
            } else {
                const procs = data.processes || {};
                for (const [name, info] of Object.entries(procs)) {
                    const el = document.getElementById(`${name}-stale`);
                    if (!el) continue;
                    if (info.stale) {
                        const count = info.affected ? info.affected.length : 0;
                        const unit = name === 'timelines_refresh' ? 'collection(s)' : 'study/studies';
                        el.textContent = `(${count} ${unit} need refresh)`;
                        el.style.display = '';
                    } else {
                        el.style.display = 'none';
                    }
                }
            }
            // A promoted preferred annotation version keeps the Study
            // Definitions refresh stale independently of any consolidation.
            const vp = data.version_promotion || {};
            if (vp.stale) {
                const el = document.getElementById('recode_refresh_studies-stale');
                if (el) {
                    el.textContent = el.style.display === '' && el.textContent
                        ? `${el.textContent} (+ new preferred annotation version)`
                        : '(new preferred annotation version — refresh needed)';
                    el.style.display = '';
                }
            }
        })
        .catch(err => console.error("Error fetching staleness:", err));
}

// Call on load
fetchEnrichmentStats();

const DM_PAGE_PERM_MAP = {
    'dm-page-ingestion':      'tab.data_management.ingestion',
    'dm-page-edit-activity':  'tab.data_management.edit_collections',
    'dm-page-studies':        'tab.data_management.studies',
    'dm-page-scrape':         'tab.data_management.scrape',
    'dm-page-annotation':     'tab.data_management.annotation',
    'dm-page-refresh':        'tab.data_management.refresh',
};

function openDataManagementPage(pageId, clickedItem) {
    // Defense in depth — refuse if the matching permission isn't granted.
    const requiredPerm = DM_PAGE_PERM_MAP[pageId];
    if (requiredPerm && Array.isArray(window.USER_PERMS) && !window.USER_PERMS.includes(requiredPerm)) {
        return;
    }

    document.querySelectorAll('#data_management .dm-page').forEach(page => {
        page.classList.remove('active');
    });

    // Deactivate all sidebar items
    document.querySelectorAll('#data_management .dm-sidebar-item').forEach(item => {
        item.classList.remove('active');
    });

    // Show selected page
    const page = document.getElementById(pageId);
    if (page) {
        page.classList.add('active');
    }

    // Activate clicked sidebar item
    if (clickedItem) {
        clickedItem.classList.add('active');
    }

    // Refresh enrichment stats when entering the Scrape, Annotation or Refresh
    // pages so queue counters and the consolidation-impact panel + pipeline
    // step list reflect current server state on navigation (they are otherwise
    // only re-rendered by event handlers, so they can show a stale snapshot
    // after navigating away and back).
    if (pageId === 'dm-page-scrape' || pageId === 'dm-page-annotation' || pageId === 'dm-page-refresh') {
        fetchEnrichmentStats();
    }

    // Fetch staleness status when entering the refresh page + apply cascade lock
    if (pageId === 'dm-page-refresh') {
        fetchStalenessStatus();
        if (_cascadeRefresh) {
            updateCascadeRefreshPageLock(true);
        }
    }

    // Populate the annotation-version dropdown on first visit to the
    // Annotation page.
    if (pageId === 'dm-page-annotation') {
        loadAnnotationVersionOptions();
    }

    // Lazy-load edit activity table on first visit
    if (pageId === 'dm-page-edit-activity') {
        const editContainer = document.getElementById('edit-activity-list-container');
        if (editContainer && editContainer.querySelectorAll('.edit-activity-item').length === 0) {
            if (typeof renderEditActivityTable === 'function') {
                renderEditActivityTable(editContainer);
            }
        }
    }

    if (typeof updateSubPageHash === 'function') {
        updateSubPageHash('data_management', pageId);
    }
}

// --- Data Ingestion Logic ---

let ingestionMetadata = { collection_ids: [], tags: [] };
let uploadSelectedTags = [];
let uploadPendingFiles = null;

// Client-side donation-zip slimming (zip.js): sources keyed by class so the
// upload modal knows which zip members its platform's ingester needs.
let _ingestionSourcesByClass = {};
let _uploadZipSuffixes = [];
let _uploadPreprocessing = false;
let _uploadBlockedFiles = [];
let _uploadSelectionGen = 0;

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
    // The ingestion sub-page only renders for users with
    // 'tab.data_management.ingestion'. Without it the endpoint aborts 403
    // (an HTML page that breaks res.json()), so skip the call entirely.
    if (!document.getElementById('dm-page-ingestion')) return;
    fetch('/api/manage/ingestion/sources')
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                renderIngestionSources(data.sources);
                renderPendingUploads(data.sources, data.total_pending || 0);
                updateProcessButton(data.total_pending || 0);
            } else {
                console.error("Failed to load ingestion sources:", data.error);
            }
        })
        .catch(err => console.error("Error loading ingestion sources:", err));
    loadStructureWarnings();
    loadIngestionHistory();
}

function loadStructureWarnings() {
    if (!document.getElementById('structure-warnings-panel')) return;
    fetch('/api/manage/ingestion/structure/warnings')
        .then(res => res.json())
        .then(data => renderStructureWarnings(data))
        .catch(err => console.error('Error loading structure warnings:', err));
}

function renderStructureWarnings(data) {
    const panel = document.getElementById('structure-warnings-panel');
    const listEl = document.getElementById('structure-warnings-list');
    const countEl = document.getElementById('structure-warnings-count');
    if (!panel || !listEl) return;

    const files = Array.isArray(data.files) ? data.files : [];
    if (files.length === 0) {
        panel.style.display = 'none';
        listEl.innerHTML = '';
        return;
    }

    panel.style.display = '';
    if (countEl) {
        const bits = [];
        if (data.n_quarantined > 0) bits.push(`${data.n_quarantined} quarantined`);
        if (data.n_warn > 0) bits.push(`${data.n_warn} warning${data.n_warn === 1 ? '' : 's'}`);
        countEl.textContent = bits.join(' · ');
    }

    listEl.innerHTML = '';
    files.forEach(f => {
        const isQuarantined = f.status === 'quarantined';
        const badgeColor = isQuarantined ? 'var(--color-danger)' : 'var(--color-warning)';
        const badgeLabel = isQuarantined ? 'quarantined' : 'warning';
        const provenance = [f.platform, f.source].filter(Boolean).join(' · ');
        const nFindings = (f.findings || []).length;

        const row = document.createElement('div');
        row.style.cssText = 'display: flex; align-items: center; gap: 12px; padding: 8px 12px; background: var(--color-bg-elevated); border-left: 3px solid ' + badgeColor + '; border-radius: 4px;';
        row.innerHTML = `
            <div style="flex: 1; min-width: 0;">
                <div class="text-sm" style="word-break: break-all;">
                    ${_escapeHtml(f.filename)}
                    <span class="text-xxs font-bold" style="color: ${badgeColor}; margin-left: 8px; text-transform: uppercase;">${badgeLabel}</span>
                </div>
                <div class="text-xxs" style="color: var(--color-text-tertiary);">
                    ${_escapeHtml(provenance)} · ${nFindings} finding${nFindings === 1 ? '' : 's'}
                </div>
            </div>
            <button type="button" class="action-btn text-xs" style="padding: 4px 10px;" data-role="review">Review</button>
            <button type="button" class="action-btn text-xs" style="padding: 4px 10px;" data-role="approve">Approve</button>
            <button type="button" class="btn-discreet text-xs" style="padding: 4px 10px;" data-role="reject">Reject</button>
        `;
        row.querySelector('[data-role="review"]').addEventListener('click', () => openStructureReviewModal(f));
        row.querySelector('[data-role="approve"]').addEventListener('click', (e) => approveStructureWarning(e.target, f.filename));
        row.querySelector('[data-role="reject"]').addEventListener('click', (e) => rejectStructureWarning(e.target, f.filename));
        listEl.appendChild(row);
    });
}

function _structureFindingHtml(finding) {
    const sevColor = finding.severity === 'quarantine' ? 'var(--color-danger)' : 'var(--color-warning)';
    const items = (finding.items || []).slice(0, 30);
    const itemsHtml = items.length
        ? `<ul class="text-xxs" style="margin: 4px 0 0 0; padding-left: 18px; color: var(--color-text-secondary); font-family: var(--font-mono); word-break: break-all;">
               ${items.map(i => `<li>${_escapeHtml(i)}</li>`).join('')}
           </ul>`
        : '';
    const statLine = finding.metric !== undefined
        ? `<div class="text-xxs" style="color: var(--color-text-secondary); margin-top: 2px;">
               value ${_escapeHtml(finding.value)} vs baseline mean ${_escapeHtml(finding.baseline_mean)}
               (range ${_escapeHtml(finding.baseline_min)}–${_escapeHtml(finding.baseline_max)}, z = ${_escapeHtml(finding.z)})
           </div>`
        : '';
    return `
        <div style="padding: 8px 10px; border-left: 3px solid ${sevColor}; background: var(--color-bg-input); border-radius: 4px;">
            <div class="text-sm">
                <span class="text-xxs font-bold" style="color: ${sevColor}; text-transform: uppercase; margin-right: 8px;">${_escapeHtml(finding.severity)}</span>
                ${_escapeHtml(finding.detail || finding.code)}
            </div>
            ${statLine}
            ${itemsHtml}
        </div>
    `;
}

function openStructureReviewModal(verdict) {
    const existing = document.getElementById('structureReviewModal');
    if (existing) existing.remove();

    const structureFindings = (verdict.findings || []).filter(f => f.layer === 'structure');
    const statFindings = (verdict.findings || []).filter(f => f.layer === 'stats');
    const raw = verdict.raw_stats || {};
    const processed = verdict.processed_stats || {};

    const section = (title, bodyHtml) => `
        <div style="margin-bottom: 16px;">
            <div class="text-sm font-semibold" style="margin-bottom: 6px; color: var(--color-text-secondary);">${title}</div>
            ${bodyHtml}
        </div>
    `;
    const none = '<div class="text-xs" style="color: var(--color-text-tertiary);">No findings.</div>';
    const statsSummary = [
        raw.raw_rows !== undefined ? `${Number(raw.raw_rows).toLocaleString()} raw rows` : null,
        raw.file_size_mb !== undefined ? `${raw.file_size_mb} MB` : null,
        processed.kept_ratio !== undefined ? `kept ratio ${processed.kept_ratio}` : null,
        processed.null_item_id_frac !== undefined ? `null item_id ${processed.null_item_id_frac}` : null,
    ].filter(Boolean).join(' · ');

    const overlay = document.createElement('div');
    overlay.id = 'structureReviewModal';
    overlay.className = 'upload-modal-overlay';
    overlay.innerHTML = `
        <div class="upload-modal" style="max-width: 640px; max-height: 80vh; overflow-y: auto;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                <h3 style="margin: 0; word-break: break-all;">Structure review — ${_escapeHtml(verdict.filename)}</h3>
                <button type="button" class="btn-discreet" data-role="close">&times;</button>
            </div>
            <div class="text-xs" style="color: var(--color-text-tertiary); margin-bottom: 16px;">
                ${_escapeHtml([verdict.platform, verdict.source].filter(Boolean).join(' · '))}
                · evaluated ${_escapeHtml(fypFmtDateTime(verdict.ts_evaluated))}
                ${statsSummary ? ' · ' + _escapeHtml(statsSummary) : ''}
            </div>
            ${section('Structure changes', structureFindings.length ? structureFindings.map(_structureFindingHtml).join('') : none)}
            ${section('Parse sanity & drift', statFindings.length ? statFindings.map(_structureFindingHtml).join('') : none)}
            <div style="display: flex; gap: 8px; justify-content: flex-end; margin-top: 8px;">
                <button type="button" class="action-btn" data-role="approve">Approve — accept structure</button>
                <button type="button" class="btn-discreet" data-role="reject">Reject — exclude file</button>
            </div>
        </div>
    `;
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
    overlay.querySelector('[data-role="close"]').addEventListener('click', () => overlay.remove());
    overlay.querySelector('[data-role="approve"]').addEventListener('click', (e) => {
        approveStructureWarning(e.target, verdict.filename, () => overlay.remove());
    });
    overlay.querySelector('[data-role="reject"]').addEventListener('click', (e) => {
        rejectStructureWarning(e.target, verdict.filename, () => overlay.remove());
    });
    document.body.appendChild(overlay);
    overlay.style.display = 'flex';
}

async function _postStructureReview(btn, endpoint, filename, confirmMessage, done) {
    if (!(await showAppConfirm(confirmMessage, { title: 'Structure review' }))) return;
    btn.disabled = true;
    fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ filename: filename }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                showToast(data.message, 'success', 7000);
                if (done) done();
                loadStructureWarnings();
                loadIngestionSources();
            } else {
                btn.disabled = false;
                showToast('Structure review failed: ' + (data.error || data.message || 'Unknown error'), 'error', 7000);
            }
        })
        .catch(err => {
            btn.disabled = false;
            console.error('Structure review error:', err);
            showToast('Structure review request failed.', 'error', 7000);
        });
}

function approveStructureWarning(btn, filename, done) {
    _postStructureReview(
        btn,
        '/api/manage/ingestion/structure/approve',
        filename,
        `Approve '${filename}'?\n\nIts structure becomes part of the accepted baseline and the file will be ingested on the next refresh.`,
        done
    );
}

function rejectStructureWarning(btn, filename, done) {
    _postStructureReview(
        btn,
        '/api/manage/ingestion/structure/reject',
        filename,
        `Reject '${filename}'?\n\nThe file is marked manually excluded and will never be ingested (you can un-skip it later from the ledger).`,
        done
    );
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
    const cancelBtn = document.getElementById('clearPendingUploadsBtn');
    if (cancelBtn) {
        cancelBtn.style.display = totalPending > 0 ? '' : 'none';
        cancelBtn.disabled = totalPending === 0;
    }
}

function renderPendingUploads(sources, totalPending) {
    const panel = document.getElementById('pending-uploads-panel');
    const listEl = document.getElementById('pending-uploads-list');
    const emptyEl = document.getElementById('pending-uploads-empty');
    if (!panel || !listEl || !emptyEl) return;

    if (totalPending === 0) {
        panel.style.display = 'none';
        emptyEl.style.display = '';
        listEl.innerHTML = '';
        return;
    }

    panel.style.display = '';
    emptyEl.style.display = 'none';
    listEl.innerHTML = '';

    const sourcesWithFiles = sources.filter(s => (s.files || []).length > 0);
    sourcesWithFiles.forEach(source => {
        const block = document.createElement('div');
        const fileItems = source.files.map(f => {
            const tagSuffix = (f.tags && f.tags.length)
                ? ` <span class="text-xxs" style="color: var(--color-text-tertiary);">[${f.tags.join(', ')}]</span>`
                : '';
            const cidSuffix = f.collection_id
                ? ` <span class="text-xxs" style="color: var(--color-text-tertiary);">→ ${f.collection_id}</span>`
                : '';
            return `<li style="margin-left: 16px; word-break: break-all;">${f.filename}${cidSuffix}${tagSuffix}</li>`;
        }).join('');
        block.innerHTML = `
            <div class="text-sm font-semibold" style="margin-bottom: 4px;">
                ${source.class_name}
                <span class="text-xs" style="color: var(--color-text-tertiary); font-weight: var(--weight-normal);">
                    (${source.files.length})
                </span>
            </div>
            <ul class="text-sm" style="margin: 0; padding: 0; list-style: disc inside;">
                ${fileItems}
            </ul>
        `;
        listEl.appendChild(block);
    });
}

let _toastContainer = null;

function showToast(message, level = 'success', duration = 5000) {
    if (!_toastContainer) {
        _toastContainer = document.createElement('div');
        _toastContainer.id = 'dm-toast-container';
        _toastContainer.style.cssText = 'position: fixed; bottom: 24px; right: 24px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; pointer-events: none;';
        document.body.appendChild(_toastContainer);
    }

    const colorVar = level === 'error'
        ? 'var(--color-danger)'
        : level === 'warning'
            ? 'var(--color-warning)'
            : 'var(--color-success-light, var(--color-text-primary))';

    const toast = document.createElement('div');
    toast.className = 'text-sm';
    toast.style.cssText = `
        background: var(--color-bg-elevated, var(--color-bg-input));
        color: var(--color-text-primary);
        border-left: 4px solid ${colorVar};
        padding: 12px 16px;
        border-radius: 4px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.25);
        max-width: 420px;
        opacity: 0;
        transform: translateX(20px);
        transition: opacity 0.2s ease, transform 0.2s ease;
        pointer-events: auto;
    `;
    toast.textContent = message;
    _toastContainer.appendChild(toast);

    // Defer the target opacity/transform to a later tick so the browser has
    // a chance to paint the initial (faded) state first. Setting them in the
    // same tick — even after a reflow — gets collapsed into a single paint
    // by Chrome and the transition is skipped.
    setTimeout(() => {
        toast.style.opacity = '1';
        toast.style.transform = 'translateX(0)';
    }, 16);

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(20px)';
        setTimeout(() => toast.remove(), 250);
    }, duration);
}

async function startDemoDataset() {
    const ok = await showAppConfirm(
        'Generate the synthetic demo dataset?\n\n' +
        'This installs 5 synthetic donor files (as pending "TikTok demo" uploads), a ' +
        'fabricated scrape batch and annotations, and creates the "Demo study ' +
        '(synthetic data)" definition if it does not exist. Deterministic and safe to ' +
        're-run — a repeat overwrites the same artifacts.\n\n' +
        'Afterwards, run Process New Collections here, then Consolidate & Refresh.',
        { title: 'Generate demo dataset', okLabel: 'Generate' }
    );
    if (!ok) return;
    startProcess('demo_dataset');
}

async function clearPendingUploads(btn) {
    const ok = await showAppConfirm(
        'Cancel all pending uploads?\n\n' +
        'This deletes the staged raw files from storage and clears every ingestion manifest. ' +
        'The action cannot be undone.',
        { title: 'Cancel pending uploads', okLabel: 'Cancel uploads', danger: true }
    );
    if (!ok) return;

    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Cancelling...';

    fetch('/api/manage/ingestion/clear_pending', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
    })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                const removed = data.total_removed || 0;
                const failed = (data.failures || []).length;
                if (failed > 0) {
                    showToast(`Cancelled ${removed} pending upload(s); ${failed} failure(s) — check logs.`, 'warning', 7000);
                } else {
                    showToast(`Cancelled ${removed} pending upload(s).`, 'success');
                }
                loadIngestionSources();
            } else {
                showToast('Failed to cancel pending uploads: ' + (data.error || data.message || 'Unknown error'), 'error', 7000);
            }
        })
        .catch(err => {
            console.error('Error cancelling pending uploads:', err);
            showToast('Error cancelling pending uploads.', 'error', 7000);
        })
        .finally(() => {
            btn.textContent = originalText;
            // updateProcessButton called by loadIngestionSources will set disabled
        });
}
window.clearPendingUploads = clearPendingUploads;

function renderIngestionSources(sources) {
    _ingestionSourcesByClass = {};
    sources.forEach(source => { _ingestionSourcesByClass[source.class_name] = source; });

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

        let buttonsHtml;
        if (source.ingestion_mode === 'fetch') {
            buttonsHtml = `
                <div style="display: flex; align-items: center; gap: 8px;">
                    <label class="text-sm" style="white-space: nowrap;">Days back:</label>
                    <input type="number" class="aio-days-back text-sm" value="1" min="1" max="365"
                        style="width: 60px; padding: 4px 8px; background: var(--color-bg-input); color: var(--color-text-primary); border: 1px solid var(--color-border); border-radius: 4px;">
                    <button type="button" class="action-btn" onclick="fetchAIOData(this)">
                        Fetch from AWS
                    </button>
                </div>
            `;
        } else {
            buttonsHtml = `
                <button type="button" class="action-btn" onclick="openUploadModal('${source.class_name}', '${source.raw_path}', 'files')">
                    Add Files
                </button>
                <button type="button" class="action-btn" onclick="openUploadModal('${source.class_name}', '${source.raw_path}', 'folder')">
                    Add Folder
                </button>
            `;
        }

        card.innerHTML = `
            <div class="font-bold text-body" style="margin-bottom: 5px;">${source.class_name}${pendingBadge}</div>
            <div class="text-sm" style="color: var(--color-text-tertiary); margin-bottom: 15px;">
                <strong>Platform:</strong> ${source.source_platform} | <strong>Source:</strong> ${source.data_source}
            </div>
            <div style="margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap;">
                ${buttonsHtml}
            </div>
        `;

        container.appendChild(card);
    });
}


let _aioFetchPollActive = false;

function fetchAIOData(btn) {
    const card = btn.closest('.ingest-card');
    const daysInput = card.querySelector('.aio-days-back');
    const daysBack = Math.max(1, parseInt(daysInput.value, 10) || 1);
    const hoursBack = daysBack * 24;

    const originalText = btn.textContent;
    btn.textContent = 'Fetching...';
    btn.disabled = true;

    const restoreButton = () => {
        btn.textContent = originalText;
        btn.disabled = false;
    };

    fetch('/api/manage/ingestion/fetch_aio', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ hours_back: hoursBack })
    })
    .then(res => res.json())
    .then(data => {
        if (data.status === 'started') {
            pollAioFetchStatus(btn, originalText);
        } else {
            console.error('AIO fetch error:', data.message || data.error);
            restoreButton();
        }
    })
    .catch(err => {
        console.error('Error fetching AIO data:', err);
        restoreButton();
    });
}

function pollAioFetchStatus(btn, originalText) {
    if (_aioFetchPollActive) return;
    _aioFetchPollActive = true;
    let done = false;

    const interval = setInterval(() => {
        if (done) return;
        fetch('/api/status')
            .then(r => r.json())
            .then(statusData => {
                if (done) return;
                const af = statusData.aio_fetch;
                if (!af) return;

                if (af.state === 'running') {
                    const msg = af.progress && af.progress.message;
                    const pct = af.progress && af.progress.percent;
                    if (msg) {
                        btn.textContent = pct != null
                            ? `Fetching... ${pct}% — ${msg}`
                            : `Fetching... ${msg}`;
                    }
                    return;
                }

                done = true;
                clearInterval(interval);
                _aioFetchPollActive = false;
                btn.textContent = originalText;
                btn.disabled = false;

                const data = af.data || {};
                if (af.last_run_outcome === 'Fail') {
                    console.error('AIO fetch failed.');
                    showToast('AWS fetch failed. Check the task logs.', 'error', 7000);
                } else {
                    const found = data.donations_found || 0;
                    const uploaded = data.donations_uploaded || 0;
                    if (found === 0) {
                        showToast('AWS fetch: no new donations in the selected window.', 'success');
                    } else {
                        showToast(`AWS fetch: ${uploaded} donation(s) uploaded (${found} found).`, 'success');
                    }
                }
                loadIngestionSources();
            })
            .catch(err => {
                console.error('Error polling aio_fetch status:', err);
            });
    }, 2000);
}


// --- Upload Modal ---

let _uploadMode = 'files';

function openUploadModal(className, rawPath, mode) {
    // Reset state
    uploadSelectedTags = [];
    uploadPendingFiles = null;
    _uploadMode = mode;
    _uploadZipSuffixes = (_ingestionSourcesByClass[className] || {}).zip_member_suffixes || [];
    _uploadPreprocessing = false;
    _uploadBlockedFiles = [];
    _uploadSelectionGen++;  // invalidate any zip scan still running from a previous modal
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
    document.getElementById('uploadDonorTz').value = '';
    document.getElementById('uploadModalTitle').textContent = `Add to ${className}`;

    // Reset radio to default
    document.querySelector('input[name="collectionIdMode"][value="per_file"]').checked = true;

    // Show modal immediately — the existing-collection dropdown is only
    // needed when the user picks the 'existing' radio, so we can populate
    // it asynchronously without blocking the modal paint.
    const sel = document.getElementById('uploadExistingCollectionId');
    sel.innerHTML = '<option value="">Loading collections...</option>';
    sel.disabled = true;
    document.getElementById('uploadModal').style.display = 'flex';

    loadIngestionMetadata().then(() => {
        sel.innerHTML = '';
        sel.disabled = false;
        const displayIds = ingestionMetadata.display_ids || {};
        const entries = ingestionMetadata.collection_ids.map(id => {
            const disp = displayIds[id];
            return {
                id,
                label: disp && disp !== id ? `${disp} (${id})` : id,
            };
        });
        entries.sort((a, b) => a.label.localeCompare(b.label));
        entries.forEach(({ id, label }) => {
            const opt = document.createElement('option');
            opt.value = id;
            opt.textContent = label;
            sel.appendChild(opt);
        });
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

// --- Client-side donation-zip slimming (zip.js) ---

let _zipLibPromise = null;

function loadZipLib() {
    // Lazy one-time injection of the vendored zip.js UMD build — nothing is
    // loaded for platforms whose uploads are consumed whole (e.g. TikTok).
    if (window.zip) return Promise.resolve(window.zip);
    if (!_zipLibPromise) {
        _zipLibPromise = new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = '/static/js/vendor/zip-full.min.js';
            script.onload = () => {
                window.zip.configure({ useWebWorkers: false });
                resolve(window.zip);
            };
            script.onerror = () => {
                _zipLibPromise = null;
                reject(new Error('zip.js failed to load'));
            };
            document.head.appendChild(script);
        });
    }
    return _zipLibPromise;
}

function formatBytes(bytes) {
    if (!(bytes >= 0)) return '?';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
    return `${bytes >= 100 || i === 0 ? Math.round(bytes) : bytes.toFixed(1)} ${units[i]}`;
}

async function repackDonationZip(file, suffixes, onStatus) {
    // Rebuild the donation zip with only the members the server ingester
    // needs, mirroring fyp.utils.read_zip_members semantics: path-suffix
    // matching, directory entries skipped, first match per suffix wins.
    // Member paths and the upload filename are preserved so the server-side
    // ingest path is untouched.
    let zipLib;
    try {
        zipLib = await loadZipLib();
    } catch (err) {
        console.warn('zip.js unavailable, uploading original file:', err);
        return { file, action: 'passthrough' };
    }
    let reader = null;
    try {
        reader = new zipLib.ZipReader(new zipLib.BlobReader(file));
        const entries = await reader.getEntries();
        const remaining = new Set(suffixes);
        const matches = [];
        for (const entry of entries) {
            if (entry.directory || remaining.size === 0) continue;
            for (const suffix of remaining) {
                if (entry.filename.endsWith(suffix)) {
                    matches.push(entry);
                    remaining.delete(suffix);
                    break;
                }
            }
        }
        if (matches.length === 0) return { file, action: 'blocked' };

        const writer = new zipLib.ZipWriter(new zipLib.BlobWriter('application/zip'));
        for (const entry of matches) {
            onStatus(`Extracting ${entry.filename}...`);
            const blob = await entry.getData(new zipLib.BlobWriter());
            await writer.add(entry.filename, new zipLib.BlobReader(blob));
        }
        const outBlob = await writer.close();
        const outFile = new File([outBlob], file.name,
            { type: 'application/zip', lastModified: file.lastModified });
        return { file: outFile, action: 'repacked', originalSize: file.size, newSize: outFile.size };
    } catch (err) {
        console.warn(`zip repack failed for ${file.name}, uploading original:`, err);
        return { file, action: 'passthrough' };
    } finally {
        if (reader) { try { await reader.close(); } catch (err) { /* already closed */ } }
    }
}

function renderUploadFileList(lines) {
    const listDiv = document.getElementById('uploadFilesList');
    listDiv.innerHTML = lines.join('') +
        `<div class="text-xs" style="margin-top: 6px; color: var(--color-accent); cursor: pointer;" onclick="triggerFilePicker()">Change selection...</div>`;
}

async function handleFilesSelected(files) {
    if (!files || files.length === 0) return;
    const fileArray = Array.from(files);
    const statusDiv = document.getElementById('uploadStatus');
    const submitBtn = document.getElementById('uploadSubmitBtn');
    _uploadBlockedFiles = [];

    // Platforms without a member list — and whole folder trees — pass through
    // untouched; slimming applies only to file-mode .zip selections.
    const slim = _uploadZipSuffixes.length > 0 && _uploadMode !== 'folder';
    if (!slim) {
        uploadPendingFiles = fileArray;
        let lines;
        if (fileArray.length <= 10) {
            lines = fileArray.map(f => `<div class="text-xs" style="padding: 2px 0;">${f.name}</div>`);
        } else {
            lines = [`<div class="text-sm">${fileArray.length} files selected</div>`,
                ...fileArray.slice(0, 5).map(f =>
                    `<div class="text-xs" style="padding: 2px 0; color: var(--color-text-tertiary);">${f.name}</div>`),
                `<div class="text-xs" style="color: var(--color-text-tertiary);">... and ${fileArray.length - 5} more</div>`];
        }
        renderUploadFileList(lines);
        return;
    }

    const generation = ++_uploadSelectionGen;
    _uploadPreprocessing = true;
    submitBtn.disabled = true;
    statusDiv.style.display = 'block';
    statusDiv.style.color = 'var(--color-text-tertiary)';
    document.getElementById('uploadFilesList').innerHTML =
        `<div class="text-xs" style="padding: 2px 0; color: var(--color-text-tertiary);">Scanning selection...</div>`;

    const zipCount = fileArray.filter(f => /\.zip$/i.test(f.name)).length;
    let zipIndex = 0;
    const processed = [];
    const lines = [];
    for (const file of fileArray) {
        if (!/\.zip$/i.test(file.name)) {
            processed.push(file);
            lines.push(`<div class="text-xs" style="padding: 2px 0;">${file.name} ` +
                `<span style="color: var(--color-text-tertiary);">(uploaded as-is)</span></div>`);
            continue;
        }
        zipIndex++;
        statusDiv.textContent = `Scanning donation zip ${zipIndex}/${zipCount}: ${file.name}...`;
        const result = await repackDonationZip(file, _uploadZipSuffixes,
            msg => { statusDiv.textContent = `[${zipIndex}/${zipCount}] ${msg}`; });
        if (generation !== _uploadSelectionGen) return;  // selection changed mid-scan
        if (result.action === 'blocked') {
            _uploadBlockedFiles.push(file.name);
            lines.push(`<div class="text-xs" style="padding: 2px 0; color: var(--color-danger);">${file.name} ` +
                `— no matching donation files found in this zip</div>`);
        } else if (result.action === 'repacked') {
            processed.push(result.file);
            lines.push(`<div class="text-xs" style="padding: 2px 0;">${file.name} ` +
                `<span style="color: var(--color-success-light);">(repacked ` +
                `${formatBytes(result.originalSize)} → ${formatBytes(result.newSize)})</span></div>`);
        } else {
            processed.push(result.file);
            lines.push(`<div class="text-xs" style="padding: 2px 0;">${file.name} ` +
                `<span style="color: var(--color-text-tertiary);">(uploaded as-is)</span></div>`);
        }
    }

    uploadPendingFiles = processed;
    _uploadPreprocessing = false;
    renderUploadFileList(lines);

    if (_uploadBlockedFiles.length > 0) {
        statusDiv.textContent = `Cannot upload: ${_uploadBlockedFiles.join(', ')} ` +
            `contain${_uploadBlockedFiles.length === 1 ? 's' : ''} none of the files this platform needs. ` +
            `Change the selection or pick another export.`;
        statusDiv.style.color = 'var(--color-danger)';
        submitBtn.disabled = true;
    } else {
        statusDiv.textContent = '';
        statusDiv.style.display = 'none';
        submitBtn.disabled = false;
    }
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
    if (_uploadPreprocessing) {
        showAppAlert('Still scanning the selected donation zip(s) — one moment.');
        return;
    }
    if (_uploadBlockedFiles.length > 0) {
        showAppAlert('Some selected zips contain none of the files this platform needs. Change the selection first.');
        return;
    }
    if (!uploadPendingFiles || uploadPendingFiles.length === 0) {
        showAppAlert('Please select files first.');
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
            showAppAlert('Please select an existing collection.');
            return;
        }
    } else if (mode === 'new') {
        collectionId = document.getElementById('uploadNewCollectionId').value.trim();
        collectionIdMode = 'single';
        if (!collectionId) {
            showAppAlert('Please enter a collection ID.');
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
    formData.append('tz', document.getElementById('uploadDonorTz').value.trim());

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
                const files = data.files || [];
                const preview = files.slice(0, 3).join(', ');
                const more = files.length > 3 ? `, +${files.length - 3} more` : '';
                showToast(
                    `Added ${files.length} file(s)${files.length ? ': ' + preview + more : ''}.`,
                    'success'
                );
                statusDiv.textContent = data.message;
                statusDiv.style.color = 'var(--color-success-light)';
                loadIngestionSources();
                setTimeout(() => closeUploadModal(), 800);
            } else {
                statusDiv.textContent = 'Error: ' + data.error;
                statusDiv.style.color = 'var(--color-danger)';
                submitBtn.disabled = false;
                showToast('Upload failed: ' + (data.error || 'Unknown error'), 'error', 7000);
            }
        })
        .catch(err => {
            statusDiv.textContent = 'Upload failed.';
            statusDiv.style.color = 'var(--color-danger)';
            submitBtn.disabled = false;
            showToast('Upload failed.', 'error', 7000);
        });
}


let _ingestRefreshPollActive = false;

window.refreshIngestionCollection = function (btn) {
    const originalText = btn.textContent;
    const originalClass = btn.className;
    btn.textContent = "Processing...";
    btn.disabled = true;
    btn.className = 'btn-running';

    const restoreButton = () => {
        btn.className = originalClass;
        btn.textContent = originalText;
        btn.disabled = false;
    };

    fetch('/api/manage/ingestion/refresh', {
        method: 'POST',
        headers: { 'X-CSRFToken': csrfToken }
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'started') {
                pollIngestRefreshStatus(btn, originalText, originalClass);
            } else {
                console.error("Ingestion refresh error:", data.message || data.error);
                restoreButton();
            }
        })
        .catch(err => {
            console.error("Error triggering refresh:", err);
            restoreButton();
        });
}

const _ingestOutcomeLabels = {
    added_as_new: { label: 'Added to new collection', color: 'var(--color-text-primary)' },
    merged_with_existing: { label: 'Added to existing collection', color: 'var(--color-text-primary)' },
    fully_deduped: { label: 'Skipped — already in dataset', color: 'var(--color-text-tertiary)' },
    discarded_at_load: { label: 'Skipped — too few rows', color: 'var(--color-text-tertiary)' },
    manually_excluded: { label: 'Manually excluded', color: 'var(--color-text-tertiary)' },
    quarantined_structure: { label: 'Quarantined — structure drift (review above)', color: 'var(--color-danger)' },
    load_failed: { label: 'Failed to read — will retry next refresh', color: 'var(--color-danger)' },
};

// Plain-language labels for the per-file drop-reason breakdown captured by
// the ingest load loop (ledger key: dropped = {reason: count}).
const _ingestDropReasonLabels = {
    not_parseable: (n) => `${n.toLocaleString()} row${n === 1 ? '' : 's'} couldn't be interpreted (unreadable timestamp or missing video reference)`,
    missing_required: (n) => `${n.toLocaleString()} row${n === 1 ? '' : 's'} ${n === 1 ? 'was' : 'were'} missing essential information and ${n === 1 ? 'was' : 'were'} excluded`,
};

function _ingestDropLines(r) {
    const lines = [];
    const dropped = r.dropped || {};
    Object.keys(dropped).forEach(reason => {
        const n = Number(dropped[reason]) || 0;
        if (n <= 0) return;
        const fmt = _ingestDropReasonLabels[reason];
        lines.push(fmt ? fmt(n) : `${n.toLocaleString()} rows dropped (${reason})`);
    });
    if ((r.deduped_rows ?? 0) > 0) {
        lines.push(`${r.deduped_rows.toLocaleString()} row${r.deduped_rows === 1 ? '' : 's'} duplicated activity already in the archive`);
    }
    return lines;
}

function _ingestDroppedCellHtml(r) {
    // Entries written before the drop-stats extension have no `dropped` key —
    // render an em-dash rather than implying "nothing was dropped".
    if (r.dropped === undefined && r.deduped_rows === undefined) {
        return '<span style="color: var(--color-text-tertiary);">—</span>';
    }
    const lines = _ingestDropLines(r);
    if (lines.length === 0) return '<span style="color: var(--color-text-tertiary);">0</span>';
    return lines
        .map(l => `<div class="text-xxs" style="color: var(--color-text-tertiary); white-space: normal;">${_escapeHtml(l)}</div>`)
        .join('');
}

function _formatSiblings(siblings) {
    if (!siblings || siblings.length === 0) return '';
    if (siblings.length === 1) return siblings[0];
    return `${siblings[0]} (+${siblings.length - 1} more)`;
}

function _escapeHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function unskipIngestionFile(btn, filename) {
    if (!filename) return;
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = 'Un-skipping...';
    fetch('/api/manage/ingestion/ledger/unskip', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken,
        },
        body: JSON.stringify({ filename: filename }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success' || data.status === 'noop') {
                const row = btn.closest('tr');
                if (row) row.style.opacity = '0.4';
                btn.textContent = 'Un-skipped';
            } else {
                btn.disabled = false;
                btn.textContent = originalText;
                console.error('Un-skip failed:', data);
            }
        })
        .catch(err => {
            btn.disabled = false;
            btn.textContent = originalText;
            console.error('Un-skip error:', err);
        });
}

function renderIngestResultsPanel(data) {
    const panel = document.getElementById('ingest-results-panel');
    const summaryEl = document.getElementById('ingest-results-summary');
    const wrap = document.getElementById('ingest-results-table-wrap');
    const reconcileEl = document.getElementById('ingest-results-reconciliation');
    const skippedWrapEl = document.getElementById('ingest-results-skipped');
    const skippedCountEl = document.getElementById('ingest-results-skipped-count');
    const skippedTableEl = document.getElementById('ingest-results-skipped-wrap');
    if (!panel || !summaryEl || !wrap) return;

    const perFile = Array.isArray(data.per_file_summary) ? data.per_file_summary : [];
    const skippedPreviously = Array.isArray(data.skipped_previously) ? data.skipped_previously : [];

    if (perFile.length === 0 && skippedPreviously.length === 0) {
        panel.style.display = 'block';
        summaryEl.textContent = '';
        wrap.innerHTML = '<div class="text-sm" style="color: var(--color-text-tertiary); padding: 8px 0;">No new files were scanned.</div>';
        if (reconcileEl) reconcileEl.style.display = 'none';
        if (skippedWrapEl) skippedWrapEl.style.display = 'none';
        return;
    }

    const rowsBefore = data.rows_before;
    const rowsAfter = data.rows_after;
    const rowsAdded = data.rows_added;
    const filesAdded = data.files_added ?? perFile.filter(r => r.outcome === 'added_as_new').length;
    const filesMerged = data.files_merged_with_existing ?? perFile.filter(r => r.outcome === 'merged_with_existing').length;
    const filesDeduped = data.files_fully_deduped ?? perFile.filter(r => r.outcome === 'fully_deduped').length;
    const filesDiscarded = data.files_discarded_at_load ?? perFile.filter(r => r.outcome === 'discarded_at_load').length;
    const filesSkippedPrev = data.files_skipped_previously ?? skippedPreviously.length;

    const summaryBits = [];
    if (typeof rowsBefore === 'number' && typeof rowsAfter === 'number') {
        const sign = (rowsAdded ?? 0) >= 0 ? '+' : '';
        summaryBits.push(`${rowsBefore.toLocaleString()} → ${rowsAfter.toLocaleString()} rows (${sign}${(rowsAdded ?? 0).toLocaleString()})`);
    }
    const scanned = perFile.length;
    const groupBits = [];
    if (filesAdded > 0) groupBits.push(`${filesAdded} added`);
    if (filesMerged > 0) groupBits.push(`${filesMerged} merged into existing collection`);
    if (filesDeduped > 0) groupBits.push(`${filesDeduped} fully deduped`);
    if (filesDiscarded > 0) groupBits.push(`${filesDiscarded} discarded (too few rows)`);
    const filesQuarantined = data.files_quarantined ?? perFile.filter(r => r.outcome === 'quarantined_structure').length;
    if (filesQuarantined > 0) groupBits.push(`${filesQuarantined} quarantined (structure drift)`);
    const filesLoadFailed = data.files_load_failed ?? perFile.filter(r => r.outcome === 'load_failed').length;
    if (filesLoadFailed > 0) groupBits.push(`${filesLoadFailed} unreadable (will retry)`);
    if (scanned > 0) {
        summaryBits.push(`Scanned ${scanned} file${scanned === 1 ? '' : 's'}${groupBits.length ? ': ' + groupBits.join(', ') : ''}`);
    }
    if (filesSkippedPrev > 0) {
        summaryBits.push(`${filesSkippedPrev} skipped (previously known)`);
    }
    summaryEl.textContent = '— ' + summaryBits.join(' · ');

    const thStyle = 'padding: 6px 8px; text-align: left; border-bottom: 2px solid var(--color-border-strong); font-weight: var(--weight-semibold);';
    const tdStyle = 'padding: 6px 8px; border-bottom: 1px solid var(--color-border); vertical-align: top;';
    const numStyle = tdStyle + ' text-align: right; font-variant-numeric: tabular-nums;';

    if (perFile.length === 0) {
        wrap.innerHTML = '<div class="text-sm" style="color: var(--color-text-tertiary); padding: 8px 0;">No new files were scanned this run.</div>';
    } else {
        const rowsHtml = perFile.map(r => {
            const meta = _ingestOutcomeLabels[r.outcome] || { label: r.outcome, color: 'var(--color-text-secondary)' };
            const provenance = [r.platform, r.source].filter(Boolean).join(' · ');
            const cidLine = r.canonical_collection_id
                ? `<div class="text-xxs" style="color: var(--color-text-tertiary);">collection: ${_escapeHtml(r.canonical_collection_id)}</div>`
                : '';
            const siblingsLine = (r.outcome === 'merged_with_existing' && r.merged_with_siblings && r.merged_with_siblings.length)
                ? `<div class="text-xxs" style="color: var(--color-text-tertiary);">joined with: ${_escapeHtml(_formatSiblings(r.merged_with_siblings))}</div>`
                : '';
            const notesLine = ((r.outcome === 'quarantined_structure' || r.outcome === 'load_failed') && r.notes)
                ? `<div class="text-xxs" style="color: var(--color-text-tertiary);">${_escapeHtml(r.notes)}</div>`
                : '';
            return `
                <tr>
                    <td style="${tdStyle}">
                        <div class="text-sm" style="word-break: break-all;">${_escapeHtml(r.filename)}</div>
                        ${provenance ? `<div class="text-xxs" style="color: var(--color-text-tertiary);">${_escapeHtml(provenance)}</div>` : ''}
                    </td>
                    <td style="${tdStyle} color: ${meta.color};">
                        <div class="text-sm">${meta.label}</div>
                        ${siblingsLine}
                        ${cidLine}
                        ${notesLine}
                    </td>
                    <td style="${numStyle}">${(r.raw_rows ?? 0).toLocaleString()}</td>
                    <td style="${numStyle}">${(r.processed_rows ?? 0).toLocaleString()}</td>
                    <td style="${numStyle}">${(r.final_rows ?? 0).toLocaleString()}</td>
                    <td style="${tdStyle} max-width: 260px;">${_ingestDroppedCellHtml(r)}</td>
                </tr>
            `;
        }).join('');

        wrap.innerHTML = `
            <table class="text-sm" style="width: 100%; border-collapse: collapse; min-width: 860px;">
                <thead>
                    <tr>
                        <th style="${thStyle}">File</th>
                        <th style="${thStyle}">Outcome</th>
                        <th style="${thStyle} text-align: right;">Raw rows</th>
                        <th style="${thStyle} text-align: right;">Processed</th>
                        <th style="${thStyle} text-align: right;">Rows kept</th>
                        <th style="${thStyle}">Rows left out — why</th>
                    </tr>
                </thead>
                <tbody>${rowsHtml}</tbody>
            </table>
        `;
    }

    // Reconciliation block: explain when newer rows superseded older ones in
    // the same collection (net dataset change differs from per-file contribution).
    const contributed = data.rows_contributed_by_new_files ?? 0;
    const superseded = data.rows_superseded_in_existing_collections ?? 0;
    if (reconcileEl) {
        if (superseded > 0) {
            const supersedeLines = perFile
                .filter(r => r.outcome === 'added_as_new' || r.outcome === 'merged_with_existing')
                .filter(r => (r.deduped_rows ?? 0) > 0 || r.outcome === 'merged_with_existing')
                .map(r => {
                    const cid = r.canonical_collection_id ? ` in collection "${_escapeHtml(r.canonical_collection_id)}"` : '';
                    if (r.outcome === 'merged_with_existing') {
                        return `<li>${(r.final_rows ?? 0).toLocaleString()} new rows from <code>${_escapeHtml(r.filename)}</code> joined existing rows${cid}, replacing the older copies where they overlapped.</li>`;
                    }
                    return `<li><code>${_escapeHtml(r.filename)}</code> contributed ${(r.final_rows ?? 0).toLocaleString()} rows${cid}.</li>`;
                })
                .join('');
            reconcileEl.innerHTML = `
                <div class="text-sm font-semibold" style="margin-bottom: 6px;">Why the net change is smaller than the rows added</div>
                <div class="text-sm" style="color: var(--color-text-secondary);">
                    ${contributed.toLocaleString()} new rows were contributed by this run, but ${superseded.toLocaleString()} older rows in the same collection(s) were superseded (the newer donation's rows win on overlapping events). Net change: ${(rowsAdded ?? 0) >= 0 ? '+' : ''}${(rowsAdded ?? 0).toLocaleString()} rows.
                </div>
                ${supersedeLines ? `<ul class="text-xxs" style="color: var(--color-text-tertiary); margin: 8px 0 0 20px; padding: 0;">${supersedeLines}</ul>` : ''}
            `;
            reconcileEl.style.display = 'block';
        } else {
            reconcileEl.style.display = 'none';
        }
    }

    // Previously-skipped section
    if (skippedWrapEl && skippedCountEl && skippedTableEl) {
        if (skippedPreviously.length === 0) {
            skippedWrapEl.style.display = 'none';
        } else {
            skippedCountEl.textContent = `(${skippedPreviously.length})`;
            const skippedRowsHtml = skippedPreviously.map(r => {
                const meta = _ingestOutcomeLabels[r.outcome] || { label: r.outcome, color: 'var(--color-text-tertiary)' };
                const provenance = [r.platform, r.source].filter(Boolean).join(' · ');
                const lastSeen = fypFmtDate(r.ts_last_seen);
                const cidLine = r.collection_id
                    ? `<div class="text-xxs" style="color: var(--color-text-tertiary);">collection: ${_escapeHtml(r.collection_id)}</div>`
                    : '';
                const rawRows = (r.raw_rows ?? null) !== null && r.raw_rows > 0
                    ? r.raw_rows.toLocaleString() : '—';
                return `
                    <tr>
                        <td style="${tdStyle}">
                            <div class="text-sm" style="word-break: break-all;">${_escapeHtml(r.filename)}</div>
                            ${provenance ? `<div class="text-xxs" style="color: var(--color-text-tertiary);">${_escapeHtml(provenance)}</div>` : ''}
                        </td>
                        <td style="${tdStyle} color: ${meta.color};">
                            <div class="text-sm">${meta.label}</div>
                            ${cidLine}
                        </td>
                        <td style="${tdStyle} text-align: right; font-variant-numeric: tabular-nums; color: var(--color-text-tertiary);">${rawRows}</td>
                        <td style="${tdStyle} text-align: right; font-variant-numeric: tabular-nums; color: var(--color-text-tertiary);">${lastSeen}</td>
                        <td style="${tdStyle} text-align: right;">
                            <button type="button" class="action-btn" style="padding: 4px 10px;" onclick="unskipIngestionFile(this, '${_escapeHtml(r.filename).replace(/'/g, "\\'")}')">Un-skip</button>
                        </td>
                    </tr>
                `;
            }).join('');
            skippedTableEl.innerHTML = `
                <table class="text-sm" style="width: 100%; border-collapse: collapse; min-width: 680px;">
                    <thead>
                        <tr>
                            <th style="${thStyle}">File</th>
                            <th style="${thStyle}">Recorded outcome</th>
                            <th style="${thStyle} text-align: right;">Rows read</th>
                            <th style="${thStyle} text-align: right;">Last seen</th>
                            <th style="${thStyle} text-align: right;">Action</th>
                        </tr>
                    </thead>
                    <tbody>${skippedRowsHtml}</tbody>
                </table>
            `;
            skippedWrapEl.style.display = 'block';
        }
    }

    panel.style.display = 'block';
}

// --- Ingestion history (the persistent ledger) ---
// Unlike the "Last run results" panel above (live task-status data, gone on
// page reload), this renders from the ingestion ledger on disk, so the
// per-file intake report survives across sessions.

function loadIngestionHistory() {
    if (!document.getElementById('ingestion-history-panel')) return;
    fetch('/api/manage/ingestion/ledger')
        .then(res => res.json())
        .then(data => renderIngestionHistory(Array.isArray(data.files) ? data.files : []))
        .catch(err => console.error('Error loading ingestion history:', err));
}

function renderIngestionHistory(entries) {
    const panel = document.getElementById('ingestion-history-panel');
    const countEl = document.getElementById('ingestion-history-count');
    const wrap = document.getElementById('ingestion-history-wrap');
    if (!panel || !wrap) return;

    if (!entries.length) {
        panel.style.display = 'none';
        return;
    }

    const thStyle = 'padding: 6px 8px; text-align: left; border-bottom: 2px solid var(--color-border-strong); font-weight: var(--weight-semibold);';
    const tdStyle = 'padding: 6px 8px; border-bottom: 1px solid var(--color-border); vertical-align: top;';
    const numStyle = tdStyle + ' text-align: right; font-variant-numeric: tabular-nums;';

    const rowsHtml = entries.map(r => {
        const meta = _ingestOutcomeLabels[r.outcome] || { label: r.outcome, color: 'var(--color-text-secondary)' };
        const provenance = [r.platform, r.source].filter(Boolean).join(' · ');
        const numOrDash = (v) => (v === undefined || v === null) ? '—' : Number(v).toLocaleString();
        return `
            <tr>
                <td style="${tdStyle}">
                    <div class="text-sm" style="word-break: break-all;">${_escapeHtml(r.filename)}</div>
                    ${provenance ? `<div class="text-xxs" style="color: var(--color-text-tertiary);">${_escapeHtml(provenance)}</div>` : ''}
                </td>
                <td style="${tdStyle} color: ${meta.color};">
                    <div class="text-sm">${meta.label}</div>
                    ${r.collection_id ? `<div class="text-xxs" style="color: var(--color-text-tertiary);">collection: ${_escapeHtml(r.collection_id)}</div>` : ''}
                </td>
                <td style="${numStyle}">${numOrDash(r.raw_rows)}</td>
                <td style="${numStyle}">${numOrDash(r.kept_rows)}</td>
                <td style="${tdStyle} max-width: 280px;">${_ingestDroppedCellHtml(r)}</td>
                <td style="${tdStyle} text-align: right; font-variant-numeric: tabular-nums; color: var(--color-text-tertiary);">${fypFmtDate(r.ts_last_seen)}</td>
            </tr>
        `;
    }).join('');

    if (countEl) countEl.textContent = `(${entries.length})`;
    wrap.innerHTML = `
        <table class="text-sm" style="width: 100%; border-collapse: collapse; min-width: 860px;">
            <thead>
                <tr>
                    <th style="${thStyle}">File</th>
                    <th style="${thStyle}">Outcome</th>
                    <th style="${thStyle} text-align: right;">Rows read</th>
                    <th style="${thStyle} text-align: right;">Rows kept</th>
                    <th style="${thStyle}">Rows left out — why</th>
                    <th style="${thStyle} text-align: right;">Last processed</th>
                </tr>
            </thead>
            <tbody>${rowsHtml}</tbody>
        </table>
    `;
    panel.style.display = 'block';
}

function pollIngestRefreshStatus(btn, originalText, originalClass) {
    if (_ingestRefreshPollActive) return;
    _ingestRefreshPollActive = true;
    let done = false;

    const interval = setInterval(() => {
        if (done) return;
        fetch('/api/status')
            .then(r => r.json())
            .then(statusData => {
                if (done) return;
                const ir = statusData.ingest_refresh;
                if (!ir) return;

                if (ir.state === 'running') {
                    const msg = ir.progress && ir.progress.message;
                    const pct = ir.progress && ir.progress.percent;
                    if (msg) {
                        btn.textContent = pct != null
                            ? `Processing... ${pct}% — ${msg}`
                            : `Processing... ${msg}`;
                    }
                    return;
                }

                done = true;
                clearInterval(interval);
                _ingestRefreshPollActive = false;
                btn.className = originalClass;
                btn.textContent = originalText;
                btn.disabled = false;

                if (ir.last_run_outcome === 'Fail') {
                    console.error('Ingestion refresh failed.');
                }
                renderIngestResultsPanel(ir.data || {});
                loadAvailableCollections();
                loadIngestionSources();
            })
            .catch(err => {
                console.error('Error polling ingest_refresh status:', err);
            });
    }, 2000);
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
            <th style="${thStyle} max-width: 160px;" onclick="sortCollectionTable(this)">Collection / Display ID</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">Tags</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">First Event</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">Last Event</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">Added</th>
            <th style="${thStyle}" onclick="sortCollectionTable(this)">Activities</th>
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
                    pLastEvent = fypWallDate(itemInfo.personas.last_event_ts);
                }
                if (itemInfo.personas.first_event_ts) {
                    pFirstEvent = fypWallDate(itemInfo.personas.first_event_ts);
                }
                const tz = itemInfo.personas.inferred_tz_offset;
                if (tz !== null && tz !== undefined) {
                    pTimezone = `UTC${tz >= 0 ? '+' : ''}${tz}`;
                }
            }
            if (itemInfo.other && itemInfo.other.ts_added_to_dataset) {
                pAdded = fypFmtDate(itemInfo.other.ts_added_to_dataset);
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
            // Clear previous row highlight
            document.querySelectorAll('.edit-activity-item').forEach(row => {
                row.style.background = 'transparent';
            });
            // Highlight this row
            tr.style.background = 'var(--table-row-selected)';
            window.pe_selectedId = item;
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
        const idCell = createCell(primaryId, true, item);
        idCell.style.maxWidth = '160px';
        idCell.style.overflow = 'hidden';
        idCell.style.textOverflow = 'ellipsis';
        tr.appendChild(idCell);
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

    // Apply saved sort state, or default to Added descending
    const savedState = tableSortStates.get('edit-activity');
    const sortText = savedState ? savedState.text : 'Added';
    const sortDir = savedState ? savedState.dir : 'desc';
    const headers = Array.from(thead.querySelectorAll('th'));
    const targetHeader = headers.find(h => h.textContent.trim() === sortText);
    if (targetHeader) {
        window.sortCollectionTable(targetHeader, sortDir);
    }
}

window.refreshCollectionMetadata = function (btn) {
    const origText = btn.textContent;
    btn.textContent = 'Refreshing...';
    btn.disabled = true;

    fetch('/api/manage/refresh-collection-metadata', {
        method: 'POST',
        headers: { 'X-CSRFToken': csrfToken }
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'started') {
                btn.textContent = 'Refreshing in background...';
                setTimeout(() => {
                    btn.textContent = origText;
                    btn.disabled = false;
                    loadAvailableCollections();
                }, 5000);
            } else {
                showAppAlert('Error: ' + (data.message || data.error || 'Unknown error'));
                btn.textContent = origText;
                btn.disabled = false;
            }
        })
        .catch(err => {
            showAppAlert('Request failed: ' + err);
            btn.textContent = origText;
            btn.disabled = false;
        });
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

    // Delete is a single-collection operation only — hide in bulk mode.
    const deleteBtn = document.getElementById('delete-collection-btn');
    if (deleteBtn) deleteBtn.style.display = '';

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
            color: var(--chip-text);
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
                    showAppAlert('Failed to save: ' + (data.error || 'Unknown error'));
                }
            })
            .catch(err => {
                if (saveBtn) saveBtn.disabled = false;
                console.error("Error saving annotation:", err);
                showAppAlert("Error saving annotation.");
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
                showAppAlert(`Saved ${payloads.length - failed} of ${payloads.length} collections. ${failed} failed.`);
            }
            closeEditCollectionModal();
            loadAvailableCollections();
        })();
    }
}


function dm_deleteCollection() {
    if (!currentEditCollectionId || bulkEditMode) return;

    const id = currentEditCollectionId;
    const obj = availableCollections.find(c => c.id === id);
    const displayId = (obj && obj.displayId) || id;
    const deleteBtn = document.getElementById('delete-collection-btn');

    if (deleteBtn) deleteBtn.disabled = true;

    fetch(`/api/manage/collections/affected_studies?collection_id=${encodeURIComponent(id)}`)
        .then(r => r.json())
        .then(async data => {
            const studies = (data && data.studies) || [];
            const studyClause = studies.length === 0
                ? "No studies reference this collection."
                : `${studies.length} study/studies will be refreshed: ${studies.join(", ")}.`;
            const ok = await showAppConfirm(
                `Delete collection "${displayId}"?\n\n` +
                `${studyClause}\n\n` +
                `Raw upload files will be moved to the archive folder and can be restored. ` +
                `Scraped video data and machine annotations will be kept.`,
                { title: 'Delete collection', okLabel: 'Delete', danger: true }
            );
            if (!ok) {
                if (deleteBtn) deleteBtn.disabled = false;
                return;
            }
            return fetch('/api/manage/collections/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                body: JSON.stringify({ collection_id: id })
            })
                .then(r => r.json())
                .then(resp => {
                    if (resp && resp.status === 'started') {
                        closeEditCollectionModal();
                        pollCollectionDeleteStatus(id, displayId, deleteBtn);
                    } else {
                        if (deleteBtn) deleteBtn.disabled = false;
                        showAppAlert('Failed to start delete: ' + ((resp && resp.message) || (resp && resp.error) || 'Unknown error'));
                    }
                });
        })
        .catch(err => {
            if (deleteBtn) deleteBtn.disabled = false;
            console.error("Error deleting collection:", err);
            showAppAlert("Error deleting collection.");
        });
}
window.dm_deleteCollection = dm_deleteCollection;

let _collectionDeletePollActive = false;

function pollCollectionDeleteStatus(collectionId, displayId, deleteBtn) {
    if (_collectionDeletePollActive) return;
    _collectionDeletePollActive = true;
    let done = false;

    const interval = setInterval(() => {
        if (done) return;
        fetch('/api/status')
            .then(r => r.json())
            .then(statusData => {
                if (done) return;
                const cd = statusData.collection_delete;
                if (!cd) return;
                if (cd.state === 'running') return;

                done = true;
                clearInterval(interval);
                _collectionDeletePollActive = false;
                if (deleteBtn) deleteBtn.disabled = false;

                const data = cd.data || {};
                if (cd.last_run_outcome === 'Success') {
                    selectedCollectionIds.delete(collectionId);
                    updateEditSelectedButton();
                    const archived = (data.archived_files || []).length;
                    const failures = (data.archive_failures || []).length;
                    const affected = (data.affected_studies || []).length;
                    const dropped = data.rows_dropped || 0;
                    let msg = `Deleted collection "${displayId}". `;
                    msg += `Dropped ${dropped.toLocaleString()} row(s), `;
                    msg += `archived ${archived} raw file(s)`;
                    if (failures > 0) msg += ` (${failures} archive failure(s))`;
                    msg += `. Refreshing ${affected} study/studies in the background.`;
                    showAppAlert(msg);
                    loadAvailableCollections();
                } else {
                    showAppAlert(`Failed to delete "${displayId}". Check the task logs for details.`);
                }
            })
            .catch(err => {
                console.error('Error polling collection_delete status:', err);
            });
    }, 2000);
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

    // Delete collection is a single-collection operation only; too risky to
    // expose it while editing several collections as a batch.
    const deleteBtn = document.getElementById('delete-collection-btn');
    if (deleteBtn) deleteBtn.style.display = 'none';

    dm_renderTags();
    document.getElementById('editCollectionModal').style.display = 'block';
}
window.openEditSelectedCollections = openEditSelectedCollections;

async function queueVotedVideos(btnElement) {
    if (!(await showAppConfirm("Are you sure you want to add all machine-voted videos to the scrape and annotation queues?",
        { title: 'Queue voted videos', okLabel: 'Add to queues' }))) {
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
                showAppAlert(`Success: Added ${data.added_to_scrape} to scrape queue and ${data.added_to_annotate} to annotate queue.`);
                fetchEnrichmentStats(); // Refresh the stats
            } else if (data.status === 'no_votes' || data.status === 'no_matches') {
                showAppAlert(data.message);
            } else {
                showAppAlert('Error queuing voted videos: ' + data.error);
            }
        })
        .catch(error => {
            btnElement.textContent = originalText;
            btnElement.disabled = false;
            console.error('Error queuing voted videos:', error);
            showAppAlert('Error queuing voted videos.');
        });
}

