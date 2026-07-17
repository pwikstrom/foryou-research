// Admin → System Information sub-page: environment facts + system health.
// Loaded lazily by admin_tab.js on first open of the sub-page.

function loadSystemInfo() {
    fetch('/api/system-info')
        .then(r => r.json())
        .then(info => {
            const rows = [
                ['Environment', info.environment],
                info.revision ? ['Revision', info.revision] : null,
                ['OS', info.os],
                ['Architecture', info.architecture],
                ['Python', info.python_version],
                ['CPUs', info.cpu_count],
                ['Data storage', info.data_location],
                ['Media storage', info.media_location],
                ['Cache storage', info.cache_location],
            ].filter(Boolean);

            const html = rows.map(([label, value]) =>
                `<div style="display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid var(--color-border);">
                    <span class="text-sm font-medium" style="color: var(--color-text-secondary);">${label}</span>
                    <span class="text-sm" style="color: var(--color-text-primary);">${value}</span>
                </div>`
            ).join('');

            document.getElementById('system-info-container').innerHTML = html;
        })
        .catch(() => {
            document.getElementById('system-info-container').innerHTML =
                '<span class="text-sm" style="color: var(--color-text-muted);">Unable to load system info.</span>';
        });
}


// ---------------------------------------------------------------------------
// System health panel
// ---------------------------------------------------------------------------

const _HEALTH_STATUS_META = {
    ok:        { cls: 'ok',      label: 'OK' },
    warn:      { cls: 'warn',    label: 'Warning' },
    fail:      { cls: 'bad',     label: 'Failing' },
    running:   { cls: 'unknown', label: 'Checking…' },
    skipped:   { cls: 'unknown', label: 'Skipped' },
    never_run: { cls: 'unknown', label: 'Never run' },
};

const _HEALTH_CHECK_LABELS = {
    scrape_tiktok:    'TikTok scraper',
    scrape_instagram: 'Instagram scraper',
    scrape_youtube:   'YouTube scraper',
    // Historical key — the check now covers whichever annotation backend is
    // active (Gemini ping, or the local backend's requirement checks).
    gemini:           'Annotation backend',
};

let _healthPollTimer = null;


function _healthPill(status, tooltip, labelPrefix) {
    const meta = _HEALTH_STATUS_META[status] || _HEALTH_STATUS_META.never_run;
    const label = (labelPrefix ? labelPrefix + ': ' : '') + meta.label;
    const tip = tooltip ? ` data-tooltip="${_escapeAttr(tooltip)}"` : '';
    // Right-anchored: the pills sit at the panel's right edge, so the bubble
    // must extend leftward to stay on screen.
    const cls = tooltip ? 'meta-tooltip tooltip-right-anchored' : '';
    return `<span class="cookie-pill cookie-pill--${meta.cls} ${cls}"${tip}>${label}</span>`;
}


function _escapeAttr(text) {
    return String(text).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}


function _relativeTime(iso) {
    if (!iso) return '';
    const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
    if (!isFinite(seconds) || seconds < 0) return '';
    if (seconds < 90) return 'just now';
    if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
    if (seconds < 129600) return `${Math.round(seconds / 3600)}h ago`;
    return `${Math.round(seconds / 86400)}d ago`;
}


function _renderHealthCheckRow(key, check) {
    const label = _HEALTH_CHECK_LABELS[key] || key;
    const detailParts = [];
    if (check.detail) detailParts.push(check.detail);
    if (check.item_id) detailParts.push(`Test item: ${check.item_id}`);
    // Scraper rows: the main pill is the metadata check (test scrape +
    // fill-profile drift) — label it like the Cookie/Media sub-pills.
    const pillPrefix = key.startsWith('scrape_') ? 'Metadata' : 'API';
    const pill = _healthPill(check.status, detailParts.join(' • '), pillPrefix);

    const subPills = [];
    if (check.cookie) {
        const c = check.cookie;
        const cls = { healthy: 'ok', expiring_soon: 'warn', stale: 'warn',
                      expired: 'bad', missing: 'bad' }[c.status] || 'unknown';
        subPills.push(`<span class="cookie-pill cookie-pill--${cls} meta-tooltip tooltip-right-anchored"
            data-tooltip="${_escapeAttr(c.message || 'No cookie detail available')}">Cookie: ${c.status || 'unknown'}</span>`);
    }
    if (check.media && check.media.status !== 'skipped') {
        subPills.push(_healthPill(check.media.status,
            [check.media.message, check.media.detail].filter(Boolean).join(' • '), 'Media'));
    }

    const timing = [];
    if (typeof check.duration_s === 'number') timing.push(`${check.duration_s.toFixed(1)}s`);
    const checkedAt = _relativeTime(check.checked_at);
    if (checkedAt) timing.push(checkedAt);

    return `<div style="display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--color-border);">
        <div style="min-width: 0;">
            <div class="text-sm font-medium" style="color: var(--color-text-secondary);">${label}</div>
            <div class="text-xs" style="color: var(--color-text-muted); overflow: hidden; text-overflow: ellipsis;">${check.message || ''}</div>
        </div>
        <div style="display: flex; align-items: center; gap: 6px; flex: 0 0 auto;">
            ${subPills.join('')}
            ${pill}
            <span class="text-xxs" style="color: var(--color-text-muted); min-width: 70px; text-align: right;">${timing.join(' · ')}</span>
        </div>
    </div>`;
}


// Banner rows for active scraper alerts (raised by the scrape worker when a
// platform's scraper looks systematically broken — e.g. a permanent-failure
// storm). Dismissal lives on the Data Management → enrichment card; here the
// alert is informational.
function _renderScraperAlertBanners(alerts) {
    return Object.entries(alerts || {}).map(([platform, alert]) => {
        const raised = _relativeTime(alert.raised_at);
        const seen = alert.occurrences > 1
            ? ` — seen ${alert.occurrences}× since ${raised || '?'}`
            : (raised ? ` — raised ${raised}` : '');
        const text = `${platform} scraper needs attention${seen}: ${alert.message || alert.kind}`;
        return `<div class="scraper-alert"><span class="scraper-alert-text">⚠ ${_escapeAttr(text)}</span></div>`;
    }).join('');
}


function _renderHealth(doc) {
    const container = document.getElementById('system-health-container');
    if (!container) return;

    const alertBanners = _renderScraperAlertBanners(doc.scraper_alerts);

    const overallPill = _healthPill(doc.overall, doc.interrupted ? 'Previous run was interrupted' : '');
    const lastRun = _relativeTime(doc.finished_at);
    const triggerNote = doc.trigger ? ` (${doc.trigger} run${lastRun ? ', ' + lastRun : ''})` : '';

    const header = `<div style="display: flex; justify-content: space-between; align-items: center; padding: 0 0 8px 0;">
        <span class="text-sm font-semibold" style="color: var(--color-text-primary);">Overall</span>
        <span style="display: flex; align-items: center; gap: 8px;">
            ${overallPill}
            <span class="text-xs" style="color: var(--color-text-muted);">${doc.overall === 'never_run' ? 'No health check has run yet' : triggerNote}</span>
        </span>
    </div>`;

    const rows = Object.entries(doc.checks || {})
        .map(([key, check]) => _renderHealthCheckRow(key, check)).join('');

    container.innerHTML = alertBanners + header + rows;

    const btn = document.getElementById('run-health-check-btn');
    if (doc.overall === 'running') {
        if (btn) btn.disabled = true;
        clearTimeout(_healthPollTimer);
        _healthPollTimer = setTimeout(loadSystemHealth, 3000);
    } else if (btn) {
        btn.disabled = false;
    }
}


function loadSystemHealth() {
    fetch('/api/system-health')
        .then(r => r.json())
        .then(_renderHealth)
        .catch(() => {
            const container = document.getElementById('system-health-container');
            if (container) container.innerHTML =
                '<span class="text-sm" style="color: var(--color-text-muted);">Unable to load system health.</span>';
        });
}


function runSystemHealthCheck() {
    const btn = document.getElementById('run-health-check-btn');
    if (btn) btn.disabled = true;
    const csrfToken = document.querySelector('meta[name="csrf-token"]').getAttribute('content');
    fetch('/api/system-health/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
    })
        .then(() => loadSystemHealth())  // 409 (already running) just resumes polling
        .catch(() => { if (btn) btn.disabled = false; });
}
