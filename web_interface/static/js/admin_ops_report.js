// Admin → System → Daily Ops Report pane.
// The report itself is a self-contained HTML page stored by the ops_report
// worker; the pane shows it in a sandboxed iframe and offers a manual run.

let _opsReportLoaded = false;

async function loadOpsReport(force = false) {
    if (_opsReportLoaded && !force) return;
    const metaEl = document.getElementById('ops-report-meta');
    const frame = document.getElementById('ops-report-frame');
    if (!metaEl || !frame) return;
    try {
        const resp = await fetch('/api/admin/ops-report');
        const meta = await resp.json();
        if (!meta.available) {
            metaEl.textContent = 'No report has been generated yet — use "Generate now" to run the first one.';
            return;
        }
        const counts = meta.counts || {};
        metaEl.textContent =
            `Generated ${meta.generated_at_local || meta.generated_at} — overall ${meta.overall}` +
            ` (${counts.red || 0} red, ${counts.yellow || 0} yellow, ` +
            `${counts.blue || 0} info, ${counts.green || 0} ok)` +
            (meta.narrative_source === 'fallback' ? ' — assessment written without Gemini' : '');
        // Cache-bust so a fresh run replaces the iframe content.
        frame.src = '/api/admin/ops-report/html?ts=' + encodeURIComponent(meta.generated_at || Date.now());
        _opsReportLoaded = true;
    } catch (err) {
        metaEl.textContent = 'Could not load the ops report: ' + err;
    }
}

async function runOpsReportNow() {
    const btn = document.getElementById('ops-report-run-btn');
    const metaEl = document.getElementById('ops-report-meta');
    if (btn) btn.disabled = true;
    if (metaEl) metaEl.textContent = 'Generating a fresh report on the task runner (this takes a minute or two)...';
    try {
        const resp = await fetch('/api/admin/ops-report/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').getAttribute('content') },
        });
        const data = await resp.json();
        if (!data.started) {
            if (metaEl) metaEl.textContent = 'Could not start the report: ' + (data.message || 'already running?');
            if (btn) btn.disabled = false;
            return;
        }
        // Poll the task status until it finishes, then reload the report.
        const poll = async () => {
            try {
                const s = await (await fetch('/api/status')).json();
                const st = (s.ops_report && s.ops_report.status) || '';
                if (st === 'running' || st === 'pending') {
                    setTimeout(poll, 5000);
                    return;
                }
            } catch (e) { /* fall through to reload */ }
            if (btn) btn.disabled = false;
            loadOpsReport(true);
        };
        setTimeout(poll, 8000);
    } catch (err) {
        if (metaEl) metaEl.textContent = 'Could not start the report: ' + err;
        if (btn) btn.disabled = false;
    }
}
