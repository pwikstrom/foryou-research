// CSS variable helper for dynamic JS styling
function getCSSVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
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
let _pendingStopProcess = null;
let _activeLogModal = null;
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
                <span>${tag} <span class="text-xs" style="color: var(--color-text-muted);">(${count})</span></span>
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
    if (!confirm(`Are you sure you want to delete the tag "${tagName}"? This will remove it from all videos and cannot be undone.`)) {
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
            alert("Error deleting tag: " + (data.message || "Unknown error"));
        }

    } catch (e) {
        console.error(e);
        alert("Failed to delete tag.");
    }
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
            }
        });

    } catch (e) {
        console.error("Error loading defined studies:", e);
    }
}

async function startProcess(name) {
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

    if (name === 'queue_scraper') {
        const bsEl = document.getElementById('scrapes-batch-size');
        const mbEl = document.getElementById('scrapes-max-batches');
        batchSize = bsEl ? bsEl.value : null;
        maxBatches = mbEl ? mbEl.value : null;
    } else if (name === 'queue_annotator') {
        const bsEl = document.getElementById('annotations-batch-size');
        const mbEl = document.getElementById('annotations-max-batches');
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
            alert("Please select or enter a study name.");
            return;
        }
        body = {
            study_name: studyName,
            batch_size: batchSize,
            max_batches: maxBatches
        };
    } else {
        body = {
            batch_size: batchSize,
            max_batches: maxBatches
        };
    }
    try {
        const res = await fetch(`/api/start/${name}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await res.json();
        if (data.status !== 'success') {
            alert("Error: " + data.message);
        }
        updateStatus();
    } catch (e) {
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
}



async function stopProcess(name) {
    try {
        const res = await fetch(`/api/stop/${name}`, { method: 'POST' });
        const data = await res.json();
        if (data.status !== 'success') {
            console.error("Stop process error:", data.message);
        }
        updateStatus();
    } catch (e) {
        console.error(e);
    }
}



async function toggleProcess(name, label) {
    // Check current state inferred from UI or wait for status update
    // But better to just check the status from the status object if we had it global.
    // Use the API to check status is safer, but simpler is: just try to start, if 409 (already running) -> stop.
    // However, the button logic is: "button that is both starting and stopping".

    // Let's fetch status first to be sure
    try {
        const res = await fetch('/api/status');
        const data = await res.json();
        const pData = data[name];

        if (pData && pData.state === 'running') {
            await stopProcess(name);
        } else {
            await startProcess(name);
        }
    } catch (e) {
        console.error(e);
    }
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
    document.getElementById('log-modal-title').innerText = `${displayLabel} Log`;
    document.getElementById('log-modal-content').textContent = '';
    document.getElementById('log-modal-overlay').classList.add('visible');
    fetchLogs(name);
}

function closeLogModal() {
    _activeLogModal = null;
    document.getElementById('log-modal-overlay').classList.remove('visible');
}



async function updateStatus() {
    try {
        const res = await fetch('/api/status');
        const data = await res.json();

        setStatus('downloader', data.downloader);
        setStatus('monitor', data.monitor);
        setStatus('annotator', data.annotator);
        //setStatus('create_subsets', data.create_subsets);
        setStatus('queue_scraper', data.queue_scraper);
        setStatus('queue_annotator', data.queue_annotator);
        setStatus('meta_refresh_viewer', data.meta_refresh_viewer);
        setStatus('meta_refresh_groups', data.meta_refresh_groups);
        setStatus('timelines_refresh', data.timelines_refresh);
        setStatus('recode_refresh_studies', data.recode_refresh_studies);
        setStatus('pca_refresh', data.pca_refresh);

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
        ['queue_scraper', 'queue_annotator'].forEach(name => {
            const pData = data[name];
            if (pData && previousProcessStates[name] === 'running' && pData.state !== 'running') {
                if (typeof fetchEnrichmentStats === 'function') {
                    fetchEnrichmentStats();
                }
            }
            if (pData) previousProcessStates[name] = pData.state;
        });

        // Detect downstream process completion → refresh staleness indicators + cascade logic
        ['recode_refresh_studies', 'meta_refresh_viewer', 'meta_refresh_groups', 'timelines_refresh', 'pca_refresh'].forEach(name => {
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
                    const allDone = ['recode_refresh_studies', 'meta_refresh_viewer', 'meta_refresh_groups', 'timelines_refresh', 'pca_refresh'].every(p => {
                        const pd = data[p];
                        return !pd || pd.state !== 'running';
                    });
                    if (allDone && _cascadeRefresh.phase === 'waiting_for_meta' && typeof onCascadeRefreshComplete === 'function') {
                        // Ensure meta processes were actually started before declaring complete
                        if (_cascadeRefresh.startedMetaViewer || _cascadeRefresh.startedMetaGroups || _cascadeRefresh.startedPca) {
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



function setStatus(name, data) {
    if (!data) return;
    const status = data.state;
    const info = data.progress || {};

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
            toggleBtn.onclick = function () { startProcess(name); };
        }
    }

    const bar = document.getElementById(`${name}-bar`);
    const text = document.getElementById(`${name}-text`);
    if (bar && text) {
        if (Object.keys(info).length > 0 && (info.total > 0 || info.percent !== undefined)) {
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
            const startDate = new Date(data.start_time);
            const sdd = String(startDate.getDate()).padStart(2, '0');
            const smon = startDate.toLocaleString('en-US', { month: 'short' });
            const hh = String(startDate.getHours()).padStart(2, '0');
            const mi = String(startDate.getMinutes()).padStart(2, '0');
            lastRunEl.innerText = `This run started: ${sdd}-${smon} ${hh}:${mi}`;
            lastRunEl.style.color = 'var(--color-success-light)';
        } else if (data.last_run_end_time) {
            const endDate = new Date(data.last_run_end_time);
            const dd = String(endDate.getDate()).padStart(2, '0');
            const mon = endDate.toLocaleString('en-US', { month: 'short' });
            const hh = String(endDate.getHours()).padStart(2, '0');
            const mi = String(endDate.getMinutes()).padStart(2, '0');

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

            lastRunEl.innerText = `Last: ${dd}-${mon} ${hh}:${mi}${durStr}${outcomeStr}`;
        } else {
            lastRunEl.innerText = '';
        }
    }

    // Update queue displays from ::DATA:: output (only while running —
    // when idle the management stats endpoint is the source of truth)
    if (data.state === 'running') {
        const procData = data.data || {};
        if (name === 'queue_scraper' && procData.scrape_queue_len !== undefined) {
            const el = document.getElementById('enrich_scrape_targets');
            if (el) el.textContent = procData.scrape_queue_len.toLocaleString();
        }
        if (name === 'queue_annotator' && procData.annotate_queue_len !== undefined) {
            const el = document.getElementById('enrich_annotate_targets');
            if (el) el.textContent = procData.annotate_queue_len.toLocaleString();
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
            const start = new Date(data.start_time);
            const now = new Date();
            const diff = now - start;
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
            const sd = new Date(data.last_success);
            const sdd = String(sd.getDate()).padStart(2, '0');
            const smon = sd.toLocaleString('en-US', { month: 'short' });
            const shh = String(sd.getHours()).padStart(2, '0');
            const smi = String(sd.getMinutes()).padStart(2, '0');
            text.innerText = `Last success: ${sdd}-${smon} ${shh}:${smi}`;
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



async function updateLogs() {
    await fetchLogs('downloader');
    await fetchLogs('monitor');
    await fetchLogs('annotator');
    //await fetchLogs('create_subsets');
    await fetchLogs('queue_scraper');
    await fetchLogs('queue_annotator');
    await fetchLogs('meta_refresh_viewer');
    await fetchLogs('meta_refresh_groups');
    await fetchLogs('timelines_refresh');
    await fetchLogs('recode_refresh_studies');
}



async function fetchLogs(name) {
    try {
        if (_activeLogModal !== name) return;

        const el = document.getElementById('log-modal-content');
        if (!el) return;

        const res = await fetch(`/api/logs/${name}`);
        const data = await res.json();

        const atBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 5;
        el.textContent = data.logs;

        if (atBottom || el.scrollTop === 0) {
            el.scrollTop = el.scrollHeight;
        }
    } catch (e) {
        console.error(e);
    }
}





async function clearLogs(name) {
    try {
        const res = await fetch(`/api/logs/clear/${name}`, { method: 'POST' });
        const el = document.getElementById(`${name}-logs`);
        if (el) {
            el.textContent = "";
        }
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



    // Settings Tab Logic
    if (tabName === 'settings' && typeof renderSettingsUI === 'function') {
        renderSettingsUI();
    }

    // Persona Explorer - init on first open
    if (tabName === 'collections') {
        if (typeof pe_onShow === 'function') {
            pe_onShow();
        } else if (typeof pe_init === 'function' && (!window.pe_data || window.pe_data.length === 0)) {
            pe_init();
        }
    }

    // Trigger window resize so any charts (Plotly, etc.) can recalculate their width now that their container is visible
    setTimeout(() => {
        window.dispatchEvent(new Event('resize'));
    }, 100);
}

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

async function checkDatasets() {
    const studyName = document.getElementById('build-study-name').value;
    const container = document.getElementById('dataset-check-results');

    if (!studyName) {
        alert("Please select a study first.");
        return;
    }

    container.innerHTML = '<p>Checking datasets (this may take a moment)...</p>';

    try {
        const res = await fetch(`/api/check_datasets/${studyName}`);
        const data = await res.json();

        if (data.error) {
            container.innerHTML = `<p style="color: var(--color-danger-soft);">Error: ${data.error}</p>`;
            return;
        }

        if (data.length === 0) {
            container.innerHTML = '<p>No datasets found for this study.</p>';
            return;
        }

        let html = '<table style="width: 100%; text-align: left; border-collapse: collapse; margin-top: 10px;">';
        html += '<tr style="border-bottom: 1px solid var(--color-border-strong);">';
        html += '<th style="padding: 4px 15px 4px 4px;">Filename</th>';
        html += '<th style="padding: 4px 15px 4px 4px;">Rows</th>';
        html += '<th style="padding: 4px 15px 4px 4px;">Cols</th>';
        html += '<th style="padding: 4px 15px 4px 4px;">Nunique Items</th>';
        html += '<th style="padding: 4px 15px 4px 4px;">Group Size</th>';
        html += '<th style="padding: 4px 15px 4px 4px;">Size (KB)</th>';
        html += '</tr>';

        data.forEach(file => {
            // Handle errors per file
            if (file.error) {
                html += `<tr><td>${file.filename}</td><td colspan="3" style="color: var(--color-danger-soft);">${file.error}</td></tr>`;
            } else {
                html += `<tr>
                    <td style="padding: 4px 15px 4px 4px;">${file.filename}</td>
                    <td style="padding: 4px 15px 4px 4px;">${file.rows.toLocaleString()}</td>
                    <td style="padding: 4px 15px 4px 4px;">${file.cols.toLocaleString()}</td>
                    <td style="padding: 4px 15px 4px 4px;">${file.nunique_items.toLocaleString()}</td>
                    <td style="padding: 4px 15px 4px 4px;">${file.group_factor_counts.toLocaleString()}</td>
                    <td style="padding: 4px 15px 4px 4px;">${file.size_kb.toLocaleString()}</td>
                 </tr>`;
            }
        });

        html += '</table>';
        container.innerHTML = html;

    } catch (e) {
        console.error(e);
        container.innerHTML = `<p style="color: var(--color-danger-soft);">Failed to check datasets.</p>`;
    }
}

async function checkVideoCounts() {
    const studyName = document.getElementById('global-study-name').value;
    const display = document.getElementById('video-counts-display');

    if (!studyName) {
        alert("Please select a study first.");
        return;
    }

    display.innerHTML = 'Checking...';
    display.style.color = 'var(--color-text-tertiary)';

    try {
        const res = await fetch(`/api/check_video_counts/${studyName}`);
        const data = await res.json();

        if (data.error) {
            display.innerHTML = `Error: ${data.error}`;
            display.style.color = 'var(--color-danger-soft)';
            return;
        }

        // data = { "annotate": [rows, cols], "scrape": [rows, cols] }
        // Tuple usually comes as array in JSON: [rows, cols]

        const scrapeCount = data.scrape ? data.scrape[0] : 0;
        const annotateCount = data.annotate ? data.annotate[0] : 0;

        display.innerHTML = `Videos to scrape: <b>${scrapeCount.toLocaleString()}</b> | Videos to annotate: <b>${annotateCount.toLocaleString()}</b>`;
        display.style.color = 'var(--color-text-primary)';

    } catch (e) {
        console.error(e);
        display.innerHTML = `Failed to check counts.`;
        display.style.color = 'var(--color-danger-soft)';
    }
}
