// Data Pipeline: Study management: the collection selector, the studies table and the study modal
// (date window, sampling, access, daily-activity chart, report slot). Also serves
// the My Studies page.
// One of the js/data_management/*.js files; they share one global scope and
// load in a fixed order (templates/index.html).

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

    // Create Header (a subset of the Edit Collections columns, same order)
    const thead = document.createElement('thead');
    const sThStyle = 'padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; cursor: pointer; user-select: none; border-bottom: 2px solid var(--color-border-strong);';
    thead.innerHTML = `
        <tr style="text-align: left;">
            <th style="padding: 8px 5px; width: 30px; position: sticky; top: 0; background: var(--color-border); z-index: 10; border-bottom: 2px solid var(--color-border-strong);"><input type="checkbox" class="select-all-collections" title="Select / deselect all" style="cursor: pointer;"></th>
            <th style="${sThStyle} max-width: 160px;" data-sort-type="text" onclick="sortCollectionTable(this)">Collection</th>
            <th style="${sThStyle}" data-sort-type="text" onclick="sortCollectionTable(this)">Tags</th>
            <th style="${sThStyle}" data-sort-type="date" onclick="sortCollectionTable(this)">Last Event</th>
            <th style="${sThStyle}" data-sort-type="date" onclick="sortCollectionTable(this)">Added</th>
            <th style="${sThStyle}" data-sort-type="number" onclick="sortCollectionTable(this)">Activities</th>
            <th style="${sThStyle}" data-sort-type="number" onclick="sortCollectionTable(this)">Active Days</th>
        </tr>
    `;
    table.appendChild(thead);

    const tbody = document.createElement('tbody');

    // Picking collections for a study is picking them BY NAME, so a name two
    // collections share has to say so here as well as in Edit Collections.
    const duplicateDisplayKeys = _dmDuplicateDisplayKeys();

    availableCollections.forEach(itemInfo => {
        const item = typeof itemInfo === 'string' ? itemInfo : itemInfo.id;

        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid var(--chart-grid)';
        tr.className = 'collection-item'; // Keep class for CSS/JS targeting

        let pAccount = '', pAdded = '', pDisplayId = '', pTags = '';
        let pActiveDays = '', pTotalEvents = '', pLastEvent = '';
        let rawAdded = null, rawLastEvent = null;
        let searchString = item;

        if (typeof itemInfo === 'object') {
            if (itemInfo.displayId) pDisplayId = itemInfo.displayId;
            if (itemInfo.tags && Array.isArray(itemInfo.tags)) pTags = itemInfo.tags.join(', ');
            pAccount = `${_dmAccountCell(itemInfo)} ${itemInfo.user_id || ''}`;
            if (itemInfo.personas) {
                pActiveDays = itemInfo.personas.active_days ?? '';
                pTotalEvents = itemInfo.personas.total_events ?? '';
                rawLastEvent = itemInfo.personas.last_event_ts || null;
                if (rawLastEvent) {
                    pLastEvent = fypWallDate(rawLastEvent);
                }
            }
            if (itemInfo.other && itemInfo.other.ts_added_to_dataset) {
                rawAdded = itemInfo.other.ts_added_to_dataset;
                pAdded = fypFmtDate(rawAdded);
            }
            searchString = `${item} ${pDisplayId} ${pTags} ${pAccount} ${pActiveDays} ${pTotalEvents} ${pLastEvent} ${pAdded}`;
        }

        const isDuplicateDisplayId = duplicateDisplayKeys.has(
            _dmDisplayKey(pDisplayId || item)) || _dmHasUnlistedTwin(itemInfo);
        if (isDuplicateDisplayId) searchString += ' duplicate';

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

        const createCell = (text, isBold = false, tooltip = null, sortValue = null) => {
            const td = document.createElement('td');
            td.style.padding = '5px';
            if (tooltip) {
                td.title = tooltip;
            }
            if (sortValue !== null) {
                td.dataset.sortValue = sortValue;
            }
            if (isBold) td.innerHTML = `<strong>${_escapeHtml(text)}</strong>`;
            else td.textContent = text;
            return td;
        }

        tr.appendChild(tdCheck);
        const primaryId = pDisplayId ? pDisplayId : item;
        // Sort on the name alone: the duplicate flag below is part of the
        // cell's text, and sorting would otherwise read it as part of it.
        const idCell = createCell(primaryId, true, item, primaryId);
        idCell.style.maxWidth = '160px';
        idCell.style.overflow = 'hidden';
        idCell.style.textOverflow = 'ellipsis';
        if (isDuplicateDisplayId) idCell.appendChild(_dmDuplicateFlag(itemInfo));
        tr.appendChild(idCell);
        tr.appendChild(createCell(pTags));
        tr.appendChild(createCell(pLastEvent, false, null, _dmSortTs(rawLastEvent)));
        tr.appendChild(createCell(pAdded, false, null, _dmSortTs(rawAdded)));
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

// Collection summary for the read-only My Studies modal: how many collections
// the study draws on, never which ones. A collection id names a participant's
// donation, so the list stays with the people who manage studies. Counted from
// the study's own SELECTED_COLLECTIONS, not the global `availableCollections`
// (that comes from an endpoint a plain viewer is refused anyway).
function _renderReadOnlyCollectionList(container, selectedList) {
    const count = Array.from(selectedList).length;
    const summary = document.createElement('div');
    summary.className = 'text-sm';
    summary.style.cssText = 'padding: 10px 5px; color: var(--color-text-secondary);';
    summary.textContent = count
        ? `${count.toLocaleString()} collection${count === 1 ? '' : 's'} in this study`
        : 'This study has no collections.';
    container.appendChild(summary);
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
    // Hard cap first: the last estimate response carries the server's verdict
    // (accounting for sampling and the date window). Over-cap designs cannot be
    // saved — the dialog offers no "Continue anyway" and the save endpoint would
    // refuse anyway.
    if (formContainer.dataset.capExceeded === '1') {
        const projected = Number(formContainer.dataset.capProjected || 0);
        const limit = Number(formContainer.dataset.capLimit || _LARGE_STUDY_THRESHOLD);
        return new Promise(resolve => {
            const overlay = document.getElementById('large-study-warning');
            const textEl = document.getElementById('large-study-warning-text');
            textEl.innerHTML = `This study would contain approximately <strong>${projected.toLocaleString()}</strong> activities — ` +
                `above the cap of <strong>${limit.toLocaleString()}</strong>. It cannot be saved: ` +
                `narrow the date range, drop collections, or enable sampling.`;
            const continueBtn = document.getElementById('large-study-warning-continue');
            continueBtn.style.display = 'none';
            overlay.classList.add('visible');
            document.getElementById('large-study-warning-back').onclick = () => {
                overlay.classList.remove('visible');
                continueBtn.style.display = '';
                resolve(false);
            };
        });
    }

    const sampleSelect = formContainer.querySelector('[data-field="SAMPLE_FRAME"]');
    const sampleValue = sampleSelect ? sampleSelect.value : 'off';
    if (sampleValue !== 'off') return Promise.resolve(true);

    // Advisory below the cap. The daily chart holds a per-day count for exactly
    // the selected collections, so summing it across the study window is the size
    // the study will actually have. Only when the chart has no data (fetch failed /
    // not yet returned) do we fall back to the whole-collection totals, which
    // ignore the window.
    const threshold = Number(formContainer.dataset.capLimit || 0) || _LARGE_STUDY_THRESHOLD;
    const windowed = _windowActivityCount(formContainer);
    const totalActivities = windowed != null ? windowed : _sumSelectedActivities(formContainer);
    if (totalActivities <= threshold) return Promise.resolve(true);

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
        _showStudyVizLoading(formContainer, 'Estimating study size…');
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

    // Date columns render a human string ("14-Mar-2025"), which sorts by day
    // number under localeCompare — so the header declares its own type and the
    // cells carry a machine-comparable `data-sort-value` (epoch ms for dates).
    // The name list stays as the fallback for headers built without a type.
    const sortType = th.dataset.sortType
        || (['Age', 'Active Days', 'Activities', 'Watch Time'].includes(textContent) ? 'number' : 'text');
    const isNumeric = sortType === 'number' || sortType === 'date';

    const cellValue = (row) => {
        const td = row.children[columnIndex];
        if (!td) return '';
        const raw = td.dataset.sortValue;
        return raw !== undefined ? raw : td.textContent.trim();
    };

    rows.sort((a, b) => {
        const cellA = cellValue(a);
        const cellB = cellValue(b);

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
            // Dim the row's data cells to show it is not clickable, but leave
            // the Action cell at full strength — dimming the whole row is what
            // used to make its status message unreadable.
            tr.style.cursor = 'default';
            tr.classList.add('study-row-busy');
        } else {
            // My Studies opens the same modal read-only: every field disabled,
            // no Save/Delete/Access, and rendered without the Data-Management
            // endpoints a plain viewer would be refused. System-managed
            // participant studies are read-only everywhere — the backend
            // refuses edits to them, so the modal must not offer any.
            tr.style.cursor = 'pointer';
            tr.onclick = () => openStudyModal(index, !allowEdit || !!study.SYSTEM);
        }

        const stats = study.stats || {};
        const formatNum = (num) => num !== undefined ? num.toLocaleString() : '-';

        let actionHtml = '';
        if (isSaving) {
            actionHtml = '<span class="study-row-status font-semibold">Saving...</span>';
        } else if (isRefreshing) {
            const msg = refreshingStudies.get(study.STUDY_NAME).message || 'Refreshing...';
            actionHtml = `<span class="study-row-status font-semibold">${escapeHtml(msg)}</span>`;
        } else if (!allowEdit) {
            // Per-study provenance note (My Studies only — it lists every study
            // the user can see, so the note is offered per row rather than for
            // one "active" study the way Explore used to). Wired below rather
            // than inline, so a study name containing a quote is safe.
            actionHtml = '<button class="btn-discreet text-xs js-study-methods-btn">Methods</button>';
        }

        const nameLabel = escapeHtml(study.DISPLAY_LABEL || study.STUDY_NAME);
        const systemBadge = study.SYSTEM
            ? ' <span class="text-xs" style="color: var(--color-text-muted);" title="Auto-managed participant study — updated automatically, not editable.">(auto)</span>'
            : '';
        tr.innerHTML = `
            <td style="padding: 5px;"><strong>${nameLabel}</strong>${systemBadge}</td>
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
        // The Define Studies table hides the auto-managed participant pairs
        // (two per participant — they would bury the real studies) behind a
        // toggle. My Studies keeps them: a participant's own pair is exactly
        // what that view is for.
        const showSystem = !allowEdit
            || !!document.getElementById('dm-show-participant-studies')?.checked;
        let hiddenSystem = 0;
        allStudies.forEach((study, index) => {
            if (!showSystem && study.SYSTEM) {
                hiddenSystem += 1;
                return;
            }
            tbody.appendChild(buildRow(study, index, allowEdit));
        });
        const toggleLabel = document.getElementById('dm-participant-studies-count');
        if (toggleLabel && allowEdit) {
            const nSystem = allStudies.filter(s => s.SYSTEM).length;
            toggleLabel.textContent = nSystem ? `(${nSystem})` : '(0)';
        }
        if (hiddenSystem && tbody.children.length === 0) {
            const tr = document.createElement('tr');
            const td = document.createElement('td');
            td.colSpan = 10;
            td.className = 'text-sm';
            td.style.cssText = 'padding: 16px; text-align: center; color: var(--color-text-muted);';
            td.textContent = `Only auto-managed participant studies exist (${hiddenSystem} hidden).`;
            tr.appendChild(td);
            tbody.appendChild(tr);
        }
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

    // The name input lives in the template's top strip (shown only for a new
    // study, via [data-is-new]) so it shares a row with the study report.
    title.textContent = isNew ? 'New Study' : study.STUDY_NAME;

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
    // A disabled Delete/Save still reads as an offer. Nothing here is
    // actionable from My Studies, so drop the whole action row and the
    // collection search box (there is no list to search).
    // The top strip holds the name box and the study report, neither of which a
    // viewer can produce here — /calculate_stats is Data-Management only.
    row.querySelectorAll('.study-form-actions, .collection-search-row, .study-top-strip').forEach(el => {
        el.style.display = 'none';
    });
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

        // Handle Lists/JSON (USER_ACCESS is a checkbox group, handled elsewhere)
        if (field === 'SELECTED_COLLECTIONS') {
            // input[data-field="SELECTED_COLLECTIONS"] is a hidden input, a
            // sibling of div.collection-selector. Store the list on it and
            // render the checklist into the selector's container.
            const selectorDiv = input.parentElement.querySelector('.collection-selector');
            if (selectorDiv) {
                const container = selectorDiv.querySelector('.collection-checklist-container');
                // Value is the array
                const selectedList = Array.isArray(value) ? value : [];
                input.value = JSON.stringify(selectedList); // Set hidden value

                // Render Checklist
                renderCollectionSelector(container, selectedList, readOnly);
            } else {
                // Fallback for a row without the collection selector markup
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
    } else if (readOnly && Array.isArray(study.SELECTED_COLLECTIONS) && study.SELECTED_COLLECTIONS.length) {
        // No persisted universe (study saved before it was recorded, or the
        // refresh skipped it). Read-only can't run the editable modal's
        // estimate, so fetch the mosaic for this one saved study instead —
        // otherwise the spinner below would never resolve.
        _fetchReadOnlySetViz(row, study);
    } else if (Array.isArray(study.SELECTED_COLLECTIONS) && study.SELECTED_COLLECTIONS.length) {
        // Collections are selected but no seeded stats exist (e.g. a duplicated
        // study) — the estimate is already on its way, so say so instead of
        // showing the misleading "select collections" placeholder.
        _showStudyVizLoading(row, 'Estimating study size…');
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
                // updateCollectionSelection writes a clean JSON array into the
                // hidden input; single quotes are normalised defensively in
                // case the value was set some other way.
                if (value && value.trim()) {
                    let safeVal = value.replace(/'/g, '"');

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

        // Every box checked is saved as ["all"]; otherwise the explicit list
        // of checked roles.
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
            headers: { 'Content-Type': 'application/json' },
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
    refreshingStudies.set(studyName, { message: 'Starting...' });
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

// My Studies has no /calculate_stats access (it is Data-Management only, and
// takes a client-supplied definition). This asks for the mosaic of ONE saved
// study by name, which the server recomputes from the study's own definition.
function _fetchReadOnlySetViz(row, study) {
    _showStudyVizLoading(row, 'Estimating study size…');
    fetch(`/api/manage/studies/${encodeURIComponent(study.STUDY_NAME)}/set_viz`)
        .then(res => res.json())
        .then(data => {
            if (data.status !== 'success' || !data.universe
                || !(Number(data.universe.activities) > 0)) {
                _showStudyVizUnavailable(row);
                return;
            }
            _renderStudySetViz(row, {
                universe: data.universe,
                included: data.stats || {},
                frame: data.frame || study.SAMPLE_FRAME,
            });
        })
        .catch(() => _showStudyVizUnavailable(row));
}

function _showStudyVizUnavailable(row) {
    const viz = row.querySelector('.study-set-viz');
    if (!viz) return;
    viz.style.opacity = '1';
    viz.dataset.state = 'empty';
    viz.innerHTML =
        '<div class="study-set-viz-empty text-xs" style="color: var(--color-text-tertiary); padding: 12px; border: 1px dashed var(--color-border); border-radius: 4px; background: var(--color-bg-surface);">'
        + 'Coverage breakdown is not available for this study yet. It appears once the study has been refreshed.</div>';
}

function _scheduleStudyEstimate(row, delay = 200) {
    const prev = _studyEstimateDebounce.get(row);
    if (prev) clearTimeout(prev);
    _studyEstimateDebounce.set(row, setTimeout(() => _runStudyEstimate(row), delay));
}

// Reflect the server's hard cap on the Save button: over-cap designs can't be
// saved (the save endpoint refuses them anyway — this just says so up front).
function _updateSaveCapState(row) {
    const saveBtn = row.querySelector('button[onclick^="saveStudy"]');
    if (!saveBtn) return;
    const exceeded = row.dataset.capExceeded === '1';
    saveBtn.disabled = exceeded;
    saveBtn.style.opacity = exceeded ? '0.5' : '';
    saveBtn.title = exceeded
        ? `Too many activities (~${Number(row.dataset.capProjected || 0).toLocaleString()}; cap ${Number(row.dataset.capLimit || 0).toLocaleString()}). Narrow the window, drop collections, or enable sampling.`
        : '';
}

// A failed estimate must not leave the first-load note standing as if it were
// still the explanation. The placeholder is left alone — it says nothing wrong.
function _reportEstimateFailed(row) {
    if (_studyReportState(row) !== 'note') return;
    _setStudyReport(row, 'note', 'The study report could not be computed. '
        + 'Change a setting to try again, or reopen the study.');
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
    else if (!viz || viz.dataset.state !== 'loading') _showStudyVizLoading(row, 'Estimating study size…');

    fetch('/api/manage/studies/calculate_stats', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
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
                const cap = data.cap || {};
                row.dataset.capExceeded = cap.exceeded ? '1' : '';
                row.dataset.capLimit = cap.limit != null ? String(cap.limit) : '';
                row.dataset.capProjected = cap.projected != null ? String(cap.projected) : '';
                _updateSaveCapState(row);
                const cached = (typeof allStudies !== 'undefined') ? allStudies.find(s => s.STUDY_NAME === row.dataset.studyName) : null;
                if (cached) cached.stats = stats;   // keep client cache fresh so reopen seeds instantly
            } else if (data.error) {
                console.error('estimate error:', data.error);
                _reportEstimateFailed(row);
            }
        })
        .catch(err => {
            console.error('estimate request failed', err);
            if (_studyEstimateSeq.get(row) === seq) _reportEstimateFailed(row);
        })
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
        headers: { 'Content-Type': 'application/json' },
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
        headers: { 'Content-Type': 'application/json' },
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
// first auto-estimate is fast. Nothing awaits it before rendering: the modal calls
// this on open and on every collection-selection change (via _fetchDailyChart), so
// the (possibly slow) build / disk-load happens during the user's think-time rather
// than on the first estimate. The returned promise only feeds the first-load notice.
function _prewarmStudyCheck(selected, studyName) {
    if (!Array.isArray(selected) || selected.length === 0) return Promise.resolve(null);
    return fetch('/api/manage/studies/prewarm_check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ SELECTED_COLLECTIONS: selected, STUDY_NAME: studyName })
    })
        .then(res => res.json())
        .catch(() => null);   /* best-effort warming */
}

// The corpus preview cells are built once per data change per server instance, and
// the chart, the mosaic and the report all wait on that build. A warm instance
// answers the prewarm in milliseconds, so a prewarm still in flight after this long
// means the user is sitting through the build itself — say so, rather than leaving
// three spinners to suggest something is stuck.
const STUDY_FIRST_LOAD_NOTICE_MS = 700;
const STUDY_FIRST_LOAD_NOTE =
    '<span class="global-tasks-spinner" style="flex: 0 0 auto; margin-top: 3px;"></span>'
    + '<span>First load since the data last changed &mdash; building the study preview cache. '
    + 'This takes a few seconds and nothing is wrong; the chart, mosaic and report fill in '
    + 'together, and every later check in this session comes back straight away.</span>';

const _studyFirstLoadTimer = new WeakMap();

// Drop a pending notice — the wait it would have explained is over or moot.
function _cancelStudyFirstLoad(row) {
    const prev = _studyFirstLoadTimer.get(row);
    if (prev) clearTimeout(prev);
    _studyFirstLoadTimer.delete(row);
}

function _watchStudyFirstLoad(row, prewarm) {
    _cancelStudyFirstLoad(row);
    let displaced = null;
    _studyFirstLoadTimer.set(row, setTimeout(() => {
        displaced = _captureStudyReport(row);
        _setStudyReport(row, 'note', STUDY_FIRST_LOAD_NOTE);
    }, STUDY_FIRST_LOAD_NOTICE_MS));

    prewarm.then(info => {
        _cancelStudyFirstLoad(row);
        // Warm all along — the notice was tripped by a slow round trip, not by a
        // build. Take the claim back and put back whatever it displaced, rather
        // than leave a wrong explanation up. The estimate still in flight
        // overwrites it with the real report as soon as it lands.
        if (info && info.warm === true && displaced && _studyReportState(row) === 'note') {
            _setStudyReport(row, displaced.state, displaced.html);
        }
    });
}

function _fetchDailyChart(row) {
    const selected = _getSelectedCollections(row);
    const s = _getChartState(row);
    s.includedPerDay = null;

    if (!selected.length) {
        s.totalPerDay = [];
        s.loading = false;
        // Nothing is being fetched, so a notice queued by the previous selection
        // would land with no wait behind it.
        _cancelStudyFirstLoad(row);
        _renderDailyChart(row);
        return;
    }

    // Mark loading so the empty/loading placeholder can render correctly
    // until the response comes back.
    s.loading = true;
    _renderDailyChart(row);

    const studyName = row.dataset.studyName || null;
    _watchStudyFirstLoad(row, _prewarmStudyCheck(selected, studyName));
    fetch('/api/manage/studies/daily_activities', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
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
    _studyChartDebounce.set(row, setTimeout(() => _fetchDailyChart(row), 200));
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
        // The "adjust to refresh" nudge only makes sense where the controls
        // are live — read-only has nothing to adjust.
        (seeded && row.dataset.readOnly !== '1'
            ? `<div class="study-viz__seeded-note text-xxs">Showing the last saved result; adjust the sampling or date range to refresh.</div>`
            : '');
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

// --- Study report slot ---
// The fixed-size box above the mosaic. It shows either the design report, a
// waiting note, or the placeholder — never nothing, so the form below it never
// moves when a report arrives, changes or clears.

const STUDY_REPORT_EMPTY_NOTE = 'Select one or more collections to see the study report.';

function _setStudyReport(row, state, html) {
    const slot = row.querySelector('.study-report');
    if (!slot) return;
    const note = slot.querySelector('.study-report-note');
    const list = slot.querySelector('.study-issues-list');
    if (state === 'issues') {
        if (list) list.innerHTML = html;
    } else {
        if (note) note.innerHTML = html;
        if (list) list.innerHTML = '';
    }
    slot.dataset.state = state;
    slot.scrollTop = 0;
}

function _studyReportState(row) {
    return row.querySelector('.study-report')?.dataset.state || '';
}

// What the slot is showing right now, in the shape _setStudyReport takes back.
function _captureStudyReport(row) {
    const slot = row.querySelector('.study-report');
    if (!slot) return null;
    const state = slot.dataset.state || 'empty';
    const source = state === 'issues' ? '.study-issues-list' : '.study-report-note';
    return { state, html: slot.querySelector(source)?.innerHTML || '' };
}

function _clearStudyIssues(row) {
    _setStudyReport(row, 'empty', STUDY_REPORT_EMPTY_NOTE);
}

function _renderStudyIssues(row, issues) {
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
    _setStudyReport(row, 'issues', issues.map(i => {
        const color = colorMap[i.severity] || colorMap.warn;
        return '<div style="display: flex; align-items: center; gap: 6px;">' +
            `<span style="display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: ${color}; flex: 0 0 auto;"></span>` +
            `<span style="color: var(--color-text-secondary);">${esc(i.message)}</span>` +
            '</div>';
    }).join(''));
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

