// Poll intervals
setInterval(updateStatus, 2000);
setInterval(updateLogs, 1000);

// Initial load
window.onload = function () {
    loadConfig('studies.toml');
    loadConfig('config.toml');
    updateStatus();
};

async function startProcess(name) {
    let body = {};
    // Determine context (tab) for study name input
    let studyNameInputId = 'global-study-name'; // default for scrape/annotate
    if (name === 'create_subsets') {
        studyNameInputId = 'overview-study-name';
    } else if (['create_event_log', 'recode_event_log', 'calculate_pca'].includes(name)) {
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
        setDiscreetStatus('regenerate_datasets', data.regenerate_datasets);
        setDiscreetStatus('create_event_log', data.create_event_log);
        setDiscreetStatus('recode_event_log', data.recode_event_log);
        setDiscreetStatus('calculate_pca', data.calculate_pca);
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
            text.innerText = `${info.done.toLocaleString()} / ${info.total.toLocaleString()} (${pct.toFixed(1)}%) - ${info.rate.toFixed(2)}/s - ETA ${etaStr}`;
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
        if (data.start_time) {
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
        if (data.last_success) {
            const successTime = new Date(data.last_success);
            text.innerText = "Last success: " + successTime.toLocaleString();
        } else {
            text.innerText = "Last success: Never"; // Or empty
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

async function loadConfig(filename) {
    try {
        const res = await fetch(`/api/config?file=${filename}`);
        const data = await res.json();

        const targetId = filename === 'studies.toml' ? 'config-editor-studies' : 'config-editor-core';
        document.getElementById(targetId).value = data.content;
    } catch (e) {
        console.error(e);
    }
}

async function saveConfig(filename) {
    const targetId = filename === 'studies.toml' ? 'config-editor-studies' : 'config-editor-core';
    const content = document.getElementById(targetId).value;
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
        await fetch(`/api/logs/clear/${name}`, { method: 'POST' });
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
}
