// CSS variable helper for dynamic JS styling
function getCSSVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// HTML-escape untrusted strings before inserting into innerHTML
function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Format a numeric metric for display: large values as rounded integers with
// thousands separators, small values (e.g. per-play ratios) with 3 significant
// digits so they don't collapse to 0.
function formatMetricNumber(n) {
    if (n === null || n === undefined || !isFinite(n)) return String(n);
    const abs = Math.abs(n);
    if (abs >= 100 || Number.isInteger(n)) return Math.round(n).toLocaleString();
    if (abs === 0) return '0';
    return Number(n.toPrecision(3)).toLocaleString(undefined, { maximumFractionDigits: 6 });
}

// Build a frequency-scaled noUiSlider range from backend percentile pivots
// (info.quantiles: {"25": value, ...}). Each segment between pivots holds an
// equal share of the data, so slider travel matches data density. Returns
// null when no usable interior pivots exist (caller falls back to log/linear).
function buildQuantileSliderRange(info) {
    const q = info.quantiles;
    if (!q || info.min === undefined || info.max === undefined || info.min >= info.max) return null;
    const pivots = Object.entries(q)
        .map(([p, v]) => [parseFloat(p), Math.min(Math.max(v, info.min), info.max)])
        .filter(([p]) => isFinite(p) && p > 0 && p < 100)
        .sort((a, b) => a[0] - b[0]);
    const range = { 'min': info.min, 'max': info.max };
    let last = info.min;
    let added = 0;
    for (const [p, v] of pivots) {
        if (v > last && v < info.max) {
            range[p + '%'] = v;
            last = v;
            added++;
        }
    }
    return added > 0 ? range : null;
}

// Poll intervals
// The updateStatus interval is now handled within window.onload

// --- Global CSRF Interaction ---
(function () {
    const originalFetch = window.fetch;
    window.fetch = function (url, options) {
        options = options || {};
        const method = options.method ? options.method.toUpperCase() : 'GET';
        if (method === 'POST' || method === 'PUT' || method === 'DELETE' || method === 'PATCH') {
            const csrfToken = document.querySelector('meta[name="csrf-token"]').getAttribute('content');
            if (csrfToken) {
                options.headers = options.headers || {};
                // If headers is an instance of Headers, append; otherwise set property
                if (options.headers instanceof Headers) {
                    options.headers.append('X-CSRFToken', csrfToken);
                } else {
                    options.headers['X-CSRFToken'] = csrfToken;
                }
            }
        }
        return originalFetch.apply(this, arguments).then(function (response) {
            if (response.status === 401) {
                window.location.href = '/login';
                return Promise.reject(new Error('Session expired'));
            }
            return response;
        });
    };
})();

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

// --- Theme Toggle ---
function toggleTheme() {
    const html = document.documentElement;
    const current = html.getAttribute('data-theme') || 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', next);
    localStorage.setItem('fyp-theme', next);
    updateThemeIcon(next);
    window.dispatchEvent(new CustomEvent('theme-changed', { detail: { theme: next } }));
}

function updateThemeIcon(theme) {
    // Sync settings tab checkbox
    const settingsToggle = document.getElementById('setting-theme-toggle');
    if (settingsToggle) {
        settingsToggle.checked = theme === 'dark';
    }
}

// Apply saved theme immediately (before onload to avoid flash)
(function () {
    const saved = localStorage.getItem('fyp-theme');
    if (saved && saved !== document.documentElement.getAttribute('data-theme')) {
        document.documentElement.setAttribute('data-theme', saved);
    }
})();

// Initial load
window.onload = function () {
    // Apply theme icon
    const theme = document.documentElement.getAttribute('data-theme') || 'dark';
    updateThemeIcon(theme);

    updateStatus();
    setInterval(updateStatus, 1000); // 1 second interval

    // Load study definitions for dropdowns
    loadDefinedStudies();

    // Load User Settings
    loadUserSettings();

    // Listener for build study name change
    const buildStudySelect = document.getElementById('build-study-name');
    if (buildStudySelect) {
        buildStudySelect.addEventListener('change', function () {
            fetchStudyFiles(this.value);
        });
    }
};

// --- User Settings Logic ---
window.userSettings = {};

async function loadUserSettings() {
    try {
        const res = await fetch('/api/user/settings');
        if (res.ok) {
            window.userSettings = await res.json();
            // Trigger UI update if settings tab is open (or just generic event)
            if (typeof renderSettingsUI === 'function') {
                renderSettingsUI();
            }
        }
    } catch (e) {
        console.error("Failed to load user settings", e);
    }
}

async function saveUserSettings(newSettings) {
    try {
        const res = await fetch('/api/user/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(newSettings)
        });
        const data = await res.json();
        if (data.status === 'success') {
            window.userSettings = { ...window.userSettings, ...newSettings };
        } else {
            console.error("Error saving settings:", data.error);
        }
    } catch (e) {
        console.error("Failed to save settings", e);
    }
}

// --- Platform links ---

// Build the "open on platform" URL for an item from its source_platform, using
// the registry-derived templates injected by the server. Returns null for an
// unknown/absent platform so callers can hide the affordance rather than open a
// wrong link — the Semantic Space map used to hardcode TikTok for every dot.
function fypPlatformUrl(platform, itemId) {
    const templates = window.PLATFORM_URL_TEMPLATES || {};
    const template = platform ? templates[platform] : null;
    if (!template || !itemId) return null;
    return template.replace('{item_id}', encodeURIComponent(itemId));
}

// Home-tab getting-started panel: hide now, persist the one-shot dismissal.
async function dismissGettingStarted() {
    const panel = document.getElementById('getting-started-panel');
    if (panel) panel.style.display = 'none';
    await saveUserSettings({ getting_started_dismissed: true });
}

// The reverse, offered from My stuff -> Preferences and the help modal. The
// panel is rendered by Jinja on page load, so it only exists in the DOM when it
// was dismissed during this same session; otherwise a reload is what brings it
// back.
async function restoreGettingStarted() {
    await saveUserSettings({ getting_started_dismissed: false });
    if (typeof renderSettingsUI === 'function') renderSettingsUI();
    const panel = document.getElementById('getting-started-panel');
    if (panel) {
        panel.style.display = '';
        _navigateToTabPage('home');
        panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
    }
    window.location.hash = '#home';
    window.location.reload();
}

// --- Tag Management ---
async function loadAndRenderUserTags() {
    const container = document.getElementById('settings-tags-container');
    if (!container) return;

    container.innerHTML = '<span style="color: var(--color-text-tertiary);">Loading...</span>';

    try {
        const res = await fetch('/api/video_analysis/tags');
        const tagsData = await res.json();

        // Flatten and Count
        const tagCounts = {};
        Object.values(tagsData).forEach(item => {
            Object.values(item).forEach(tagList => {
                if (Array.isArray(tagList)) {
                    tagList.forEach(t => {
                        tagCounts[t] = (tagCounts[t] || 0) + 1;
                    });
                }
            });
        });

        // Sort by Count Descending
        const sortedTags = Object.entries(tagCounts).sort((a, b) => b[1] - a[1]);

        if (sortedTags.length === 0) {
            container.innerHTML = '<span class="italic" style="color: var(--color-text-faint);">No tags found.</span>';
            return;
        }

        container.innerHTML = '';
        sortedTags.forEach(([tag, count]) => {
            const chip = document.createElement('div');
            chip.style.cssText = `
                background: var(--color-border-subtle);
                color: var(--color-text-primary);
                border: 1px solid var(--color-border-strong);
                padding: 4px 10px;
                border-radius: 12px;
                display: flex;
                gap: 8px;
                align-items: center;
            `;
            chip.classList.add('text-sm');

            chip.innerHTML = `
                <span>${escapeHtml(tag)} <span class="text-xs" style="color: var(--color-text-muted);">(${escapeHtml(count)})</span></span>
                <span class="delete-tag-btn font-bold" style="cursor: pointer; color: var(--color-danger-soft);" title="Delete Tag">×</span>
            `;

            chip.querySelector('.delete-tag-btn').onclick = () => deleteUserTag(tag);
            container.appendChild(chip);
        });

    } catch (e) {
        console.error(e);
        container.innerHTML = '<span style="color: var(--color-danger-soft);">Error loading tags.</span>';
    }
}

async function deleteUserTag(tagName) {
    if (!(await showAppConfirm(
        `Are you sure you want to delete the tag "${tagName}"? This will remove it from all videos and cannot be undone.`,
        { title: 'Delete tag', okLabel: 'Delete', danger: true }))) {
        return;
    }

    try {
        // Tag name needs to be URL encoded properly, but Flask path param handles basic, 
        // explicit encodeURIComponent is safer for special chars.
        const res = await fetch(`/api/video_analysis/tags/${encodeURIComponent(tagName)}`, {
            method: 'DELETE'
        });
        const data = await res.json();

        if (data.status === 'success') {
            // Reload tags
            loadAndRenderUserTags();
        } else {
            showAppAlert("Error deleting tag: " + (data.message || "Unknown error"));
        }

    } catch (e) {
        console.error(e);
        showAppAlert("Failed to delete tag.");
    }
}

const _LARGE_LOAD_THRESHOLD = 100000;

function showLargeStudyLoadWarning(studyName, uniqueVideos) {
    return new Promise(resolve => {
        const overlay = document.getElementById('large-load-warning');
        const textEl = document.getElementById('large-load-warning-text');
        textEl.innerHTML = `<strong>${escapeHtml(studyName)}</strong> contains ${uniqueVideos.toLocaleString()} items. ` +
            `Loading this study may take a moment and app performance may be affected.`;
        overlay.classList.add('visible');
        document.getElementById('large-load-warning-back').onclick = () => {
            overlay.classList.remove('visible');
            resolve(false);
        };
        document.getElementById('large-load-warning-continue').onclick = () => {
            overlay.classList.remove('visible');
            resolve(true);
        };
    });
}

async function loadDefinedStudies() {
    try {
        const response = await fetch('/api/studies/defined');
        const studies = await response.json();

        const dropdownIds = [
            'global-study-name',
            'build-study-name'
        ];

        dropdownIds.forEach(id => {
            const select = document.getElementById(id);
            if (select) {
                const currentValue = select.value;

                // Keep the first "Select..." option if it exists and value is empty
                let hasDefault = false;
                if (select.options.length > 0 && select.options[0].value === "") {
                    hasDefault = true;
                }

                // Clear existing options except default
                select.innerHTML = '';
                if (hasDefault) {
                    const defaultOption = document.createElement('option');
                    defaultOption.value = "";
                    defaultOption.text = "Select a study...";
                    defaultOption.disabled = true;
                    defaultOption.selected = true;
                    select.appendChild(defaultOption);
                }

                studies.forEach(study => {
                    const option = document.createElement('option');
                    option.value = study;
                    option.text = study;
                    select.appendChild(option);
                });

                // Preserve previous selection if still available
                if (currentValue && studies.includes(currentValue)) {
                    select.value = currentValue;
                }
            }
        });

    } catch (e) {
        console.error("Error loading defined studies:", e);
    }
}

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
// by data_management.js's fetchEnrichmentStats. Returns null for processes
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

// Rebuild the Semantic Space niche map. When "Reset all labels" is ticked, every
// niche is renamed from scratch (no carry-over); otherwise stable labels are
// preserved (default). Reset is destructive, so confirm first; once a run has
// actually started, untick the box so a later Rebuild click doesn't silently
// re-run a reset.
async function rebuildNicheMap() {
    const box = document.getElementById('video_map_reset-labels');
    const reset = !!(box && box.checked);
    if (reset && !(await showAppConfirm(
        'Reset: regenerate all niche labels from scratch?\n\n' +
        'Existing labels will NOT be preserved. Cluster IDs are kept, so saved ' +
        'niche-filtered analyses still point at the same clusters.',
        { title: 'Reset niche labels', okLabel: 'Reset', danger: true }
    ))) return;
    const started = await startProcess('video_map_refresh', { reset_labels: reset });
    if (started && reset && box) box.checked = false;
}

// ── Generic pretty dialogs — the app-wide replacement for native alert()/
// confirm(). Both return Promises so callers can await a choice; the look
// reuses the stop-worker modal (.stop-confirm-overlay / .stop-confirm-card).
let _appDialogResolver = null;

function _closeAppDialog(result) {
    const overlay = document.getElementById('app-dialog-overlay');
    if (overlay) overlay.classList.remove('visible');
    document.removeEventListener('keydown', _appDialogKeydown);
    if (_appDialogResolver) {
        const r = _appDialogResolver;
        _appDialogResolver = null;
        r(result);
    }
}

function _appDialogKeydown(e) {
    if (e.key === 'Escape') _closeAppDialog(false);
    else if (e.key === 'Enter') _closeAppDialog(true);
}

function _showAppDialog({ message, title = null, okLabel = 'OK', cancelLabel = null, danger = false }) {
    const overlay = document.getElementById('app-dialog-overlay');
    // Defensive fallback if the markup isn't on the page.
    if (!overlay) {
        if (cancelLabel === null) { window.alert(message); return Promise.resolve(true); }
        return Promise.resolve(window.confirm(message));
    }
    // Resolve any dialog already open as cancelled before showing a new one.
    if (_appDialogResolver) _closeAppDialog(false);

    const titleEl = document.getElementById('app-dialog-title');
    const textEl = document.getElementById('app-dialog-text');
    const okBtn = document.getElementById('app-dialog-ok-btn');
    const cancelBtn = document.getElementById('app-dialog-cancel-btn');

    titleEl.textContent = title || '';
    titleEl.style.display = title ? 'block' : 'none';
    textEl.textContent = message == null ? '' : String(message);
    okBtn.textContent = okLabel || 'OK';
    okBtn.className = danger ? 'btn-stop' : 'btn-primary';
    cancelBtn.textContent = cancelLabel || 'Cancel';
    cancelBtn.style.display = cancelLabel === null ? 'none' : '';

    okBtn.onclick = () => _closeAppDialog(true);
    cancelBtn.onclick = () => _closeAppDialog(false);
    overlay.onclick = (e) => { if (e.target === overlay) _closeAppDialog(false); };

    document.addEventListener('keydown', _appDialogKeydown);
    overlay.classList.add('visible');
    setTimeout(() => { try { okBtn.focus(); } catch (_) { } }, 50);
    return new Promise(resolve => { _appDialogResolver = resolve; });
}

// Pretty alert: one OK button. Resolves when dismissed. Safe to fire-and-forget.
function showAppAlert(message, opts = {}) {
    return _showAppDialog({ ...opts, message, cancelLabel: null });
}

// Pretty confirm: OK + Cancel. Resolves to true (OK) / false (Cancel/Esc/backdrop).
function showAppConfirm(message, opts = {}) {
    return _showAppDialog({
        okLabel: 'OK',
        cancelLabel: 'Cancel',
        ...opts,
        message,
    });
}


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
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
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



function showStopConfirm(name) {
    _pendingStopProcess = name;
    const btn = document.getElementById(`${name}-toggle`);
    const label = btn ? btn.getAttribute('data-start-label') : name;
    const shortName = label.replace(/^(Start |Recalculate )/, '').replace(/\(.*\)/, '').trim();
    document.getElementById('stop-confirm-text').innerText = `Stop ${shortName}?`;
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
    try {
        const res = await fetch('/api/status');
        const data = await res.json();

        setStatus('downloader', data.downloader);
        setStatus('monitor', data.monitor);
        setStatus('annotator', data.annotator);
        //setStatus('create_subsets', data.create_subsets);
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

        // Spinner on the "Refresh Caches" sidebar item (same style as the
        // global badge spinner) while any of that page's processes runs.
        const refreshSpinner = document.getElementById('refresh-caches-running-spinner');
        if (refreshSpinner) {
            const refreshProcs = [
                'consolidate_enrichment', 'embeddings_refresh', 'video_map_refresh',
                'recode_refresh_studies', 'meta_refresh_groups', 'pca_refresh',
                'timelines_refresh',
            ];
            const anyRefreshRunning = refreshProcs.some(n => {
                const p = data[n];
                return p && (p.state === 'running' || p.state === 'stopping');
            });
            refreshSpinner.style.display = anyRefreshRunning ? 'inline-block' : 'none';
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

        // Discreet processes
        const discreetProcesses = ['create_event_log', 'recode_event_log', 'calculate_pca'];

        discreetProcesses.forEach(name => {
            const pData = data[name];
            setDiscreetStatus(name, pData);

            if (pData) {
                // Check for process completion to refresh file list
                if (previousProcessStates[name] === 'running' && pData.state !== 'running') {
                    // Process just finished
                    const buildStudySelect = document.getElementById('build-study-name');
                    if (buildStudySelect && buildStudySelect.value) {
                        fetchStudyFiles(buildStudySelect.value);
                    }
                }
                previousProcessStates[name] = pData.state;
            }
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

    const el = document.getElementById(`${name}-status`);
    if (el) el.className = `status-indicator status-${status}`;

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
                const extraRaw = toggleBtn.getAttribute('data-start-extra');
                const extra = extraRaw ? JSON.parse(extraRaw) : {};
                startProcess(name, extra);
            };
        }
    }

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
            text.style.color = status === 'queued'
                ? 'var(--color-warning)'
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



function setDiscreetStatus(name, data) {
    const dot = document.getElementById(`dot-${name}`);
    const text = document.getElementById(`text-${name}`);
    if (!dot || !text || !data) return;

    const state = data.state;

    // Update Dot
    dot.className = 'status-dot'; // reset
    if (state === 'running') {
        dot.classList.add('running');
    } else {
        dot.classList.add('stopped');
    }

    // Update Text
    if (state === 'running') {
        text.style.color = 'var(--color-text-tertiary)'; // Reset to neutral color
        if (data.last_message && data.last_message.trim() !== '') {
            let msg = data.last_message;
            if (msg.length > 80) {
                msg = msg.substring(0, 80) + "...";
            }
            text.innerText = msg;
            text.title = data.last_message; // Full text on hover
        } else if (data.start_time) {
            const start = fypParseInstant(data.start_time);
            const diff = start ? Date.now() - start.getTime() : 0;
            // Format duration HH:MM:SS
            const duration = new Date(diff).toISOString().substr(11, 8);
            text.innerText = duration;
        } else {
            text.innerText = "Running...";
        }
    } else {
        // Updated formatting: "Last run for study 'study_name'. <Success/Fail> <mm:ss>"
        if (data.last_run_end_time) {
            let runInfo = "Last run";
            if (data.last_run_study) {
                runInfo += ` for study '${data.last_run_study}'`;
            }

            let outcome = data.last_run_outcome || "Unknown";
            // Colour code outcome? simpler to just text for now as requested.

            let durationStr = "00:00";
            if (data.last_run_duration !== undefined) {
                let s = Math.floor(data.last_run_duration);
                let m = Math.floor(s / 60);
                s = s % 60;
                // format mm:ss
                durationStr = `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
            }

            text.innerText = `${runInfo}. ${outcome} ${durationStr}`;

            // Dynamic color for text based on outcome
            if (outcome === 'Success') {
                text.style.color = 'var(--color-success-light)';
            } else if (outcome === 'Fail') {
                text.style.color = 'var(--color-danger-soft)';
            } else {
                text.style.color = 'var(--color-text-tertiary)';
            }

        } else if (data.last_success) {
            text.innerText = `Last success: ${fypFmtDateTimeShort(data.last_success)}`;
            text.title = fypFmtDateTimeFull(data.last_success);
            text.style.color = 'var(--color-text-tertiary)';
        } else {
            text.innerText = "Last success: Never"; // Or empty
            text.style.color = 'var(--color-text-tertiary)';
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



// Tail whichever log the modal is showing. This used to iterate a hardcoded
// list of process names, so the six cards missing from it (pca_refresh,
// embeddings_refresh, sessions_refresh, video_map_refresh, demo_dataset,
// consolidate_enrichment) rendered a snapshot on open and then froze.
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





function openTab(evt, tabName) {
    // Hide all tab panes
    const tabPanes = document.getElementsByClassName("tab-pane");
    for (let i = 0; i < tabPanes.length; i++) {
        tabPanes[i].className = tabPanes[i].className.replace(" active", "");
    }

    // Remove active class from all buttons
    const tabButtons = document.getElementsByClassName("tab-button");
    for (let i = 0; i < tabButtons.length; i++) {
        tabButtons[i].className = tabButtons[i].className.replace(" active", "");
    }

    // Show current tab and activate button
    const tab = document.getElementById(tabName);
    if (tab) {
        tab.className += " active";
    }
    if (evt && evt.currentTarget) {
        evt.currentTarget.className += " active";
    }

    // Video Viewer Logic integration
    if (tabName !== 'video_analysis') {
        if (typeof pauseViewerVideo === 'function') pauseViewerVideo();
    } else {
        // Drill-down from Explore tab: apply pending filters before anything else
        if (typeof checkPendingDrillDown === 'function') {
            checkPendingDrillDown();
        }

        if (typeof playViewerVideo === 'function') {
            // Check User Settings for Autostart
            // If undefined, default to false (as requested "default unchecked")
            if (window.userSettings && window.userSettings.video_autostart) {
                playViewerVideo();
            }
        }
    }



    // My stuff tab logic (preferences / tags / profile forms)
    if (tabName === 'my_stuff' && typeof renderSettingsUI === 'function') {
        renderSettingsUI();
    }

    // Semantic Space - lazy-load the global video map on first open
    if (tabName === 'semantic_space' && typeof initSemanticSpace === 'function') {
        initSemanticSpace();
    }

    // Sessions - lazy init on first open; pause its episode players on tab-away
    if (tabName !== 'sessions') {
        if (typeof pauseSessionsVideos === 'function') pauseSessionsVideos();
    } else if (typeof initSessions === 'function') {
        initSessions();
    }

    // Trigger window resize so any charts (Plotly, etc.) can recalculate their width now that their container is visible
    setTimeout(() => {
        window.dispatchEvent(new Event('resize'));
    }, 100);

    // Update the mobile current-tab label next to the hamburger
    _setCurrentTabLabel(tabName, evt);

    // Close mobile nav drawer (if open) once a tab is selected
    closeNavDrawer();

    // Keep the URL hash in sync (a sub-page opener may refine it to
    // #tab/sub-page right after).
    if (!_applyingHashNav) {
        history.replaceState(null, '', `#${tabName}`);
    }
}

// ============================================================
// Hash deep-linking (#tab or #tab/sub-page, e.g. #admin/backends)
// ============================================================

// Sidebar page-id prefix per tab with sub-pages; the hash carries the id
// minus this prefix ("admin-page-backends" → "#admin/backends").
const _HASH_PAGE_PREFIX = {
    admin: 'admin-page-',
    data_management: 'dm-page-',
    my_stuff: 'my-stuff-page-',
};

// True while a hash is being applied, so openTab / the sub-page openers
// don't rewrite the hash mid-navigation.
let _applyingHashNav = false;

// Called by the sub-page openers (openAdminPage / openDataManagementPage /
// openMyStuffPage) after they switch pages.
function updateSubPageHash(tabId, pageId) {
    if (_applyingHashNav) return;
    const prefix = _HASH_PAGE_PREFIX[tabId];
    if (!prefix || !pageId) return;
    const slug = pageId.startsWith(prefix) ? pageId.slice(prefix.length) : pageId;
    history.replaceState(null, '', `#${tabId}/${slug}`);
}

// Switch to a tab (and optionally one of its sidebar sub-pages) the same way
// a user click would — used by the mobile sub-nav and by hash navigation.
function _navigateToTabPage(tabId, pageId) {
    const pane = document.getElementById(tabId);
    if (!pane) return;
    const activePane = document.querySelector('.tab-pane.active');
    if (!activePane || activePane.id !== tabId) {
        // Call openTab directly (no event) to avoid re-triggering the
        // capture-phase expand handler on the tab button.
        openTab(null, tabId);
        // openTab clears all .tab-button.active and only re-applies from
        // evt.currentTarget; manually set the active state and label.
        const tabBtn = document.querySelector(
            `.tab-button[data-tab="${tabId}"], .tab-button[data-subpages-for="${tabId}"]`);
        if (tabBtn) {
            tabBtn.classList.add('active');
            const label = document.getElementById('current-tab-label');
            if (label) label.textContent = tabBtn.textContent.trim();
        }
        if (tabId === 'admin' && typeof loadUsers === 'function') {
            loadUsers();
        }
    }
    if (pageId) {
        const sidebarItem = pane.querySelector(`.dm-sidebar .dm-sidebar-item[data-page="${pageId}"]`);
        if (sidebarItem) sidebarItem.click();
    }
}

// Sub-page slugs that were renamed; old deep links keep working.
const _HASH_SLUG_ALIASES = {
    'data_management/enrichment': 'scrape',
};

function _applyHashNavigation() {
    const m = (location.hash || '').match(/^#([a-z_]+)(?:\/([a-z0-9-]+))?$/);
    if (!m) return;
    const tabId = m[1];
    let slug = m[2];
    if (slug) slug = _HASH_SLUG_ALIASES[`${tabId}/${slug}`] || slug;
    const pane = document.getElementById(tabId);
    // Only navigate to real tab panes the current user can see.
    if (!pane || !pane.classList.contains('tab-pane')) return;
    const pageId = slug ? (_HASH_PAGE_PREFIX[tabId] || '') + slug : null;
    _applyingHashNav = true;
    try {
        _navigateToTabPage(tabId, pageId);
    } finally {
        _applyingHashNav = false;
    }
}

const _TAB_TITLE_MAP = {
    home: 'Home',
    explore: 'Explore',
    timelines: 'Timelines',
    video_analysis: 'Video Analysis',
    correlations: 'Correlations',
    semantic_space: 'Semantic Space',
    sessions: 'Sessions',
    my_stuff: 'My stuff',
    data_management: 'Data Pipeline',
    admin: 'Admin'
};

function _setCurrentTabLabel(tabName, evt) {
    const label = document.getElementById('current-tab-label');
    if (!label) return;
    let text = '';
    if (evt && evt.currentTarget && evt.currentTarget.textContent) {
        text = evt.currentTarget.textContent.trim();
    }
    if (!text) text = _TAB_TITLE_MAP[tabName] || tabName;
    label.textContent = text;
}


// ============================================================
// Small-screen drawer helpers (header nav + side panels)
// ============================================================

function _responsiveBackdrop() {
    return document.getElementById('responsive-backdrop');
}

function _isMobileViewport() {
    return window.matchMedia('(max-width: 860px)').matches;
}

function _showBackdrop() {
    const bd = _responsiveBackdrop();
    if (bd) bd.classList.add('is-visible');
}

function _hideBackdropIfIdle() {
    const bd = _responsiveBackdrop();
    if (!bd) return;
    const navOpen = document.getElementById('main-tab-nav')?.classList.contains('is-open');
    const anyPanelOpen = !!document.querySelector('[data-mobile-drawer].is-open');
    if (!navOpen && !anyPanelOpen) {
        bd.classList.remove('is-visible');
    }
}

function toggleNavDrawer() {
    const nav = document.getElementById('main-tab-nav');
    if (!nav) return;
    const willOpen = !nav.classList.contains('is-open');

    // Close any open panel drawers first
    document.querySelectorAll('[data-mobile-drawer].is-open').forEach(p => p.classList.remove('is-open'));

    nav.classList.toggle('is-open', willOpen);
    const hamburger = document.getElementById('nav-hamburger');
    if (hamburger) hamburger.setAttribute('aria-expanded', String(willOpen));

    if (willOpen) {
        _showBackdrop();
        _syncTabSubnavExpansion();
    } else {
        _hideBackdropIfIdle();
    }
}

// ----- Two-level mobile menu (chevron + nested sub-pages) -----

function _buildTabSubnavs() {
    document.querySelectorAll('.tab-subnav[data-subpages-of]').forEach(ul => {
        const tabId = ul.getAttribute('data-subpages-of');
        const pane = document.getElementById(tabId);
        if (!pane) return;
        // Fresh build — clear any prior content (idempotent). Walk items and
        // section headers in document order so groups carry over.
        ul.innerHTML = '';
        pane.querySelectorAll('.dm-sidebar .dm-sidebar-item, .dm-sidebar .dm-sidebar-group').forEach(src => {
            if (src.classList.contains('dm-sidebar-group')) {
                const li = document.createElement('li');
                li.className = 'tab-subnav-group';
                li.textContent = src.textContent.trim();
                ul.appendChild(li);
                return;
            }
            const pageId = src.getAttribute('data-page');
            if (!pageId) return;
            // Skip items hidden by feature logic (e.g. "My Tasks" until the
            // user has coding invitations) — rebuilt when they are unhidden.
            if (src.style.display === 'none') return;
            const li = document.createElement('li');
            li.className = 'tab-subnav-item';
            li.setAttribute('data-target-tab', tabId);
            li.setAttribute('data-target-page', pageId);
            if (src.classList.contains('active')) {
                li.classList.add('active');
            }
            // Read the label without any appended badge (e.g. task count).
            const clone = src.cloneNode(true);
            clone.querySelectorAll('.hc-tab-badge').forEach(b => b.remove());
            li.textContent = clone.textContent.trim();
            ul.appendChild(li);
        });
    });
}

function _collapseAllTabSubnavs(exceptTabId) {
    document.querySelectorAll('.tab-button.has-subpages').forEach(btn => {
        const tabId = btn.getAttribute('data-subpages-for');
        if (tabId === exceptTabId) return;
        btn.classList.remove('is-expanded');
        const ul = document.querySelector(`.tab-subnav[data-subpages-of="${tabId}"]`);
        if (ul) ul.hidden = true;
    });
}

function _toggleTabSubnav(button) {
    const tabId = button.getAttribute('data-subpages-for');
    if (!tabId) return;
    const ul = document.querySelector(`.tab-subnav[data-subpages-of="${tabId}"]`);
    if (!ul) return;
    const willExpand = !button.classList.contains('is-expanded');
    _collapseAllTabSubnavs(tabId);
    button.classList.toggle('is-expanded', willExpand);
    ul.hidden = !willExpand;
}

function _syncTabSubnavExpansion() {
    // When the drawer opens, expand the sub-nav for the currently-active tab
    // (if it has sub-pages) so the user sees where they are.
    const activePane = document.querySelector('.tab-pane.active');
    if (!activePane) {
        _collapseAllTabSubnavs(null);
        return;
    }
    const activeTabId = activePane.id;
    const expandBtn = document.querySelector(`.tab-button.has-subpages[data-subpages-for="${activeTabId}"]`);
    if (!expandBtn) {
        _collapseAllTabSubnavs(null);
        return;
    }
    _collapseAllTabSubnavs(activeTabId);
    expandBtn.classList.add('is-expanded');
    const ul = document.querySelector(`.tab-subnav[data-subpages-of="${activeTabId}"]`);
    if (ul) {
        ul.hidden = false;
        // Mirror the .active state from the original sidebar onto the sub-rows.
        const activeSidebarItem = activePane.querySelector('.dm-sidebar .dm-sidebar-item.active');
        const activePage = activeSidebarItem ? activeSidebarItem.getAttribute('data-page') : null;
        ul.querySelectorAll('.tab-subnav-item').forEach(li => {
            li.classList.toggle('active', li.getAttribute('data-target-page') === activePage);
        });
    }
}

function closeNavDrawer() {
    const nav = document.getElementById('main-tab-nav');
    if (!nav || !nav.classList.contains('is-open')) return;
    nav.classList.remove('is-open');
    const hamburger = document.getElementById('nav-hamburger');
    if (hamburger) hamburger.setAttribute('aria-expanded', 'false');
    _hideBackdropIfIdle();
}

function toggleMobileDrawer(targetSelector) {
    if (!targetSelector) return;
    const target = document.querySelector(targetSelector);
    if (!target) return;

    const willOpen = !target.classList.contains('is-open');

    // Close any other open drawers (panels + nav) — single-drawer-at-a-time policy
    document.querySelectorAll('[data-mobile-drawer].is-open').forEach(p => {
        if (p !== target) p.classList.remove('is-open');
    });
    closeNavDrawer();

    target.classList.toggle('is-open', willOpen);
    if (willOpen) {
        _showBackdrop();
    } else {
        _hideBackdropIfIdle();
    }
}

function closeAllResponsiveDrawers() {
    document.querySelectorAll('[data-mobile-drawer].is-open').forEach(p => p.classList.remove('is-open'));
    closeNavDrawer();
    _hideBackdropIfIdle();
}

// Wire two-level mobile menu and auto-close behaviour.
document.addEventListener('DOMContentLoaded', function () {
    // Backwards-compat: any remaining .mobile-drawer-trigger buttons still work.
    document.querySelectorAll('.mobile-drawer-trigger').forEach(btn => {
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            const target = this.getAttribute('data-drawer-target');
            toggleMobileDrawer(target);
        });
    });

    // Build the nested sub-page lists once on load.
    _buildTabSubnavs();

    // Deep links: honour a #tab/sub-page hash on load and on back/forward.
    _applyHashNavigation();
    window.addEventListener('hashchange', _applyHashNavigation);

    // Intercept clicks on top-level tab buttons that have sub-pages: on mobile
    // they expand the nested list instead of navigating. Capture phase +
    // stopImmediatePropagation blocks the inline onclick on the same element.
    document.querySelectorAll('.tab-button.has-subpages').forEach(btn => {
        btn.addEventListener('click', function (e) {
            if (!_isMobileViewport()) return;
            e.stopImmediatePropagation();
            e.preventDefault();
            _toggleTabSubnav(this);
        }, true);
    });

    // Sub-nav item click → switch tab (if needed) and open the sub-page.
    document.addEventListener('click', function (e) {
        const sub = e.target.closest('.tab-subnav-item');
        if (!sub) return;
        e.stopPropagation();
        e.preventDefault();
        const tabId = sub.getAttribute('data-target-tab');
        const pageId = sub.getAttribute('data-target-page');
        if (!document.getElementById(tabId)) return;
        _navigateToTabPage(tabId, pageId);
        // Mirror active state onto the sub-rows.
        sub.parentElement.querySelectorAll('.tab-subnav-item').forEach(li => li.classList.remove('active'));
        sub.classList.add('active');
        closeNavDrawer();
    });

    // When a sidebar item inside a mobile drawer is clicked, close the drawer
    // so the user sees the page they just selected.
    document.addEventListener('click', function (e) {
        if (!_isMobileViewport()) return;
        const item = e.target.closest('.dm-sidebar-item');
        if (item) {
            // small delay so the sidebar's own click handler runs first
            setTimeout(closeAllResponsiveDrawers, 0);
        }
    });

    // Close on Escape
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') closeAllResponsiveDrawers();
    });

    // If user resizes from mobile to desktop while a drawer is open, clean up
    window.addEventListener('resize', function () {
        if (!_isMobileViewport()) closeAllResponsiveDrawers();
    });
});

// Expose for inline handlers
window.toggleNavDrawer = toggleNavDrawer;
window.toggleMobileDrawer = toggleMobileDrawer;
window.closeAllResponsiveDrawers = closeAllResponsiveDrawers;


async function fetchStudyFiles(studyName) {
    if (!studyName) return;

    const container = document.getElementById('study-export-files-container');
    if (!container) return;

    container.innerHTML = '<p>Loading...</p>';

    try {
        const res = await fetch(`/api/study_files/${studyName}`);
        const files = await res.json();

        if (files.error) {
            container.innerHTML = `<p style="color: var(--color-danger-soft);">Error: ${files.error}</p>`;
            return;
        }

        let html = '<ul style="list-style: none; padding-left: 0; margin-top: 5px;">';
        // Order: HALF_BAKED, UNIQUE, LOG, RECODED, PCA (Custom order if desired, or just iterate)
        const order = ["HALF_BAKED", "UNIQUE", "LOG", "RECODED", "PCA"];

        order.forEach(category => {
            if (files[category]) {
                // Make category name nicer?
                // e.g. HALF_BAKED -> Half Baked
                // But keeping it consistent with the keys is fine too, or simple title case.
                // Let's just use the key for now or a map.
                const labelMap = {
                    "HALF_BAKED": "Half-Baked Datasets",
                    "UNIQUE": "Unique Subsets",
                    "LOG": "Event Log",
                    "RECODED": "Recoded Log",
                    "PCA": "PCA Scores"
                };
                const label = labelMap[category] || category;

                html += `<li style="margin-bottom: 5px;">
                   <strong style="color: var(--color-text-primary);">${label}:</strong> <span style="color: var(--color-text-tertiary);">${files[category]}</span>
               </li>`;
            }
        });
        html += '</ul>';
        container.innerHTML = html;

    } catch (e) {
        console.error(e);
        container.innerHTML = `<p style="color: var(--color-danger-soft);">Failed to load files.</p>`;
    }
}
