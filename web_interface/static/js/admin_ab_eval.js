/**
 * Admin "Prompt A/B Testing" sub-page.
 *
 * Manages candidate contracts (/api/manage/ab-candidates), the curated eval
 * set (/api/manage/ab-eval-set), run launching (/api/manage/ab-eval/run +
 * /api/status polling), and result comparison (/api/manage/ab-eval/runs).
 * Candidate activation funnels through the NORMAL contract flow: the activate
 * endpoint returns the candidate text + impact, and this module drives the
 * standard confirm POST to /api/manage/annotation-contract. The global fetch
 * wrapper in main.js injects the CSRF header.
 */
(function () {
    "use strict";

    const CAND = "/api/manage/ab-candidates";
    const EVALSET = "/api/manage/ab-eval-set";
    const EVAL = "/api/manage/ab-eval";
    const AC = "/api/manage/annotation-contract";

    const st = {
        candidates: [],
        evalSet: { item_ids: [], resolved: [], max_items: 50 },
        setDirty: false,
        runs: [],
        currentRun: null,        // {manifest, report}
        currentRunId: null,
        rowsCache: {},           // {arm: rows[]} for the current run
        pollTimer: null,
    };

    function _esc(v) {
        const div = document.createElement("div");
        div.textContent = v == null ? "" : String(v);
        return div.innerHTML;
    }

    function _status(msg, isError) {
        const el = document.getElementById("abe-status");
        if (!el) return;
        el.textContent = msg || "";
        el.style.color = isError ? "var(--color-danger)" : "var(--color-text-muted)";
    }

    // Two-click confirmation for destructive/costly buttons — native
    // confirm()/prompt() dialogs are blocked in embedded preview browsers.
    // First click arms the button (relabels it); a second click within 4s
    // confirms. Returns true when confirmed.
    function _armTwoClick(btn, confirmLabel) {
        if (!btn) return true;
        if (btn.dataset.armed === "1") {
            btn.dataset.armed = "";
            btn.innerHTML = btn.dataset.prevHtml || btn.innerHTML;
            return true;
        }
        btn.dataset.armed = "1";
        btn.dataset.prevHtml = btn.innerHTML;
        btn.innerHTML = confirmLabel;
        setTimeout(() => {
            if (btn.dataset.armed === "1") {
                btn.dataset.armed = "";
                btn.innerHTML = btn.dataset.prevHtml;
            }
        }, 4000);
        return false;
    }

    async function _getJson(url) {
        const res = await fetch(url);
        const body = await res.json();
        if (!res.ok) throw new Error(body.error || body.message || res.statusText);
        return body;
    }

    async function _postJson(url, payload, method) {
        const res = await fetch(url, {
            method: method || "POST",
            headers: { "Content-Type": "application/json" },
            body: payload === undefined ? undefined : JSON.stringify(payload),
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
            const err = new Error(body.error || body.message || (body.errors || []).join("; ") || res.statusText);
            err.body = body;
            err.status = res.status;
            throw err;
        }
        return body;
    }

    // ---------- candidates ----------

    async function loadCandidates() {
        try {
            const body = await _getJson(CAND);
            st.candidates = body.candidates || [];
            renderCandidates();
            renderArmPicker();
        } catch (e) {
            _status(`Failed to load candidates: ${e.message}`, true);
        }
    }

    function renderCandidates() {
        const tbody = document.getElementById("abe-cand-tbody");
        if (!tbody) return;
        if (!st.candidates.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-sm" style="padding: 10px 8px;' +
                ' color: var(--color-text-muted);">No candidates yet — save the live contract as a' +
                " candidate, upload a TOML, or edit one in the form editor.</td></tr>";
            return;
        }
        const cell = "padding: 6px 8px; border-bottom: 1px solid var(--color-border); vertical-align: top;";
        tbody.innerHTML = st.candidates.map(m => `<tr>
            <td style="${cell}" class="font-mono font-semibold">${_esc(m.name)}</td>
            <td style="${cell}" class="font-mono text-xs">${_esc(m.candidate_version || "—")}</td>
            <td style="${cell}">${_esc(m.n_fields ?? "—")}</td>
            <td style="${cell}" class="text-xs">${_esc((m.created_at || "").replace("T", " "))}<br>
                <span style="color: var(--color-text-muted);">${_esc(m.created_by || "")}</span></td>
            <td style="${cell}" class="text-xs">${_esc(m.note || "")}</td>
            <td style="${cell}; white-space: nowrap;">
                <button class="btn-discreet text-xs abe-view" data-n="${_esc(m.name)}">View</button>
                <button class="btn-discreet text-xs abe-edit" data-n="${_esc(m.name)}">Edit</button>
                <button class="btn-primary text-xs abe-activate" data-n="${_esc(m.name)}">Activate</button>
                <button class="btn-danger text-xs abe-del" data-n="${_esc(m.name)}">✕</button>
            </td>
        </tr>`).join("");
        tbody.querySelectorAll(".abe-view").forEach(b =>
            b.addEventListener("click", () => viewCandidate(b.dataset.n)));
        tbody.querySelectorAll(".abe-edit").forEach(b =>
            b.addEventListener("click", () => editCandidate(b.dataset.n)));
        tbody.querySelectorAll(".abe-activate").forEach(b =>
            b.addEventListener("click", () => activateCandidate(b.dataset.n)));
        tbody.querySelectorAll(".abe-del").forEach(b =>
            b.addEventListener("click", () => deleteCandidate(b.dataset.n, b)));
    }

    // Inline naming row (native prompt() is blocked in embedded browsers, so
    // the two create-candidate flows stage their TOML text here instead).
    const nameFlow = { text: null, overwrite: false };

    function _showNameRow(purpose, defaultName) {
        nameFlow.overwrite = false;
        const row = document.getElementById("abe-name-row");
        const purposeEl = document.getElementById("abe-name-purpose");
        const input = document.getElementById("abe-name-input");
        const err = document.getElementById("abe-name-error");
        if (purposeEl) purposeEl.textContent = purpose;
        if (err) err.textContent = "";
        if (row) row.style.display = "flex";
        if (input) {
            input.value = defaultName || "";
            input.focus();
            input.select();
        }
    }

    function abeCancelName() {
        nameFlow.text = null;
        nameFlow.overwrite = false;
        const row = document.getElementById("abe-name-row");
        if (row) row.style.display = "none";
    }

    async function abeConfirmName() {
        const input = document.getElementById("abe-name-input");
        const err = document.getElementById("abe-name-error");
        const name = (input && input.value || "").trim();
        if (!/^[a-z0-9_\-]{1,40}$/.test(name)) {
            if (err) err.textContent = "1-40 chars: lowercase letters, digits, '_' or '-'.";
            return;
        }
        if (!nameFlow.text) { abeCancelName(); return; }
        try {
            await _postJson(CAND, { name, text: nameFlow.text, overwrite: nameFlow.overwrite });
            abeCancelName();
            _status(`Candidate '${name}' saved.`);
            await loadCandidates();
        } catch (e) {
            if (e.status === 409 && !nameFlow.overwrite) {
                // Two-click overwrite: surface it inline, next Save overwrites.
                nameFlow.overwrite = true;
                if (err) err.textContent = `'${name}' already exists — click Save again to overwrite it.`;
            } else {
                nameFlow.overwrite = false;
                if (err) err.textContent = e.message;
            }
        }
    }

    async function abeSaveLiveAsCandidate() {
        try {
            const dl = await fetch(`${AC}/download`);
            if (!dl.ok) throw new Error("could not read the live contract");
            nameFlow.text = await dl.text();
            _showNameRow("Snapshot the live contract as candidate:", "live-snapshot");
        } catch (e) {
            _status(`Save failed: ${e.message}`, true);
        }
    }

    async function abeOnCandidateFile(input) {
        const file = input.files && input.files[0];
        input.value = "";
        if (!file) return;
        try {
            nameFlow.text = await file.text();
            _showNameRow(`Store '${file.name}' as candidate:`,
                file.name.replace(/\.toml$/i, "").toLowerCase()
                    .replace(/[^a-z0-9_\-]/g, "-").slice(0, 40));
        } catch (e) {
            _status(`Upload failed: ${e.message}`, true);
        }
    }

    async function viewCandidate(name) {
        try {
            const body = await _getJson(`${CAND}/${encodeURIComponent(name)}`);
            const modal = document.getElementById("abe-item-modal");
            document.getElementById("abe-item-id").textContent = name + ".toml";
            document.getElementById("abe-item-body").innerHTML =
                `<pre class="text-xs" style="white-space: pre-wrap; background: var(--color-bg-elevated);` +
                ` padding: 12px; border-radius: 6px; max-height: 70vh; overflow: auto;">${_esc(body.text)}</pre>`;
            if (modal) modal.style.display = "flex";
        } catch (e) {
            _status(`View failed: ${e.message}`, true);
        }
    }

    function editCandidate(name) {
        // The Phase-2 form editor supports a candidate save target: it hydrates
        // from the candidate and saves back via POST /api/manage/ab-candidates.
        if (typeof window.aceOpen === "function") {
            window.aceOpen({ candidate: name });
        } else {
            _status("The contract form editor is not loaded on this page.", true);
        }
    }

    // Staged activation: the activate endpoint's dry-run result, awaiting the
    // modal's confirm click.
    let pendingActivate = null;

    async function activateCandidate(name) {
        try {
            const body = await _postJson(`${CAND}/${encodeURIComponent(name)}/activate`);
            const impact = body.impact || {};
            pendingActivate = { name, text: body.text, etag: body.current_etag };

            const rows = [];
            if (impact.metadata_only) {
                rows.push(`<div style="color: var(--color-success); margin-bottom: 10px;">`
                    + `✓ Metadata-only change — <strong>no new annotation version</strong>. `
                    + `Existing annotations stay valid.</div>`);
            } else {
                rows.push(`<div style="color: var(--color-warning); margin-bottom: 10px;">`
                    + `⚠ A new annotation version <span class="font-mono">${_esc(impact.candidate_version)}</span> `
                    + `will be minted on the next annotation run (current: `
                    + `<span class="font-mono">${_esc(impact.current_version)}</span>). `
                    + `It won&rsquo;t become active until promoted under <em>Annotation Versions</em>.</div>`);
            }
            const detail = [];
            detail.push(`Prompt changed: <strong>${impact.prompt_changed ? "yes" : "no"}</strong>`);
            detail.push(`Schema changed: <strong>${impact.schema_changed ? "yes" : "no"}</strong>`);
            if ((impact.fields_added || []).length) {
                detail.push(`Fields added: <span class="font-mono">${impact.fields_added.map(_esc).join(", ")}</span>`);
            }
            if ((impact.fields_removed || []).length) {
                detail.push(`Fields removed: <span class="font-mono">${impact.fields_removed.map(_esc).join(", ")}</span>`);
            }
            const modal = document.getElementById("abe-item-modal");
            document.getElementById("abe-item-id").textContent = `— activate candidate '${name}'`;
            document.getElementById("abe-item-body").innerHTML = rows.join("")
                + '<ul style="margin: 6px 0 0 18px; padding: 0;" class="text-sm">'
                + detail.map(d => `<li>${d}</li>`).join("") + "</ul>"
                + `<div style="display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px;
                        padding-top: 12px; border-top: 1px solid var(--color-border);">
                    <button onclick="abeCloseItemModal()" class="btn-discreet text-sm"
                        style="padding: 6px 12px;">Cancel</button>
                    <button onclick="abeConfirmActivate()" class="btn-save text-sm"
                        style="padding: 6px 14px;">Activate contract</button>
                </div>`;
            if (modal) modal.style.display = "flex";
        } catch (e) {
            _status(`Activate failed: ${e.message}`, true);
        }
    }

    async function abeConfirmActivate() {
        if (!pendingActivate) { abeCloseItemModal(); return; }
        const { name, text, etag } = pendingActivate;
        try {
            const res = await _postJson(AC, { text, confirm: true, expected_etag: etag });
            pendingActivate = null;
            abeCloseItemModal();
            _status(res.note || `Candidate '${name}' activated.`);
            document.dispatchEvent(new CustomEvent("fyp:contract-changed"));
        } catch (e) {
            abeCloseItemModal();
            if (e.status === 409) {
                _status("Rejected: the live contract changed underneath — reload and retry.", true);
            } else {
                _status(`Activate failed: ${e.message}`, true);
            }
        }
    }

    async function deleteCandidate(name, btn) {
        if (!_armTwoClick(btn, "sure?")) return;
        try {
            await _postJson(`${CAND}/${encodeURIComponent(name)}`, undefined, "DELETE");
            _status(`Candidate '${name}' deleted.`);
            await loadCandidates();
        } catch (e) {
            _status(`Delete failed: ${e.message}`, true);
        }
    }

    // ---------- eval set ----------

    async function loadEvalSet() {
        try {
            const body = await _getJson(EVALSET);
            st.evalSet = body;
            st.setDirty = false;
            renderEvalSet();
        } catch (e) {
            _status(`Failed to load eval set: ${e.message}`, true);
        }
    }

    function renderEvalSet() {
        const pills = document.getElementById("abe-set-pills");
        const count = document.getElementById("abe-set-count");
        const save = document.getElementById("abe-set-save");
        const ids = st.evalSet.item_ids || [];
        const resolved = {};
        (st.evalSet.resolved || []).forEach(r => { resolved[r.item_id] = r; });
        if (count) count.textContent = `${ids.length} / ${st.evalSet.max_items || 50} videos` +
            (st.setDirty ? " — unsaved" : "");
        if (save) save.disabled = !st.setDirty;
        if (!pills) return;
        pills.innerHTML = "";
        if (!ids.length) {
            pills.innerHTML = '<span class="text-xs" style="color: var(--color-text-muted);">' +
                "The set is empty — add ids or sample below.</span>";
            return;
        }
        for (const id of ids) {
            const r = resolved[id];   // undefined until the id is saved/resolved
            const pill = document.createElement("span");
            pill.className = "text-xs";
            pill.style.cssText = "display: inline-flex; align-items: center; gap: 5px;" +
                " padding: 2px 4px 2px 10px; border-radius: 12px;" +
                " background: var(--color-border); color: var(--color-text-primary);";
            const label = document.createElement("span");
            label.className = "font-mono";
            label.textContent = id;
            const meta = document.createElement("span");
            meta.className = "text-xxs";
            const unknown = r && r.platform == null && r.downloaded == null;
            meta.style.color = unknown ? "var(--color-danger)"
                : (r && r.downloaded === false) ? "var(--color-warning)" : "var(--color-text-muted)";
            meta.textContent = !r ? "new — save to check"
                : unknown ? "not found!"
                : (r.platform || "?") + (r.downloaded === false ? " · no media!" : "");
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "btn-discreet text-xs";
            remove.style.cssText = "border: none; padding: 0 6px; cursor: pointer; line-height: 1;";
            remove.textContent = "×";
            remove.onclick = () => {
                st.evalSet.item_ids = st.evalSet.item_ids.filter(x => x !== id);
                st.setDirty = true;
                renderEvalSet();
                refreshEstimate();
            };
            pill.appendChild(label);
            pill.appendChild(meta);
            pill.appendChild(remove);
            pills.appendChild(pill);
        }
    }

    function abeAddIds() {
        const input = document.getElementById("abe-add-ids");
        if (!input || !input.value.trim()) return;
        const raw = input.value.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean);
        const ids = st.evalSet.item_ids || [];
        let added = 0;
        for (const id of raw) {
            if (!ids.includes(id)) { ids.push(id); added++; }
        }
        input.value = "";
        if (added) {
            st.evalSet.item_ids = ids;
            st.setDirty = true;
            renderEvalSet();
            refreshEstimate();
        }
    }

    async function abeSample() {
        const n = parseInt((document.getElementById("abe-sample-n") || {}).value, 10) || 10;
        const platform = (document.getElementById("abe-sample-platform") || {}).value;
        const btn = document.getElementById("abe-sample-btn");
        if (btn) { btn.disabled = true; btn.textContent = "Sampling…"; }
        _status("Sampling…");
        try {
            const body = await _postJson(`${EVALSET}/sample`,
                { n, platforms: platform ? [platform] : undefined });
            const ids = st.evalSet.item_ids || [];
            let added = 0;
            for (const r of body.resolved || []) {
                if (!ids.includes(r.item_id)) {
                    ids.push(r.item_id);
                    (st.evalSet.resolved = st.evalSet.resolved || []).push(r);
                    added++;
                }
            }
            st.evalSet.item_ids = ids;
            if (added) st.setDirty = true;
            _status(added ? `Added ${added} sampled video(s) — review and Save.`
                : "Sample returned nothing new.");
            renderEvalSet();
            refreshEstimate();
        } catch (e) {
            _status(`Sample failed: ${e.message}`, true);
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = "Sample"; }
        }
    }

    async function abeSaveEvalSet() {
        const btn = document.getElementById("abe-set-save");
        if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
        _status("Saving eval set…");
        try {
            const body = await _postJson(EVALSET, { item_ids: st.evalSet.item_ids || [] });
            st.evalSet = { ...st.evalSet, ...body };
            st.setDirty = false;
            const notDownloaded = (body.not_downloaded || []).length;
            const unknown = (body.resolved || []).filter(r => r.platform == null && r.downloaded == null)
                .map(r => r.item_id);
            const warnings = [];
            if (unknown.length) warnings.push(`${unknown.length} id(s) not found in the dataset: ${unknown.join(", ")}`);
            if (notDownloaded) warnings.push(`${notDownloaded} item(s) have no downloaded media`);
            _status(warnings.length ? `Saved — warning: ${warnings.join("; ")}.` : "Eval set saved.", warnings.length > 0);
            renderEvalSet();
            refreshEstimate();
        } catch (e) {
            _status(`Save failed: ${e.message}`, true);
        } finally {
            if (btn) { btn.textContent = "Save set"; btn.disabled = !st.setDirty; }
        }
    }

    // ---------- run ----------

    function renderArmPicker() {
        const el = document.getElementById("abe-arm-picker");
        if (!el) return;
        const rows = st.candidates.map(m =>
            `<label class="text-sm" style="display: flex; align-items: center; gap: 6px;">
                <input type="checkbox" class="abe-arm" value="${_esc(m.name)}"
                    onchange="abeRefreshEstimate()">
                <span class="font-mono">${_esc(m.name)}</span>
            </label>`);
        rows.push(`<label class="text-sm" style="display: flex; align-items: center; gap: 6px;">
            <input type="checkbox" class="abe-arm" value="__live__" checked onchange="abeRefreshEstimate()">
            <span>live contract</span>
        </label>`);
        el.innerHTML = rows.join("");
        refreshEstimate();
    }

    function _selectedArms() {
        const boxes = document.querySelectorAll(".abe-arm:checked");
        const names = [];
        let live = false;
        boxes.forEach(b => {
            if (b.value === "__live__") live = true;
            else names.push(b.value);
        });
        return { names, live };
    }

    function refreshEstimate() {
        const el = document.getElementById("abe-estimate");
        if (!el) return;
        const { names, live } = _selectedArms();
        const nArms = names.length + (live ? 1 : 0);
        const nItems = (st.evalSet.item_ids || []).length;
        const unsaved = st.setDirty ? " (using the SAVED set — you have unsaved set edits)" : "";
        el.textContent = nArms && nItems
            ? `${nArms} arm(s) × ${nItems} video(s) = ${nArms * nItems} Gemini calls${unsaved}`
            : "Select at least one arm and curate a non-empty eval set.";
    }

    async function abeStartRun() {
        const { names, live } = _selectedArms();
        if (!names.length && !live) { _status("Select at least one arm.", true); return; }
        const runBtn = document.getElementById("abe-run-btn");
        try {
            // First click: fetch the authoritative estimate (visible feedback
            // while it loads), then arm the button with the real call count.
            if (!runBtn || runBtn.dataset.armed !== "1") {
                if (runBtn) { runBtn.disabled = true; runBtn.textContent = "Checking cost…"; }
                _status("Checking run cost…");
                let est;
                try {
                    est = await _postJson(`${EVAL}/estimate`, { candidate_names: names, include_live: live });
                } finally {
                    if (runBtn) { runBtn.disabled = false; runBtn.textContent = "Run…"; }
                }
                if (!est.n_items) { _status("The saved eval set is empty — save it first.", true); return; }
                _armTwoClick(runBtn, `Confirm: ${est.n_calls} Gemini calls?`);
                _status(st.setDirty
                    ? "Note: the run uses the last SAVED set — you have unsaved set edits. Click again to start."
                    : "Click again to start the run.", st.setDirty);
                return;
            }
            // Second click (armed): lock the button through the whole start
            // request so a double-click can never dispatch two runs.
            _armTwoClick(runBtn, "");
            _setRunning(true);
            _status("Starting run…");
            const body = await _postJson(`${EVAL}/run`, { candidate_names: names, include_live: live });
            _status(`Run ${body.run_id} started.`);
            _pollRun(body.run_id);
        } catch (e) {
            _setRunning(false);
            if (e.status === 409) {
                _status(`Not started: ${e.message}`, true);
                _pollRun("");   // a run is already in flight — follow it instead
                _setRunning(true);
            } else {
                _status(`Start failed: ${e.message}`, true);
            }
        }
    }

    function _setRunning(running) {
        const runBtn = document.getElementById("abe-run-btn");
        const cancelBtn = document.getElementById("abe-cancel-btn");
        if (runBtn) runBtn.disabled = running;
        if (cancelBtn) cancelBtn.style.display = running ? "inline-block" : "none";
    }

    function _pollRun(runId) {
        if (st.pollTimer) clearInterval(st.pollTimer);
        const progress = document.getElementById("abe-run-progress");
        let sawRunning = false;
        st.pollTimer = setInterval(async () => {
            try {
                const res = await fetch("/api/status");
                if (!res.ok) return;
                const entry = (await res.json()).ab_eval;
                if (!entry) return;
                if (entry.state === "running") {
                    sawRunning = true;
                    const pct = (entry.progress && entry.progress.percent != null) ? entry.progress.percent : 0;
                    const msg = (entry.progress && entry.progress.message) || "Working…";
                    if (progress) progress.textContent = `${pct}% — ${msg}`;
                    return;
                }
                if (!sawRunning) return;   // still spinning up
                clearInterval(st.pollTimer);
                st.pollTimer = null;
                _setRunning(false);
                if (progress) progress.textContent = "";
                if (entry.last_run_outcome === "Fail") {
                    _status(`Run failed: ${entry.last_message || "see logs"}`, true);
                } else {
                    _status(`Run ${runId} finished.`);
                }
                await abeRefreshRuns();
                const picker = document.getElementById("abe-run-picker");
                if (picker && [...picker.options].some(o => o.value === runId)) {
                    picker.value = runId;
                    abeLoadRun(runId);
                }
            } catch (e) { /* transient poll error — keep polling */ }
        }, 3000);
    }

    async function abeCancelRun() {
        try {
            await _postJson("/api/stop/ab_eval", {});
            _status("Cancel requested — the run stops after the in-flight items.");
        } catch (e) {
            _status(`Cancel failed: ${e.message}`, true);
        }
    }

    // ---------- results ----------

    async function abeRefreshRuns() {
        try {
            const body = await _getJson(`${EVAL}/runs`);
            st.runs = body.runs || [];
            const picker = document.getElementById("abe-run-picker");
            if (picker) {
                picker.innerHTML = st.runs.length
                    ? st.runs.map(r =>
                        `<option value="${_esc(r.run_id)}">${_esc(r.run_id)} · ${_esc((r.arms || []).join(" vs "))} · ${_esc(r.status)}</option>`).join("")
                    : '<option value="">(no runs yet)</option>';
            }
        } catch (e) {
            _status(`Failed to list runs: ${e.message}`, true);
        }
    }

    async function abeLoadRun(runId) {
        const container = document.getElementById("abe-results");
        const delBtn = document.getElementById("abe-run-delete");
        st.currentRunId = runId || null;
        st.currentRun = null;
        st.rowsCache = {};
        if (delBtn) delBtn.style.display = runId ? "inline-block" : "none";
        if (!runId || !container) { if (container) container.innerHTML = ""; return; }
        container.innerHTML = '<span class="text-sm" style="color: var(--color-text-muted);">Loading…</span>';
        try {
            st.currentRun = await _getJson(`${EVAL}/runs/${encodeURIComponent(runId)}`);
            renderRun();
        } catch (e) {
            container.innerHTML = `<span class="text-sm" style="color: var(--color-danger);">${_esc(e.message)}</span>`;
        }
    }

    async function abeDeleteRun() {
        if (!st.currentRunId) return;
        if (!_armTwoClick(document.getElementById("abe-run-delete"), "Delete — sure?")) return;
        try {
            await _postJson(`${EVAL}/runs/${encodeURIComponent(st.currentRunId)}`, undefined, "DELETE");
            _status("Run deleted.");
            await abeRefreshRuns();
            const picker = document.getElementById("abe-run-picker");
            abeLoadRun(picker ? picker.value : "");
        } catch (e) {
            _status(`Delete failed: ${e.message}`, true);
        }
    }

    function _fmt(x, digits) {
        if (x == null || Number.isNaN(x)) return "—";
        return Number(x).toFixed(digits === undefined ? 2 : digits);
    }

    function _metricOf(colMeta) {
        if (colMeta.kind === "numeric") return colMeta.correlation;
        if (colMeta.kind === "enum") return colMeta.agreement;
        if (colMeta.kind === "list") return colMeta.mean_jaccard;
        return null;
    }

    function renderRun() {
        const container = document.getElementById("abe-results");
        if (!container || !st.currentRun) return;
        const manifest = st.currentRun.manifest || {};
        const report = st.currentRun.report;
        const arms = (manifest.arms || []).map(a => a.name);

        let html = `<div class="text-xs" style="color: var(--color-text-muted); margin-bottom: 10px;">
            ${_esc(manifest.run_id)} · started ${_esc((manifest.started_at || "").replace("T", " "))}
            by ${_esc(manifest.started_by || "?")} · status
            <strong style="color: ${manifest.status === "complete" ? "var(--color-success)"
                : manifest.status === "failed" ? "var(--color-danger)" : "var(--color-warning)"};">
            ${_esc(manifest.status)}</strong>
            ${manifest.error ? ` · ${_esc(manifest.error)}` : ""}</div>`;

        if (!report) {
            container.innerHTML = html +
                '<div class="text-sm" style="color: var(--color-text-muted);">No report (run incomplete).</div>';
            return;
        }

        // Headline tiles: per-arm costs.
        html += '<div style="display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 14px;">';
        for (const arm of report.arms || arms) {
            const c = (report.costs || {})[arm] || {};
            const meta = (manifest.arms || []).find(a => a.name === arm) || {};
            const promotable = meta.source === "candidate";
            html += `<div style="border: 1px solid var(--color-border); border-radius: 6px;
                    padding: 10px 14px; min-width: 190px;">
                <div class="font-semibold font-mono">${_esc(arm)}</div>
                <div class="text-xs" style="color: var(--color-text-muted); margin-top: 4px;">
                    ${_esc(String(c.total_tokens ?? "—"))} tokens ·
                    ${_esc(String(c.n_errors ?? "—"))} errors ·
                    ${_fmt(c.mean_inference_duration, 1)}s mean</div>
                ${promotable
                    ? `<button class="btn-primary text-xs abe-promote" data-n="${_esc(arm)}"
                        style="margin-top: 6px;">Promote (activate)</button>`
                    : `<span class="text-xxs" style="color: var(--color-text-muted);">live contract</span>`}
            </div>`;
        }
        html += "</div>";

        // Pairwise aggregate table (pair selector when >1 pair).
        const pairs = Object.keys(report.comparisons || {});
        if (pairs.length) {
            const pairSel = pairs.length > 1
                ? `<select id="abe-pair-picker" class="text-sm" onchange="abeRenderPair(this.value)"
                    style="padding: 4px 8px; border: 1px solid var(--color-border); border-radius: 4px;
                    background: var(--color-bg-input); color: var(--color-text-primary);">
                    ${pairs.map(p => `<option value="${_esc(p)}">${_esc(p.replace("|", " vs "))}</option>`).join("")}
                   </select>`
                : `<span class="font-semibold">${_esc(pairs[0].replace("|", " vs "))}</span>`;
            html += `<div style="margin-bottom: 6px;" class="text-sm">Aggregate metrics: ${pairSel}</div>
                <div id="abe-pair-table" style="overflow-x: auto; margin-bottom: 14px;"></div>`;
        }

        // Adjudication (per-item disagreements).
        const adj = report.adjudication || [];
        const adjCols = [...new Set(adj.map(r => r.column))].sort();
        html += `<div class="text-sm" style="margin-bottom: 6px;">
            Disagreements (${adj.length}${adj.length >= 3000 ? "+, capped" : ""}) —
            <select id="abe-adj-filter" class="text-xs" onchange="abeRenderAdjudication()"
                style="padding: 3px 6px; border: 1px solid var(--color-border); border-radius: 4px;
                background: var(--color-bg-input); color: var(--color-text-primary);">
                <option value="">all columns</option>
                ${adjCols.map(c => `<option value="${_esc(c)}">${_esc(c)}</option>`).join("")}
            </select>
            <span class="text-xxs" style="color: var(--color-text-muted);">— click an item id for the full field-by-field view</span>
        </div>
        <div id="abe-adj-table" style="overflow-x: auto; max-height: 50vh; overflow-y: auto;"></div>`;

        container.innerHTML = html;
        container.querySelectorAll(".abe-promote").forEach(b =>
            b.addEventListener("click", () => activateCandidate(b.dataset.n)));
        if (pairs.length) abeRenderPair(pairs[0]);
        abeRenderAdjudication();
    }

    function abeRenderPair(pairKey) {
        const el = document.getElementById("abe-pair-table");
        const report = st.currentRun && st.currentRun.report;
        if (!el || !report) return;
        const comp = report.comparisons[pairKey];
        if (!comp) return;
        const [armA, armB] = pairKey.split("|");
        const cols = Object.entries(comp.columns || {})
            .sort((x, y) => {
                const mx = _metricOf(x[1]), my = _metricOf(y[1]);
                return (mx == null ? 2 : mx) - (my == null ? 2 : my);   // lowest agreement first
            });
        const cell = "padding: 4px 8px; border-bottom: 1px solid var(--color-border);";
        const dist = report.distributions || {};
        const rows = cols.map(([name, m]) => {
            const metric = m.kind === "numeric" ? `corr ${_fmt(m.correlation)} · MAD ${_fmt(m.mean_abs_diff, 1)}`
                : m.kind === "enum" ? `agreement ${_fmt(m.agreement)}`
                : m.kind === "list" ? `jaccard ${_fmt(m.mean_jaccard)}`
                : "—";
            let distHtml = "";
            const d = dist[name];
            if (d && d.arms) {
                const inner = Object.entries(d.arms).map(([arm, counts]) =>
                    `<div style="min-width: 160px;"><span class="font-mono font-semibold">${_esc(arm)}</span><br>`
                    + Object.entries(counts).map(([v, n]) => `${_esc(v)}: ${n}`).join("<br>") + "</div>"
                ).join("");
                distHtml = `<details><summary class="text-xxs" style="cursor: pointer;
                    color: var(--color-text-muted);">values</summary>
                    <div style="display: flex; gap: 16px; padding: 6px 0;" class="text-xxs">${inner}</div></details>`;
            }
            return `<tr>
                <td style="${cell}" class="font-mono">${_esc(name)}</td>
                <td style="${cell}">${_esc(m.kind)}</td>
                <td style="${cell}">${metric}</td>
                <td style="${cell}">${_fmt(m.coverage_a)}</td>
                <td style="${cell}">${_fmt(m.coverage_b)}</td>
                <td style="${cell}">${distHtml}</td>
            </tr>`;
        }).join("");
        el.innerHTML = `<table style="border-collapse: collapse; width: 100%;" class="text-xs">
            <thead><tr class="text-xxs" style="text-align: left; color: var(--color-text-muted);">
                <th style="${cell}">column</th><th style="${cell}">kind</th>
                <th style="${cell}">metric (lowest first)</th>
                <th style="${cell}">coverage ${_esc(armA)}</th>
                <th style="${cell}">coverage ${_esc(armB)}</th>
                <th style="${cell}"></th>
            </tr></thead><tbody>${rows}</tbody></table>`;
    }

    function abeRenderAdjudication() {
        const el = document.getElementById("abe-adj-table");
        const report = st.currentRun && st.currentRun.report;
        if (!el || !report) return;
        const filter = (document.getElementById("abe-adj-filter") || {}).value || "";
        const arms = report.arms || [];
        const rows = (report.adjudication || []).filter(r => !filter || r.column === filter);
        if (!rows.length) {
            el.innerHTML = '<span class="text-xs" style="color: var(--color-text-muted);">No disagreements 🎉</span>';
            return;
        }
        const cell = "padding: 4px 8px; border-bottom: 1px solid var(--color-border); vertical-align: top;";
        el.innerHTML = `<table style="border-collapse: collapse; width: 100%;" class="text-xs">
            <thead><tr class="text-xxs" style="text-align: left; color: var(--color-text-muted);">
                <th style="${cell}">item</th><th style="${cell}">column</th>
                ${arms.map(a => `<th style="${cell}" class="font-mono">${_esc(a)}</th>`).join("")}
            </tr></thead>
            <tbody>${rows.slice(0, 500).map(r => `<tr>
                <td style="${cell}"><a href="#" class="abe-item font-mono" data-i="${_esc(r.item_id)}"
                    style="color: var(--color-accent);">${_esc(r.item_id)}</a></td>
                <td style="${cell}" class="font-mono">${_esc(r.column)}</td>
                ${arms.map(a => `<td style="${cell}">${_esc((r.values || {})[a] ?? "")}</td>`).join("")}
            </tr>`).join("")}</tbody></table>`
            + (rows.length > 500 ? `<div class="text-xxs" style="color: var(--color-text-muted); padding: 6px 0;">Showing 500 of ${rows.length} — filter by column to narrow.</div>` : "");
        el.querySelectorAll(".abe-item").forEach(a =>
            a.addEventListener("click", (ev) => { ev.preventDefault(); openItemView(a.dataset.i); }));
    }

    async function _armRows(arm) {
        if (!st.rowsCache[arm]) {
            const body = await _getJson(
                `${EVAL}/runs/${encodeURIComponent(st.currentRunId)}/rows?arm=${encodeURIComponent(arm)}`);
            st.rowsCache[arm] = body.rows || [];
        }
        return st.rowsCache[arm];
    }

    async function openItemView(itemId) {
        const modal = document.getElementById("abe-item-modal");
        const body = document.getElementById("abe-item-body");
        const report = st.currentRun && st.currentRun.report;
        if (!modal || !body || !report) return;
        document.getElementById("abe-item-id").textContent = itemId;
        body.innerHTML = '<span style="color: var(--color-text-muted);">Loading…</span>';
        modal.style.display = "flex";
        try {
            const arms = report.arms || [];
            const rowByArm = {};
            for (const arm of arms) {
                const rows = await _armRows(arm);
                rowByArm[arm] = rows.find(r => String(r.item_id) === String(itemId)) || {};
            }
            const columns = [...new Set(arms.flatMap(a => Object.keys(rowByArm[a])))]
                .filter(c => c !== "item_id").sort();
            const cell = "padding: 4px 8px; border-bottom: 1px solid var(--color-border); vertical-align: top;";
            body.innerHTML = `<table style="border-collapse: collapse; width: 100%;">
                <thead><tr class="text-xxs" style="text-align: left; color: var(--color-text-muted);">
                    <th style="${cell}">field</th>
                    ${arms.map(a => `<th style="${cell}" class="font-mono">${_esc(a)}</th>`).join("")}
                </tr></thead>
                <tbody>${columns.map(c => {
                    const values = arms.map(a => String(rowByArm[a][c] ?? ""));
                    const differs = new Set(values).size > 1;
                    return `<tr ${differs ? 'style="background: color-mix(in srgb, var(--color-warning) 12%, transparent);"' : ""}>
                        <td style="${cell}" class="font-mono">${_esc(c)}</td>
                        ${values.map(v => `<td style="${cell}">${_esc(v)}</td>`).join("")}
                    </tr>`;
                }).join("")}</tbody></table>`;
        } catch (e) {
            body.innerHTML = `<span style="color: var(--color-danger);">${_esc(e.message)}</span>`;
        }
    }

    function abeCloseItemModal() {
        const modal = document.getElementById("abe-item-modal");
        if (modal) modal.style.display = "none";
    }

    // ---------- bootstrap ----------

    let bootstrapped = false;

    function _maybeBootstrap() {
        const page = document.getElementById("admin-page-abeval");
        if (!page || bootstrapped || !page.classList.contains("active")) return;
        bootstrapped = true;
        loadCandidates();
        loadEvalSet().then(refreshEstimate);
        abeRefreshRuns();
        // Resume progress display if a run is already in flight.
        fetch("/api/status").then(r => r.ok ? r.json() : null).then(s => {
            if (s && s.ab_eval && s.ab_eval.state === "running") {
                _setRunning(true);
                _pollRun((s.ab_eval.data || {}).run_id || "");
            }
        }).catch(() => {});
        // Populate the sample-platform select from the resolved set + defaults.
        const sel = document.getElementById("abe-sample-platform");
        if (sel) {
            for (const p of ["tiktok", "instagram", "youtube"]) {
                const opt = document.createElement("option");
                opt.value = p;
                opt.textContent = p;
                sel.appendChild(opt);
            }
        }
    }

    function _watchForActivation() {
        const page = document.getElementById("admin-page-abeval");
        if (!page) return;
        const observer = new MutationObserver(_maybeBootstrap);
        observer.observe(page, { attributes: true, attributeFilter: ["class"] });
        _maybeBootstrap();
    }

    // Candidate list can change from the form editor's save-as-candidate path.
    document.addEventListener("fyp:candidates-changed", function () {
        if (bootstrapped) loadCandidates();
    });

    window.abeSaveLiveAsCandidate = abeSaveLiveAsCandidate;
    window.abeOnCandidateFile = abeOnCandidateFile;
    window.abeConfirmName = abeConfirmName;
    window.abeCancelName = abeCancelName;
    window.abeConfirmActivate = abeConfirmActivate;
    window.abeAddIds = abeAddIds;
    window.abeSample = abeSample;
    window.abeSaveEvalSet = abeSaveEvalSet;
    window.abeStartRun = abeStartRun;
    window.abeCancelRun = abeCancelRun;
    window.abeRefreshRuns = abeRefreshRuns;
    window.abeLoadRun = abeLoadRun;
    window.abeDeleteRun = abeDeleteRun;
    window.abeRenderPair = abeRenderPair;
    window.abeRenderAdjudication = abeRenderAdjudication;
    window.abeCloseItemModal = abeCloseItemModal;
    window.abeRefreshEstimate = refreshEstimate;

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", _watchForActivation);
    } else {
        _watchForActivation();
    }
})();
