// Poll intervals
// The updateStatus interval is now handled within window.onload
let previousProcessStates = {};
setInterval(updateLogs, 1000);

// Initial load
window.onload = function () {
    updateStatus();
    setInterval(updateStatus, 1000); // 1 second interval

    // Load study definitions for dropdowns
    loadDefinedStudies();

    // Listener for build study name change
    const buildStudySelect = document.getElementById('build-study-name');
    if (buildStudySelect) {
        buildStudySelect.addEventListener('change', function () {
            fetchStudyFiles(this.value);
        });
    }
};

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


    const studyName = document.getElementById(studyNameInputId).value;
    const batchSize = document.getElementById('global-batch-size') ? document.getElementById('global-batch-size').value : null;
    const maxBatches = document.getElementById('global-max-batches') ? document.getElementById('global-max-batches').value : null;

    if (['downloader', 'annotator', 'create_subsets', 'regenerate_datasets', 'create_event_log', 'recode_event_log', 'calculate_pca'].includes(name)) {
        if (!studyName) {
            alert("Please enter a study name.");
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
            alert("Error: " + data.message);
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



async function updateStatus() {
    try {
        const res = await fetch('/api/status');
        const data = await res.json();

        setStatus('downloader', data.downloader);
        setStatus('monitor', data.monitor);
        setStatus('annotator', data.annotator);
        setStatus('create_subsets', data.create_subsets);

        // Discreet processes
        const discreetProcesses = ['regenerate_datasets', 'create_event_log', 'recode_event_log', 'calculate_pca'];

        discreetProcesses.forEach(name => {
            setDiscreetStatus(name, data[name]);

            // Check for process completion to refresh file list
            if (previousProcessStates[name] === 'running' && data[name].state !== 'running') {
                // Process just finished
                const buildStudySelect = document.getElementById('build-study-name');
                if (buildStudySelect && buildStudySelect.value) {
                    fetchStudyFiles(buildStudySelect.value);
                }
            }
            previousProcessStates[name] = data[name].state;
        });

    } catch (e) {
        console.error(e);
    }
}



function setStatus(name, data) {
    const status = data.state;
    const info = data.progress || {};

    const el = document.getElementById(`${name}-status`);
    if (el) el.className = `status-indicator status-${status}`;

    const bar = document.getElementById(`${name}-bar`);
    const text = document.getElementById(`${name}-text`);
    if (bar && text) {
        if (Object.keys(info).length > 0 && info.total > 0) {
            const pct = (info.done / info.total) * 100;
            bar.style.width = `${pct}%`;
            const etaStr = formatETA(info.eta);

            // Calculate duration
            let durationStr = "";
            if (data.start_time) {
                const start = new Date(data.start_time);
                const now = new Date();
                const diff = (now - start) / 1000; // seconds
                durationStr = " - Time: " + formatETA(diff);
            }

            text.innerText = `${info.done.toLocaleString()} / ${info.total.toLocaleString()} (${pct.toFixed(1)}%) - ${info.rate.toFixed(2)}/s - ETA ${etaStr}${durationStr}`;
        } else {
            if (status === 'stopped') {
                // bar.style.width = '0%';
                // text.innerText = 'Idle';
            } else {
                // keep last state or show init?
                if (bar.style.width === '0%' || bar.style.width === '') {
                    text.innerText = 'Initializing...';
                }
            }
            if (status === 'stopped') {
                text.innerText = 'Idle';
            }
        }
    }

    if (name === 'create_subsets' && data.data && Object.keys(data.data).length > 0) {
        renderSubsetChart(data.data);
    }
}



function setDiscreetStatus(name, data) {
    const dot = document.getElementById(`dot-${name}`);
    const text = document.getElementById(`text-${name}`);
    if (!dot || !text) return;

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
        text.style.color = '#aaa'; // Reset to neutral color
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
            // Color code outcome? simpler to just text for now as requested.

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
                text.style.color = '#4cd964'; // Greenish
            } else if (outcome === 'Fail') {
                text.style.color = '#ff3b30'; // Reddish
            } else {
                text.style.color = '#aaa';
            }

        } else if (data.last_success) {
            // Fallback for old stats or undefined new stats
            const successTime = new Date(data.last_success);
            text.innerText = "Last success: " + successTime.toLocaleString();
            text.style.color = '#aaa';
        } else {
            text.innerText = "Last success: Never"; // Or empty
            text.style.color = '#aaa';
        }
    }
}




function renderSubsetChart(data) {
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
            font: { color: '#d4d4d4' }
        }
    };

    Plotly.react('subsets-pie-chart', plotData, layout, { displayModeBar: false });
}



function formatETA(seconds) {
    if (seconds === undefined || seconds === null) return "--";
    let val = parseFloat(seconds);
    if (isNaN(val)) return seconds;

    // Use absolute value just in case, though ETA shouldn't be negative
    val = Math.abs(val);

    let h = Math.floor(val / 3600);
    let rem = val % 3600;
    let m = Math.floor(rem / 60);
    let s = Math.floor(rem % 60);

    let str = "";
    if (h > 0) str += h + "h";
    if (m > 0) str += m + "m";
    if (s > 0) str += s + "s";

    if (str === "") return "0s";
    return str;
}



async function updateLogs() {
    await fetchLogs('downloader');
    await fetchLogs('monitor');
    await fetchLogs('annotator');
    await fetchLogs('create_subsets');
    // await fetchLogs('regenerate_datasets'); // Optional if we want logs visible somewhere
}



async function fetchLogs(name) {
    try {
        const el = document.getElementById(`${name}-logs`);
        if (!el) return;

        const res = await fetch(`/api/logs/${name}`);
        const data = await res.json();


        // Basic check to see if we should scroll (simple autoscroll)
        const atBottom = el.scrollHeight - el.scrollTop === el.clientHeight;

        el.textContent = data.logs;

        if (atBottom || el.scrollTop === 0) { // Keep autoscrolling if already at bottom or just started
            el.scrollTop = el.scrollHeight;
        }
    } catch (e) {
        console.error(e);
    }
}



async function loadConfig(filename, targetIdOverride = null) {
    try {
        const res = await fetch(`/api/config?file=${filename}&_t=${Date.now()}`);
        const data = await res.json();

        let targetId = targetIdOverride;
        if (!targetId) {
            targetId = filename === 'studies.toml' ? 'config-editor-studies' : 'config-editor-core';
        }

        const el = document.getElementById(targetId);
        if (el) el.value = data.content;
    } catch (e) {
        console.error(e);
    }
}

async function saveConfig(filename, sourceIdOverride = null) {
    let sourceId = sourceIdOverride;
    if (!sourceId) {
        sourceId = filename === 'studies.toml' ? 'config-editor-studies' : 'config-editor-core';
    }

    const el = document.getElementById(sourceId);
    if (!el) return;

    const content = el.value;
    try {
        const res = await fetch(`/api/config?file=${filename}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: content })
        });
        const data = await res.json();
        if (data.status === 'success') {
            alert(`${filename} saved!`);
        } else {
            alert("Error saving config: " + data.message);
        }
    } catch (e) {
        console.error(e);
        alert("Error saving config");
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

// --- Settings 2 Sidebar Logic ---
let currentSettingsFile = 'studies.toml';

async function openSettingsFile(filename) {
    currentSettingsFile = filename;
    document.getElementById('settings-file-title').innerText = filename;

    // Highlight active button
    const buttons = document.querySelectorAll('#settings_tab_v2 .settings-menu-btn');
    buttons.forEach(btn => {
        if (btn.innerText.trim() === filename) {
            btn.style.background = '#37373d';
            btn.style.fontWeight = 'bold';
        } else {
            btn.style.background = 'none';
            btn.style.fontWeight = 'normal';
        }
    });

    await loadConfig(filename, 'settings-editor-v2');
}

async function saveCurrentSettings() {
    await saveConfig(currentSettingsFile, 'settings-editor-v2');
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
    if (tabName !== 'video_viewer') {
        if (typeof pauseViewerVideo === 'function') pauseViewerVideo();
    } else {
        if (typeof playViewerVideo === 'function') playViewerVideo();
    }

    // Force reload of config when settings tab is opened
    if (tabName === 'settings') {
        loadConfig('studies.toml');
        loadConfig('config.toml');
    }

    if (tabName === 'settings_tab_v2') {
        openSettingsFile('studies.toml');
    }
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
            container.innerHTML = `<p style="color: #ff6b6b;">Error: ${files.error}</p>`;
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
                   <strong style="color: #d4d4d4;">${label}:</strong> <span style="color: #aaa;">${files[category]}</span>
               </li>`;
            }
        });
        html += '</ul>';
        container.innerHTML = html;

    } catch (e) {
        console.error(e);
        container.innerHTML = `<p style="color: #ff6b6b;">Failed to load files.</p>`;
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
            container.innerHTML = `<p style="color: #ff6b6b;">Error: ${data.error}</p>`;
            return;
        }

        if (data.length === 0) {
            container.innerHTML = '<p>No datasets found for this study.</p>';
            return;
        }

        let html = '<table style="width: 100%; text-align: left; border-collapse: collapse; margin-top: 10px;">';
        html += '<tr style="border-bottom: 1px solid #555;">';
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
                html += `<tr><td>${file.filename}</td><td colspan="3" style="color: #ff6b6b;">${file.error}</td></tr>`;
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
        container.innerHTML = `<p style="color: #ff6b6b;">Failed to check datasets.</p>`;
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
    display.style.color = '#aaa';

    try {
        const res = await fetch(`/api/check_video_counts/${studyName}`);
        const data = await res.json();

        if (data.error) {
            display.innerHTML = `Error: ${data.error}`;
            display.style.color = '#ff6b6b';
            return;
        }

        // data = { "annotate": [rows, cols], "scrape": [rows, cols] }
        // Tuple usually comes as array in JSON: [rows, cols]

        const scrapeCount = data.scrape ? data.scrape[0] : 0;
        const annotateCount = data.annotate ? data.annotate[0] : 0;

        display.innerHTML = `Videos to scrape: <b>${scrapeCount.toLocaleString()}</b> | Videos to annotate: <b>${annotateCount.toLocaleString()}</b>`;
        display.style.color = '#d4d4d4';

    } catch (e) {
        console.error(e);
        display.innerHTML = `Failed to check counts.`;
        display.style.color = '#ff6b6b';
    }
}
