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
    const isTest = document.getElementById('global-test-mode') ? document.getElementById('global-test-mode').checked : false;
    const studyName = document.getElementById('global-study-name').value;

    if (name === 'downloader') {
        if (!studyName) {
            alert("Please enter a study name in the Global Controls");
            return;
        }
        body = { study_name: studyName, testing: isTest };
    } else if (name === 'annotator') {
        if (!studyName) {
            alert("Please enter a study name in the Global Controls");
            return;
        }
        body = { study_name: studyName, testing: isTest };
    } else {
        body = { testing: isTest };
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

async function updateStatus() {
    try {
        const res = await fetch('/api/status');
        const data = await res.json();

        setStatus('downloader', data.downloader);
        setStatus('monitor', data.monitor);
        setStatus('annotator', data.annotator);
    } catch (e) {
        console.error(e);
    }
}

function setStatus(name, data) {
    const status = data.state;
    const info = data.progress || {};

    const el = document.getElementById(`${name}-status`);
    el.className = `status-indicator status-${status}`;

    const bar = document.getElementById(`${name}-bar`);
    const text = document.getElementById(`${name}-text`);
    if (bar && text) {
        if (Object.keys(info).length > 0 && info.total > 0) {
            const pct = (info.done / info.total) * 100;
            bar.style.width = `${pct}%`;
            text.innerText = `${info.done.toLocaleString()} / ${info.total.toLocaleString()} (${pct.toFixed(1)}%) - ${info.rate.toFixed(2)}/s - ETA ${info.eta}`;
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
}

async function updateLogs() {
    await fetchLogs('downloader');
    await fetchLogs('monitor');
    await fetchLogs('annotator');
}

async function fetchLogs(name) {
    try {
        const res = await fetch(`/api/logs/${name}`);
        const data = await res.json();
        const el = document.getElementById(`${name}-logs`);

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

function clearLogs(name) {
    const el = document.getElementById(`${name}-logs`);
    if (el) {
        el.textContent = "";
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
