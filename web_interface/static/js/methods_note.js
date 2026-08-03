/**
 * Study methods/provenance note — the "how was this study built" modal.
 *
 * Lives on the My Studies page (My stuff), which lists every study the user can
 * see, so the modal takes its study as an argument rather than reading one tab's
 * active-study state. Loaded for anyone with the My Studies or Explore/Video
 * Analysis permission; the route (/api/studies/<study>/methods) does the real
 * per-study access check.
 */

let studyMethodsNote = null;   // last note fetched (for the JSON download)

async function openStudyMethodsModal(study) {
    if (!study) return;

    const modal = document.getElementById('study-methods-modal');
    const content = document.getElementById('study-methods-content');
    if (!modal || !content) return;

    content.innerHTML = '<div style="text-align:center; color: var(--color-text-muted);">Loading…</div>';
    modal.style.display = 'block';

    try {
        const resp = await fetch(`/api/studies/${encodeURIComponent(study)}/methods`);
        const data = await resp.json();
        if (!resp.ok || data.error) {
            const hint = data.hint ? `<p class="text-sm" style="color: var(--color-text-muted);">${escapeHtml(data.hint)}</p>` : '';
            content.innerHTML = `<h3 class="text-h3">Methods</h3><p class="text-sm">${escapeHtml(data.error || 'Could not load the methods note.')}</p>${hint}`;
            return;
        }
        studyMethodsNote = data;
        content.innerHTML = renderStudyMethodsNote(study, data);
    } catch (e) {
        console.error('Methods note fetch failed:', e);
        content.innerHTML = '<p class="text-sm" style="color: var(--color-danger);">Could not load the methods note.</p>';
    }
}

function closeStudyMethodsModal(event) {
    const modal = document.getElementById('study-methods-modal');
    if (modal) modal.style.display = 'none';
}

function downloadStudyMethodsJson() {
    if (!studyMethodsNote) return;
    const study = (studyMethodsNote.study && studyMethodsNote.study.name) || 'study';
    const blob = new Blob([JSON.stringify(studyMethodsNote, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${study}_methods.json`;
    a.click();
    URL.revokeObjectURL(a.href);
}

function renderStudyMethodsNote(study, note) {
    const parts = [];
    const sel = note.selection || {};
    const counts = note.counts || {};
    const ann = note.annotation || {};
    const fresh = note.freshness || {};
    const stale = note.staleness && note.staleness.stale;
    const fmtN = (n) => (n === null || n === undefined) ? '?' : Number(n).toLocaleString();
    const fmtD = (d) => d ? fypFmtDateTime(d) : 'unknown';

    parts.push(`<h3 class="text-h3" style="margin-top:0;">How "${escapeHtml(study)}" was built</h3>`);

    const provenance = note.data_provenance || {};
    if (provenance.synthetic) {
        parts.push('<p class="text-sm font-semibold" style="color: var(--color-warning, var(--color-text-muted));">You are exploring synthetic demonstration data — nothing here comes from real people.</p>');
        if (provenance.synthetic_label) {
            parts.push(`<p class="text-sm">${escapeHtml(provenance.synthetic_label)}</p>`);
        }
    }

    if (stale) {
        parts.push('<p class="text-sm" style="color: var(--color-warning, var(--color-text-muted));">The study data has been rebuilt since this note was written — refresh the study to update it.</p>');
    }

    // Scope: counts + date window
    const win = sel.date_window || {};
    let scope = `This study covers <strong>${fmtN(counts.collections)} collection(s)</strong> `
        + `with <strong>${fmtN(counts.activities)} watched or observed videos</strong> `
        + `(${fmtN(counts.unique_videos)} unique videos)`;
    if (win.configured_start || win.configured_end) {
        scope += ` between <strong>${escapeHtml(win.configured_start || 'the earliest data')}</strong> and `
            + `<strong>${escapeHtml(win.configured_end || 'the latest data')}</strong> (the end date is included in full)`;
    }
    scope += '.';
    if (win.actual_min && win.actual_max) {
        scope += ` The rows actually span ${fmtD(win.actual_min)} – ${fmtD(win.actual_max)}.`;
    }
    parts.push(`<p class="text-sm">${scope}</p>`);
    parts.push(`<p class="text-sm">Only viewing activity is included (plays and observations); likes, shares and other actions are folded into the matching viewing rows during ingestion.</p>`);

    // Sampling
    if (sel.sampling_active) {
        const th = sel.thresholds || {};
        let s = `Rows were sampled: <strong>${escapeHtml(sel.sample_frame_label || sel.sample_frame || '')}</strong>.`;
        const report = sel.sampling_report || {};
        if (report.collections_excluded_by_thresholds) {
            s += ` ${fmtN(report.collections_excluded_by_thresholds)} collection(s) were excluded for falling below the study's minimum activity thresholds.`;
        }
        if (report.collections_downsampled) {
            s += ` ${fmtN(report.collections_downsampled)} collection(s) were downsampled to the study's maximum.`;
        }
        s += ' Sampling uses a fixed random seed, so rebuilding from the same inputs selects the same rows.';
        parts.push(`<p class="text-sm">${s}</p>`);
        const thBits = [];
        if (th.min_activity_per_group != null) thBits.push(`min ${fmtN(th.min_activity_per_group)} activities per group`);
        if (th.max_activity_per_group != null) thBits.push(`max ${fmtN(th.max_activity_per_group)} activities per group`);
        if (th.min_groups_per_collection != null) thBits.push(`min ${fmtN(th.min_groups_per_collection)} groups per collection`);
        if (th.max_groups_per_collection != null) thBits.push(`max ${fmtN(th.max_groups_per_collection)} groups per collection`);
        if (thBits.length) parts.push(`<p class="text-xs" style="color: var(--color-text-muted);">Thresholds: ${thBits.join('; ')}.</p>`);
    } else {
        parts.push('<p class="text-sm">No sampling was applied — every matching activity row is included.</p>');
    }

    // Annotation / labelling
    const desc = ann.version_in_use_descriptor || {};
    if (ann.version_in_use) {
        let a = `Video labels come from labelling version <strong>${escapeHtml(ann.version_in_use)}</strong>`;
        if (desc.model) a += ` (model: ${escapeHtml(desc.model)}${desc.backend && desc.backend !== 'gemini' ? `, backend: ${escapeHtml(desc.backend)}` : ''})`;
        a += '.';
        if (ann.pinned_version) a += ' This study is pinned to that version and keeps reading it even when a newer one is promoted.';
        parts.push(`<p class="text-sm">${a}</p>`);
    } else if (ann.version_in_use_note) {
        parts.push(`<p class="text-sm">${escapeHtml(ann.version_in_use_note)}</p>`);
    }
    if (ann.mixed_versions && ann.mixed_versions_note) {
        parts.push(`<p class="text-sm" style="color: var(--color-text-muted);">⚠ ${escapeHtml(ann.mixed_versions_note)}</p>`);
    }
    const vRows = ann.versions_in_rows || {};
    const vKeys = Object.keys(vRows);
    if (vKeys.length) {
        const items = vKeys.map(k => `${escapeHtml(k)}: ${fmtN(vRows[k])} rows`).join(' · ');
        parts.push(`<p class="text-xs" style="color: var(--color-text-muted);">Label versions present: ${items}</p>`);
    }

    // Semantic map
    if (note.semantic_map && note.semantic_map.embedding_model) {
        parts.push(`<p class="text-sm">Topic clusters ("niches") come from a semantic map built with the `
            + `<strong>${escapeHtml(note.semantic_map.embedding_model)}</strong> embedding model`
            + `${note.semantic_map.built_at ? ` on ${fmtD(note.semantic_map.built_at)}` : ''}.</p>`);
    }

    // Freshness + download
    parts.push(`<p class="text-xs" style="color: var(--color-text-muted);">Dataset last processed: ${fmtD(fresh.built_at)}.</p>`);
    parts.push('<div style="margin-top: 12px;"><button class="btn-discreet text-xs" onclick="downloadStudyMethodsJson()">Download the full methods record (JSON)</button></div>');

    return parts.join('\n');
}
