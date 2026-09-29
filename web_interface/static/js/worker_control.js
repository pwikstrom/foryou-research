// Background-worker controls: the process cards (start / stop, the optimistic
// "Starting…" state, the arm-consolidate prompt), the 1 s status poll that
// drives them, the log modal and its run picker, and the run-progress views.
// Loaded right after main.js, whose window.onload starts the status poll.

let previousProcessStates = {};

// Per-platform scraper process names (queue_scraper_<platform>), derived from
// the toggle buttons the enrichment template renders — one per registered
// platform. Falls back to the TikTok worker before the tab has rendered.
function scraperProcessNames() {
    const names = Array.from(document.querySelectorAll('[id^="queue_scraper_"][id$="-toggle"]'))
        .map(el => el.id.slice(0, -'-toggle'.length));
    return names.length ? names : ['queue_scraper_tiktok'];
}
let _pendingStopProcess = null;
let _activeLogModal = null;
// Which run the modal is showing ('' = the newest), how far through it we have
// read, the full text we hold (so the filter box can re-render without a
// refetch), and a signature of the run list so the poll only rebuilds the
// picker when the runs actually change — rebuilding every second would fight
// the user for the dropdown.
let _activeLogRun = '';
let _activeLogSince = 0;
let _activeLogText = '';
let _activeLogRunSig = '';
let _activeLogRunDone = false;
setInterval(updateLogs, 1000);

// Processes the user has just clicked Start on but for which the server has not
// yet reported a 'running' state. While a name sits here, setStatus keeps the
// card in an optimistic "Starting…" state so the 1 s status poll can't flip the
// button back to "Start"/"Refresh" during the dispatch + task-runner boot gap.
const pendingStarts = new Set();

// Apply the optimistic "Starting…" UI to a process card the instant the user
// clicks Start, before any network round-trip or status poll completes.
function markStarting(name) {
    pendingStarts.add(name);

    const statusEl = document.getElementById(`${name}-status`);
    if (statusEl) statusEl.className = 'status-indicator status-running';

    const toggleBtn = document.getElementById(`${name}-toggle`);
    if (toggleBtn) {
        toggleBtn.className = 'btn-running';
        toggleBtn.innerText = 'Starting…';
        toggleBtn.style.padding = '4px 12px';
        toggleBtn.onclick = null;
    }

    const text = document.getElementById(`${name}-text`);
    if (text) {
        text.innerText = 'Starting…';
        text.style.color = '';
    }
    const bar = document.getElementById(`${name}-bar`);
    if (bar) bar.style.width = '0%';

    // Failsafe: if the server never reports 'running' (e.g. a dispatch that
    // silently failed), don't wedge the card in "Starting…" forever — release
    // it so a later poll can restore the real (idle) state.
    setTimeout(() => { pendingStarts.delete(name); }, 20000);
}

// Latest card health for the enrichment processes ({status, summary,
// checked_at} per scraper platform + annotation), cached on window._cardHealth
// by js/data_management/enrichment.js's fetchEnrichmentStats. Returns null for processes
// without a health chip or before the stats have loaded.
function _healthEntryForProcess(name) {
    const h = window._cardHealth;
    if (!h) return null;
    if (name.startsWith('queue_scraper_')) {
        return (h.platforms || {})[name.slice('queue_scraper_'.length)] || null;
    }
    if (name === 'queue_annotator' || name === 'queue_annotator_batch') {
        return h.annotation || null;
    }
    return null;
}

// Ask before starting a scraper/annotator whose health chip is yellow or red.
// Gray (unknown / check not run yet) starts silently. Resolves true to proceed.
async function _confirmDegradedHealth(name) {
    const entry = _healthEntryForProcess(name);
    if (!entry || (entry.status !== 'warn' && entry.status !== 'fail')) return true;
    const label = entry.status === 'fail' ? 'Failing' : 'Warning';
    let checked = '';
    if (entry.checked_at) {
        const rel = fypFmtRelative(entry.checked_at);
        if (rel) checked = ` (checked ${rel})`;
    }
    const detail = entry.summary ? `\n\n${entry.summary}` : '';
    return showAppConfirm(
        `System health for this process is ${label}${checked}.${detail}\n\nStart anyway?`,
        { title: 'Health warning', okLabel: 'Start anyway', danger: true });
}

async function startProcess(name, extraBody = {}) {
    // Block the annotator early when Gemini isn't configured — before the
    // optimistic "Starting…" flip and the arm-consolidate prompt — so the user
    // gets a clear reason instead of a worker that boots and fails every item.
    // (The server rejects it too; this is the friendly front door.)
    if (name === 'queue_annotator' || name === 'queue_annotator_batch') {
        try {
            const stats = await fetch('/api/manage/enrichment/stats').then(r => r.json());
            if (stats && stats.annotation_configured === false) {
                await showAppAlert(
                    stats.annotation_config_reason || 'Gemini annotation is not configured.',
                    { title: 'Gemini not configured' });
                return false;
            }
        } catch (e) {
            // Fail open — the server-side gate is authoritative and will refuse.
            console.warn('Annotation-config pre-check failed; deferring to server.', e);
        }
    }
    if (!(await _confirmDegradedHealth(name))) return false;
    let body = {};
    // Determine context (tab) for study name input
    let studyNameInputId = 'global-study-name'; // default for scrape/annotate
    if (name === 'create_subsets') {
        studyNameInputId = 'overview-study-name';
    } else if (['create_event_log', 'recode_event_log', 'calculate_pca', 'regenerate_datasets'].includes(name)) {
        studyNameInputId = 'build-study-name';
    }

    let studyName = "";
    if (document.getElementById(studyNameInputId)) {
        studyName = document.getElementById(studyNameInputId).value;
    }

    let batchSize = null;
    let maxBatches = null;

    if (name.startsWith('queue_scraper_')) {
        const platform = name.slice('queue_scraper_'.length);
        const bsEl = document.getElementById('scrapes-batch-size-' + platform);
        const mbEl = document.getElementById('scrapes-max-batches-' + platform);
        batchSize = bsEl ? bsEl.value : null;
        maxBatches = mbEl ? mbEl.value : null;
    } else if (name === 'queue_annotator') {
        const bsEl = document.getElementById('annotations-batch-size');
        const mbEl = document.getElementById('annotations-max-batches');
        batchSize = bsEl ? bsEl.value : null;
        maxBatches = mbEl ? mbEl.value : null;
    } else if (name === 'queue_annotator_batch') {
        const bsEl = document.getElementById('batch-annotations-batch-size');
        const mbEl = document.getElementById('batch-annotations-max-batches');
        batchSize = bsEl ? bsEl.value : null;
        maxBatches = mbEl ? mbEl.value : null;
    } else {
        const bsEl = document.getElementById('global-batch-size');
        const mbEl = document.getElementById('global-max-batches');
        batchSize = bsEl ? bsEl.value : null;
        maxBatches = mbEl ? mbEl.value : null;
    }

    if (['downloader', 'annotator', 'create_subsets', 'regenerate_datasets', 'create_event_log', 'recode_event_log', 'calculate_pca'].includes(name)) {
        if (!studyName) {
            showAppAlert("Please select or enter a study name.");
            return false;
        }
        body = {
            study_name: studyName,
            batch_size: batchSize,
            max_batches: maxBatches,
            ...extraBody
        };
    } else {
        body = {
            batch_size: batchSize,
            max_batches: maxBatches,
            ...extraBody
        };
    }
    // Immediate optimistic feedback: flip the card to "Starting…" the moment
    // the click is committed — before the arm-consolidate prompt, the dispatch
    // round-trip, and the task-runner boot — so the click is never silently
    // swallowed. setStatus keeps this state until the server reports 'running'.
    // The arm-prompt never aborts a start, so showing "Starting…" ahead of it
    // is safe.
    markStarting(name);

    // Before starting queue_scraper / queue_annotator(_batch), offer to auto-arm
    // Consolidate & Refresh so the pipeline fires on completion. Only when
    // (a) not already armed, and (b) the queue has work to do.
    if (name.startsWith('queue_scraper_') || name === 'queue_annotator' || name === 'queue_annotator_batch') {
        try {
            await _maybePromptArmConsolidate(name);
        } catch (e) {
            console.error('Arm-prompt flow failed (continuing to start):', e);
        }
    }

    // Pre-flight check: the toggle button is updated on a 1-s poll, but the
    // user can click between polls or right after navigating to the tab
    // before the first poll has landed. Fetch fresh status so we never POST
    // /api/start for a process the server already knows is running — that
    // produces a 409 in the network panel even though we now handle it
    // gracefully. Skip the POST and show the dialog directly instead.
    try {
        const statusRes = await fetch('/api/status');
        const statusData = await statusRes.json();
        const current = statusData && statusData[name];
        if (current && current.state === 'running') {
            pendingStarts.delete(name);
            updateStatus();
            _showAlreadyRunningDialog(name, extraBody);
            return false;
        }
    } catch (e) {
        // Fall through to the POST — the server is still the source of truth
        // and will 409 if there's a real conflict; the catch keeps a network
        // blip from blocking starts entirely.
        console.warn('Pre-start status check failed; attempting start anyway.', e);
    }

    let started = false;
    try {
        const res = await fetch(`/api/start/${name}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await res.json();
        if (res.status === 409) {
            // Already running — drop the optimistic state and let the poll
            // render the real running status.
            pendingStarts.delete(name);
            _showAlreadyRunningDialog(name, extraBody);
        } else if (res.status === 423 || data.status === 'busy') {
            // A refresh run holds the pipeline. Not the same as "this worker is
            // already running", so explain it rather than offering to stop and
            // retry — there is nothing here to stop.
            pendingStarts.delete(name);
            showAppAlert(data.message || 'A refresh run is already in progress.',
                         { title: 'Refresh run in progress' });
        } else if (data.status !== 'success') {
            // Dispatch refused — revert the card so it doesn't sit in
            // "Starting…"; the next poll restores the idle UI.
            pendingStarts.delete(name);
            showAppAlert("Error: " + data.message);
        } else {
            started = true;
            if (window._pendingArmAfterStart) {
                // Successful start — arm Consolidate & Refresh so it fires when
                // the queue finishes. Non-blocking; fires in background.
                _armAfterQueueStart();
            }
        }
        updateStatus();
    } catch (e) {
        // Network error — don't strand the card in "Starting…".
        pendingStarts.delete(name);
        console.error(e);
    }

    // A refresh step's start plans a run server-side before it dispatches, so
    // the chart can show it immediately. Without this the block sat on the
    // previous run until the next stats poll — or a page reload.
    if (started && _REFRESH_RUN_STEPS.includes(name)) {
        try {
            if (typeof fetchEnrichmentStats === 'function') fetchEnrichmentStats();
            if (typeof pollConsolidationStatus === 'function') pollConsolidationStatus(name);
        } catch (e) {
            console.warn('Could not refresh the pipeline chart after start:', e);
        }
    }

    // Auto-start Monitor if Downloader is starting and checkbox is checked
    if (name === 'downloader') {
        const autoStart = document.getElementById('monitor-auto-start');
        if (autoStart && autoStart.checked) {
            setTimeout(() => {
                startProcess('monitor');
            }, 1000); // 1 second delay
        }
    }

    return started;
}

// Rebuild the Semantic Space niche map, and re-segment the session index. The
// rebuild option lives in the start dialog, alongside the list of what the run
// sets off. Kept as named functions because the card buttons reference them by name
// (data-start-handler), which setStatus re-applies on every poll.
function rebuildNicheMap() {
    openRefreshStartModal('video_map_refresh');
}

function startSessionsRefresh() {
    openRefreshStartModal('sessions_refresh');
}

// ── Generic pretty dialogs — the app-wide replacement for native alert()/
// confirm(). Both return Promises so callers can await a choice; the look
// reuses the stop-worker modal (.stop-confirm-overlay / .stop-confirm-card).

// Arm-prompt state (module-scoped). _armPromptResolver is set while the
// overlay is visible so the Yes/No buttons can resolve the awaited promise.
let _armPromptResolver = null;

function _resolveArmPrompt(value) {
    const overlay = document.getElementById('arm-prompt-overlay');
    if (overlay) overlay.classList.remove('visible');
    if (_armPromptResolver) {
        const r = _armPromptResolver;
        _armPromptResolver = null;
        r(value);
    }
}

async function _maybePromptArmConsolidate(name) {
    // Bail out fast when the modal markup isn't on the page (e.g. a plugin
    // starts a queue from a different screen).
    const overlay = document.getElementById('arm-prompt-overlay');
    if (!overlay) return;

    // Fetch current enrichment stats to decide whether to show the prompt.
    let stats;
    try {
        stats = await fetch('/api/manage/enrichment/stats').then(r => r.json());
    } catch {
        return; // Fail open — don't block the start.
    }
    if (!stats) return;

    // Already armed → nothing to prompt.
    if (stats.consolidate_auto_armed) return;

    // Queue empty → nothing meaningful will happen, skip the prompt.
    const qLen = name.startsWith('queue_scraper_') ? stats.scrape_queue_len : stats.annotate_queue_len;
    if (!qLen || qLen <= 0) return;

    // Show the modal and await user choice.
    const textEl = document.getElementById('arm-prompt-text');
    if (textEl) {
        const action = name.startsWith('queue_scraper_') ? 'scraping' : 'annotation';
        textEl.textContent = `Would you like to automatically consolidate enrichment data and refresh all affected caches once the ${action} finishes?`;
    }
    overlay.classList.add('visible');

    const armed = await new Promise(resolve => { _armPromptResolver = resolve; });
    if (!armed) return;

    // User said yes — arm via the consolidate endpoint. When workers are
    // idle (queue not yet started), the endpoint will actually fire
    // consolidate right now, which we DON'T want. To force the "armed"
    // branch server-side, we arm by briefly setting the flag directly via
    // the existing consolidate POST when workers are running — but since
    // workers aren't yet running at this point, we arm via a dedicated
    // flow: send a POST and if the server returns 'started' we're out of
    // luck (race). To avoid that, we arm AFTER kicking off the queue.
    // Flag the intent here; actual arming happens in _armAfterQueueStart().
    window._pendingArmAfterStart = name;
}

async function _armAfterQueueStart() {
    // Called after a queue scraper/annotator has been successfully started;
    // arms Consolidate & Refresh so it auto-fires when the queue finishes.
    const intent = window._pendingArmAfterStart;
    if (!intent) return;
    window._pendingArmAfterStart = null;

    // Give the server a moment to register the queue process as running —
    // otherwise the arm POST would hit the "no workers → fire now" branch.
    await new Promise(r => setTimeout(r, 1200));

    try {
        const res = await fetch('/api/manage/enrichment/consolidate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ auto_refresh: true }),
        });
        const resp = await res.json();
        if (resp.status !== 'armed') {
            console.warn('Arm POST did not arm (server response):', resp);
        }
        if (typeof fetchEnrichmentStats === 'function') fetchEnrichmentStats();
    } catch (e) {
        console.error('Failed to arm after queue start:', e);
    }
}



async function stopProcess(name) {
    try {
        const res = await fetch(`/api/stop/${name}`, { method: 'POST' });
        const data = await res.json();
        if (data.status !== 'success') {
            console.error("Stop process error:", data.message);
        }
        updateStatus();
        return data;
    } catch (e) {
        console.error(e);
        return null;
    }
}



function _processDisplayLabel(name) {
    const btn = document.getElementById(`${name}-toggle`);
    const raw = btn ? btn.getAttribute('data-start-label') : null;
    if (raw) {
        return raw.replace(/^(Start |Recalculate )/, '').replace(/\(.*\)/, '').trim();
    }
    return name.replace(/_/g, ' ');
}


function _showAlreadyRunningDialog(name, extraBody) {
    const overlay = document.getElementById('already-running-overlay');
    const textEl = document.getElementById('already-running-text');
    const cancelBtn = document.getElementById('already-running-cancel-btn');
    const stopRetryBtn = document.getElementById('already-running-stop-retry-btn');
    if (!overlay || !textEl || !cancelBtn || !stopRetryBtn) {
        showAppAlert(`${_processDisplayLabel(name)} is already running. Stop it before starting again.`);
        return;
    }
    const label = _processDisplayLabel(name);
    textEl.textContent = `${label} is already running. Stop it and try starting it again?`;

    const close = () => {
        overlay.classList.remove('visible');
        cancelBtn.onclick = null;
        stopRetryBtn.onclick = null;
        stopRetryBtn.disabled = false;
        stopRetryBtn.textContent = 'Stop and retry';
    };
    cancelBtn.onclick = close;
    stopRetryBtn.onclick = async () => {
        stopRetryBtn.disabled = true;
        stopRetryBtn.textContent = 'Stopping…';
        await stopProcess(name);
        // Give the backend a moment to reflect the cancel sentinel before retrying.
        await new Promise(r => setTimeout(r, 1500));
        close();
        // Retry once. If the server still 409s, startProcess will fall through to
        // the original alert() because we re-enter with the same dialog path.
        startProcess(name, extraBody || {});
    };
    overlay.classList.add('visible');
}



// What a graceful stop actually waits for, per worker. A worker that never
// reads the cancel sentinel (consolidate, the semantic map) cannot honour one
// at all, so it must not be offered a button that silently does nothing. The
// rest finish the unit named here first — which for a refresh worker is a study
// or a collection, not the "batch" the scrapers count in.
const _STOP_BOUNDARIES = (window.PIPELINE_REGISTRY && window.PIPELINE_REGISTRY.stop_boundaries) || {};

function _stopBoundary(name) {
    if (name in _STOP_BOUNDARIES) return _STOP_BOUNDARIES[name];
    // Everything outside the refresh graph — scrapers, annotators — works in
    // batches and has always offered the graceful stop.
    return _REFRESH_RUN_STEPS.includes(name) ? null : 'batch';
}

function showStopConfirm(name) {
    _pendingStopProcess = name;
    // Name the worker, not its button. Four refresh cards are all labelled
    // "Refresh", and the consolidate card has no -toggle at all, so reading the
    // button gave "Stop Refresh?" or the raw process name — neither of which
    // says which worker is about to stop. That matters now the stop button also
    // lives on a chart row, away from its card.
    let shortName = (typeof _PIPELINE_STEP_LABELS !== 'undefined' && _PIPELINE_STEP_LABELS[name]) || '';
    if (!shortName) {
        const btn = document.getElementById(`${name}-toggle`);
        const label = btn ? btn.getAttribute('data-start-label') : name;
        shortName = (label || name).replace(/^(Start |Recalculate )/, '').replace(/\(.*\)/, '').trim();
    }
    document.getElementById('stop-confirm-text').innerText = `Stop ${shortName}?`;

    const boundary = _stopBoundary(name);
    const gracefulBtn = document.getElementById('stop-confirm-graceful-btn');
    if (gracefulBtn) {
        gracefulBtn.style.display = boundary ? '' : 'none';
        if (boundary) gracefulBtn.innerText = `Stop after this ${boundary}`;
    }
    document.getElementById('stop-confirm-overlay').classList.add('visible');
}

function closeStopConfirm() {
    document.getElementById('stop-confirm-overlay').classList.remove('visible');
    _pendingStopProcess = null;
}

function confirmStop() {
    if (_pendingStopProcess) {
        stopProcess(_pendingStopProcess);
    }
    closeStopConfirm();
}

async function gracefulStopProcess(name) {
    try {
        const res = await fetch(`/api/stop_graceful/${name}`, { method: 'POST' });
        const data = await res.json();
        if (data.status !== 'success') {
            console.error("Graceful stop error:", data.message);
        }
        updateStatus();
    } catch (e) {
        console.error(e);
    }
}

function confirmStopGraceful() {
    if (_pendingStopProcess) {
        gracefulStopProcess(_pendingStopProcess);
    }
    closeStopConfirm();
}

function openLogModal(name, displayLabel) {
    _activeLogModal = name;
    _activeLogRun = '';
    _activeLogSince = 0;
    _activeLogText = '';
    _activeLogRunSig = '';
    _activeLogRunDone = false;
    document.getElementById('log-modal-title').innerText = `${displayLabel} Log`;
    document.getElementById('log-modal-content').textContent = '';
    const filter = document.getElementById('log-modal-filter');
    if (filter) filter.value = '';
    document.getElementById('log-modal-overlay').classList.add('visible');
    document.addEventListener('keydown', _logModalKeydown);
    fetchLogs(name);
}

function closeLogModal() {
    _activeLogModal = null;
    _activeLogText = '';
    document.removeEventListener('keydown', _logModalKeydown);
    document.getElementById('log-modal-overlay').classList.remove('visible');
}

function _logModalKeydown(e) {
    if (e.key === 'Escape') closeLogModal();
}

function _logModalBackdropClick(e) {
    // Only a click on the backdrop itself, not one that bubbled up from the card.
    if (e.target && e.target.id === 'log-modal-overlay') closeLogModal();
}

// Switching runs re-reads from the top: a past run is immutable, so the poll
// settles into a no-op once it has been fetched.
function selectLogRun(runId) {
    _activeLogRun = runId || '';
    _activeLogSince = 0;
    _activeLogText = '';
    _activeLogRunDone = false;
    document.getElementById('log-modal-content').textContent = '';
    if (_activeLogModal) fetchLogs(_activeLogModal);
}

function filterLogModal() {
    _renderLogModal(true);
}

function copyLogModal() {
    navigator.clipboard.writeText(_activeLogText || '');
}

function downloadLogModal() {
    const stamp = (_activeLogRun || 'current').replace(/[^A-Za-z0-9_.-]/g, '');
    const blob = new Blob([_activeLogText || ''], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${_activeLogModal || 'process'}-${stamp}.log`;
    a.click();
    URL.revokeObjectURL(url);
}

async function clearLogHistory() {
    if (!_activeLogModal) return;
    const ok = await _showAppDialog({
        title: 'Clear log history',
        message: 'Delete every retained run of this process, for all admins? '
            + 'This cannot be undone.',
        okLabel: 'Clear history', cancelLabel: 'Cancel', danger: true,
    });
    if (!ok) return;
    // The global fetch wrapper adds the CSRF header on POST.
    await fetch(`/api/logs/clear/${_activeLogModal}`, { method: 'POST' });
    selectLogRun('');
}

function _runOptionLabel(run) {
    const started = run.started_at ? new Date(run.started_at) : null;
    const when = started ? started.toLocaleString() : 'unknown time';
    const who = run.started_by || 'system';
    const state = run.state === 'running' ? 'running' : run.state;
    return `${when} · ${who} · ${state}`;
}

function _renderRunPicker(runs) {
    const sel = document.getElementById('log-modal-run');
    if (!sel) return;
    const sig = runs.map(r => `${r.run_id}:${r.state}`).join('|');
    if (sig === _activeLogRunSig) return;
    _activeLogRunSig = sig;
    sel.innerHTML = '';
    runs.forEach((run, i) => {
        const opt = document.createElement('option');
        opt.value = run.run_id;
        opt.textContent = (i === 0 ? 'Latest — ' : '') + _runOptionLabel(run);
        sel.appendChild(opt);
    });
    sel.value = _activeLogRun || (runs[0] ? runs[0].run_id : '');
    sel.disabled = runs.length === 0;
}

function _renderLogModal(preserveScroll) {
    const el = document.getElementById('log-modal-content');
    if (!el) return;
    const needle = (document.getElementById('log-modal-filter') || {}).value || '';
    const text = needle
        ? _activeLogText.split('\n')
            .filter(l => l.toLowerCase().includes(needle.toLowerCase())).join('\n')
        : _activeLogText;
    const atBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 5;
    el.textContent = text;
    if (!preserveScroll && (atBottom || el.scrollTop === 0)) {
        el.scrollTop = el.scrollHeight;
    }
}



function updateDmSidebarSpinners(data) {
    // One running-process spinner per Data Pipeline sidebar item. Each item's
    // template markup declares the processes its page owns in `data-procs`, so
    // the wiring lives next to the menu entry rather than in a list here that
    // silently rots when a page gains a process. A trailing `*` matches by
    // prefix, which is how the Scrape item covers every registered platform's
    // queue_scraper_<platform> worker.
    document.querySelectorAll('.dm-sidebar-spinner[data-procs]').forEach(el => {
        const patterns = el.dataset.procs.split(',')
            .map(s => s.trim()).filter(Boolean);
        const matches = (name) => patterns.some(p => p.endsWith('*')
            ? name.startsWith(p.slice(0, -1))
            : name === p);
        const running = Object.entries(data).some(([name, p]) =>
            p && (p.state === 'running' || p.state === 'stopping') && matches(name));
        el.style.display = running ? 'inline-block' : 'none';
    });
}



// Adaptive /api/status cadence: 1s only while quick feedback matters (a run
// in flight, an open log modal, a cascade refresh, or an optimistic start
// awaiting its first confirmed state); 5s when everything is idle. The 1s
// interval keeps ticking — _pollStatusTick just skips fetches that are not
// due yet, so the cadence tightens within a second of a state change.
let _lastStatusFetchMs = 0;

function _statusPollDelayMs() {
    if (_activeLogModal) return 1000;
    if (typeof _cascadeRefresh !== 'undefined' && _cascadeRefresh) return 1000;
    if (pendingStarts.size > 0) return 1000;
    const anyActive = Object.values(previousProcessStates).some(
        s => s === 'running' || s === 'stopping');
    return anyActive ? 1000 : 5000;
}

function _pollStatusTick() {
    if (Date.now() - _lastStatusFetchMs < _statusPollDelayMs()) return;
    updateStatus();
}

function _statusPollNeededWhileHidden() {
    // Keep polling /api/status even when the tab is backgrounded if anything
    // is in flight, so completion detection and cascade-refresh chaining never
    // miss a running → done transition. Otherwise an idle backgrounded tab
    // need not poll at all.
    if (typeof _cascadeRefresh !== 'undefined' && _cascadeRefresh) return true;
    if (_activeLogModal) return true;
    return Object.values(previousProcessStates).some(
        s => s === 'running' || s === 'stopping');
}

async function updateStatus() {
    // Skip the 1/s poll while the tab is hidden and nothing is in flight —
    // saves idle Cloud Run requests. The interval keeps ticking and we only
    // no-op the fetch, so polling resumes within 1s of the tab regaining focus.
    if (document.hidden && !_statusPollNeededWhileHidden()) return;
    _lastStatusFetchMs = Date.now();
    try {
        const res = await fetch('/api/status');
        const data = await res.json();
        // The refresh start dialog estimates a run from what each step cost
        // last time, which is already in this payload.
        window._lastStatusData = data;

        setStatus('downloader', data.downloader);
        setStatus('monitor', data.monitor);
        setStatus('annotator', data.annotator);
        scraperProcessNames().forEach(n => setStatus(n, data[n]));
        setStatus('queue_annotator', data.queue_annotator);
        setStatus('queue_annotator_batch', data.queue_annotator_batch);
        refreshBatchFeed(data.queue_annotator_batch);
        setStatus('meta_refresh_groups', data.meta_refresh_groups);
        setStatus('timelines_refresh', data.timelines_refresh);
        setStatus('recode_refresh_studies', data.recode_refresh_studies);
        setStatus('pca_refresh', data.pca_refresh);
        setStatus('embeddings_refresh', data.embeddings_refresh);
        setStatus('video_map_refresh', data.video_map_refresh);
        setStatus('sessions_refresh', data.sessions_refresh);
        // The consolidate card carries the same dot/bar/last-run chrome as the
        // rest. It has no `-toggle` element (its button is bespoke: arm,
        // disarm, two option checkboxes), and setStatus skips that block when
        // the element is absent.
        setStatus('consolidate_enrichment', data.consolidate_enrichment);

        updateDmSidebarSpinners(data);

        // An ingest finishing is news to EVERY open tab, not just the one that
        // clicked Process New Collections. pollIngestRefreshStatus refreshes
        // the clicking tab; without this, any other tab keeps showing the
        // pre-ingest collection list — and because the Edit Collections table
        // only lazy-renders while it is still empty, revisiting that sub-page
        // does not re-render it either. The newly ingested collections then
        // stay invisible until a full page reload.
        {
            const ir = data.ingest_refresh;
            if (ir && previousProcessStates.ingest_refresh === 'running' && ir.state !== 'running') {
                if (typeof loadAvailableCollections === 'function') loadAvailableCollections();
                if (typeof loadIngestionSources === 'function') loadIngestionSources();
            }
            if (ir) previousProcessStates.ingest_refresh = ir.state;
        }

        // Update global running-tasks badge
        const runningNames = Object.entries(data)
            .filter(([, v]) => v && (v.state === 'running' || v.state === 'stopping'))
            .map(([k]) => k.replace(/_/g, ' '));
        const badge = document.getElementById('global-tasks-badge');
        const countEl = document.getElementById('global-tasks-count');
        if (badge && countEl) {
            if (runningNames.length > 0) {
                badge.style.display = 'inline-flex';
                countEl.textContent = runningNames.length;
                badge.title = runningNames.join(', ');
            } else {
                badge.style.display = 'none';
            }
        }

        // Detect scraper/annotator completion → refresh enrichment stats for consolidation warning
        [...scraperProcessNames(), 'queue_annotator', 'queue_annotator_batch'].forEach(name => {
            const pData = data[name];
            if (pData && previousProcessStates[name] === 'running' && pData.state !== 'running') {
                if (typeof fetchEnrichmentStats === 'function') {
                    fetchEnrichmentStats();
                }
            }
            if (pData) previousProcessStates[name] = pData.state;
        });

        // Detect downstream process completion → refresh staleness indicators + cascade logic
        ['recode_refresh_studies', 'meta_refresh_groups', 'timelines_refresh', 'pca_refresh'].forEach(name => {
            const pData = data[name];
            if (pData && previousProcessStates[name] === 'running' && pData.state !== 'running') {
                if (typeof fetchStalenessStatus === 'function') {
                    fetchStalenessStatus();
                }

                // Cascade refresh: chain meta refreshes after study refresh completes
                if (typeof _cascadeRefresh !== 'undefined' && _cascadeRefresh) {
                    if (name === 'recode_refresh_studies' && typeof onCascadeStudiesComplete === 'function') {
                        onCascadeStudiesComplete();
                    }
                    // Check if all cascade processes have finished
                    const allDone = ['recode_refresh_studies', 'meta_refresh_groups', 'timelines_refresh', 'pca_refresh'].every(p => {
                        const pd = data[p];
                        return !pd || pd.state !== 'running';
                    });
                    if (allDone && _cascadeRefresh.phase === 'waiting_for_meta' && typeof onCascadeRefreshComplete === 'function') {
                        // Ensure meta processes were actually started before declaring complete
                        if (_cascadeRefresh.startedMetaGroups || _cascadeRefresh.startedPca) {
                            onCascadeRefreshComplete();
                        }
                    }
                }
            }
            if (pData) previousProcessStates[name] = pData.state;
        });

    } catch (e) {
        console.error(e);
    }
}



// Show/hide the "· N in batch job" indicator next to the annotation-queue count.
// The videos are claimed out of the queue by an in-flight async batch job, so
// this makes clear they are being processed, not lost.
function updateAnnotateInflight(claimedLen) {
    const el = document.getElementById('enrich_annotate_inflight');
    if (!el) return;
    const n = Number(claimedLen) || 0;
    if (n > 0) {
        // Make clear these left the pending queue because the async job reserved
        // them, and are still being processed (not finished).
        el.textContent = `+ ${n.toLocaleString()} claimed by async annotator (processing)`;
        el.style.display = '';
    } else {
        el.style.display = 'none';
    }
}


// The Async Annotator card streams the worker's log lines instead of a progress
// bar (a batch job polls for hours with no meaningful percentage). Fetch the log
// tail on a throttle while it runs; show "Idle" when it isn't.
let _batchFeedTick = 0;
let _batchFeedWasRunning = false;

function renderBatchFeed(el, logsText) {
    const lines = String(logsText || '').split('\n').filter(s => s.trim() !== '');
    const atBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 5;
    el.textContent = lines.slice(-40).join('\n') || 'Working…';
    if (atBottom) el.scrollTop = el.scrollHeight;
}

async function refreshBatchFeed(procData) {
    const el = document.getElementById('queue_annotator_batch-feed');
    if (!el) return;
    const running = !!(procData && procData.state === 'running');
    if (!running) {
        if (_batchFeedWasRunning) {
            _batchFeedWasRunning = false;
            el.textContent = 'Idle';
        }
        return;
    }
    _batchFeedWasRunning = true;
    // updateStatus ticks ~1/s; fetch the log tail every ~4s to keep it light.
    if (_batchFeedTick++ % 4 !== 0) return;
    try {
        const res = await fetch('/api/logs/queue_annotator_batch');
        if (!res.ok) return;
        const d = await res.json();
        renderBatchFeed(el, d.logs);
    } catch (e) { /* keep last content on a transient error */ }
}



// Steps of the refresh pipeline. While a refresh run is in flight, starting a
// second one would interleave writes to the same caches, so their cards are
// disabled and say which run holds the pipeline (the server refuses with a 423
// regardless — this is the visible half).
const _REFRESH_RUN_STEPS = (window.PIPELINE_REGISTRY && window.PIPELINE_REGISTRY.order) || [
    'consolidate_enrichment', 'embeddings_refresh', 'video_map_refresh',
    'recode_refresh_studies', 'meta_refresh_groups', 'pca_refresh',
    'timelines_refresh', 'sessions_refresh',
];

function applyRefreshRunLock(only) {
    const lock = window._refreshRunLock || null;
    const names = only ? [only] : _REFRESH_RUN_STEPS;
    for (const name of names) {
        if (!_REFRESH_RUN_STEPS.includes(name)) continue;
        const btn = document.getElementById(`${name}-toggle`);
        if (!btn) continue;
        // Never disable a Stop button — a running step must stay stoppable.
        const isStop = btn.className === 'btn-stop';
        const shouldLock = !!lock && !isStop && name !== lock.origin;
        if (shouldLock) {
            btn.disabled = true;
            btn.title = lock.reason;
        } else if (btn.dataset.lockedByRun === 'yes') {
            // Only release what THIS lock disabled; a button disabled for another
            // reason (a start in flight, say) must stay as it is.
            btn.disabled = false;
            btn.title = '';
        }
        btn.dataset.lockedByRun = shouldLock ? 'yes' : 'no';
    }
    // The two option checkboxes that change what a start would do.
    for (const id of ['video_map_reset-labels', 'sessions_refresh-force']) {
        const box = document.getElementById(id);
        if (box) box.disabled = !!lock;
    }
}

function setStatus(name, data) {
    if (!data) return;
    const status = data.state;
    const info = data.progress || {};

    // Optimistic "Starting…" guard: while a just-clicked process is awaiting the
    // server's first 'running' report, keep the Starting UI and skip the normal
    // render so a poll returning the *prior* state (typically 'stopped' before
    // the start request lands, or a stale 'failed' from a previous run) can't
    // flip the button back to Start/Refresh. Only the awaited 'running' signal
    // releases the guard here; failed dispatches are cleared by the POST-result
    // handlers in startProcess, and a stuck start by the failsafe timeout.
    if (pendingStarts.has(name)) {
        if (status === 'running') {
            pendingStarts.delete(name);
        } else {
            return;
        }
    }

    // Status-light colour logic (consistent across worker cards and the
    // pipeline step list): green = running, blue = standing by (idle, queued,
    // stopping), amber = last run didn't work, red = critical (couldn't start).
    const el = document.getElementById(`${name}-status`);
    if (el) {
        let dotClass = `status-${status}`;
        if ((status === 'stopped' || status === 'completed')
            && data.last_run_outcome === 'Fail') {
            dotClass = 'status-warn';
        }
        el.className = `status-indicator ${dotClass}`;
    }

    // Toggle button state
    const toggleBtn = document.getElementById(`${name}-toggle`);
    if (toggleBtn) {
        if (status === 'running') {
            toggleBtn.className = 'btn-stop';
            toggleBtn.innerText = 'Stop';
            toggleBtn.style.padding = '4px 12px';
            toggleBtn.onclick = function () { showStopConfirm(name); };
        } else if (status === 'stopping') {
            toggleBtn.className = 'btn-running';
            toggleBtn.innerText = 'Stopping...';
            toggleBtn.style.padding = '4px 12px';
            toggleBtn.onclick = null;
        } else {
            toggleBtn.className = 'btn-primary';
            const startLabel = toggleBtn.getAttribute('data-start-label') || 'Start';
            toggleBtn.innerText = startLabel;
            toggleBtn.style.padding = '4px 12px';
            toggleBtn.onclick = function () {
                // A button may declare a custom start handler (e.g. one that
                // reads a checkbox and confirms) via data-start-handler. Honour
                // it so this poll-driven rebind doesn't clobber that logic — the
                // inline onclick alone is overwritten on the first status poll.
                const handler = toggleBtn.getAttribute('data-start-handler');
                if (handler && typeof window[handler] === 'function') {
                    window[handler]();
                    return;
                }
                // A refresh step never starts alone any more — it plans
                // everything downstream of it — so it goes through the start
                // dialog, which states what that is before committing.
                if (_REFRESH_RUN_STEPS.includes(name)
                    && typeof openRefreshStartModal === 'function') {
                    openRefreshStartModal(name);
                    return;
                }
                const extraRaw = toggleBtn.getAttribute('data-start-extra');
                const extra = extraRaw ? JSON.parse(extraRaw) : {};
                startProcess(name, extra);
            };
        }
    }

    // This poll rebuilds the button above from scratch, so the refresh-run lock
    // has to be re-applied here or the cards flicker back to enabled every tick.
    applyRefreshRunLock(name);

    // Show running process settings for scraper/annotator. Only *reset* on
    // the running→stopped transition (disabled flips back off); on every
    // other poll tick leave the inputs alone so the user can type freely
    // without every 2s poll clobbering their value back to the default.
    if (name.startsWith('queue_scraper_') || name === 'queue_annotator') {
        const isScraper = name.startsWith('queue_scraper_');
        const suffix = isScraper ? '-' + name.slice('queue_scraper_'.length) : '';
        const prefix = isScraper ? 'scrapes' : 'annotations';
        const bsEl = document.getElementById(`${prefix}-batch-size${suffix}`);
        const mbEl = document.getElementById(`${prefix}-max-batches${suffix}`);
        // While running, disable the inputs and reflect the entered values from
        // the echoed task_args. Only *overwrite* a box when task_args carries a
        // finite positive number — never blank it. On Cloud Run task_args can be
        // briefly absent/empty between worker status writes; blanking would flash
        // the "Inf" placeholder and wipe what the user typed (the "resets to Inf"
        // bug). Leaving the box untouched keeps the displayed value stable.
        const ta = (data && data.task_args) || {};
        const finitePos = (v) => v != null && v !== '' && Number.isFinite(Number(v)) && Number(v) > 0;
        if (status === 'running') {
            if (bsEl) { if (finitePos(ta.batch_size)) bsEl.value = ta.batch_size; bsEl.disabled = true; }
            if (mbEl) { if (finitePos(ta.max_batches)) mbEl.value = ta.max_batches; mbEl.disabled = true; }
        } else if (status !== 'running') {
            // Only re-enable + reset when transitioning *out* of running.
            if (bsEl && bsEl.disabled) { bsEl.value = 500; bsEl.disabled = false; }
            if (mbEl && mbEl.disabled) { mbEl.value = ''; mbEl.disabled = false; }
        }
    }

    const bar = document.getElementById(`${name}-bar`);
    const text = document.getElementById(`${name}-text`);
    if (bar && text) {
        if (status === 'stopped' || status === 'completed') {
            // Finished/idle process. A completed Cloud Task lingers in GCS as
            // {state:"stopped", progress:{percent:100, message:"Completed"}};
            // rendering that verbatim would leave the bar stuck at
            // "Completed (100%)" and flash 100% at the next start before the new
            // run overwrites it. Force Idle/0% regardless of a leftover percent —
            // the per-run outcome still shows in the separate last-run line below.
            bar.style.width = '0%';
            text.innerText = 'Idle';
        } else if (status === 'queued' || status === 'failed' || status === 'error') {
            // A forked pipeline leaf that is waiting for a worker ('queued') or
            // could not be initiated ('failed'/'error', e.g. dropped by a 429).
            // Show the status message directly so the card does not look like a
            // stalled in-progress run.
            const fallback = status === 'queued' ? 'Queued…' : "Couldn't start";
            text.innerText = (info && info.message) || data.error || fallback;
            if (data.error) text.title = data.error;
            // Queued = standing by (blue), failed-to-start = critical (red).
            text.style.color = status === 'queued'
                ? 'var(--color-info)'
                : 'var(--color-danger-soft)';
            bar.style.width = '0%';
        } else if (Object.keys(info).length > 0 && (info.total > 0 || info.percent !== undefined)) {
            let barPct = 0;
            let etaStr = "";

            if (info.percent !== undefined) {
                barPct = parseFloat(info.percent);
                text.innerText = `${info.message || ""} (${barPct.toFixed(0)}%)`;
            } else {
                // Progress bar shows current batch progress
                if (info.batch_total > 0) {
                    barPct = (info.batch_done / info.batch_total) * 100;
                } else {
                    barPct = (info.done / info.total) * 100;
                }

                let batchStr = info.batch ? `Batch ${info.batch}` : "";
                let itemsStr = `${info.done.toLocaleString()}/${info.total.toLocaleString()}`;
                if (info.eta !== undefined && info.eta > 0) {
                    etaStr = " ETA " + formatETA(info.eta);
                }

                text.innerText = batchStr
                    ? `${batchStr} (${itemsStr})${etaStr}`
                    : `${itemsStr}${etaStr}`;
            }

            bar.style.width = `${barPct}%`;

        } else {
            if (status !== 'stopped') {
                if (bar.style.width === '0%' || bar.style.width === '') {
                    text.innerText = 'Initializing...';
                }
            } else {
                text.innerText = 'Idle';
            }
        }
    }

    // Last run / current run display
    const lastRunEl = document.getElementById(`${name}-last-run`);
    if (lastRunEl) {
        if (status === 'running' && data.start_time) {
            lastRunEl.innerText = `This run started: ${fypFmtDateTimeShort(data.start_time)}`;
            lastRunEl.title = fypFmtDateTimeFull(data.start_time);
            lastRunEl.style.color = 'var(--color-success-light)';
        } else if (data.last_run_end_time) {
            const when = fypFmtDateTimeShort(data.last_run_end_time);
            lastRunEl.title = fypFmtDateTimeFull(data.last_run_end_time);

            let durStr = '';
            if (data.last_run_duration != null) {
                const s = Math.round(data.last_run_duration);
                durStr = s >= 60 ? ` (${Math.floor(s / 60)}m ${s % 60}s)` : ` (${s}s)`;
            }

            let outcomeStr = '';
            if (data.last_run_outcome === 'Success') {
                outcomeStr = ' OK';
                lastRunEl.style.color = 'var(--color-success-light)';
            } else if (data.last_run_outcome === 'Fail') {
                outcomeStr = ' Failed';
                lastRunEl.style.color = 'var(--color-danger-soft)';
            } else {
                lastRunEl.style.color = 'var(--color-text-tertiary)';
            }

            lastRunEl.innerText = `Last: ${when}${durStr}${outcomeStr}`;
        } else {
            lastRunEl.innerText = '';
            lastRunEl.title = '';
        }
    }

    // Update queue displays from ::DATA:: output (only while running —
    // when idle the management stats endpoint is the source of truth)
    if (data.state === 'running') {
        const procData = data.data || {};
        if (name.startsWith('queue_scraper_') && procData.scrape_queue_len !== undefined) {
            const platform = name.slice('queue_scraper_'.length);
            const el = document.getElementById('enrich_scrape_targets_' + platform);
            if (el) el.textContent = procData.scrape_queue_len.toLocaleString();
        }
        if ((name === 'queue_annotator' || name === 'queue_annotator_batch') && procData.annotate_queue_len !== undefined) {
            const el = document.getElementById('enrich_annotate_targets');
            if (el) el.textContent = procData.annotate_queue_len.toLocaleString();
        }
        if (name === 'queue_annotator_batch' && procData.annotate_claimed_len !== undefined) {
            updateAnnotateInflight(procData.annotate_claimed_len);
        }
    }

    // Thread count for scraper (show while running, hide when idle)
    if (name.startsWith('queue_scraper_')) {
        const threadsEl = document.getElementById(`${name}-threads`);
        if (threadsEl) {
            const procData = data.data || {};
            if (data.state === 'running' && procData.threads !== undefined) {
                threadsEl.textContent = `${procData.threads} threads`;
                threadsEl.style.display = '';
            } else {
                threadsEl.style.display = 'none';
            }
        }
    }
}



let _lastSubsetData = null;

window.addEventListener('theme-changed', () => {
    if (_lastSubsetData) renderSubsetChart(_lastSubsetData);
});

function renderSubsetChart(data) {
    _lastSubsetData = data;
    const labels = Object.keys(data);
    const values = Object.values(data);

    const plotData = [{
        values: values,
        labels: labels,
        type: 'pie'
    }];

    const layout = {
        height: 400,
        width: 500,
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        legend: {
            font: { color: getCSSVar('--color-text-primary') }
        }
    };

    Plotly.react('subsets-pie-chart', plotData, layout, { displayModeBar: false });
}



function formatETA(seconds) {
    if (seconds === undefined || seconds === null) return "--";
    let val = parseFloat(seconds);
    if (isNaN(val)) return "--";

    val = Math.abs(val);

    if (val < 60) return "<1m";

    let h = Math.floor(val / 3600);
    let m = Math.floor((val % 3600) / 60);

    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
}



// Tail whichever log the modal is showing, whatever its process name. A
// hardcoded list of names would leave every card missing from it rendering a
// snapshot on open and then freezing.
async function updateLogs() {
    if (!_activeLogModal) return;
    // A finished run is immutable, so once we have caught up there is nothing
    // left to poll for — this is what stops an open modal from issuing a
    // request every second forever. A live run keeps tailing even when the tab
    // is hidden, matching _statusPollNeededWhileHidden().
    if (_activeLogRunDone) return;
    await fetchLogs(_activeLogModal);
}



async function fetchLogs(name) {
    try {
        if (_activeLogModal !== name) return;

        const el = document.getElementById('log-modal-content');
        if (!el) return;

        const params = new URLSearchParams();
        if (_activeLogRun) params.set('run', _activeLogRun);
        if (_activeLogSince) params.set('since', String(_activeLogSince));
        const query = params.toString();
        const res = await fetch(`/api/logs/${name}${query ? '?' + query : ''}`);
        if (!res.ok) {
            // A 403 renders as HTML, so res.json() would throw every second and
            // leave the pane silently blank.
            el.textContent = res.status === 403
                ? 'You do not have permission to view process logs.'
                : `Could not load the log (HTTP ${res.status}).`;
            _activeLogModal = null;
            return;
        }
        const data = await res.json();

        _renderRunPicker(data.runs || []);
        if (data.run_id && !_activeLogRun) _activeLogRun = data.run_id;
        // The footer is flushed before the terminal state is written, so a
        // response that reports a finished run already carries its last lines.
        const state = (data.run || {}).state || '';
        _activeLogRunDone = !!state && state !== 'running';

        const incoming = data.logs || '';
        if (data.reset || !_activeLogText) {
            _activeLogText = incoming;
        } else if (incoming) {
            _activeLogText += '\n' + incoming;
        }
        _activeLogSince = data.next_since || 0;
        _renderLogModal(false);
    } catch (e) {
        console.error(e);
    }
}
