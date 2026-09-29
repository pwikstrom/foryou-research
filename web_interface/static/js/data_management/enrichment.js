// Data Pipeline: Enrichment stats, the cascade refresh, per-platform scraper chips and alerts, and
// the consolidation pipeline chart.
// One of the js/data_management/*.js files; they share one global scope and
// load in a fixed order (templates/index.html).

// --- Enrichment Stats & Logic ---

function renderConsolidateStatus(stats) {
    const statusEl = document.getElementById('consolidate-status');
    if (!statusEl || !stats) return;
    const lines = [];
    if (stats.last_consolidation) {
        // The when/how-long/outcome of the run is the card's own last-run line
        // now, so this one carries only what is unique to it: what came in.
        lines.push(`Last run: ${stats.new_scrape_files ?? 0} new scrape file(s) and ${stats.new_annotation_files ?? 0} new annotation file(s).`);
    }
    // No "Last enrichment status refresh" line: the worker stamps
    // last_status_refresh and finishes within the same few seconds, so it only
    // ever restated the card's own "Last: <date> OK" line above.
    // Persistent pipeline outcome — written by the orchestrator at the end
    // of a consolidate+refresh run (or by the consolidate worker itself when
    // no downstream refresh was needed). Shown in success green so the user
    // has an explicit statement of what happened.
    if (stats.last_pipeline_summary) {
        const esc = escapeHtml(stats.last_pipeline_summary);
        // A partial/aborted pipeline is surfaced as an amber warning, not a
        // green ✓ — otherwise an aborted refresh reads as a success.
        const partial = !!stats.last_pipeline_partial;
        const icon = partial ? '⚠' : '✓';
        const color = partial ? 'var(--color-warning)' : 'var(--color-success-light)';
        lines.push(`<span style="color: ${color}; font-weight: var(--weight-medium);">${icon} ${esc}</span>`);
    }
    if (lines.length) {
        statusEl.innerHTML = lines.join('<br>');
        statusEl.style.color = 'var(--color-success-light)';
    }
}

function checkConsolidationNeeded(data) {
    const warningEl = document.getElementById('consolidate-warning');
    if (!warningEl) return;

    const consolidateBtn = document.getElementById('btn-consolidate');
    const setNeedsAction = (needs) => {
        // Mirror the state on the "Dataset Assembly" sidebar item, so the
        // stale signal is visible without opening the page.
        const staleDot = document.getElementById('refresh-caches-stale-dot');
        if (staleDot) staleDot.style.display = needs ? 'inline-block' : 'none';
        if (!consolidateBtn) return;
        if (needs) {
            consolidateBtn.classList.add('btn-has-pending');
        } else {
            consolidateBtn.classList.remove('btn-has-pending');
        }
    };

    // Suppress the "scraper/annotator completed after last consolidation"
    // warning whenever the consolidate pipeline is actively running (or the
    // local-dev poll loop is active) — the pipeline IS the response to that
    // condition, so showing the warning during it is misleading.
    if (data.consolidate_pipeline_active || _consolidatePollActive) {
        warningEl.style.display = 'none';
        setNeedsAction(false);
        return;
    }

    const lastConsolidation = data.consolidate_stats?.last_consolidation;
    const scraperSuccess = data.scraper_last_success;
    const annotatorSuccess = data.annotator_last_success;

    if (!lastConsolidation) {
        // Never consolidated — warn if any process has run
        if (scraperSuccess || annotatorSuccess) {
            warningEl.textContent = 'New enrichment data has not been consolidated yet. Click "Consolidate" to update.';
            warningEl.style.display = '';
            setNeedsAction(true);
        } else {
            setNeedsAction(false);
        }
        return;
    }

    // Parsed through the shared helper so a zone-less legacy value is read as
    // UTC rather than as the viewer's local time, which would skew the compare.
    const consolTs = fypParseInstant(lastConsolidation)?.getTime();
    const isNewer = (ts) => {
        const d = fypParseInstant(ts);
        return !!(d && consolTs != null && d.getTime() > consolTs);
    };
    const scraperNewer = isNewer(scraperSuccess);
    const annotatorNewer = isNewer(annotatorSuccess);

    if (scraperNewer || annotatorNewer) {
        const parts = [];
        if (scraperNewer) parts.push('scraper');
        if (annotatorNewer) parts.push('annotator');
        // When the enrichment loop started that worker, the consolidation is
        // the loop's own next step — say so, instead of asking for a click
        // the loop is about to make itself.
        const owes = data.loop_owes || {};
        if (owes.settle) {
            warningEl.textContent = `The ${parts.join(' and ')} finished after the last consolidation. `
                + 'Automatic enrichment started that run and will consolidate the results itself in its '
                + 'next cycle (within the hour at the latest) — press "Consolidate" only if you would rather not wait.';
        } else {
            warningEl.textContent = `The ${parts.join(' and ')} completed after the last consolidation. Click "Consolidate" to incorporate new data.`;
        }
        warningEl.style.display = '';
        setNeedsAction(true);
    } else {
        warningEl.style.display = 'none';
        setNeedsAction(false);
    }
}

// --- Cascade Refresh State ---
// Tracks the active cascade refresh so that:
//   1. main.js can chain meta refreshes after study refresh completes
//   2. Dataset Assembly page buttons are disabled while a cascade is running
let _cascadeRefresh = null;

function renderConsolidationImpact(impact, partial = null) {
    const panel = document.getElementById('consolidate-impact');
    const details = document.getElementById('impact-details');
    const actions = document.getElementById('impact-actions');
    const note = document.getElementById('impact-partial-note');
    if (!panel || !details || !actions) return;

    if (!impact || !impact.changed_item_count) {
        panel.style.display = 'none';
        return;
    }

    // When the last auto-refresh aborted partway, explain why the impact is
    // still here so the panel doesn't read as "nothing happened".
    if (note) {
        const owes = (_lastEnrichmentStats && _lastEnrichmentStats.loop_owes) || {};
        if (partial && partial.partial) {
            const where = partial.failedAt
                ? ` at "${escapeHtml(_humanizePipelineSteps(partial.failedAt))}"` : '';
            note.textContent = `⚠ The auto-refresh stopped${where}; the items below were not fully refreshed. `
                + `Click "Refresh All Affected" to complete.`;
            note.style.display = '';
        } else if (owes.refresh) {
            // The loop's own consolidations deferred this refresh; it runs
            // it itself once the loop goes quiet, so the button is a
            // shortcut, not a chore.
            note.textContent = 'Automatic enrichment has postponed this refresh until its plan finishes and '
                + 'will run it itself (within the hour at the latest). Use "Refresh All Affected" only if '
                + 'you want the analyses updated sooner.';
            note.style.display = '';
        } else {
            note.style.display = 'none';
        }
    }

    const parts = [];
    if (impact.new_scrape_item_count) parts.push(`${impact.new_scrape_item_count.toLocaleString()} newly scraped`);
    if (impact.new_annotation_item_count) parts.push(`${impact.new_annotation_item_count.toLocaleString()} newly annotated`);
    const itemSummary = parts.length ? parts.join(', ') : `${impact.changed_item_count.toLocaleString()} changed`;

    const collCount = impact.affected_collection_ids ? impact.affected_collection_ids.length : 0;
    const studyNames = impact.affected_study_names || [];

    let html = `${itemSummary} item(s) across <strong>${collCount}</strong> collection(s)`;
    if (studyNames.length) {
        html += ` in <strong>${studyNames.length}</strong> study/studies: ${studyNames.join(', ')}`;
    }
    details.innerHTML = html;

    // Single cascade button
    actions.innerHTML = '';
    const btn = document.createElement('button');
    btn.className = 'action-btn text-xs';
    btn.id = 'btn-cascade-refresh';
    btn.style.padding = '4px 8px';
    btn.textContent = 'Refresh All Affected';
    btn.onclick = () => startCascadeRefresh(impact, btn);
    // Disable if cascade is already running
    if (_cascadeRefresh) {
        btn.disabled = true;
        btn.textContent = _cascadeRefresh.statusText || 'Refreshing...';
        btn.className = 'btn-running text-xs';
    } else {
        btn.classList.add('btn-has-pending');
    }
    actions.appendChild(btn);

    panel.style.display = '';
}

function startCascadeRefresh(impact, btn) {
    // Start the same refresh run a consolidation with auto-refresh would, against
    // the stored impact. The backend plans it, prunes what has not changed and
    // dispatches the first step; we then poll the shared run status, so the
    // chart narrates it exactly as it narrates a consolidation's own run.
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Starting…';
        btn.className = 'btn-running text-xs';
    }
    fetch('/api/manage/enrichment/refresh-downstream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
    })
        .then(res => res.json())
        .then(resp => {
            if (resp.status === 'started') {
                // Hide the impact card while the pipeline runs; the step list +
                // status line take over. It only re-appears if the run was partial.
                renderConsolidationImpact(null);
                pollConsolidationStatus();
            } else {
                // Answer where the click happened. This used to write only to
                // the Consolidate card's status line, which is a different part
                // of the page — so a "nothing to refresh" verdict was invisible
                // and the button read as dead. It is a legitimate verdict: the
                // supervisor's deferred refresh may already have done this work.
                const noop = resp.status === 'noop';
                const msg = resp.message || 'Could not start refresh.';
                const actionsEl = document.getElementById('impact-actions');
                if (actionsEl) {
                    let line = document.getElementById('impact-action-note');
                    if (!line) {
                        line = document.createElement('span');
                        line.id = 'impact-action-note';
                        line.className = 'text-xxs';
                        line.style.marginLeft = '8px';
                        actionsEl.appendChild(line);
                    }
                    line.textContent = msg;
                    line.style.color = noop
                        ? 'var(--color-text-secondary)' : 'var(--color-danger)';
                }
                const statusEl = document.getElementById('consolidate-status');
                if (statusEl) {
                    statusEl.textContent = msg;
                    statusEl.style.color = noop
                        ? 'var(--color-text-secondary)' : 'var(--color-danger)';
                }
                if (btn) { btn.disabled = false; btn.textContent = 'Refresh All Affected'; btn.className = 'action-btn text-xs'; }
                // Nothing to refresh means this panel is showing work that has
                // already been done — usually by the supervisor, minutes after
                // the consolidation, with no browser poll running to notice.
                // Re-read so the panel and the badges correct themselves.
                if (noop) {
                    if (typeof fetchEnrichmentStats === 'function') fetchEnrichmentStats();
                    if (typeof fetchStalenessStatus === 'function') fetchStalenessStatus();
                }
            }
        })
        .catch(err => {
            console.error('Failed to start downstream refresh:', err);
            if (btn) { btn.disabled = false; btn.textContent = 'Refresh All Affected'; btn.className = 'action-btn text-xs'; }
        });
}

function onCascadeStudiesComplete() {
    if (!_cascadeRefresh || _cascadeRefresh.phase !== 'waiting_for_studies') return;
    _cascadeRefresh.phase = 'waiting_for_meta';
    _cascadeRefresh.statusText = 'Refreshing metadata...';
    updateCascadeButton();
    startMetaRefreshes();
}

function startMetaRefreshes() {
    const studyFilter = _cascadeRefresh.studyNames.length
        ? { studies: _cascadeRefresh.studyNames.join(',') } : {};
    const promises = [];
    promises.push(
        startTargetedRefresh('meta_refresh_groups', {})
            .then(() => { _cascadeRefresh.startedMetaGroups = true; })
    );
    promises.push(
        startTargetedRefresh('pca_refresh', studyFilter)
            .then(() => { _cascadeRefresh.startedPca = true; })
    );
    Promise.allSettled(promises).then(() => {
        // Now waiting for meta + PCA processes to finish — detected by main.js
    });
}

function onCascadeRefreshComplete() {
    // Called when all cascade processes have finished
    _cascadeRefresh = null;
    updateCascadeButton();
    updateCascadeRefreshPageLock(false);
    // Call staleness FIRST so the backend clears `consolidation_impact` from
    // process_stats when all downstream is fresh. Only then refresh enrichment
    // stats — otherwise the stats endpoint still returns the stale impact and
    // the panel re-appears.
    const stalePromise = (typeof fetchStalenessStatus === 'function')
        ? fetchStalenessStatus()
        : Promise.resolve();
    Promise.resolve(stalePromise).finally(() => fetchEnrichmentStats());
}

function updateCascadeButton() {
    const btn = document.getElementById('btn-cascade-refresh');
    if (!btn) return;
    if (_cascadeRefresh) {
        btn.disabled = true;
        btn.textContent = _cascadeRefresh.statusText || 'Refreshing...';
        btn.className = 'btn-running text-xs';
    } else {
        btn.disabled = false;
        btn.textContent = 'Refresh All Affected';
        btn.className = 'action-btn text-xs';
    }
}

function updateCascadeRefreshPageLock(locked) {
    // Disable/enable the toggle buttons on the Dataset Assembly page
    const processNames = ['recode_refresh_studies', 'meta_refresh_groups', 'timelines_refresh', 'pca_refresh'];
    processNames.forEach(name => {
        const toggleBtn = document.getElementById(`${name}-toggle`);
        if (toggleBtn) {
            toggleBtn.disabled = locked;
            if (locked) {
                toggleBtn.title = 'Cascade refresh in progress';
            } else {
                toggleBtn.title = '';
            }
        }
    });
}

function startTargetedRefresh(processName, params) {
    return fetch(`/api/start/${processName}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params)
    })
        .then(res => res.json())
        .then(data => {
            if (data.status !== 'success') {
                console.error(`Failed to start ${processName}: ${data.message}`);
            }
            return data;
        })
        .catch(err => {
            console.error(`Failed to start ${processName}:`, err);
        });
}

// Map a system-health chip status → { label, cls } for the per-card pill. The
// class selects a semantic-token color in style.css (no hardcoded colors here).
const _HEALTH_CHIP_META = {
    ok: { label: 'OK', cls: 'ok' },
    warn: { label: 'Warning', cls: 'warn' },
    fail: { label: 'Failing', cls: 'bad' },
    unknown: { label: 'Unknown', cls: 'unknown' },
};

// Compact relative time for a chip tooltip ("just now", "5m ago", "3h ago").
function _healthRelativeTime(iso) {
    return fypFmtRelative(iso);
}

// Paint one health pill from a { status, summary, checked_at } entry. The
// summary carries the combined scrape/media/cookie (or Gemini) detail; the
// tooltip appends when the underlying check last ran.
function _renderHealthChip(el, prefix, entry) {
    if (!el || !entry) return;
    const meta = _HEALTH_CHIP_META[entry.status] || _HEALTH_CHIP_META.unknown;
    el.className = `cookie-pill cookie-pill--${meta.cls} meta-tooltip`;
    el.textContent = `${prefix}: ${meta.label}`;

    const parts = [];
    if (entry.summary) parts.push(entry.summary);
    const rel = _healthRelativeTime(entry.checked_at);
    parts.push(rel ? `Checked ${rel}` : 'Health check has not run yet');
    el.setAttribute('data-tooltip', parts.join(' • '));
}

// Render the per-platform scraper chips and the annotation chip from the
// enrichment-stats payload's derived card_health (see system_health.derive_card_health).
function renderCardHealth(cardHealth) {
    if (!cardHealth) return;
    for (const [platform, entry] of Object.entries(cardHealth.platforms || {})) {
        _renderHealthChip(document.getElementById('cookie-health-' + platform), 'Scraper', entry);
    }
    // Label follows the active backend (renderAnnotationConfigNotice keeps
    // window._annotationBackend current from the same stats payload).
    const backendLabel = (window._annotationBackend && window._annotationBackend !== 'gemini')
        ? window._annotationBackend : 'Gemini';
    _renderHealthChip(document.getElementById('annotation-health'), backendLabel, cardHealth.annotation);
}

// Show/hide the per-platform scraper alert banners from the enrichment-stats
// payload's scraper_alerts (raised by the worker on a systematic failure such
// as a permanent-failure storm; cleared on the next healthy batch or by the
// Dismiss button).
function renderScraperAlerts(alerts) {
    document.querySelectorAll('[id^="scraper-alert-"]').forEach(banner => {
        const platform = banner.id.replace('scraper-alert-', '');
        const alert = (alerts || {})[platform];
        if (!alert) {
            banner.style.display = 'none';
            return;
        }
        const raised = _healthRelativeTime(alert.raised_at);
        const seen = alert.occurrences > 1 ? ` (seen ${alert.occurrences}× since ${raised || '?'})`
                                           : (raised ? ` (${raised})` : '');
        banner.querySelector('.scraper-alert-text').textContent =
            `⚠ Scraper needs attention${seen}: ${alert.message || alert.kind}`;
        banner.style.display = 'flex';
    });
}

// Show the "Gemini not configured" notice from the enrichment-stats payload,
// so the state is visible on the card rather than only on a refused Start.
// Not dismissable: unlike a scraper alert it is a standing setup state, and it
// disappears by itself once Gemini is configured.
function renderAnnotationConfigNotice(stats) {
    // Backend badge: which engine the annotator card will run (Gemini vs a
    // local model, selected in Admin → Backends).
    const backend = (stats && stats.annotation_backend) || 'gemini';
    window._annotationBackend = backend;
    const badge = document.getElementById('annotation-backend-badge');
    if (badge) {
        badge.textContent = backend === 'gemini' ? 'Gemini' : backend;
        badge.style.display = 'inline-block';
    }

    const notice = document.getElementById('annotation-config-notice');
    if (!notice) return;
    if (!stats || stats.annotation_configured !== false) {
        notice.style.display = 'none';
        return;
    }
    const reason = stats.annotation_config_reason || 'Machine annotation is not configured.';
    const localAnchors = {
        qwen_local: 'docs/installation.md#enabling-local-qwen-annotation',
        minicpm_local: 'docs/installation.md#enabling-local-minicpm-annotation',
    };
    const docsAnchor = localAnchors[stats.annotation_backend]
        || 'docs/installation.md#enabling-gemini-later';
    notice.querySelector('.config-notice-text').textContent = `⚙ ${reason} See ${docsAnchor}`;
    notice.style.display = 'block';
}

function dismissScraperAlert(platform) {
    fetch('/api/manage/enrichment/scraper_alert/dismiss', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ platform: platform }),
    })
        .then(res => res.json())
        .then(() => fetchEnrichmentStats())
        .catch(err => console.error('Failed to dismiss scraper alert:', err));
}

function fetchEnrichmentStats() {
    // The Scrape / Annotation / Refresh sub-pages only render for users with
    // the matching 'tab.data_management.*' permission. Without any of them the
    // endpoint aborts 403 (an HTML page that breaks res.json()), so skip the
    // call entirely. Every DOM write below is null-guarded because the target
    // elements are spread across those three pages and any of them may be
    // absent for the current role.
    if (!document.getElementById('dm-page-scrape')
        && !document.getElementById('dm-page-annotation')
        && !document.getElementById('dm-page-refresh')) return;
    const setText = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    };
    fetch('/api/manage/enrichment/stats')
        .then(res => res.json())
        .then(data => {
            // Stats (scrape-page header + annotation-page header)
            setText('enrich_total_videos', (data.total_videos !== undefined) ? data.total_videos.toLocaleString() : '-');
            setText('enrich_scraped', (data.scraped_videos !== undefined) ? data.scraped_videos.toLocaleString() : '-');
            setText('enrich_annotated', (data.annotated_videos !== undefined) ? data.annotated_videos.toLocaleString() : '-');
            setText('annot_scraped', (data.scraped_videos !== undefined) ? data.scraped_videos.toLocaleString() : '-');
            setText('annot_annotated', (data.annotated_videos !== undefined) ? data.annotated_videos.toLocaleString() : '-');

            // Queues (per-platform scrape counters)
            if (data.scrape_queues) {
                for (const [platform, len] of Object.entries(data.scrape_queues)) {
                    const el = document.getElementById('enrich_scrape_targets_' + platform);
                    if (el) {
                        el.textContent = len.toLocaleString();
                        el.style.color = 'var(--color-success-light)';
                    }
                }
            }

            // Which armed plans will drain each queue, for the notes beside
            // the counters and for the Empty Queue confirm.
            _lastEnrichmentStats = data;
            renderArmedPlanNotes(data);

            // Per-card health pills (scrapers + annotation), combining the last
            // system-health check with the fresh cookie status. Also cached
            // globally so main.js's startProcess can warn before starting a
            // scraper/annotator whose health is degraded.
            if (data.card_health) {
                window._cardHealth = data.card_health;
                renderCardHealth(data.card_health);
            }
            renderScraperAlerts(data.scraper_alerts);
            renderAnnotationConfigNotice(data);
            const annotateTargets = document.getElementById('enrich_annotate_targets');
            if (annotateTargets && data.annotate_queue_len !== undefined) {
                annotateTargets.textContent = data.annotate_queue_len.toLocaleString();
                annotateTargets.style.color = 'var(--color-success-light)';
            }
            if (typeof updateAnnotateInflight === 'function') updateAnnotateInflight(data.annotate_claimed_len);

            // Consolidation status from process_stats (only when not actively polling a run)
            if (!_consolidatePollActive && data.consolidate_stats) {
                renderConsolidateStatus(data.consolidate_stats);
                // Suppress the impact panel while the consolidate pipeline is
                // running (auto-pipeline or manual cascade) — those flows are
                // already refreshing the same downstream caches the panel's
                // button would invoke, so showing it is misleading.
                const pipelineActive = !!data.consolidate_pipeline_active || !!_cascadeRefresh;
                renderConsolidationImpact(
                    pipelineActive ? null : data.consolidate_stats.consolidation_impact,
                    { partial: !!data.last_pipeline_partial, failedAt: data.last_pipeline_failed_at }
                );
            }

            // The staleness badges on the cards ("N new embeddings to map") are
            // computed from the consolidated data, so they only change when a
            // run does. Refresh them when this poll first sees a run finish —
            // otherwise the numbers a consolidation just produced sit stale
            // until the operator reloads the page, which is how they were
            // being read before.
            _noteRefreshRunTransition(data.refresh_run);

            // Persistent + live refresh-run chart (updates every tick).
            renderPipelineSteps(data.pipeline_steps, data.refresh_run, {
                armed: !!data.consolidate_auto_armed,
                autoRefresh: !!data.consolidate_auto_armed_auto_refresh,
            });

            // Button state (armed / workers-running / idle) and the card lock.
            applyConsolidateButtonState(data);
            applyRefreshCardState(data);

            // If a run is in flight (e.g. after a page reload mid-run), kick off
            // the poll so the chart shows live progress.
            if (!_consolidatePollActive && data.consolidate_pipeline_active) {
                pollConsolidationStatus();
            }

            // Auto-fire: flag is armed AND workers now idle → POST consolidate.
            // Race-safe because the server rejects a double-dispatch.
            if (data.consolidate_auto_armed
                && (data.workers_blocking_consolidate || []).length === 0
                && !_consolidatePollActive) {
                const autoRefresh = !!data.consolidate_auto_armed_auto_refresh;
                fetch('/api/manage/enrichment/consolidate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ auto_refresh: autoRefresh }),
                })
                    .then(res => res.json())
                    .then(resp => {
                        if (resp.status === 'started') {
                            pollConsolidationStatus();
                        }
                    })
                    .catch(err => console.error('Auto-fire consolidate failed:', err));
            }

            // Check if consolidation is needed
            checkConsolidationNeeded(data);
        })
        .catch(err => console.error("Error fetching enrichment stats:", err));
}

// All per-platform scrape-queue counter elements (one per scraper block).
function scrapeTargetEls() {
    return Array.from(document.querySelectorAll('[id^="enrich_scrape_targets_"]'));
}

// Ask the server what a queue build WOULD do, then confirm with the user.
// Returns true when the caller should proceed with the real request.
// A dry-run failure is not fatal — we fall through to the normal request
// rather than blocking the user on an estimate.
async function confirmQueueBuild(endpoint, payload, noun) {
    let est;
    try {
        const res = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...payload, dry_run: true })
        });
        est = await res.json();
    } catch (err) {
        console.error('Queue dry-run failed:', err);
        return true;
    }
    if (!est || est.status !== 'success' || est.would_queue === undefined) return true;

    if (est.would_queue === 0) {
        showAppAlert(`Nothing to queue — no ${noun} matched the current selection.`);
        return false;
    }

    const lines = [`Queue ${est.would_queue.toLocaleString()} ${noun}?`];
    if (est.cost_estimate && est.cost_estimate.est_cost_usd) {
        lines.push(`Estimated cost: ~$${est.cost_estimate.est_cost_usd.toLocaleString()} `
            + `(${est.cost_estimate.backend}).`);
    }
    if (est.capped) {
        lines.push(`${est.requested.toLocaleString()} matched, but a per-request cap `
            + `of ${est.cap.toLocaleString()} applies to your account.`);
    }
    return showAppConfirm(lines.join('\n'));
}

async function queueVideosForScraping(btnElement) {
    const studyName = document.getElementById('enrichment-study-select').value;
    const scrapeTargets = scrapeTargetEls();

    if (!studyName) {
        showAppAlert("Please select a target study from the dropdown first.");
        return;
    }

    const retryEl = document.getElementById('retry-failed-attempts');
    const retryFailed = !!(retryEl && retryEl.checked);
    const retryMediaEl = document.getElementById('retry-missing-media');
    const retryMissingMedia = !!(retryMediaEl && retryMediaEl.checked);

    const scrapePayload = {
        study_name: studyName,
        retry_failed: retryFailed,
        retry_missing_media: retryMissingMedia,
    };
    if (!(await confirmQueueBuild('/api/manage/enrichment/calculate_to_scrape',
        scrapePayload, 'video(s) for scraping'))) return;

    // UI Loading state
    const originalText = btnElement.textContent;
    btnElement.textContent = "Queueing...";
    btnElement.disabled = true;

    scrapeTargets.forEach(el => {
        el.textContent = "Calc...";
        el.style.color = 'var(--color-text-tertiary)';
    });

    fetch('/api/manage/enrichment/calculate_to_scrape', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(scrapePayload)
    })
        .then(res => res.json())
        .then(scrapeData => {
            btnElement.textContent = originalText;
            btnElement.disabled = false;

            // Update scrape display (per-platform queue lengths)
            if (scrapeData.status === 'success') {
                const byPlatform = scrapeData.videos_to_scrape_by_platform || {};
                scrapeTargets.forEach(el => {
                    const platform = el.id.slice('enrich_scrape_targets_'.length);
                    const len = byPlatform[platform];
                    el.textContent = (len !== undefined) ? len.toLocaleString() : '0';
                    el.style.color = 'var(--color-success-light)';
                });
            } else {
                scrapeTargets.forEach(el => {
                    el.textContent = "Error";
                    el.style.color = 'var(--color-danger)';
                });
                console.error("Scrape Error:", scrapeData.error);
            }

            // Refresh total stats
            fetchEnrichmentStats();
        })
        .catch(err => {
            btnElement.textContent = originalText;
            btnElement.disabled = false;
            scrapeTargets.forEach(el => {
                el.textContent = "Failed";
                el.style.color = 'var(--color-danger)';
            });
            console.error("Error queueing videos for scraping:", err);
            showAppAlert("Error queueing videos for scraping.");
        });
}

// Selected annotation-queue selection mode on the Annotation page.
function _annotationSelectionMode() {
    const checked = document.querySelector('input[name="annot-selection-mode"]:checked');
    return checked ? checked.value : 'study';
}

// Enable/disable the per-mode controls when the selection-mode radio changes.
function updateAnnotationModeControls() {
    const mode = _annotationSelectionMode();
    const versionSelect = document.getElementById('annot-version-select');
    const fromDate = document.getElementById('annot-from-date');
    const toDate = document.getElementById('annot-to-date');
    const retryLabel = document.getElementById('annot-retry-failed-label');
    if (versionSelect) versionSelect.disabled = mode !== 'version';
    if (fromDate) fromDate.disabled = mode !== 'timeframe';
    if (toDate) toDate.disabled = mode !== 'timeframe';
    // "Include previously failed attempts" only applies to the study mode —
    // the other modes select successfully-annotated videos by definition.
    if (retryLabel) retryLabel.style.display = (mode === 'study') ? 'flex' : 'none';

    // The selection modes sit under Advanced, so name the active one beside the
    // collapsed link — otherwise a re-annotation run is armed out of sight.
    const hint = document.getElementById('annot-mode-hint');
    if (hint) {
        const labels = { version: 'Annotated with version', timeframe: 'Annotated between' };
        hint.textContent = labels[mode] ? `Selecting: ${labels[mode]}` : '';
        hint.style.display = labels[mode] ? '' : 'none';
    }
}

// Populate the annotation-version dropdown from the enrichment-scoped version
// list (versions that actually occur in the annotation archive).
let _annotVersionsLoaded = false;
function loadAnnotationVersionOptions(force = false) {
    const select = document.getElementById('annot-version-select');
    if (!select || (_annotVersionsLoaded && !force)) return;
    fetch('/api/manage/enrichment/annotation_versions')
        .then(res => res.json())
        .then(data => {
            const versions = data.versions || [];
            const current = select.value;
            select.innerHTML = '<option value="">-- Select annotation version --</option>';
            versions.forEach(v => {
                const opt = document.createElement('option');
                opt.value = v.annotation_version;
                const label = v.label || v.model || v.annotation_version;
                const shortHash = String(v.annotation_version).slice(0, 11);
                opt.textContent = `${label} (${shortHash}…)${v.active ? ' — preferred' : ''}`;
                select.appendChild(opt);
            });
            if (current && versions.some(v => v.annotation_version === current)) {
                select.value = current;
            }
            _annotVersionsLoaded = true;
        })
        .catch(err => console.error("Error loading annotation versions:", err));
}

async function queueVideosForAnnotation(btnElement) {
    const mode = _annotationSelectionMode();
    const studySelect = document.getElementById('annotation-study-select');
    const studyName = studySelect ? studySelect.value : '';
    const annotateTargetsDisplay = document.getElementById('enrich_annotate_targets');
    const resultEl = document.getElementById('annot-queue-result');

    // Every selection mode operates within a target study.
    if (!studyName) {
        showAppAlert("Please select a target study from the dropdown first.");
        return;
    }

    const payload = { selection_mode: mode, study_name: studyName };

    if (mode === 'study') {
        const retryEl = document.getElementById('annot-retry-failed');
        payload.retry_failed = !!(retryEl && retryEl.checked);
    } else if (mode === 'version') {
        const versionSelect = document.getElementById('annot-version-select');
        payload.annotation_version = versionSelect ? versionSelect.value : '';
        if (!payload.annotation_version) {
            showAppAlert("Please select an annotation version first.");
            return;
        }
    } else if (mode === 'timeframe') {
        const fromEl = document.getElementById('annot-from-date');
        const toEl = document.getElementById('annot-to-date');
        payload.annotated_from = fromEl ? fromEl.value : '';
        payload.annotated_to = toEl ? toEl.value : '';
        if (!payload.annotated_from && !payload.annotated_to) {
            showAppAlert("Please set at least one of the timeframe dates.");
            return;
        }
    }

    if (!(await confirmQueueBuild('/api/manage/enrichment/calculate_to_annotate',
        payload, 'video(s) for annotation'))) return;

    // UI Loading state
    const originalText = btnElement.textContent;
    btnElement.textContent = "Queueing...";
    btnElement.disabled = true;
    if (resultEl) resultEl.style.display = 'none';

    if (annotateTargetsDisplay) {
        annotateTargetsDisplay.textContent = "Calc...";
        annotateTargetsDisplay.style.color = 'var(--color-text-tertiary)';
    }

    fetch('/api/manage/enrichment/calculate_to_annotate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    })
        .then(res => res.json())
        .then(annotateData => {
            btnElement.textContent = originalText;
            btnElement.disabled = false;

            if (annotateData.status === 'success') {
                if (annotateTargetsDisplay) {
                    annotateTargetsDisplay.textContent = annotateData.videos_to_annotate.toLocaleString();
                    annotateTargetsDisplay.style.color = 'var(--color-success-light)';
                }
                if (resultEl) {
                    const parts = [`Queued ${(annotateData.newly_queued ?? annotateData.videos_to_annotate).toLocaleString()} video(s)`];
                    if (annotateData.selected !== undefined) parts.push(`${annotateData.selected.toLocaleString()} matched the selection`);
                    if (annotateData.skipped_no_media) parts.push(`${annotateData.skipped_no_media.toLocaleString()} skipped (media not downloaded)`);
                    if (annotateData.skipped_no_inference_ts) parts.push(`${annotateData.skipped_no_inference_ts.toLocaleString()} skipped (no stored annotation timestamp — run the inference_ts backfill to include them)`);
                    if (annotateData.capped) parts.push(`capped at ${annotateData.cap.toLocaleString()} of ${annotateData.requested.toLocaleString()} matched`);
                    resultEl.textContent = parts.join(' · ');
                    resultEl.style.display = '';
                }
            } else {
                if (annotateTargetsDisplay) {
                    annotateTargetsDisplay.textContent = "Error";
                    annotateTargetsDisplay.style.color = 'var(--color-danger)';
                }
                console.error("Annotate Error:", annotateData.error);
                showAppAlert("Error queueing videos for annotation: " + (annotateData.error || 'unknown error'));
            }

            // Refresh total stats
            fetchEnrichmentStats();
        })
        .catch(err => {
            btnElement.textContent = originalText;
            btnElement.disabled = false;
            if (annotateTargetsDisplay) {
                annotateTargetsDisplay.textContent = "Failed";
                annotateTargetsDisplay.style.color = 'var(--color-danger)';
            }
            console.error("Error queueing videos for annotation:", err);
            showAppAlert("Error queueing videos for annotation.");
        });
}

async function emptyQueue(queueType, platform) {
    if (!queueType) return;

    // Name what is about to be dropped — and whose plan is about to lose its
    // current slice, since an armed plan's cursors have already moved past
    // the days it queued and it will not revisit them.
    const stats = _lastEnrichmentStats || {};
    const armedAll = Object.values(stats.armed_plans_by_platform || {}).flat();
    const armed = queueType === 'scrape'
        ? ((stats.armed_plans_by_platform || {})[platform] || []) : armedAll;
    const queued = queueType === 'scrape'
        ? ((stats.scrape_queues || {})[platform] || 0) : (stats.annotate_queue_len || 0);
    const own = queueType === 'scrape' ? ((stats.queue_plan_items_by_platform || {})[platform] || 0) : 0;
    const which = queueType === 'scrape' ? `${_dmPlatformLabel(platform)} scrape` : 'annotation';
    let msg = `Empty the ${which} queue? The ${queued.toLocaleString()} queued video(s) will be removed `
            + `and will not be ${queueType === 'scrape' ? 'scraped' : 'annotated'}.`;
    if (armed.length) {
        msg += `\n\nAutomatic enrichment is running for ${_armedPlanLabel(armed)} and uses this queue.`;
        if (own) {
            msg += ` ${own.toLocaleString()} of the queued videos are its current batch: if you remove them, `
                 + 'the plan treats those days as done and moves on without them.';
        }
    }
    const ok = await showAppConfirm(msg, { okLabel: 'Empty the queue', danger: true });
    if (!ok) return;

    fetch(`/api/manage/enrichment/empty_queue/${queueType}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(platform ? { platform: platform } : {})
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'success') {
                fetchEnrichmentStats();
            } else {
                showAppAlert("Error: " + data.error);
            }
        })
        .catch(err => console.error("Failed to empty queue: " + err));
}

// Tracks whether the consolidation pipeline is currently being polled so that
// periodic fetchEnrichmentStats refreshes don't start a second polling loop.
let _consolidatePollActive = false;

// The refresh pipeline's steps and labels, server-rendered from the registry
// (window.PIPELINE_REGISTRY) so this file cannot drift from the planner. The
// literals are the fallback for a cached page served before the injection.
const _PIPELINE_REGISTRY = window.PIPELINE_REGISTRY || {};
const _PIPELINE_STEPS = _PIPELINE_REGISTRY.order || [
    "consolidate_enrichment",
    "embeddings_refresh",
    "video_map_refresh",
    "recode_refresh_studies",
    "meta_refresh_groups",
    "pca_refresh",
    "timelines_refresh",
    "sessions_refresh",
];

// Short human labels, used to humanize a step name in prose (the chart's rows
// use the long labels the server puts on each row).
const _PIPELINE_STEP_LABELS = _PIPELINE_REGISTRY.short_labels || {
    consolidate_enrichment: "Consolidate enrichment data",
    embeddings_refresh: "Semantic embeddings",
    video_map_refresh: "Semantic map",
    recode_refresh_studies: "Study definitions",
    meta_refresh_groups: "Explore metadata",
    pca_refresh: "Correlations",
    timelines_refresh: "Timelines",
    sessions_refresh: "Sessions",
};

function _humanizePipelineSteps(csv) {
    // failed_at may be a single step name or a comma-separated list of leaf
    // names. Return a readable, comma-joined label list.
    if (!csv) return '';
    return String(csv).split(',')
        .map(s => _PIPELINE_STEP_LABELS[s.trim()] || s.trim())
        .filter(Boolean)
        .join(', ');
}

// The pipeline chart re-renders from this cache once a second while any step
// is live, so a running bar grows between polls instead of jumping.
let _pipelineStepsCache = null;
let _pipelineRunCache = null;
let _pipelineArmedCache = null;
let _pipelineTicker = null;

function _fmtDuration(seconds) {
    if (seconds == null || !isFinite(seconds)) return '';
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return `${s} s`;
    const m = Math.floor(s / 60), r = s % 60;
    if (m < 60) return r ? `${m} min ${r} s` : `${m} min`;
    return `${Math.floor(m / 60)} h ${m % 60} min`;
}

function _ganttTickStep(spanSeconds) {
    // The coarsest step that still gives the axis at least four ticks.
    for (const t of [15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]) {
        if (spanSeconds / t <= 6) return t;
    }
    return 14400;
}

function _startPipelineTicker() {
    if (_pipelineTicker) return;
    _pipelineTicker = setInterval(() => {
        if (_pipelineStepsCache) renderPipelineSteps(_pipelineStepsCache);
    }, 1000);
}

function _stopPipelineTicker() {
    if (!_pipelineTicker) return;
    clearInterval(_pipelineTicker);
    _pipelineTicker = null;
}

function _renderRunHeader(run, steps) {
    // "Started from Semantic Map by patrik at 14:02 — finished in 12 min:
    // nothing downstream needed refreshing." A run can start from any card now,
    // so the chart has to say which one, or a reader has no way to tell a
    // map rebuild's cascade from a consolidation's.
    const el = document.getElementById('pipeline-run-header');
    if (!el) return;
    if (!run || !run.origin) { el.style.display = 'none'; el.innerHTML = ''; return; }
    el.style.display = '';
    const started = run.started_ts ? Date.parse(run.started_ts) : NaN;
    const parts = [`Started from <strong>${escapeHtml(run.origin_label || run.origin)}</strong>`];
    if (run.started_by) parts.push(`by ${escapeHtml(run.started_by)}`);
    if (Number.isFinite(started)) parts.push(`at ${escapeHtml(fypFmtTime(started))}`);
    let tail = '';
    if (run.in_flight) {
        tail = ' — running';
    } else if (run.finished_ts && Number.isFinite(started)) {
        const secs = (Date.parse(run.finished_ts) - started) / 1000;
        tail = Number.isFinite(secs) ? ` — finished in ${escapeHtml(_fmtDuration(secs))}` : ' — finished';
    } else if (run.finished_ts) {
        tail = ' — finished';
    }
    let summary = '';
    if (!run.in_flight && run.summary) {
        summary = `<span class="${run.partial ? 'text-warning' : 'text-muted'}">`
            + escapeHtml(run.summary) + '</span>';
    }
    el.innerHTML = `<span class="text-xs">${parts.join(' ')}${tail}</span>`
        + (summary ? `<span class="text-xs"> ${summary}</span>` : '');
}

let _lastSeenRunSignature = null;

function _noteRefreshRunTransition(run) {
    // Fire the staleness refetch once per run-state change, not on every poll:
    // the endpoint reads the consolidated stores and is far too costly for a
    // 2s loop. run_id + in_flight is enough to catch "a run finished" and
    // "a new run started", including runs the server started on its own (an
    // armed consolidate), which no browser poll was watching.
    const sig = run ? `${run.run_id}:${run.in_flight ? 1 : 0}` : 'none';
    if (sig === _lastSeenRunSignature) return;
    const first = _lastSeenRunSignature === null;
    _lastSeenRunSignature = sig;
    if (first) return;              // page load already fetches staleness
    if (typeof fetchStalenessStatus === 'function') fetchStalenessStatus();
}

function applyRefreshCardState(data) {
    // One refresh run at a time: two would interleave writes to the same caches.
    // The server refuses a second start with a 423, and this is the half the
    // operator can see — every refresh card's button disabled, saying which run
    // holds the pipeline. Stored on window because setStatus() in main.js
    // rebuilds these buttons on every status poll and re-applies the lock.
    const run = (data && data.refresh_run) || null;
    const active = !!(data && data.consolidate_pipeline_active);
    if (active && run && run.in_flight) {
        window._refreshRunLock = {
            origin: run.origin,
            label: run.origin_label || run.origin,
            reason: `A refresh run started from ${run.origin_label || run.origin} is in progress`,
        };
    } else {
        window._refreshRunLock = null;
    }
    if (typeof applyRefreshRunLock === 'function') applyRefreshRunLock();
}

// ── Start dialog ────────────────────────────────────────────────────────────
// A card start does not run one worker: it plans that worker plus everything
// downstream of it. So every start goes through one dialog that states what is
// about to run, roughly how long that has taken before, and carries the run's
// options.

const REFRESH_START_OPTIONS = {
    consolidate_enrichment: [
        { key: 'auto_refresh', label: 'Refresh caches afterwards', checked: true,
          help: 'Follow the consolidation with the downstream refresh pipeline. Untick to consolidate the new enrichment files only.' },
        { key: 'force', label: 'Force full rebuild',
          help: 'Reconsolidate every scrape and annotation file from scratch instead of folding in only the new ones. Slow; needs every worker idle.' },
    ],
    video_map_refresh: [
        { key: 'reset_labels', label: 'Reset all labels', danger: true,
          help: 'Regenerate every niche name from scratch instead of carrying stable ones over. Cluster ids are kept, so saved niche-filtered analyses still point at the same clusters.' },
    ],
    sessions_refresh: [
        { key: 'force', label: 'Force full rebuild',
          help: 'Re-segment every covered collection. The default re-segments only collections whose data or date window moved.' },
    ],
};

let _refreshStartStep = null;

function _refreshStepLabel(step) {
    return (_PIPELINE_STEP_LABELS && _PIPELINE_STEP_LABELS[step]) || step;
}

function _refreshTypicalSeconds(step) {
    // What this step cost last time, straight from /api/status. A measured
    // number beats an invented one, and when a step has never run we say so
    // rather than guessing.
    const st = (window._lastStatusData || {})[step];
    const d = st && st.last_run_duration;
    return (typeof d === 'number' && isFinite(d) && d > 0) ? d : null;
}

function _refreshPlannedSteps(step) {
    // The origin plus everything that reads what it writes. With "Refresh
    // caches afterwards" unticked a consolidation runs alone.
    const deps = (_PIPELINE_REGISTRY.dependents || {})[step] || [];
    if (step === 'consolidate_enrichment' && !_refreshOptionChecked('auto_refresh')) return [step];
    return [step, ...deps];
}

function _refreshOptionChecked(key) {
    const el = document.getElementById(`refresh-opt-${key}`);
    return !!(el && el.checked);
}

function _renderRefreshStartPlan() {
    const planEl = document.getElementById('refresh-start-plan');
    const estEl = document.getElementById('refresh-start-estimate');
    if (!planEl) return;
    const steps = _refreshPlannedSteps(_refreshStartStep);
    const leafSet = new Set(_PIPELINE_REGISTRY.leaves || []);

    // Wall-clock, not effort. The run is a chain of steps that each wait for the
    // one before, EXCEPT the leaves, which are dispatched together and run
    // concurrently — so they cost the slowest of them, not their sum. Adding
    // them up overstated a full run by minutes.
    let serial = 0, slowestLeaf = 0, unknown = 0, leafCount = 0;
    planEl.innerHTML = steps.map((st, i) => {
        const secs = _refreshTypicalSeconds(st);
        const isLeaf = leafSet.has(st);
        if (secs == null) unknown++;
        else if (isLeaf) slowestLeaf = Math.max(slowestLeaf, secs);
        else serial += secs;
        if (isLeaf) leafCount++;
        const when = secs == null ? 'not run before' : _fmtDuration(secs);
        const cls = (st === _refreshStartStep ? ' refresh-plan-row--origin' : '')
            + (isLeaf ? ' refresh-plan-row--parallel' : '');
        return `<div class="refresh-plan-row${cls}">`
            + `<span class="refresh-plan-num text-xxs">${isLeaf ? '' : i + 1}</span>`
            + `<span class="refresh-plan-name text-xs">${escapeHtml(_refreshStepLabel(st))}</span>`
            + `<span class="refresh-plan-time text-xxs">${escapeHtml(when)}</span>`
            + `</div>`;
    }).join('');

    const total = serial + slowestLeaf;
    if (estEl) {
        if (!total && unknown) {
            estEl.textContent = 'No previous run to estimate from.';
            return;
        }
        let txt = `Roughly ${_fmtDuration(total)} in total, based on each step's last run`;
        if (leafCount > 1) txt += `; the last ${leafCount} run at the same time, so they cost the slowest of them`;
        txt += unknown
            ? ` — ${unknown} step${unknown > 1 ? 's have' : ' has'} never run, so the real time will be longer.`
            : '.';
        estEl.textContent = txt;
    }
}

function openRefreshStartModal(step) {
    const overlay = document.getElementById('refresh-start-overlay');
    if (!overlay) { _startRefreshStep(step, {}); return; }   // markup absent: just start
    if (window._refreshRunLock) {
        showAppAlert(window._refreshRunLock.reason, { title: 'Refresh run in progress' });
        return;
    }
    // An armed Consolidate uses the same button to cancel the arm. That is a
    // stop, not a start, so it must not open a dialog offering to run it.
    if (step === 'consolidate_enrichment') {
        const cbtn = document.getElementById('btn-consolidate');
        if (cbtn && cbtn.dataset.armed === '1') { consolidateEnrichmentData(cbtn); return; }
    }
    _refreshStartStep = step;
    document.getElementById('refresh-start-title').textContent = _refreshStepLabel(step);
    const deps = (_PIPELINE_REGISTRY.dependents || {})[step] || [];
    document.getElementById('refresh-start-sub').textContent = deps.length
        ? 'Starts this step and everything that depends on it.'
        : 'Nothing downstream depends on this step.';

    const optEl = document.getElementById('refresh-start-options');
    const opts = REFRESH_START_OPTIONS[step] || [];
    // A forced reconsolidation needs every scraper and annotator idle; the
    // server refuses it otherwise. Say so where the choice is made, and take
    // the option away rather than let the start bounce.
    const blocking = ((window._lastEnrichmentStats || {}).workers_blocking_consolidate) || [];
    optEl.innerHTML = opts.map(o => {
        const blocked = (step === 'consolidate_enrichment' && o.key === 'force' && blocking.length > 0);
        const help = blocked
            ? `Needs ${blocking.join(', ')} to finish first.`
            : o.help;
        return `<label class="refresh-opt${blocked ? ' refresh-opt--blocked' : ''}">`
            + `<input type="checkbox" id="refresh-opt-${o.key}"${o.checked && !blocked ? ' checked' : ''}`
            + `${blocked ? ' disabled' : ''}>`
            + `<span class="text-sm"${o.danger ? ' style="color: var(--color-warning);"' : ''}>${escapeHtml(o.label)}</span>`
            + `<span class="refresh-opt-help text-xxs">${escapeHtml(help)}</span>`
            + `</label>`;
    }).join('');
    // Only the consolidate options change what the run will do, so only they
    // need to redraw the plan.
    opts.forEach(o => {
        const el = document.getElementById(`refresh-opt-${o.key}`);
        if (el) el.onchange = _renderRefreshStartPlan;
    });

    _renderRefreshStartPlan();
    document.getElementById('refresh-start-cancel-btn').onclick = closeRefreshStartModal;
    document.getElementById('refresh-start-ok-btn').onclick = _confirmRefreshStart;
    overlay.onclick = (e) => { if (e.target === overlay) closeRefreshStartModal(); };
    document.addEventListener('keydown', _refreshStartKeydown);
    overlay.classList.add('visible');
}

function closeRefreshStartModal() {
    const overlay = document.getElementById('refresh-start-overlay');
    if (overlay) overlay.classList.remove('visible');
    document.removeEventListener('keydown', _refreshStartKeydown);
    _refreshStartStep = null;
}

function _refreshStartKeydown(e) {
    if (e.key === 'Escape') closeRefreshStartModal();
}

async function _confirmRefreshStart() {
    const step = _refreshStartStep;
    const args = {};
    for (const o of (REFRESH_START_OPTIONS[step] || [])) args[o.key] = _refreshOptionChecked(o.key);
    closeRefreshStartModal();
    await _startRefreshStep(step, args);
}

async function _startRefreshStep(step, args) {
    if (step === 'consolidate_enrichment') {
        consolidateEnrichmentData(document.getElementById('btn-consolidate'), args);
        return;
    }
    // The card buttons' own argument shapes, now fed from the dialog.
    let body = {};
    if (step === 'video_map_refresh') body = { reset_labels: !!args.reset_labels };
    else if (step === 'sessions_refresh') body = args.force ? {} : { stale_only: true };
    else if (step === 'recode_refresh_studies') body = { force_full_rebuild: true };
    await startProcess(step, body);
}

function _scopeNote(s) {
    // What this step was actually dispatched with, and why it is what it is.
    // The consolidation's impact is only the floor: a semantic map that moved
    // videos between niches invalidates the niche columns of every study and
    // every collection, so those steps run unfiltered. Without this the chart
    // kept showing the impact's narrower numbers while the run rebuilt more.
    if (!s || !s.scope) return '';
    return s.reason ? `${s.scope} — ${s.reason}` : s.scope;
}

function _pendingWork(step, impact) {
    // What this step is waiting to do, from the consolidation's own impact.
    // A bare "pending" says only that the run has not reached it; the operator
    // wants to know what it will find when it gets there.
    if (!impact) return '';
    const n = (v) => Number(v || 0);
    const plural = (c, one, many) => `${c.toLocaleString()} ${c === 1 ? one : many}`;
    switch (step) {
        case 'embeddings_refresh':
            return n(impact.new_annotations)
                ? `${plural(n(impact.new_annotations), 'new annotation', 'new annotations')} to embed`
                : '';
        case 'video_map_refresh':
            return n(impact.new_annotations)
                ? 'Re-clusters the niches once the new videos are embedded' : '';
        case 'recode_refresh_studies':
        case 'meta_refresh_groups':
        case 'pca_refresh':
            return n(impact.studies)
                ? `${plural(n(impact.studies), 'study', 'studies')} affected` : '';
        case 'timelines_refresh':
            return n(impact.collections)
                ? `${plural(n(impact.collections), 'collection', 'collections')} affected` : '';
        case 'sessions_refresh':
            return n(impact.collections)
                ? `${plural(n(impact.collections), 'collection', 'collections')} to re-check` : '';
        default:
            return '';
    }
}

function _stepHelpText(step) {
    // The (i) tooltip from this worker's card in "Rebuild Downstream Datasets",
    // reused verbatim on its row here. Harvested from the DOM rather than
    // copied into JS so the description lives in exactly one place and the two
    // can never drift apart.
    if (!step) return '';
    const el = document.querySelector(`.card-info[data-step="${step}"]`);
    return el ? (el.getAttribute('data-tooltip') || '') : '';
}

function _stepLogLink(step, label) {
    // The card's View Log, doubled up on the chart row so a run can be read and
    // opened from one place. Admin-only, exactly like the card's link; everyone
    // else gets the empty cell so the grid keeps its columns.
    if (!window.USER_IS_ADMIN) return '<span class="gantt-log"></span>';
    const arg = escapeHtml(String(label).replace(/\\/g, '\\\\').replace(/'/g, "\\'"));
    return `<a class="gantt-log text-xxs view-log-link"`
        + ` onclick="openLogModal(&#39;${step}&#39;, &#39;${arg}&#39;)">View Log</a>`;
}

function _stepRunButton(step, state, isArmed) {
    // The card's own Start/Stop, repeated on the row: play while the worker is
    // idle, stop while it runs — the transport controls anyone recognises, so
    // the chart is somewhere you act from and not only somewhere you read.
    // Admin-only, matching the cards' own gating.
    if (!window.USER_IS_ADMIN) return '<span class="gantt-run"></span>';
    const running = state === 'running' || state === 'queued';
    if (running) {
        return `<button type="button" class="gantt-run gantt-run--stop" title="Stop this worker"`
            + ` aria-label="Stop" onclick="showStopConfirm(&#39;${step}&#39;)">`
            + '<svg viewBox="0 0 12 12" aria-hidden="true"><rect x="2" y="2" width="8" height="8" rx="1"/></svg>'
            + '</button>';
    }
    // While a run holds the pipeline every other worker's start is refused
    // server-side, so show that here rather than let the click bounce.
    const locked = !!window._refreshRunLock;
    let title = locked ? window._refreshRunLock.reason : 'Start this step and everything downstream of it';
    if (isArmed) title = 'Armed — runs automatically when the scraper and annotator finish';
    const armedCls = isArmed ? ' gantt-run--armed' : '';
    return `<button type="button" class="gantt-run gantt-run--play${armedCls}"${locked ? ' disabled' : ''}`
        + ` title="${escapeHtml(title)}" aria-label="Start"`
        + ` onclick="openRefreshStartModal(&#39;${step}&#39;)">`
        + '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 2l7 4-7 4z"/></svg>'
        + '</button>';
}

function renderPipelineSteps(steps, run, armed) {
    // The last (or running) refresh run as a timeline. One row per step in
    // dispatch order; each bar is placed by the step's start and sized by its
    // duration on a shared wall-clock axis anchored at the earliest start — so
    // bars measure TIME, not progress. Live steps grow to "now" and pulse;
    // queued leaves show a dashed wait from the moment they were queued; a
    // dashed guideline marks where the run forked. Every step is always listed,
    // so a step outside this run still gets a row — greyed, bar-less, and
    // worded in the right column, which is where a skipped step says WHY it was
    // skipped. With no run recorded the empty-state line shows.
    _pipelineStepsCache = steps || null;
    if (run !== undefined) _pipelineRunCache = run || null;
    run = _pipelineRunCache;
    if (armed !== undefined) _pipelineArmedCache = armed || null;
    armed = _pipelineArmedCache;
    // An armed Consolidate is a run that WILL happen once the scraper or
    // annotator finishes. Mark every step it will set off, so the block shows
    // what is queued up rather than looking idle until it fires.
    const armedSteps = new Set();
    if (armed && armed.armed) {
        armedSteps.add('consolidate_enrichment');
        if (armed.autoRefresh) {
            for (const d of ((_PIPELINE_REGISTRY.dependents || {})['consolidate_enrichment'] || [])) {
                armedSteps.add(d);
            }
        }
    }
    const chart = document.getElementById('pipeline-gantt');
    const note = document.getElementById('pipeline-steps-note');
    const empty = document.getElementById('pipeline-gantt-empty');
    const legend = document.getElementById('pipeline-gantt-legend');
    if (!chart) return;
    if (!steps || !steps.length) {
        chart.innerHTML = '';
        chart.style.display = 'none';
        if (empty) empty.style.display = '';
        if (legend) legend.style.display = 'none';
        if (note) note.style.display = 'none';
        _stopPipelineTicker();
        return;
    }
    if (empty) empty.style.display = 'none';
    const allIdle = steps.every(s => s.state === 'idle');
    if (legend) legend.style.display = allIdle ? 'none' : '';
    chart.style.display = '';
    // Who started this run and how it ended. Without it the chart silently
    // implies every run is a consolidation, which is exactly what stopped being
    // true when any card started planning its own dependents.
    _renderRunHeader(run, steps);
    // While the plan is the one made at dispatch, say so — the run greys out
    // the steps it turns out not to need as it learns what actually changed.
    if (note) note.style.display = steps.some(s => s.provisional) ? '' : 'none';

    const now = Date.now();
    const parse = v => (v ? Date.parse(v) : NaN);
    const anyLive = steps.some(s => s.state === 'running' || s.state === 'queued');

    // Axis: from the earliest known start (or queue stamp) to the latest end,
    // extended to now while anything is live; never narrower than 30 s so a
    // fresh run is readable.
    const known = [];
    for (const s of steps) {
        for (const v of [s.started_at, s.queued_at]) {
            const t = parse(v);
            if (Number.isFinite(t)) known.push(t);
        }
    }
    const t0 = known.length ? Math.min(...known) : NaN;
    let tEnd = t0;
    for (const s of steps) {
        const e = parse(s.ended_at);
        if (Number.isFinite(e)) tEnd = Math.max(tEnd, e);
    }
    if (anyLive) tEnd = Math.max(tEnd, now);
    const spanMs = Math.max(tEnd - t0, 30_000);
    const pct = ms => Math.min(100, Math.max(0, ((ms - t0) / spanMs) * 100));
    const haveAxis = Number.isFinite(t0);

    // The fork: where the step that fanned the leaves out finished. Which step
    // that is depends on the run — a consolidation whose studies were untouched
    // forks straight from the consolidate row.
    const forkStep = (run && run.fork_at) || 'recode_refresh_studies';
    const forkRow = steps.find(s => s.step === forkStep);
    const forkAt = forkRow ? parse(forkRow.ended_at) : NaN;
    const markers = (haveAxis ? (
        (Number.isFinite(forkAt) ? `<span class="gantt-fork" style="left:${pct(forkAt)}%"></span>` : '')
        + (anyLive ? `<span class="gantt-now" style="left:${pct(now)}%"></span>` : '')
    ) : '');

    const rows = steps.map(s => {
        const state = s.state || 'pending';
        const a = parse(s.started_at), e = parse(s.ended_at), q = parse(s.queued_at);
        let bar = '', dur = '', title = '';
        if (state === 'pruned') {
            // Planned, then skipped because nothing upstream changed. This is
            // the ordinary outcome of a quiet run and the most useful thing the
            // chart says, so the reason goes in the empty track where the bar
            // would have been — not into a tooltip nobody hovers.
            dur = 'not needed';
            const why = s.reason || 'nothing upstream changed';
            bar = `<span class="gantt-msg gantt-msg--reason text-xxs" style="left:0">${escapeHtml(why)}</span>`;
            title = `Skipped \u2014 ${why}`;
        } else if (state === 'upstream') {
            dur = 'not in this run';
            title = 'Runs before this step \u2014 not part of this run';
        } else if (state === 'not_planned') {
            // Nothing in this run feeds it. Checked before the axis guard so it
            // reads right even in the first poll, before anything has started.
            const only = s.plan_mode === 'consolidate_only';
            dur = only ? 'not requested' : 'not needed';
            title = only
                ? 'Not requested \u2014 this run consolidated without the refresh pipeline'
                : "Not needed \u2014 nothing in this run feeds it";
        } else if (state === 'idle') {
            // Nothing has ever run. The row is here so the block always lists
            // every worker in dependency order, with a way into its log. Sits
            // with the other bar-less states, ahead of the no-axis guard: an
            // all-idle chart has no timing anywhere, so that guard would
            // otherwise swallow every row.
            bar = '<span class="gantt-msg gantt-msg--live text-xxs" style="left:0">Idle</span>';
            title = 'No refresh run recorded yet';
        } else if (!haveAxis) {
            dur = state;
        } else if (state === 'running' && Number.isFinite(a)) {
            const left = pct(a);
            bar = `<span class="gantt-bar gantt-bar--running" style="left:${left}%;width:${Math.max(pct(now) - left, 0)}%"></span>`;
            // The worker's own progress message, the same line its card shows,
            // written from the bar's left edge — without the percentage, which
            // the bar's own length already tells you. It may run past the end
            // of its bar; the track clips it so it can never reach the
            // duration column.
            if (s.message) {
                bar += `<span class="gantt-msg gantt-msg--live text-xxs" style="left:${left}%">`
                    + escapeHtml(s.message) + '</span>';
            }
            dur = `${_fmtDuration((now - a) / 1000)} …`;
            const runScope = _scopeNote(s);
            title = `Started ${fypFmtTime(a)}, running`
                + (runScope ? ` — ${runScope}` : '');
        } else if (state === 'queued') {
            const from = Number.isFinite(q) ? q : (Number.isFinite(forkAt) ? forkAt : now);
            const left = pct(from);
            bar = `<span class="gantt-bar gantt-bar--queued" style="left:${left}%;width:${Math.max(pct(now) - left, 0)}%"></span>`;
            bar += `<span class="gantt-msg gantt-msg--live text-xxs" style="left:${left}%">`
                + escapeHtml(s.message || 'Queued…') + '</span>';
            dur = 'waiting';
            title = 'Queued — waiting for a worker';
        } else if ((state === 'success' || state === 'failed') && Number.isFinite(a) && Number.isFinite(e)) {
            const left = pct(a);
            bar = `<span class="gantt-bar gantt-bar--${state}" style="left:${left}%;width:${Math.max(pct(e) - left, 0)}%"></span>`;
            dur = _fmtDuration(s.duration_s != null ? s.duration_s : (e - a) / 1000);
            const doneScope = _scopeNote(s);
            title = `${fypFmtTime(a)} → ${fypFmtTime(e)}`
                + (state === 'failed' ? ' — failed' : '')
                + (doneScope ? ` — ${doneScope}` : '');
        } else if (state === 'skipped') {
            // Planned work that never happened — an anomaly, unlike "pruned".
            dur = 'skipped';
            title = 'Planned but never ran \u2014 the run ended before reaching it';
        } else {
            dur = state === 'failed' ? 'failed' : 'pending';
            const work = _pendingWork(s.step, run && run.impact);
            if (work) {
                // "at least": a map rebuild ahead of this step can widen it to
                // every study or collection, and saying so up front is better
                // than the number silently growing when the run gets here.
                const widens = ['recode_refresh_studies', 'meta_refresh_groups',
                                'pca_refresh', 'timelines_refresh'].includes(s.step);
                title = widens
                    ? `Waiting to run — at least ${work}; a rebuilt semantic map widens this to all of them`
                    : `Waiting to run — ${work}`;
                bar = `<span class="gantt-msg gantt-msg--reason text-xxs" style="left:0">`
                    + escapeHtml(work) + '</span>';
            } else {
                title = 'Planned — waiting for the steps before it';
            }
        }
        const originCls = s.is_origin ? ' gantt-row--origin' : '';
        const isArmed = armedSteps.has(s.step);
        const armedCls = isArmed ? ' gantt-row--armed' : '';
        const label = s.label || s.step;
        // The worker's own description, on its name. The row's timing title
        // stays on the track instead of the whole row, so hovering the name
        // shows one tooltip (what this step does) and hovering its bar the
        // other (when it ran) — never both at once.
        const help = _stepHelpText(s.step);
        const labelCell = help
            ? `<span class="gantt-label gantt-label--help meta-tooltip tooltip-wide text-xs"`
                + ` tabindex="0" data-tooltip="${escapeHtml(help)}">${escapeHtml(label)}</span>`
            : `<span class="gantt-label text-xs">${escapeHtml(label)}</span>`;
        return `<div class="gantt-row gantt-row--${state}${originCls}${armedCls}">`
            + _stepLogLink(s.step, label)
            + _stepRunButton(s.step, state, isArmed)
            + labelCell
            + `<span class="gantt-track" title="${escapeHtml(title)}">${markers}${bar}</span>`
            + `<span class="gantt-dur text-xs">${escapeHtml(dur)}</span>`
            + `</div>`;
    });

    let axis = '';
    if (haveAxis) {
        const spanS = spanMs / 1000;
        const step = _ganttTickStep(spanS);
        // Both ends carry a wall-clock time — the left when the run started,
        // the right when it finished — so the chart answers "when was this?"
        // and not only "how long did it take?". While a run is live the right
        // edge is "now" and moving, so no finish time is claimed.
        const endLabel = (!anyLive && Number.isFinite(tEnd) && tEnd > t0)
            ? `<span class="gantt-tick gantt-tick--end text-xxs" style="left:100%">${escapeHtml(fypFmtTime(tEnd))}</span>`
            : '';
        const ticks = [`<span class="gantt-tick gantt-tick--origin text-xxs" style="left:0%">${escapeHtml(fypFmtTime(t0))}</span>`];
        for (let t = step; t <= spanS; t += step) {
            const at = (t / spanS) * 100;
            if (endLabel && at > 88) continue;   // don't collide with the finish time
            const label = t % 60 === 0 ? `+${t / 60} min` : `+${t} s`;
            ticks.push(`<span class="gantt-tick text-xxs" style="left:${at}%">${label}</span>`);
        }
        if (endLabel) ticks.push(endLabel);
        axis = `<div class="gantt-axis"><span></span><span></span><span></span><span class="gantt-axis-track">${ticks.join('')}</span><span></span></div>`;
    }
    chart.innerHTML = `<div class="gantt-body">${rows.join('')}</div>${axis}`;

    if (anyLive) _startPipelineTicker(); else _stopPipelineTicker();
}

function _activePipelineStep(statusData) {
    // Return the {name, state_obj} of the currently-running pipeline step, or
    // null if none is running. Only counts steps whose state is 'running'.
    for (const name of _PIPELINE_STEPS) {
        const p = statusData[name];
        if (p && p.state === 'running') return { name, state: p };
    }
    return null;
}

// The two option checkboxes next to the Consolidate button. They replace the
// former three buttons: "Consolidate & Refresh" = refresh ticked, "Consolidate
// Only" = refresh unticked, "Force Reconsolidate" = force ticked.


function consolidateEnrichmentData(btn, opts) {
    const statusEl = document.getElementById('consolidate-status');
    // Options come from the start dialog now. A caller without them (the disarm
    // click, or any older entry point) falls back to the defaults.
    const autoRefresh = opts ? !!opts.auto_refresh : true;
    const force = opts ? !!opts.force : false;

    // Both options are sent explicitly — auto_refresh server-side defaults to
    // "not force", which is not what the checkboxes mean.
    const body = { auto_refresh: autoRefresh };
    if (force) body.force = true;

    // Hide the impact panel up-front so the old run's summary doesn't linger
    // while the new run is in flight. It will re-render on completion.
    renderConsolidationImpact(null);

    // If the button is already armed, a click disarms.
    if (btn.dataset.armed === '1') {
        fetch('/api/manage/enrichment/consolidate/disarm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        })
            .then(res => res.json())
            .then(() => {
                statusEl.textContent = 'Auto-consolidation cancelled.';
                statusEl.style.color = 'var(--color-text-secondary)';
                // Button state will reconcile on next fetchEnrichmentStats tick.
                fetchEnrichmentStats();
            })
            .catch(err => console.error('Failed to disarm:', err));
        return;
    }

    // Optimistic UI: mark the clicked button as busy. The server response
    // tells us whether we fired or armed; on armed, the button is restyled
    // by applyConsolidateButtonState() when stats refresh.
    const originalText = btn.textContent;
    const originalClass = btn.className;
    btn.disabled = true;

    fetch('/api/manage/enrichment/consolidate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
        .then(res => res.json())
        .then(data => {
            if (data.status === 'started') {
                btn.textContent = 'Consolidating...';
                btn.className = 'btn-running';
                // Same optimistic flip every other card gets on Start: turns the
                // dot green and the bar to "Starting…" through the dispatch +
                // task-runner boot gap, and guards it from a poll that still
                // reports the previous run.
                if (typeof markStarting === 'function') markStarting('consolidate_enrichment');
                // No status line here: the card's own bar shows the phase and
                // the step list below shows the pipeline. pollConsolidationStatus
                // clears the previous run's summary.
                pollConsolidationStatus();
            } else if (data.status === 'armed') {
                // Auto-arm accepted. fetchEnrichmentStats will re-render the
                // button; keep a short confirmation in the status line.
                statusEl.textContent = data.message || 'Armed — will run when scraper/annotator finish.';
                statusEl.style.color = 'var(--color-warning)';
                btn.disabled = false;
                fetchEnrichmentStats();
            } else {
                statusEl.textContent = 'Error: ' + (data.message || data.error || 'Unknown error');
                statusEl.style.color = 'var(--color-danger)';
                btn.className = originalClass;
                btn.textContent = originalText;
                btn.disabled = false;
            }
        })
        .catch(err => {
            console.error('Failed to start consolidation:', err);
            statusEl.textContent = 'Failed to start consolidation.';
            statusEl.style.color = 'var(--color-danger)';
            btn.className = originalClass;
            btn.textContent = originalText;
            btn.disabled = false;
        });
}

function applyConsolidateButtonState(data) {
    // Drive the Consolidate button + its two option checkboxes off the latest
    // enrichment-stats response. Called from fetchEnrichmentStats every tick.
    // Cached so a checkbox toggle between ticks can re-render immediately.
    window._lastEnrichmentStats = data;

    const btn = document.getElementById('btn-consolidate');
    if (!btn) return;

    const blocking = data.workers_blocking_consolidate || [];
    const armed = !!data.consolidate_auto_armed;
    const workersRunning = blocking.length > 0;
    const pipelineActive = !!data.consolidate_pipeline_active;

    if (_consolidatePollActive) {
        // Polling loop owns the button text/state during an active run.
        return;
    }

    if (armed) {
        btn.dataset.armed = '1';
        btn.textContent = '⏳ Armed — click to cancel';
        btn.classList.add('action-btn', 'btn-armed-pulse');
        btn.classList.remove('btn-running', 'btn-has-pending');
        btn.title = blocking.length
            ? `Runs when ${blocking.join(', ')} finish.`
            : 'Runs when scraper/annotator finish.';
        btn.disabled = false;
        return;
    }

    btn.dataset.armed = '';
    btn.textContent = 'Consolidate';
    btn.classList.add('action-btn');
    btn.classList.remove('btn-running', 'btn-armed-pulse');

    // The button does not know whether this run will be a forced rebuild —
    // that is chosen in the start dialog, which is also where the "needs every
    // worker idle" warning belongs. So the button only reflects what is
    // true regardless of the choice.
    if (pipelineActive) {
        btn.disabled = true;
        btn.title = 'Wait for the refresh pipeline to finish.';
    } else {
        btn.disabled = false;
        btn.title = workersRunning
            ? 'Click to arm — will run when scraper/annotator finish.'
            : '';
    }
}

function pollConsolidationStatus(originStep) {
    // Poll for the refresh run. Considers consolidate_enrichment and all
    // downstream refresh steps as part of the same logical operation — as long
    // as any one of them is running, the UI stays in the "running" state. Only
    // exits when none are running AND the run is no longer in flight.
    //
    // originStep names the step whose start opened this poll, so the button
    // says the right thing from the first frame. The loop below corrects the
    // label every tick anyway, but a run started from a card used to read
    // "Consolidating..." until that first tick landed.
    if (_consolidatePollActive) return;
    _consolidatePollActive = true;

    const statusEl = document.getElementById('consolidate-status');
    const btnC = document.getElementById('btn-consolidate');
    if (btnC) {
        btnC.disabled = true;
        btnC.dataset.armed = '';
        btnC.textContent = (originStep && originStep !== 'consolidate_enrichment')
            ? 'Refreshing caches...'
            : 'Consolidating...';
        btnC.classList.add('action-btn', 'btn-running');
        btnC.classList.remove('btn-armed-pulse', 'btn-has-pending');
    }

    // Hide the "scraper/annotator completed after last consolidation" warning
    // as soon as the run starts. fetchEnrichmentStats isn't called every tick
    // while polling, so without this the warning lingers through the run.
    const warningEl = document.getElementById('consolidate-warning');
    if (warningEl) warningEl.style.display = 'none';

    // Clear the previous run's summary for the duration of this one. The step
    // list below is the live narration; this line does not echo the active
    // worker's message, whose "Stage i/N" prefix is counted over the dispatch
    // TREE's depth and matches neither the step list nor anything the user
    // can act on. It is repopulated from the fresh stats when the run ends.
    if (statusEl) statusEl.innerHTML = '';

    const interval = setInterval(() => {
        // Fetch both /api/status (for live step progress) and the enrichment
        // stats (for pipeline_in_flight across the gap between steps) each
        // tick. Light endpoints; we run this loop at 2s cadence only during
        // an active pipeline.
        Promise.all([
            fetch('/api/status').then(r => r.json()),
            fetch('/api/manage/enrichment/stats').then(r => r.json()),
        ])
            .then(([data, estats]) => {
                // Keep the chart and the card lock live during the run.
                if (estats) {
                    renderPipelineSteps(estats.pipeline_steps, estats.refresh_run, {
                        armed: !!estats.consolidate_auto_armed,
                        autoRefresh: !!estats.consolidate_auto_armed_auto_refresh,
                    });
                    applyRefreshCardState(estats);
                }

                const active = _activePipelineStep(data);
                if (active) {
                    // This loop runs for any pipeline member, including one
                    // started on its own card (a sessions refresh chained from
                    // a study save, say) — so name the phase honestly instead
                    // of always claiming a consolidation is under way.
                    if (btnC) {
                        // The weekly shadow verification runs under the
                        // consolidate key too; it is a read-only check, not a
                        // consolidation, and reads as one otherwise.
                        const args = (active.state && active.state.task_args) || {};
                        btnC.textContent = active.name !== 'consolidate_enrichment'
                            ? 'Refreshing caches...'
                            : (args.verify_consolidation ? 'Verifying (read-only)...' : 'Consolidating...');
                    }
                    return;
                }

                // No step is currently "running". If the run is still flagged
                // as in-flight, we're in the gap between steps — keep polling.
                // The chart carries that state on its own.
                if (estats && estats.consolidate_pipeline_active) return;

                // Settled. The run record is what says so — a run started from
                // a worker card never touches the consolidate step, so waiting
                // for a consolidate outcome would poll forever.
                const run = estats && estats.refresh_run;
                if (run && run.in_flight) return;
                if (!run) {
                    // No run recorded at all (a bare worker start): fall back to
                    // the consolidate outcome so an old dispatch still settles.
                    const consolidate = data.consolidate_enrichment;
                    if (!(consolidate && consolidate.last_run_outcome)) return;
                }
                const failed = run ? run.partial
                    : (data.consolidate_enrichment || {}).last_run_outcome !== 'Success';

                clearInterval(interval);
                _consolidatePollActive = false;
                window._refreshRunLock = null;
                if (typeof applyRefreshRunLock === 'function') applyRefreshRunLock();

                if (btnC) {
                    btnC.classList.remove('btn-running', 'btn-armed-pulse');
                    btnC.classList.add('action-btn');
                    btnC.textContent = 'Consolidate';
                    btnC.disabled = false;
                    btnC.dataset.armed = '';
                }

                if (!failed) {
                    // The persistent summary lives in consolidate_stats (for a
                    // consolidation) and in the chart header (for every run), so
                    // refetching the stats is enough.
                    fetchEnrichmentStats();
                    if (typeof fetchStalenessStatus === 'function') fetchStalenessStatus();
                } else {
                    // This line belongs to the Consolidate card, so only a run
                    // that card started may write it. A card-started run
                    // reports through the chart header instead — writing here
                    // would blame the consolidation for someone else's failure.
                    const consolidateRun = !run || ['consolidate', 'armed',
                        'refresh_downstream'].includes(run.origin_kind);
                    if (statusEl && consolidateRun) {
                        const where = run && run.failed_at
                            ? _humanizePipelineSteps(run.failed_at) : '';
                        statusEl.textContent = where
                            ? `Refresh run stopped at ${where}. Check logs.`
                            : 'Refresh run did not finish. Check logs.';
                        statusEl.style.color = 'var(--color-danger)';
                    }
                    fetchEnrichmentStats();
                }
            })
            .catch(err => {
                console.error('Error polling refresh-run status:', err);
                clearInterval(interval);
                _consolidatePollActive = false;
                window._refreshRunLock = null;
                if (typeof applyRefreshRunLock === 'function') applyRefreshRunLock();
                if (btnC) {
                    btnC.className = 'action-btn';
                    btnC.textContent = 'Consolidate';
                    btnC.disabled = false;
                }
            });
    }, 2000);
}

// Every refresh card that carries a "(N ... need refresh)" badge, i.e. every
// step of the refresh pipeline. Drives both the render and the clear-all pass,
// so a card can never be left showing a badge the server no longer reports.
const _STALE_BADGE_STEPS = _PIPELINE_REGISTRY.downstream || [
    'embeddings_refresh', 'video_map_refresh', 'recode_refresh_studies',
    'meta_refresh_groups', 'pca_refresh', 'timelines_refresh', 'sessions_refresh',
];

let _refreshPageIdleTimer = null;

//: How often the Dataset Assembly refresh page re-reads server state while it
//: is open and nothing is known to be running. The fast 2s poll only exists
//: during a run this browser started; a run the SERVER starts — the enrichment
//: supervisor's deferred refresh fires minutes after a consolidation — has no
//: browser watching it, so the page sat on a stale impact panel offering work
//: that had already been done, and the button that answered "nothing to
//: refresh" looked broken. 30s is well below the shortest step.
const REFRESH_PAGE_IDLE_MS = 30000;

function _setRefreshPageIdlePoll(on) {
    if (_refreshPageIdleTimer) {
        clearInterval(_refreshPageIdleTimer);
        _refreshPageIdleTimer = null;
    }
    if (!on) return;
    _refreshPageIdleTimer = setInterval(() => {
        // The run poll owns the page while it is active, and a hidden tab has
        // nobody reading it.
        if (_consolidatePollActive || document.hidden) return;
        fetchEnrichmentStats();
        dmHistoryLoad();
    }, REFRESH_PAGE_IDLE_MS);
}

function fetchStalenessStatus() {
    return fetch('/api/manage/refresh/staleness')
        .then(res => res.json())
        .then(data => {
            if (!data.has_impact) {
                _STALE_BADGE_STEPS.forEach(name => {
                    const el = document.getElementById(`${name}-stale`);
                    if (el) el.style.display = 'none';
                });
                // Authoritative signal that impact is gone — hide the panel
                // immediately so it doesn't linger while enrichment stats reload.
                if (typeof renderConsolidationImpact === 'function') {
                    renderConsolidationImpact(null);
                }
            } else {
                const procs = data.processes || {};
                for (const name of _STALE_BADGE_STEPS) {
                    const el = document.getElementById(`${name}-stale`);
                    if (!el) continue;
                    const info = procs[name];
                    // The server ships the rendered phrase — each step counts a
                    // different thing (studies, collections, annotations).
                    if (info && info.stale && info.note) {
                        el.textContent = info.note;
                        el.style.display = '';
                    } else {
                        el.style.display = 'none';
                    }
                }
            }
            // A promoted preferred annotation version keeps the Study
            // Definitions refresh stale independently of any consolidation.
            const vp = data.version_promotion || {};
            if (vp.stale) {
                const el = document.getElementById('recode_refresh_studies-stale');
                if (el) {
                    el.textContent = el.style.display === '' && el.textContent
                        ? `${el.textContent} (+ new preferred annotation version)`
                        : '(new preferred annotation version — refresh needed)';
                    el.style.display = '';
                }
            }
        })
        .catch(err => console.error("Error fetching staleness:", err));
}

// Call on load
fetchEnrichmentStats();

const DM_PAGE_PERM_MAP = {
    'dm-page-ingestion':      'tab.data_management.ingestion',
    'dm-page-edit-activity':  'tab.data_management.edit_collections',
    'dm-page-studies':        'tab.data_management.studies',
    'dm-page-scrape':         'tab.data_management.scrape',
    'dm-page-annotation':     'tab.data_management.annotation',
    'dm-page-refresh':        'tab.data_management.refresh',
};

function openDataManagementPage(pageId, clickedItem) {
    // Defense in depth — refuse if the matching permission isn't granted.
    const requiredPerm = DM_PAGE_PERM_MAP[pageId];
    if (requiredPerm && Array.isArray(window.USER_PERMS) && !window.USER_PERMS.includes(requiredPerm)) {
        return;
    }

    document.querySelectorAll('#data_management .dm-page').forEach(page => {
        page.classList.remove('active');
    });

    // Deactivate all sidebar items
    document.querySelectorAll('#data_management .dm-sidebar-item').forEach(item => {
        item.classList.remove('active');
    });

    // Show selected page
    const page = document.getElementById(pageId);
    if (page) {
        page.classList.add('active');
    }

    // Activate clicked sidebar item
    if (clickedItem) {
        clickedItem.classList.add('active');
    }

    // Refresh enrichment stats when entering the Scrape, Annotation or Refresh
    // pages so queue counters and the consolidation-impact panel + pipeline
    // step list reflect current server state on navigation (they are otherwise
    // only re-rendered by event handlers, so they can show a stale snapshot
    // after navigating away and back).
    if (pageId === 'dm-page-scrape' || pageId === 'dm-page-annotation' || pageId === 'dm-page-refresh') {
        fetchEnrichmentStats();
    }

    // Fetch staleness status when entering the refresh page + apply cascade lock
    if (pageId === 'dm-page-refresh') {
        fetchStalenessStatus();
        dmHistoryLoad();
        if (_cascadeRefresh) {
            updateCascadeRefreshPageLock(true);
        }
    }
    _setRefreshPageIdlePoll(pageId === 'dm-page-refresh');

    // Populate the annotation-version dropdown on first visit to the
    // Annotation page.
    if (pageId === 'dm-page-annotation') {
        loadAnnotationVersionOptions();
    }

    // Lazy-load edit activity table on first visit
    if (pageId === 'dm-page-edit-activity') {
        const editContainer = document.getElementById('edit-activity-list-container');
        if (editContainer && editContainer.querySelectorAll('.edit-activity-item').length === 0) {
            if (typeof renderEditActivityTable === 'function') {
                renderEditActivityTable(editContainer);
            }
        }
    }

    if (typeof updateSubPageHash === 'function') {
        updateSubPageHash('data_management', pageId);
    }
}

