// Data Pipeline: Bulk collection actions (select, delete, edit selected), queueing voted videos,
// and the theme-change re-render of the enrichment chart.
// One of the js/data_management/*.js files; they share one global scope and
// load in a fixed order (templates/index.html).

function dm_deleteCollection() {
    const ids = bulkEditMode
        ? [...selectedCollectionIds]
        : (currentEditCollectionId ? [currentEditCollectionId] : []);
    if (ids.length === 0) return;

    const labelFor = (id) => {
        const obj = availableCollections.find(c => (typeof c === 'object' ? c.id : c) === id);
        return (obj && obj.displayId) || id;
    };
    const labels = ids.map(labelFor);
    const deleteBtn = document.getElementById('delete-collection-btn');

    if (deleteBtn) deleteBtn.disabled = true;

    const query = ids.map(id => `collection_id=${encodeURIComponent(id)}`).join('&');
    fetch(`/api/manage/collections/affected_studies?${query}`)
        .then(r => r.json())
        .then(async data => {
            const studies = (data && data.studies) || [];
            const studyClause = studies.length === 0
                ? (ids.length === 1
                    ? "No studies reference this collection."
                    : "No studies reference these collections.")
                : `${studies.length} study/studies will be refreshed: ${studies.join(", ")}.`;
            // Naming every collection is the point of the dialog in bulk mode;
            // past a dozen the list stops being readable, so it truncates.
            const shown = labels.slice(0, 12).join(", ");
            const more = labels.length > 12 ? `, and ${labels.length - 12} more` : "";
            const subject = ids.length === 1
                ? `collection "${labels[0]}"`
                : `${ids.length} collections?\n\n${shown}${more}`;
            const ok = await showAppConfirm(
                `Delete ${subject}${ids.length === 1 ? '?' : ''}\n\n` +
                `${studyClause}\n\n` +
                `Raw upload files will be moved to the archive folder and can be restored. ` +
                `Scraped video data and machine annotations will be kept.`,
                {
                    title: ids.length === 1 ? 'Delete collection' : `Delete ${ids.length} collections`,
                    okLabel: 'Delete',
                    danger: true,
                }
            );
            if (!ok) {
                if (deleteBtn) deleteBtn.disabled = false;
                return;
            }
            return fetch('/api/manage/collections/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ collection_ids: ids })
            })
                .then(r => r.json())
                .then(resp => {
                    if (resp && resp.status === 'started') {
                        closeEditCollectionModal();
                        pollCollectionDeleteStatus(ids, labels, deleteBtn);
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

function pollCollectionDeleteStatus(collectionIds, displayLabels, deleteBtn) {
    if (_collectionDeletePollActive) return;
    _collectionDeletePollActive = true;
    let done = false;

    const ids = Array.isArray(collectionIds) ? collectionIds : [collectionIds];
    const labels = Array.isArray(displayLabels) ? displayLabels : [displayLabels];
    const subject = ids.length === 1
        ? `"${labels[0]}"`
        : `${ids.length} collections`;

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
                    ids.forEach(id => selectedCollectionIds.delete(id));
                    updateEditSelectedButton();
                    const archived = (data.archived_files || []).length;
                    const failures = (data.archive_failures || []).length;
                    const affected = (data.affected_studies || []).length;
                    const dropped = data.rows_dropped || 0;
                    let msg = `Deleted ${subject}. `;
                    msg += `Dropped ${dropped.toLocaleString()} row(s), `;
                    msg += `archived ${archived} raw file(s)`;
                    if (failures > 0) msg += ` (${failures} archive failure(s))`;
                    msg += `. Refreshing ${affected} study/studies in the background.`;
                    showAppAlert(msg);
                    loadAvailableCollections();
                } else {
                    showAppAlert(`Failed to delete ${subject}. Check the task logs for details.`);
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
    dmEnrichHide();

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
    _dmSavedDisplayId = '';
    const dispSave = document.getElementById('edit-collection-display-id-save');
    if (dispSave) dispSave.style.display = 'none';
    const dispTick = document.getElementById('edit-collection-display-id-tick');
    if (dispTick) dispTick.style.display = 'none';
    _dmSetSaveState('');
    _dmSavedAnything = false;

    // Bulk keeps an explicit apply: here a single tag chip would otherwise
    // rewrite every selected collection the instant it is ticked.
    const saveBtn = document.getElementById('save-collection-btn');
    if (saveBtn) {
        saveBtn.style.display = '';
        saveBtn.disabled = false;
        saveBtn.textContent = `Apply to ${selectedIds.length} collections`;
    }

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

    // Delete acts on the whole selection here. The confirmation names every
    // collection and every study that will be refreshed, and the work runs as
    // one pass over the activity parquet rather than one task per collection.
    const deleteBtn = document.getElementById('delete-collection-btn');
    if (deleteBtn) {
        deleteBtn.style.display = '';
        deleteBtn.disabled = false;
        deleteBtn.textContent = `Delete ${selectedIds.length} Collections`;
    }

    _dmResetCollectionDetails(selectedObjs);
    dm_renderTags();
    _dmFillAccountSelect('', true);
    _dmShowEditCollectionModal();
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

// Both Plotly figures on this tab bake in resolved token colours, so a theme
// switch redraws them from their cached inputs: the enrichment chart from its
// last daily payload, and each open study modal's activity chart from the
// row's chart state.
window.addEventListener('theme-changed', () => {
    if (dmEnrichDailyCache) dmEnrichRenderChart(dmEnrichDailyCache);
    document.querySelectorAll('.study-edit-form .study-daily-chart').forEach(chartDiv => {
        if (chartDiv._plotlyInited) _renderDailyChart(chartDiv.closest('.study-edit-form'));
    });
});
