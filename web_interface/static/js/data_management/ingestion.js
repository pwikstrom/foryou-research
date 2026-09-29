// Data Pipeline: Data ingestion: raw sources, the upload modal (account pickers, zip slimming,
// tags) and the ingestion history ledger.
// One of the js/data_management/*.js files; they share one global scope and
// load in a fixed order (templates/index.html).

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

// Keep the ingestion view fresh while it is on screen: participants upload
// from My Collections without ever touching this page, so a teacher watching
// a class donate needs the awaiting-files list to update by itself.
setInterval(() => {
    if (document.hidden) return;
    const page = document.getElementById('dm-page-ingestion');
    if (!page || page.offsetParent === null) return;  // page not visible
    loadIngestionSources();
}, 20000);

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
                    ${_escapeHtml(f.original_filename || f.filename)}
                    <span class="text-xxs font-bold" style="color: ${badgeColor}; margin-left: 8px; text-transform: uppercase;">${badgeLabel}</span>
                </div>
                ${f.original_filename && f.original_filename !== f.filename
                    ? `<div class="text-xxs" style="color: var(--color-text-tertiary); word-break: break-all;">stored as ${_escapeHtml(f.filename)}</div>`
                    : ''}
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
    // 'note' = sections the uploader withheld: their choice, not drift.
    const sevColor = finding.severity === 'quarantine' ? 'var(--color-danger)'
        : finding.severity === 'note' ? 'var(--color-text-tertiary)' : 'var(--color-warning)';
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
        processed.kept_rows !== undefined && raw.raw_rows !== undefined
            ? `parser kept ${Number(processed.kept_rows).toLocaleString()} of ${Number(raw.raw_rows).toLocaleString()} rows`
            : (processed.kept_ratio !== undefined ? `kept ratio ${processed.kept_ratio}` : null),
        processed.parse_rate !== undefined
            ? `${Math.round(Number(processed.parse_rate) * 100)}% of the ${Number(processed.ingestible_rows).toLocaleString()} rows in ingested sections`
            : null,
        processed.null_item_id_frac !== undefined ? `null item_id ${processed.null_item_id_frac}` : null,
    ].filter(Boolean).join(' · ');

    const overlay = document.createElement('div');
    overlay.id = 'structureReviewModal';
    overlay.className = 'upload-modal-overlay';
    overlay.innerHTML = `
        <div class="upload-modal" style="max-width: 640px; max-height: 80vh; overflow-y: auto;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                <h3 style="margin: 0; word-break: break-all;">Structure review — ${_escapeHtml(verdict.original_filename || verdict.filename)}</h3>
                ${verdict.original_filename && verdict.original_filename !== verdict.filename
                    ? `<div class="text-xxs" style="color: var(--color-text-tertiary); word-break: break-all;">stored as ${_escapeHtml(verdict.filename)}</div>`
                    : ''}
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
        headers: { 'Content-Type': 'application/json' },
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
    // A refresh landing mid-run (the demo hand-off calls this) must not undo
    // the in-flight "Processing…" state that pollIngestRefreshStatus owns.
    if (_ingestRefreshPollActive) return;
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
            const uploaderSuffix = f.user_id
                ? ` <span class="text-xxs" style="color: var(--color-accent);">uploaded by ${escapeHtml(f.user_id)}</span>`
                : '';
            // Stored names are generated; the name the uploader knows is the
            // original one, so lead with that and show the stored name after it.
            const shownName = f.original_filename ? escapeHtml(f.original_filename) : escapeHtml(f.filename);
            const storedSuffix = (f.original_filename && f.original_filename !== f.filename)
                ? ` <span class="text-xxs" style="color: var(--color-text-tertiary);">stored as ${escapeHtml(f.filename)}</span>`
                : '';
            return `<li style="margin-left: 16px; word-break: break-all;">${shownName}${storedSuffix}${cidSuffix}${tagSuffix}${uploaderSuffix}</li>`;
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
        headers: { 'Content-Type': 'application/json' },
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
        headers: { 'Content-Type': 'application/json' },
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

// --- User-account pickers -------------------------------------------------
// A collection belongs to at most one user account. Both the upload modal and
// the Edit Collection modal offer the same <select> of accounts, filled from
// /api/manage/accounts (members first, then participant accounts, then
// p-N placeholders). Cached per page load; refreshed when a modal opens.

let _dmAccounts = null;

function loadAccounts(force = false) {
    if (_dmAccounts && !force) return Promise.resolve(_dmAccounts);
    return fetch('/api/manage/accounts')
        .then(r => r.ok ? r.json() : [])
        .then(rows => { _dmAccounts = Array.isArray(rows) ? rows : []; return _dmAccounts; })
        .catch(() => { _dmAccounts = _dmAccounts || []; return _dmAccounts; });
}

function _dmAccountLabel(acc) {
    const name = acc.display_username && acc.display_username.trim();
    let label = name ? `${name} (${acc.username})` : acc.username;
    if (acc.placeholder) label += ' · placeholder';
    else if (acc.account_kind === 'participant') label += acc.can_login ? ' · participant' : ' · participant, no login';
    return label;
}

// Fill `sel` with the account options. `blankLabel` is the first option
// (value ''); `extraFirst` is an optional extra leading option {value,label}
// (bulk edit uses it for "leave unchanged"). Preserves `selected` if given.
function populateAccountSelect(sel, accounts, { blankLabel = '— no account —', extraFirst = null, selected = '' } = {}) {
    sel.innerHTML = '';
    if (extraFirst) {
        const o = document.createElement('option');
        o.value = extraFirst.value; o.textContent = extraFirst.label; sel.appendChild(o);
    }
    const blank = document.createElement('option');
    blank.value = ''; blank.textContent = blankLabel; sel.appendChild(blank);
    accounts.forEach(acc => {
        const o = document.createElement('option');
        o.value = acc.username; o.textContent = _dmAccountLabel(acc); sel.appendChild(o);
    });
    if (selected && !accounts.some(a => a.username === selected)) {
        // A link to an account that no longer exists: keep it visible so the
        // admin can see (and fix) it rather than silently dropping it.
        const o = document.createElement('option');
        o.value = selected; o.textContent = `${selected} (unknown account)`; sel.appendChild(o);
    }
    sel.value = selected || (extraFirst ? extraFirst.value : '');
}


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

    const accSel = document.getElementById('uploadUserId');
    if (accSel) {
        accSel.innerHTML = '<option value="">Loading accounts...</option>';
        accSel.disabled = true;
        loadAccounts(true).then(accounts => {
            populateAccountSelect(accSel, accounts);
            accSel.disabled = false;
        });
    }

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

// --- Client-side donation-zip slimming ---
// The implementation moved to the shared js/donation_zip.js (also used by the
// participant upload on My Collections); thin aliases keep call sites intact.

const loadZipLib = (...a) => DonationZip.loadZipLib(...a);
const formatBytes = (...a) => DonationZip.formatBytes(...a);
const repackDonationZip = (...a) => DonationZip.repackDonationZip(...a);

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
    const accSel = document.getElementById('uploadUserId');
    formData.append('user_id', accSel && !accSel.disabled ? accSel.value : '');

    const statusDiv = document.getElementById('uploadStatus');
    const submitBtn = document.getElementById('uploadSubmitBtn');
    submitBtn.disabled = true;
    statusDiv.textContent = 'Uploading...';
    statusDiv.style.color = 'var(--color-text-tertiary)';
    statusDiv.style.display = 'block';

    fetch('/api/manage/ingestion/upload', {
        method: 'POST',
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
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'started') {
                pollIngestRefreshStatus(btn, originalText, originalClass);
            } else {
                console.error("Ingestion refresh error:", data.message || data.error);
                showToast('Could not start processing: '
                    + (data.message || data.error || 'unknown error'), 'error', 10000);
                restoreButton();
            }
        })
        .catch(err => {
            console.error("Error triggering refresh:", err);
            showToast('Could not reach the server to start processing.', 'error', 10000);
            restoreButton();
        });
}

const _ingestOutcomeLabels = {
    added_as_new: { label: 'Added to new collection', color: 'var(--color-text-primary)' },
    merged_with_existing: { label: 'Added to existing collection', color: 'var(--color-text-primary)' },
    fully_deduped: { label: 'Skipped — already in dataset', color: 'var(--color-text-tertiary)' },
    discarded_at_load: { label: 'Skipped — too few rows', color: 'var(--color-text-tertiary)' },
    manually_excluded: { label: 'Manually excluded', color: 'var(--color-text-tertiary)' },
    // Seeded from the legacy flat skip list, which recorded only a filename.
    // Earlier builds reported these as "too few rows" with 0 rows read — a
    // reason and a count the legacy file never held.
    skipped_legacy: { label: 'Skipped — reason not recorded', color: 'var(--color-text-tertiary)' },
    quarantined_structure: { label: 'Quarantined — structure drift (review above)', color: 'var(--color-danger)' },
    load_failed: { label: 'Failed to read — will retry next refresh', color: 'var(--color-danger)' },
    blocked_name_collision: { label: 'Not ingested — stored name already taken (stays pending)', color: 'var(--color-danger)' },
};

// Plain-language labels for the per-file drop-reason breakdown captured by
// the ingest load loop (ledger key: dropped = {reason: count}).
const _ingestDropReasonLabels = {
    not_parseable: (n) => `${n.toLocaleString()} row${n === 1 ? '' : 's'} couldn't be interpreted (unreadable timestamp or missing video reference)`,
    missing_required: (n) => `${n.toLocaleString()} row${n === 1 ? '' : 's'} ${n === 1 ? 'was' : 'were'} missing essential information and ${n === 1 ? 'was' : 'were'} excluded`,
    outside_whitelist: (n) => `${n.toLocaleString()} record${n === 1 ? '' : 's'} in sections this platform's ingestion does not use (excluded by design)`,
    share_copies_merged: (n) => `${n.toLocaleString()} identical share record${n === 1 ? '' : 's'} merged into the send ${n === 1 ? 'it belongs' : 'they belong'} to (one video sent to several friends at once; the count is kept on the share)`,
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

function _ingestPlaysCellHtml(r) {
    // Viewing rows kept. A Zeeschuimer capture has no DDP structure and emits
    // no play events at all — every row is an observation of what the feed
    // showed — so the hover names them that way rather than calling them plays.
    const v = r.play_rows;
    if (v === undefined || v === null) {
        return '<span style="color: var(--color-text-tertiary);">—</span>';
    }
    const noun = r.source === 'zeeschuimer' ? 'observation' : 'play';
    const title = v === 0
        ? `No viewing activity — this donation contributed no ${noun}s.`
        : `${v.toLocaleString()} ${noun}${v === 1 ? '' : 's'} kept`;
    const style = v === 0 ? 'color: var(--color-danger); font-weight: 600;' : '';
    return `<span style="${style}" title="${_escapeHtml(title)}">${v.toLocaleString()}</span>`;
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
            const notesLine = ((r.outcome === 'quarantined_structure' || r.outcome === 'load_failed' || r.outcome === 'blocked_name_collision') && r.notes)
                ? `<div class="text-xxs" style="color: var(--color-text-tertiary);">${_escapeHtml(r.notes)}</div>`
                : '';
            return `
                <tr>
                    <td style="${tdStyle}">
                        <div class="text-sm" style="word-break: break-all;">${_escapeHtml(r.original_filename || r.filename)}</div>
                        ${r.original_filename && r.original_filename !== r.filename
                            ? `<div class="text-xxs" style="color: var(--color-text-tertiary); word-break: break-all;">stored as ${_escapeHtml(r.filename)}</div>`
                            : ''}
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
                    <td style="${numStyle}">${_ingestPlaysCellHtml(r)}</td>
                    <td style="${tdStyle} max-width: 260px;">${_ingestDroppedCellHtml(r)}</td>
                </tr>
            `;
        }).join('');

        wrap.innerHTML = `
            <table class="text-sm" style="width: 100%; border-collapse: collapse; min-width: 960px;">
                <thead>
                    <tr>
                        <th style="${thStyle}">File</th>
                        <th style="${thStyle}">Outcome</th>
                        <th style="${thStyle} text-align: right;">Raw rows</th>
                        <th style="${thStyle} text-align: right;">Processed</th>
                        <th style="${thStyle} text-align: right;">Rows kept</th>
                        <th style="${thStyle} text-align: right;" title="Viewing rows kept — plays, or observations for Zeeschuimer captures, which have no play events. A donation can keep thousands of rows (favourites, followers) and still contribute no viewing.">Plays</th>
                        <th style="${thStyle}">Rows left out — why</th>
                    </tr>
                </thead>
                <tbody>${rowsHtml}</tbody>
            </table>
        `;
    }

    // Reconciliation block: explain why the net dataset change is smaller
    // than the rows this run's files kept. The merge dedupes the WHOLE
    // dataset, so the gap has two sources: older copies a new donation
    // replaced in its own collection, and duplicates already stored in other
    // collections that the run cleared. Runs recorded before that split was
    // emitted only carry the total.
    const contributed = data.rows_contributed_by_new_files ?? 0;
    const superseded = data.rows_superseded_in_existing_collections ?? 0;
    if (reconcileEl) {
        if (superseded > 0) {
            const replaced = data.rows_replaced_by_this_run;
            const elsewhere = data.rows_removed_elsewhere;
            const hasSplit = typeof replaced === 'number' && typeof elsewhere === 'number';
            const net = rowsAdded ?? 0;
            const para = (html) => `<div class="text-sm" style="color: var(--color-text-secondary); margin-top: 6px;">${html}</div>`;
            const listHtml = (items) => items ? `<ul class="text-xxs" style="color: var(--color-text-tertiary); margin: 6px 0 0 20px; padding: 0;">${items}</ul>` : '';
            let body = para(`This run's files kept ${contributed.toLocaleString()} rows, but the dataset changed by ${net >= 0 ? '+' : ''}${net.toLocaleString()} rows.`);
            if (!hasSplit) {
                body += para(`${superseded.toLocaleString()} rows already in the dataset were removed as duplicates while merging. This run was recorded before the Hub noted which collections they came from.`);
            } else {
                if (replaced > 0) {
                    const mergedLines = perFile
                        .filter(r => r.outcome === 'merged_with_existing')
                        .map(r => {
                            const cid = r.canonical_collection_id ? ` in collection "${_escapeHtml(r.canonical_collection_id)}"` : '';
                            return `<li><code>${_escapeHtml(r.filename)}</code> joined an earlier donation${cid}.</li>`;
                        })
                        .join('');
                    body += para(`${replaced.toLocaleString()} rows were replaced: a new file repeated events that an earlier donation to the same collection had already supplied. Each event is stored once, so the older copy was removed and the newest donation's copy kept.`)
                        + listHtml(mergedLines);
                }
                if (elsewhere > 0) {
                    const byCid = data.rows_removed_elsewhere_by_collection || {};
                    const cidLines = Object.entries(byCid)
                        .map(([cid, n]) => `<li>${Number(n).toLocaleString()} from collection "${_escapeHtml(cid)}"</li>`)
                        .join('');
                    body += para(`${elsewhere.toLocaleString()} rows were removed from collections this run did not add to. They were duplicate events already stored there, for example left behind by a data migration. Each processing run removes duplicates across the whole dataset, so they would have gone on any run. This run's own files lost nothing to them.`)
                        + listHtml(cidLines);
                }
            }
            reconcileEl.innerHTML = `
                <div class="text-sm font-semibold">Why the net change is smaller than the rows added</div>
                ${body}
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
    const legacyWrapEl = document.getElementById('ingestion-history-legacy');
    const legacyCountEl = document.getElementById('ingestion-history-legacy-count');
    const legacyTableEl = document.getElementById('ingestion-history-legacy-wrap');
    if (!panel || !wrap) return;

    if (!entries.length) {
        panel.style.display = 'none';
        return;
    }

    // Entries carried over from the legacy flat skip list hold nothing but a
    // filename — no reason, counts, provenance or date. Left in the main table
    // they outnumber the real history several times over and read as a pile of
    // unexplained failures, so they get their own collapsed block.
    const recorded = entries.filter(r => r.outcome !== 'skipped_legacy');
    const legacy = entries.filter(r => r.outcome === 'skipped_legacy');

    const thStyle = 'padding: 6px 8px; text-align: left; border-bottom: 2px solid var(--color-border-strong); font-weight: var(--weight-semibold);';
    const tdStyle = 'padding: 6px 8px; border-bottom: 1px solid var(--color-border); vertical-align: top;';
    const numStyle = tdStyle + ' text-align: right; font-variant-numeric: tabular-nums;';
    const numOrDash = (v) => (v === undefined || v === null) ? '—' : Number(v).toLocaleString();

    const rowHtml = (r) => {
        const meta = _ingestOutcomeLabels[r.outcome] || { label: r.outcome, color: 'var(--color-text-secondary)' };
        const provenance = [r.platform, r.source].filter(Boolean).join(' · ');
        // Who uploaded it (copied from the upload manifest). An admin upload on
        // a participant's behalf also names the account it was linked to.
        let uploaderLine = '';
        if (r.uploaded_by || r.user_id) {
            uploaderLine = r.uploaded_by ? `uploaded by ${r.uploaded_by}` : `account: ${r.user_id}`;
            if (r.uploaded_by && r.user_id && r.user_id !== r.uploaded_by) uploaderLine += ` for ${r.user_id}`;
        }
        return `
            <tr>
                <td style="${tdStyle}">
                    <div class="text-sm" style="word-break: break-all;">${_escapeHtml(r.filename)}</div>
                    ${uploaderLine ? `<div class="text-xxs" style="color: var(--color-text-tertiary); word-break: break-all;">${_escapeHtml(uploaderLine)}</div>` : ''}
                    ${provenance ? `<div class="text-xxs" style="color: var(--color-text-tertiary);">${_escapeHtml(provenance)}</div>` : ''}
                </td>
                <td style="${tdStyle} color: ${meta.color};">
                    <div class="text-sm">${meta.label}</div>
                    ${r.collection_id ? `<div class="text-xxs" style="color: var(--color-text-tertiary);">collection: ${_escapeHtml(r.collection_id)}</div>` : ''}
                </td>
                <td style="${numStyle}">${numOrDash(r.raw_rows)}</td>
                <td style="${numStyle}">${numOrDash(r.kept_rows)}</td>
                <td style="${numStyle}">${_ingestPlaysCellHtml(r)}</td>
                <td style="${tdStyle} max-width: 280px;">${_ingestDroppedCellHtml(r)}</td>
                <td style="${tdStyle} text-align: right; font-variant-numeric: tabular-nums; color: var(--color-text-tertiary);">${fypFmtDate(r.ts_last_seen, '—')}</td>
            </tr>
        `;
    };

    const tableHtml = (rows) => `
        <table class="text-sm" style="width: 100%; border-collapse: collapse; min-width: 960px;">
            <thead>
                <tr>
                    <th style="${thStyle}">File</th>
                    <th style="${thStyle}">Outcome</th>
                    <th style="${thStyle} text-align: right;">Rows read</th>
                    <th style="${thStyle} text-align: right;">Rows kept</th>
                    <th style="${thStyle} text-align: right;" title="Viewing rows kept — plays, or observations for Zeeschuimer captures, which have no play events. A donation can keep thousands of rows (favourites, followers) and still contribute no viewing.">Plays</th>
                    <th style="${thStyle}">Rows left out — why</th>
                    <th style="${thStyle} text-align: right;">Last processed</th>
                </tr>
            </thead>
            <tbody>${rows.map(rowHtml).join('')}</tbody>
        </table>
    `;

    if (countEl) countEl.textContent = `(${recorded.length})`;
    wrap.innerHTML = recorded.length
        ? tableHtml(recorded)
        : '<div class="text-sm" style="color: var(--color-text-tertiary); padding: 8px 0;">No files have been processed since per-file intake logging was added.</div>';

    if (legacyWrapEl && legacyCountEl && legacyTableEl) {
        if (legacy.length === 0) {
            legacyWrapEl.style.display = 'none';
            legacyTableEl.innerHTML = '';
        } else {
            legacyCountEl.textContent = `(${legacy.length})`;
            legacyTableEl.innerHTML = tableHtml(legacy);
            legacyWrapEl.style.display = 'block';
        }
    }
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
                    // Without this the button just snapped back to its idle
                    // label and the failure was invisible outside the console.
                    showToast('Processing new collections failed — the pending files were not ingested. '
                        + 'Open View Log next to the button for the error.', 'error', 15000);
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

// Load the raw sources once at startup (self-guards on the ingestion sub-page
// being present). Runs here rather than with the other bootstraps in
// studies.js because loadIngestionSources is defined in this file.
loadIngestionSources();
