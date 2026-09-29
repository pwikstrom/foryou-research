// Data Pipeline: The Edit Collections table and modal: display ids, metadata, account links,
// autosave.
// One of the js/data_management/*.js files; they share one global scope and
// load in a fixed order (templates/index.html).

// --- Edit Activity Data Modal Logic ---

const _COVERAGE_COLUMN_LABEL = 'Scraped / annotated';

// Columns shown in the Edit Collections table. Everything else the collections
// metadata carries (timezone, last event, demographics, contact details) lives
// in the edit modal instead: the table is for finding a collection, the modal
// is for reading and changing one. `sortType` drives sortCollectionTable —
// dates render as human strings and would otherwise sort by day-of-month.
// Every column but the last comes from the collections listing; coverage
// arrives separately (see _dmFillCoverageCells).
const _EDIT_COLLECTION_COLUMNS = [
    { label: 'Collection / Display ID', sortType: 'text' },
    { label: 'Account', sortType: 'text' },
    { label: 'Tags', sortType: 'text' },
    { label: 'First Event', sortType: 'date' },
    { label: 'Added', sortType: 'date' },
    { label: 'Activities', sortType: 'number' },
    { label: 'Active Days', sortType: 'number' },
    {
        label: 'Auto enrichment', sortType: 'text',
        title: 'Whether this collection is scraping and annotating itself. '
            + 'Blank means nobody has set it up. Open the collection to change it.',
    },
    {
        label: _COVERAGE_COLUMN_LABEL, sortType: 'number',
        title: 'Share of the collection\u2019s viewing activities whose video is '
            + 'scraped / annotated \u2014 the same figure a participant sees in My Collections',
    },
];


// Every metadata reading the table no longer shows, in the order the modal
// lists them. `get` reads from the collection object the /api/manage/collections
// payload returns.
const _EDIT_COLLECTION_DETAILS = [
    { label: 'Collection ID', get: c => c.id },
    { label: 'First event', get: c => fypWallDate(c.personas?.first_event_ts, '') },
    { label: 'Last event', get: c => fypWallDate(c.personas?.last_event_ts, '') },
    { label: 'Added', get: c => fypFmtDate(c.other?.ts_added_to_dataset, '') },
    { label: 'Activities', get: c => (c.personas?.total_events ?? '') === '' ? '' : Number(c.personas.total_events).toLocaleString() },
    { label: 'Active days', get: c => c.personas?.active_days ?? '' },
    { label: 'Timezone', get: c => _dmTimezoneLabel(c) },
    { label: 'Account', get: c => _dmAccountCell(c) },
    { label: 'Campaign', get: c => c.participants?.campaign ?? '' },
    { label: 'Donation type', get: c => c.participants?.donationType ?? '' },
];


// How a plan's ledger state reads to a person, in the table and in the panel.
// `null` (never set up) is deliberately blank rather than "Off": the table
// should draw the eye to the few collections that ARE enriching themselves.
const _ENRICHMENT_STATE_LABELS = {
    running: 'Running',
    paused: 'Paused',
    // "Idle", not "Complete": done means the current target is met, and a
    // higher target can always put it back to work.
    done: 'Idle',
    blocked: 'Needs attention',
};

function _dmSetEnrichmentStateCell(td, state) {
    const label = _ENRICHMENT_STATE_LABELS[state] || '';
    td.textContent = label;
    td.dataset.sortValue = label;
    td.style.fontWeight = state === 'running' ? 'var(--weight-bold)' : '';
    td.style.color = state === 'blocked' ? 'var(--color-danger, #c0392b)'
        : (label ? 'var(--color-text-tertiary)' : '');
}


function _dmEnrichmentStateCell(collectionId, state) {
    const td = document.createElement('td');
    td.className = 'enrichment-state-cell';
    td.style.padding = '5px';
    td.style.whiteSpace = 'nowrap';
    td.dataset.collectionId = collectionId;
    _dmSetEnrichmentStateCell(td, state);
    return td;
}


// Keep the table honest after the modal changes a plan, without refetching the
// whole listing while the operator is still working in the modal.
function dmEnrichSyncTableRow(collectionId, state) {
    const obj = availableCollections.find(
        c => (typeof c === 'object' ? c.id : c) === collectionId);
    if (obj && typeof obj === 'object') obj.enrichment_state = state;
    document.querySelectorAll('td.enrichment-state-cell').forEach(td => {
        if (td.dataset.collectionId === collectionId) {
            _dmSetEnrichmentStateCell(td, state);
        }
    });
}


// Demographics no longer live on the collection — they belong to the linked
// user account (Admin → Active users). The collection shows only the link.
function _dmAccountCell(c) {
    if (!c || !c.user_id) return '';
    return c.user_known === false ? `${c.user_id} (unknown account)` : (c.user_label || c.user_id);
}


function _dmTimezoneLabel(c) {
    const tz = c.personas ? c.personas.inferred_tz_offset : null;
    if (tz === null || tz === undefined) return '';
    return `UTC${tz >= 0 ? '+' : ''}${tz}`;
}


// Epoch ms for a timestamp cell's `data-sort-value`, or '' when absent — the
// sorter reads a non-numeric value as "sorts last ascending".
function _dmSortTs(value) {
    if (value === null || value === undefined || value === '') return '';
    const ms = Date.parse(value);
    return Number.isNaN(ms) ? '' : String(ms);
}


// The Edit Collections coverage column: the share of each collection's viewing
// activities whose video is scraped / annotated. It needs a scan of the whole
// activity parquet, so it has its own endpoint and its own (server-cached)
// round trip — the table renders at metadata speed and these cells fill in
// when the answer lands. `null` here means "not fetched yet", which the cells
// show as an ellipsis rather than an em-dash: not-known-yet and known-to-be-
// nothing are different facts.
let collectionCoverage = null;
let _coverageInFlight = null;

function loadCollectionCoverage() {
    if (_coverageInFlight) return _coverageInFlight;
    _coverageInFlight = fetch('/api/manage/collections/coverage')
        .then(res => (res.ok ? res.json() : {}))
        .catch(err => {
            console.error("Error loading collection coverage:", err);
            return {};
        })
        .then(data => {
            collectionCoverage = (data && typeof data === 'object') ? data : {};
            _coverageInFlight = null;
            return collectionCoverage;
        });
    return _coverageInFlight;
}


function _dmSetCoverageCell(td, cov) {
    const muted = (text) => {
        td.textContent = text;
        td.style.color = 'var(--color-text-tertiary)';
        td.dataset.sortValue = '';
    };
    if (!collectionCoverage) return muted('\u2026');
    // Loaded, but this collection has no entry: no view activities at all, or
    // no enrichment status table to check them against. Not the same as 0%.
    if (!cov || cov.pct_scraped == null || cov.pct_annotated == null) return muted('\u2014');
    td.textContent = `${Math.round(cov.pct_scraped * 100)}% / ${Math.round(cov.pct_annotated * 100)}%`;
    td.style.color = '';
    td.dataset.sortValue = String(cov.pct_scraped);
}


function _dmCoverageCell(collectionId) {
    const td = document.createElement('td');
    td.className = 'coverage-cell';
    td.style.padding = '5px';
    td.style.whiteSpace = 'nowrap';
    td.dataset.collectionId = collectionId;
    _dmSetCoverageCell(td, collectionCoverage ? collectionCoverage[collectionId] : null);
    return td;
}


function _dmFillCoverageCells(container) {
    loadCollectionCoverage().then(cov => {
        container.querySelectorAll('td.coverage-cell').forEach(td => {
            _dmSetCoverageCell(td, cov[td.dataset.collectionId]);
        });
        // If the table is already sorted by this column it was sorting blanks;
        // settle it again now the values exist.
        const active = container.querySelector('th[data-sort-dir]:not([data-sort-dir=""])');
        if (active && active.textContent.replace(/ [\u25bc\u25b2]$/, '') === _COVERAGE_COLUMN_LABEL) {
            window.sortCollectionTable(active, active.dataset.sortDir);
        }
    });
}


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
    const headerCells = _EDIT_COLLECTION_COLUMNS.map((col, i) => {
        const extra = i === 0 ? ' max-width: 160px;' : '';
        const title = col.title ? ` title="${_escapeHtml(col.title)}"` : '';
        return `<th style="${thStyle}${extra}" data-sort-type="${col.sortType}"${title}`
            + ` onclick="sortCollectionTable(this)">${col.label}</th>`;
    }).join('');
    thead.innerHTML = `
        <tr style="text-align: left;">
            <th style="padding: 8px 5px; position: sticky; top: 0; background: var(--color-border); z-index: 10; border-bottom: 2px solid var(--color-border-strong); width: 40px; text-align: center;">
                <input type="checkbox" id="select-all-collections" onchange="toggleAllCollectionCheckboxes(this)" style="cursor: pointer;">
            </th>
            ${headerCells}
        </tr>
    `;
    table.appendChild(thead);

    const tbody = document.createElement('tbody');

    // Names shared by two collections can only be data that predates the
    // uniqueness guard: they are flagged in the ID column so the operator can
    // rename them, since nothing else tells the two rows apart at a glance.
    const duplicateDisplayKeys = _dmDuplicateDisplayKeys();

    availableCollections.forEach(itemInfo => {
        const item = typeof itemInfo === 'string' ? itemInfo : itemInfo.id;
        let pAccount = '', pAccountId = '', pAdded = '', pDisplayId = '', pTags = '';
        let pActiveDays = '', pTotalEvents = '';
        let pTimezone = '', pFirstEvent = '';
        let rawAdded = null, rawFirstEvent = null;
        let searchString = item;

        if (typeof itemInfo === 'object') {
            if (itemInfo.displayId) pDisplayId = itemInfo.displayId;
            if (itemInfo.tags && Array.isArray(itemInfo.tags)) pTags = itemInfo.tags.join(', ');
            pAccount = _dmAccountCell(itemInfo);
            pAccountId = itemInfo.user_id || '';
            if (itemInfo.personas) {
                pActiveDays = itemInfo.personas.active_days ?? '';
                pTotalEvents = itemInfo.personas.total_events ?? '';
                rawFirstEvent = itemInfo.personas.first_event_ts || null;
                if (rawFirstEvent) pFirstEvent = fypWallDateShort(rawFirstEvent);
                pTimezone = _dmTimezoneLabel(itemInfo);
            }
            if (itemInfo.other && itemInfo.other.ts_added_to_dataset) {
                rawAdded = itemInfo.other.ts_added_to_dataset;
                pAdded = fypFmtDateShort(rawAdded);
            }
            // The account (display name + id) is searchable — the search box
            // is how you find "that participant's" collections. The date
            // columns render a two-digit year but stay searchable by the
            // four-digit one, which is what a person types.
            searchString = `${item} ${pDisplayId} ${pTags} ${pAccount} ${pAccountId} ${pTimezone} `
                + `${pActiveDays} ${pTotalEvents} ${pFirstEvent} ${fypWallDate(rawFirstEvent, '')} `
                + `${pAdded} ${fypFmtDate(rawAdded, '')} `
                + `${_ENRICHMENT_STATE_LABELS[itemInfo.enrichment_state] || ''}`;
        }

        // Searchable by the word too, so "duplicate" in the box collects
        // every clashing row into one list to work through.
        const isDuplicateDisplayId = duplicateDisplayKeys.has(
            _dmDisplayKey(pDisplayId || item)) || _dmHasUnlistedTwin(itemInfo);
        if (isDuplicateDisplayId) searchString += ' duplicate';

        const tr = document.createElement('tr');
        tr.className = 'edit-activity-item';
        tr.setAttribute('data-search', searchString.toLowerCase());
        tr.setAttribute('data-collection-id', item);
        tr.style.cursor = 'pointer';

        // Apply distinct styling to hidden collections
        if (itemInfo.hidden) {
            tr.classList.add('collection-hidden');
        }

        tr.onmouseenter = () => { tr.style.background = 'var(--color-bg-input)'; };
        tr.onmouseleave = () => { tr.style.background = 'transparent'; };
        // A row IS the collection's edit control: clicking it opens that
        // collection straight away. The checkbox stops propagation, so ticking
        // several rows for a bulk edit still works.
        tr.onclick = () => {
            openEditCollectionModal(itemInfo);
        };
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
        // Sort on the name alone: the duplicate flag below is part of the
        // cell's text, and sorting would otherwise read it as part of it.
        const idCell = createCell(primaryId, true, item, primaryId);
        idCell.style.maxWidth = '160px';
        idCell.style.overflow = 'hidden';
        idCell.style.textOverflow = 'ellipsis';
        if (isDuplicateDisplayId) idCell.appendChild(_dmDuplicateFlag(itemInfo));
        tr.appendChild(idCell);
        const accountCell = createCell(pAccount, false, pAccountId || null);
        accountCell.style.maxWidth = '160px';
        accountCell.style.overflow = 'hidden';
        accountCell.style.textOverflow = 'ellipsis';
        accountCell.style.whiteSpace = 'nowrap';
        tr.appendChild(accountCell);
        tr.appendChild(createCell(pTags));
        // Two-digit years, with the full date on hover so nothing is lost.
        tr.appendChild(createCell(pFirstEvent, false, fypWallDate(rawFirstEvent, null), _dmSortTs(rawFirstEvent)));
        tr.appendChild(createCell(pAdded, false, fypFmtDate(rawAdded, null), _dmSortTs(rawAdded)));
        tr.appendChild(createCell(pTotalEvents));
        tr.appendChild(createCell(pActiveDays));
        tr.appendChild(_dmEnrichmentStateCell(
            item, typeof itemInfo === 'object' ? itemInfo.enrichment_state : null));
        tr.appendChild(_dmCoverageCell(item));

        tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    container.appendChild(table);
    _dmFillCoverageCells(container);

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
// The display ID as last WRITTEN, not as currently typed. Every autosave
// carries the whole record, so without this a tag ticked mid-edit would
// persist a half-typed ID (the box commits on blur/Enter, not per keystroke).
let _dmSavedDisplayId = '';
// Saves are chained, never parallel: the endpoint rewrites one shared JSON
// file, and two in-flight writes would race for it.
let _dmSaveChain = Promise.resolve();
// Whether anything was written while the modal was open — the table behind it
// is refreshed on close, once, instead of after every field.
let _dmSavedAnything = false;


// The name a collection answers to everywhere: its display ID, or its own
// collection ID when it has none.
function _dmDisplayName(c) {
    if (!c) return '';
    if (typeof c === 'string') return c;
    return c.displayId || c.id || '';
}


// Two display IDs are the same name when only case or stray whitespace
// separates them - the server folds them the same way.
function _dmDisplayKey(value) {
    return String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
}


// Twins the server found in the tags file that have no row in this listing:
// a collection entry with no data, which cannot be renamed here — only
// deleted. The listing is the dataset; the tags file is wider.
function _dmUnlistedTwins(c) {
    if (!c || typeof c !== 'object' || !Array.isArray(c.displayIdTwins)) return [];
    const listed = new Set((availableCollections || []).map(x => typeof x === 'object' ? x.id : x));
    return c.displayIdTwins.filter(id => !listed.has(id));
}


function _dmHasUnlistedTwin(c) {
    return _dmUnlistedTwins(c).length > 0;
}


// The marker appended to an ID cell whose name is not this collection's alone.
function _dmDuplicateFlag(c) {
    const wrap = document.createDocumentFragment();
    const flag = document.createElement('span');
    flag.className = 'dm-dup-display-id text-xs';
    flag.textContent = 'duplicate';
    const unlisted = _dmUnlistedTwins(c);
    flag.title = unlisted.length
        ? 'A collection entry with no data also answers to this display ID and '
          + 'has no row here: ' + unlisted.join(', ') + '. Delete that entry by id '
          + '(the daily ops report lists it under "Leftover collection entries").'
        : 'Another collection answers to this display ID. Open either '
          + 'row in Edit Collections and give one of them a name of its own.';
    wrap.appendChild(document.createTextNode(' '));
    wrap.appendChild(flag);
    return wrap;
}


// Every display ID more than one collection answers to, as a Set of keys.
// Only non-empty when data predates the uniqueness guard.
function _dmDuplicateDisplayKeys() {
    const seen = new Set(), dupes = new Set();
    (availableCollections || []).forEach(c => {
        const key = _dmDisplayKey(_dmDisplayName(c));
        if (!key) return;
        if (seen.has(key)) dupes.add(key);
        seen.add(key);
    });
    return dupes;
}


// The OTHER collection already using this display ID, or null. A collection
// answers to its own ID as well as its display ID - the ID is what listings
// fall back to and what this modal's header shows whatever the label says -
// so naming one collection after another's ID is a clash too. The endpoint
// enforces the same rule; this only means the operator hears about it while
// the box is still in front of them rather than after the write.
function _dmDisplayIdClash(value, collectionId) {
    const key = _dmDisplayKey(value);
    if (!key) return null;
    const hit = (availableCollections || []).find(c => {
        const id = typeof c === 'object' ? c.id : c;
        return id !== collectionId
            && (_dmDisplayKey(id) === key || _dmDisplayKey(_dmDisplayName(c)) === key);
    });
    return hit ? (typeof hit === 'object' ? hit.id : hit) : null;
}


// The modal's only save feedback for a single collection. tone: 'ok' | 'err'
// | '' (in progress). The 'ok' word clears itself; an error stays put.
function _dmSetSaveState(text, tone = '') {
    const el = document.getElementById('edit-collection-save-state');
    if (!el) return;
    el.textContent = text || '';
    el.className = `dm-save-state text-xs${tone ? ' ' + tone : ''}`;
    if (tone === 'ok') {
        const stamp = (el._dmStamp = (el._dmStamp || 0) + 1);
        setTimeout(() => {
            if (el._dmStamp === stamp && el.textContent === text) el.textContent = '';
        }, 2500);
    }
}


// Write the single collection's whole record — tags, hidden, account, and the
// display ID as last committed. Called by every field in the modal the moment
// it changes; there is no Save button on this path.
function _dmAutoSaveCollection() {
    if (bulkEditMode || !currentEditCollectionId) return Promise.resolve(false);
    const cid = currentEditCollectionId;
    const hiddenCheckbox = document.getElementById('edit-collection-hidden');
    const payload = {
        collection_id: cid,
        display_collection_id: _dmSavedDisplayId,
        tags: [...currentEditCollectionTags],
        hidden: hiddenCheckbox ? !!hiddenCheckbox.checked : false,
        ..._dmAccountPayload(false),
    };
    _dmSetSaveState('Saving\u2026');
    // Set before the write, not in its callback: closeEditCollectionModal
    // reads this synchronously, and a display ID committed BY closing would
    // otherwise leave the table behind the modal showing the old name.
    _dmSavedAnything = true;
    _dmSaveChain = _dmSaveChain
        .catch(() => {})
        .then(() => fetch('/api/manage/collection/save_annotation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        }))
        .then(r => r.json())
        .then(data => {
            if (data.status !== 'success') {
                _dmSetSaveState(`Not saved: ${data.error || 'unknown error'}`, 'err');
                return false;
            }
            _dmSetSaveState('Saved', 'ok');
            // Keep the row behind the modal honest without re-rendering the
            // whole table on every tick of a checkbox.
            const obj = availableCollections.find(
                c => (typeof c === 'object' ? c.id : c) === cid);
            if (obj && typeof obj === 'object') {
                obj.tags = [...payload.tags];
                obj.hidden = payload.hidden;
                obj.displayId = payload.display_collection_id;
                if ('user_id' in payload) obj.user_id = payload.user_id;
            }
            return true;
        })
        .catch(err => {
            console.error('Error saving collection:', err);
            _dmSetSaveState('Not saved \u2014 check your connection', 'err');
            return false;
        });
    return _dmSaveChain;
}


// Display ID commits on blur or Enter, not per keystroke. While the box
// differs from what is stored it offers a Save button; a write leaves a green
// tick behind, so an edited ID never looks like a saved one.
function dmDisplayIdInput() {
    if (bulkEditMode) return;
    const input = document.getElementById('edit-collection-display-id');
    const btn = document.getElementById('edit-collection-display-id-save');
    const tick = document.getElementById('edit-collection-display-id-tick');
    if (!input) return;
    const dirty = input.value !== _dmSavedDisplayId;
    if (btn) btn.style.display = dirty ? '' : 'none';
    if (tick && dirty) tick.style.display = 'none';
}
window.dmDisplayIdInput = dmDisplayIdInput;

function dmDisplayIdCommit() {
    if (bulkEditMode || !currentEditCollectionId) return;
    const input = document.getElementById('edit-collection-display-id');
    const btn = document.getElementById('edit-collection-display-id-save');
    const tick = document.getElementById('edit-collection-display-id-tick');
    if (!input || input.value === _dmSavedDisplayId) return;
    // A display ID belongs to one collection. Refuse the rename here rather
    // than let the endpoint refuse it, so the box keeps what was typed and
    // the Save button stays offered for the next attempt.
    const clash = _dmDisplayIdClash(
        input.value.trim() || currentEditCollectionId, currentEditCollectionId);
    if (clash) {
        _dmSetSaveState(`Not saved: ${clash} already uses that display ID`, 'err');
        if (btn) btn.style.display = '';
        if (tick) tick.style.display = 'none';
        return;
    }
    const rejected = _dmSavedDisplayId;
    _dmSavedDisplayId = input.value;
    if (btn) btn.style.display = 'none';
    _dmAutoSaveCollection().then(ok => {
        if (!ok) {
            // Every autosave carries the whole record, so a name the endpoint
            // refused must not ride along on the next tag tick.
            _dmSavedDisplayId = rejected;
            if (btn) btn.style.display = '';
            return;
        }
        if (!tick) return;
        tick.style.display = '';
        const stamp = (tick._dmStamp = (tick._dmStamp || 0) + 1);
        setTimeout(() => { if (tick._dmStamp === stamp) tick.style.display = 'none'; }, 3000);
    });
}
window.dmDisplayIdCommit = dmDisplayIdCommit;

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
    _dmSavedDisplayId = displayIdInput.value;
    dmDisplayIdInput();
    const tick = document.getElementById('edit-collection-display-id-tick');
    if (tick) tick.style.display = 'none';
    _dmSetSaveState('');
    _dmSavedAnything = false;

    const hiddenCheckbox = document.getElementById('edit-collection-hidden');
    if (hiddenCheckbox) {
        hiddenCheckbox.checked = !!collectionObj.hidden;
        hiddenCheckbox.indeterminate = false;
        hiddenCheckbox.onchange = () => _dmAutoSaveCollection();
    }

    // A single collection saves itself; the button is the bulk edit's alone.
    const saveBtn = document.getElementById('save-collection-btn');
    if (saveBtn) {
        saveBtn.style.display = 'none';
        saveBtn.disabled = false;
    }

    const deleteBtn = document.getElementById('delete-collection-btn');
    if (deleteBtn) {
        deleteBtn.style.display = '';
        deleteBtn.disabled = false;
        deleteBtn.textContent = 'Delete Collection';
    }

    _dmResetCollectionDetails([collectionObj]);
    dm_renderTags();
    _dmFillAccountSelect(collectionObj.user_id || '', false);
    dmEnrichLoad(currentEditCollectionId);
    _dmShowEditCollectionModal();
}


// Bulk mode leads with a "leave unchanged" option (value '__keep__') so a
// tag-only edit never rewrites every selected collection's account.
const _DM_ACCOUNT_KEEP = '__keep__';

function _dmFillAccountSelect(selected, bulk) {
    const sel = document.getElementById('edit-collection-user');
    if (!sel) return;
    sel.disabled = true;
    sel.innerHTML = '<option value="">Loading accounts...</option>';
    // Autosave only once the options are in: a change event can only come
    // from the operator after that, and until then _dmAccountPayload sees a
    // disabled select and leaves the stored account alone.
    sel.onchange = bulk ? null : () => _dmAutoSaveCollection();
    loadAccounts(true).then(accounts => {
        populateAccountSelect(sel, accounts, {
            blankLabel: '— no account —',
            extraFirst: bulk ? { value: _DM_ACCOUNT_KEEP, label: '(leave unchanged)' } : null,
            selected: bulk ? '' : selected,
        });
        if (bulk) sel.value = _DM_ACCOUNT_KEEP;
        sel.disabled = false;
    });
}

// The account field of the save payload. Single edit always sends it (null =
// unassigned); bulk edit omits it unless the admin picked something.
function _dmAccountPayload(bulk) {
    const sel = document.getElementById('edit-collection-user');
    if (!sel || sel.disabled) return {};
    if (bulk && sel.value === _DM_ACCOUNT_KEEP) return {};
    return { user_id: sel.value || null };
}


// Reveal the modal, and make a click on the backdrop close it - the same
// discard-without-saving the [x] does. The handler is on the overlay, so only
// a click that lands outside .modal-content counts.
function _dmShowEditCollectionModal() {
    const modal = document.getElementById('editCollectionModal');
    if (!modal) return;
    modal.onclick = (e) => { if (e.target === modal) closeEditCollectionModal(); };
    modal.style.display = 'block';
}

// Collection details opens collapsed on every open, and renders nothing until
// it is expanded: for a single collection the render costs a personality
// request, which is wasted on the many opens that only edit the fields above.
let _dmDetailObjs = null;
let _dmDetailsRendered = false;

function _dmResetCollectionDetails(objs) {
    _dmDetailObjs = objs;
    _dmDetailsRendered = false;
    const panel = document.getElementById('edit-collection-details-panel');
    if (panel) panel.style.display = 'none';
    const btn = document.getElementById('edit-collection-details-toggle');
    if (btn) {
        btn.classList.remove('open');
        btn.setAttribute('aria-expanded', 'false');
        // One collection gets its persona (the donated-data view); several get
        // a metadata row each, which is not a persona and must not claim to be.
        const label = btn.querySelector('.dm-disclosure-label');
        if (label) {
            label.textContent = (objs && objs.length === 1)
                ? 'Collection persona' : 'Collection details';
        }
    }
    const box = document.getElementById('edit-collection-details');
    if (box) box.innerHTML = '';
}

function dmToggleCollectionDetails() {
    dmToggleAdvanced('edit-collection-details-panel', 'edit-collection-details-toggle');
    const panel = document.getElementById('edit-collection-details-panel');
    const open = !!panel && panel.style.display !== 'none';
    if (open && !_dmDetailsRendered) {
        _dmDetailsRendered = true;
        _dmRenderCollectionDetails(_dmDetailObjs);
    }
}
window.dmToggleCollectionDetails = dmToggleCollectionDetails;

// Read-only metadata block in the edit modal. One collection gets a compact
// meta line plus the donated-data personality view (shared renderer from
// my_collections.js); several get a row each, so a bulk edit can still be
// checked against the collections it will touch.
function _dmRenderCollectionDetails(objs) {
    const box = document.getElementById('edit-collection-details');
    if (!box) return;
    box.innerHTML = '';

    const present = (objs || []).filter(o => o && typeof o === 'object');
    if (present.length === 0) {
        box.innerHTML = '<span style="color: var(--color-text-tertiary);">No metadata available.</span>';
        return;
    }

    if (present.length === 1) {
        const c = present[0];
        const meta = document.createElement('div');
        meta.className = 'text-xs';
        meta.style.cssText = 'margin-bottom: 12px; color: var(--color-text-tertiary);';
        const parts = [`ID: ${c.id}`];
        parts.push(`Account: ${_dmAccountCell(c) || 'no account'}`);
        const added = fypFmtDate(c.other?.ts_added_to_dataset, '');
        if (added) parts.push(`Added: ${added}`);
        const tz = _dmTimezoneLabel(c);
        if (tz) parts.push(`Timezone: ${tz}`);
        if (c.participants?.campaign) parts.push(`Campaign: ${c.participants.campaign}`);
        if (c.participants?.donationType) parts.push(`Donation type: ${c.participants.donationType}`);
        meta.textContent = parts.join(' · ');
        box.appendChild(meta);

        const holder = document.createElement('div');
        box.appendChild(holder);
        if (typeof mycRenderPersonality === 'function') {
            holder.innerHTML = '<span class="text-sm" style="color: var(--color-text-tertiary);">Computing the personality view&hellip;</span>';
            fetch(`/api/my/collections/${encodeURIComponent(c.id)}/personality`)
                .then(r => r.json().then(data => ({ ok: r.ok, data })))
                .then(({ ok, data }) => {
                    if (!ok) {
                        holder.innerHTML = `<span class="text-sm" style="color: var(--color-text-tertiary);">${(data && data.error) || 'No personality view available.'}</span>`;
                        return;
                    }
                    // Neutral voice: this is an admin looking at somebody
                    // else's collection, so the participant page's second-person
                    // copy ("your golden hour", "no judgement") would be both
                    // odd and misdirected. Same cards, same numbers.
                    mycRenderPersonality(holder, data, { voice: 'neutral' });
                })
                .catch(() => {
                    holder.innerHTML = '<span class="text-sm" style="color: var(--color-text-tertiary);">No personality view available.</span>';
                });
        }
        return;
    }

    // Collection ID is the row label in this view, so it isn't also a column.
    const fields = _EDIT_COLLECTION_DETAILS.filter(f => f.label !== 'Collection ID');
    const table = document.createElement('table');
    table.className = 'text-xs';
    table.style.cssText = 'width: 100%; border-collapse: collapse; white-space: nowrap;';
    const thStyle = 'padding: 4px 8px 4px 0; text-align: left; color: var(--color-text-tertiary); font-weight: var(--weight-normal); border-bottom: 1px solid var(--color-border-strong);';
    const tdStyle = 'padding: 4px 8px 4px 0; border-bottom: 1px solid var(--color-border);';

    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    ['Collection', ...fields.map(f => f.label)].forEach(label => {
        const th = document.createElement('th');
        th.style.cssText = thStyle;
        th.textContent = label;
        headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    present.forEach(c => {
        const tr = document.createElement('tr');
        const idTd = document.createElement('td');
        idTd.style.cssText = tdStyle;
        idTd.title = c.id;
        idTd.textContent = c.displayId || c.id;
        tr.appendChild(idTd);
        fields.forEach(f => {
            const td = document.createElement('td');
            td.style.cssText = tdStyle;
            const raw = f.get(c);
            if (raw === '' || raw === null || raw === undefined) {
                td.style.color = 'var(--color-text-tertiary)';
                td.textContent = '—';
            } else {
                td.textContent = String(raw);
            }
            tr.appendChild(td);
        });
        tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    box.appendChild(table);
}

function closeEditCollectionModal() {
    // A display ID typed but never committed still counts as an edit — the
    // box has no other way out, and closing is how most people leave it. Same
    // for a plan setting still inside its debounce: opening the next
    // collection resets the panel, which would drop the write silently.
    dmDisplayIdCommit();
    dmEnrichAutoSaveFlush();
    document.getElementById('editCollectionModal').style.display = 'none';
    currentEditCollectionId = null;
    bulkEditMode = false;
    hiddenUserTouched = false;
    const displayIdInput = document.getElementById('edit-collection-display-id');
    if (displayIdInput) {
        displayIdInput.disabled = false;
        displayIdInput.placeholder = '';
    }
    // The table behind the modal is re-rendered once, on the way out, rather
    // than after each autosaved field — a re-render drops the scroll position
    // and the multi-select.
    if (_dmSavedAnything) {
        _dmSavedAnything = false;
        _dmSaveChain.catch(() => {}).then(() => loadAvailableCollections());
    }
    _dmSetSaveState('');
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
    // Single collection: a tick IS the save. Bulk waits for the button — one
    // tick there rewrites every selected collection.
    _dmAutoSaveCollection();
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
        _dmAutoSaveCollection();
    }
}

// The bulk edit's Apply. A single collection no longer reaches this: every
// field there writes itself (_dmAutoSaveCollection), and the button is hidden.
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
            hidden: isHidden,
            ..._dmAccountPayload(false),
        };

        fetch('/api/manage/collection/save_annotation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
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
                tags: finalTags,
                ..._dmAccountPayload(true),
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
                        headers: { 'Content-Type': 'application/json' },
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


// Delete whatever the modal currently has open: one collection, or the whole
// multi-select. Both go through a single worker run — deleting N collections
