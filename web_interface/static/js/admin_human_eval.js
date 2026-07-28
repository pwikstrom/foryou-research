/**
 * Admin "Human testing" sub-page.
 *
 * Sets up human tasks on finished annotation test runs (/api/manage/human-eval
 * endpoints): pick a complete run, select the variables coders provide input
 * for, pick coders to invite, then monitor per-coder progress, recompute
 * results, or delete the task. The global fetch wrapper in main.js injects the
 * CSRF header.
 */
(function () {
    "use strict";

    const BASE = "/api/manage/human-eval";

    const st = {
        runs: [],           // finished runs (+ human_tasks flags)
        variables: [],      // available variables of the selected run
        users: [],          // approved user roster
        tasks: [],          // global task index
    };

    function _esc(v) {
        const div = document.createElement("div");
        div.textContent = v == null ? "" : String(v);
        return div.innerHTML;
    }

    function _status(msg, isError) {
        const el = document.getElementById("hev-status");
        if (!el) return;
        el.textContent = msg || "";
        el.style.color = isError ? "var(--color-danger)" : "var(--color-text-muted)";
    }

    // Two-click confirmation for destructive buttons (native confirm() is
    // blocked in embedded preview browsers) — same pattern as admin_ab_eval.js.
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

    // Immediate feedback for async buttons: disable + relabel for the whole
    // request, restore in finally. `btn` may be null (falls through to fn).
    async function _busy(btn, busyLabel, fn) {
        if (!btn) return fn();
        const prev = btn.textContent;
        btn.disabled = true;
        btn.textContent = busyLabel;
        try {
            return await fn();
        } finally {
            btn.disabled = false;
            btn.textContent = prev;
        }
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
        if (!res.ok) throw new Error(body.error || body.message || res.statusText);
        return body;
    }

    // ---------- setup form ----------

    async function loadRuns() {
        try {
            const body = await _getJson(`${BASE}/runs`);
            st.runs = body.runs || [];
            const sel = document.getElementById("hev-run-select");
            if (!sel) return;
            sel.innerHTML = '<option value="">— pick a finished run —</option>' +
                st.runs.map(r => {
                    const arms = (r.arms || []).join(" vs ");
                    const tag = (r.human_tasks || []).length ? " · has human task" : "";
                    const label = r.name ? `${r.name} · ` : "";
                    return `<option value="${_esc(r.run_id)}">${_esc(label)}${_esc(r.run_id)} — ${_esc(arms)}` +
                        ` (${r.n_items} items)${tag}</option>`;
                }).join("");
        } catch (e) {
            _status(`Could not load runs: ${e.message}`, true);
        }
    }

    async function loadUsers() {
        try {
            const body = await _getJson(`${BASE}/users`);
            st.users = body.users || [];
            const box = document.getElementById("hev-user-list");
            if (!box) return;
            box.innerHTML = st.users.map(u => {
                const badge = u.is_admin
                    ? ' <span class="text-xxs" style="color: var(--color-text-muted);">(admin — always has access)</span>'
                    : "";
                return `<label style="display: flex; align-items: center; gap: 6px; padding: 2px 0;">
                    <input type="checkbox" class="hev-user-cb" value="${_esc(u.username)}"
                        ${u.is_admin ? "checked" : ""}>
                    <span>${_esc(u.username)}${badge}</span>
                </label>`;
            }).join("") || '<span style="color: var(--color-text-muted);">No approved users.</span>';
        } catch (e) {
            _status(`Could not load users: ${e.message}`, true);
        }
    }

    async function hevOnRunChange() {
        const sel = document.getElementById("hev-run-select");
        const box = document.getElementById("hev-var-list");
        if (!sel || !box) return;
        if (!sel.value) {
            box.innerHTML = '<span style="color: var(--color-text-muted);">Select a run first.</span>';
            return;
        }
        box.innerHTML = '<span style="color: var(--color-text-muted);">Loading variables…</span>';
        sel.disabled = true;
        try {
            const body = await _getJson(`${BASE}/runs/${encodeURIComponent(sel.value)}/variables`);
            st.variables = body.variables || [];
            box.innerHTML = st.variables.map(v => {
                const values = v.values && v.values.length
                    ? ` · ${v.values.length} values` : "";
                return `<label style="display: flex; align-items: center; gap: 6px; padding: 2px 0;">
                    <input type="checkbox" class="hev-var-cb" value="${_esc(v.name)}">
                    <span class="meta-tooltip" data-tooltip="${_esc(v.description || v.name)}">
                        ${_esc(v.label)}
                        <span class="text-xxs" style="color: var(--color-text-muted);">
                            (${_esc(v.kind)}${values})</span>
                    </span>
                </label>`;
            }).join("") || '<span style="color: var(--color-text-muted);">No compared variables on this run.</span>';
            const master = document.getElementById("hev-var-check-all");
            if (master) master.checked = false;
            hevOnTypeChange();
        } catch (e) {
            box.innerHTML = `<span style="color: var(--color-danger);">${_esc(e.message)}</span>`;
        } finally {
            sel.disabled = false;
        }
    }

    function _selectedTaskType() {
        const radio = document.querySelector('input[name="hev-task-type"]:checked');
        return radio ? radio.value : "coding";
    }

    // Vote tasks show admin-selected fields side-by-side, defaulting to all;
    // coding tasks keep whatever the admin last selected. Vote tasks also get
    // the contracts-to-vote-between row (all of the run's arms, all checked).
    function hevOnTypeChange() {
        const type = _selectedTaskType();
        const title = document.getElementById("hev-var-title");
        if (title) {
            title.textContent = type === "vote"
                ? "Fields shown to voters (side-by-side, blind)"
                : "Variables the coders provide input for";
        }
        if (type === "vote") {
            document.querySelectorAll(".hev-var-cb").forEach(c => { c.checked = true; });
            const master = document.getElementById("hev-var-check-all");
            if (master) master.checked = true;
        }
        const armRow = document.getElementById("hev-arm-row");
        if (armRow) armRow.style.display = type === "vote" ? "" : "none";
        if (type === "vote") _renderArmList();
    }

    function _renderArmList() {
        const box = document.getElementById("hev-arm-list");
        if (!box) return;
        const runId = (document.getElementById("hev-run-select") || {}).value;
        const run = st.runs.find(r => r.run_id === runId);
        const arms = (run && run.arms) || [];
        box.innerHTML = arms.length
            ? arms.map(a => `<label style="display: flex; align-items: center; gap: 6px;">
                <input type="checkbox" class="hev-arm-cb" value="${_esc(a)}" checked>
                <span class="font-mono">${_esc(a)}</span>
            </label>`).join("")
            : '<span style="color: var(--color-text-muted);">Select a run first.</span>';
    }

    async function hevCreateTask(btn) {
        const errEl = document.getElementById("hev-create-error");
        errEl.textContent = "";
        const runId = (document.getElementById("hev-run-select") || {}).value;
        const variables = Array.from(document.querySelectorAll(".hev-var-cb:checked")).map(c => c.value);
        const coders = Array.from(document.querySelectorAll(".hev-user-cb:checked")).map(c => c.value);
        const taskType = _selectedTaskType();
        if (!runId) { errEl.textContent = "Pick a finished run."; return; }
        if (!variables.length) { errEl.textContent = "Select at least one variable."; return; }
        const arms = taskType === "vote"
            ? Array.from(document.querySelectorAll(".hev-arm-cb:checked")).map(c => c.value)
            : undefined;
        if (taskType === "vote" && (!arms || arms.length < 2)) {
            errEl.textContent = "Select at least two contracts to vote between.";
            return;
        }
        try {
            await _busy(btn, "Creating…", async () => {
                await _postJson(`${BASE}/tasks`, {
                    run_id: runId, task_type: taskType, variables, coders, arms,
                });
                _status(`${taskType === "vote" ? "Vote" : "Coding"} task created on ${runId}.`);
                await Promise.all([hevRefreshTasks(), loadRuns()]);
            });
        } catch (e) {
            errEl.textContent = e.message;
        }
    }

    // ---------- existing tasks ----------

    const STATUS_LABEL = {
        invited: "invited", in_progress: "in progress", submitted: "submitted",
    };

    function _runName(runId) {
        const run = st.runs.find(r => r.run_id === runId);
        return (run && run.name) || "";
    }

    async function hevRefreshTasks() {
        const box = document.getElementById("hev-task-list");
        if (!box) return;
        try {
            const body = await _getJson(`${BASE}/tasks`);
            st.tasks = body.tasks || [];
            if (!st.tasks.length) {
                box.innerHTML = '<span class="text-sm" style="color: var(--color-text-muted);">No human tasks yet.</span>';
                return;
            }
            const cards = await Promise.all(st.tasks.map(async t => {
                let detail = null;
                try {
                    detail = await _getJson(
                        `${BASE}/tasks/${encodeURIComponent(t.run_id)}/${encodeURIComponent(t.task_type)}`);
                } catch (e) { /* card renders from the index entry alone */ }
                return _taskCard(t, detail);
            }));
            box.innerHTML = cards.join("");
        } catch (e) {
            box.innerHTML = `<span class="text-sm" style="color: var(--color-danger);">${_esc(e.message)}</span>`;
        }
    }

    function _taskCard(t, detail) {
        const status = (detail || {}).coder_status || {};
        const coderMeta = ((detail || {}).task || {}).coders || {};
        const nItems = t.n_items || 0;
        const cardKey = `${t.run_id}::${t.task_type}`;
        const coderRows = Object.entries(status).map(([user, s]) => {
            const label = STATUS_LABEL[s.status] || s.status;
            const progress = s.status === "invited" ? "" : ` · ${s.n_answered}/${nItems} items`;
            const notified = (coderMeta[user] || {}).notified;
            const mailBadge = notified
                ? `<span class="text-xxs" style="color: var(--color-text-muted);">✉ sent</span>`
                : `<span class="text-xxs" style="color: var(--color-warning);">✉ not sent</span>`;
            return `<div class="text-xs" style="padding: 1px 0; color: var(--color-text-primary);
                    display: flex; align-items: center; gap: 8px;">
                <span>${_esc(user)} — <span class="${s.status === "submitted" ? "font-semibold" : ""}">${_esc(label)}</span>${progress}</span>
                ${mailBadge}
                <button class="btn-discreet btn-compact"
                    onclick="hevResendInvite(this, '${_esc(t.run_id)}', '${_esc(t.task_type)}', '${_esc(user)}')"
                    >${notified ? "Resend" : "Send invite"}</button>
            </div>`;
        }).join("") || '<div class="text-xs" style="color: var(--color-text-muted);">No coders invited.</div>';

        const invitedSet = new Set(Object.keys(coderMeta));
        const inviteOptions = st.users
            .filter(u => !invitedSet.has(u.username))
            .map(u => `<option value="${_esc(u.username)}">${_esc(u.username)}</option>`)
            .join("");
        const inviteControl = inviteOptions
            ? `<div style="margin-top: 6px; display: flex; gap: 8px; align-items: center;">
                <select id="hev-invite-${_esc(cardKey)}" class="text-xs"
                    style="padding: 3px 8px; border: 1px solid var(--color-border); border-radius: 4px;
                    background: var(--color-bg-input); color: var(--color-text-primary);">
                    ${inviteOptions}
                </select>
                <button class="btn-discreet btn-compact"
                    onclick="hevInviteCoder(this, '${_esc(t.run_id)}', '${_esc(t.task_type)}', 'hev-invite-${_esc(cardKey)}')"
                    >Invite more coders</button>
            </div>` : "";

        const results = (detail || {}).results;
        const nSubmitted = Object.values(status).filter(s => s.status === "submitted").length;
        const resultsLine = results
            ? `<span class="text-xs" style="color: var(--color-text-muted);">Results computed ${_esc(fypFmtDateTime(results.computed_at))} — see the run's report under Annotation testing.</span>`
            : `<span class="text-xs" style="color: var(--color-text-muted);">${nSubmitted ? "" : "No results yet — waiting for submissions."}</span>`;

        return `<div style="border: 1px solid var(--color-border); border-radius: 6px;
                padding: 10px 12px; margin-bottom: 10px;">
            <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
                <span class="font-semibold">${_esc(_runName(t.run_id) || t.run_id)}</span>
                ${_runName(t.run_id) ? `<span class="text-xs font-mono" style="color: var(--color-text-muted);">${_esc(t.run_id)}</span>` : ""}
                <span class="text-xs" style="color: var(--color-text-muted);">
                    ${_esc(t.task_type)} · ${nItems} items · ${t.n_variables} variables ·
                    created ${_esc(fypFmtDateTime(t.created_at))} by ${_esc(t.created_by || "")}
                </span>
                <span style="flex: 1;"></span>
                <button class="btn-discreet btn-compact"
                    onclick="hevRecompute(this, '${_esc(t.run_id)}', '${_esc(t.task_type)}')">Recompute</button>
                <button class="btn-danger btn-compact"
                    onclick="hevDeleteTask(this, '${_esc(t.run_id)}', '${_esc(t.task_type)}')">Delete</button>
            </div>
            <div style="margin-top: 6px;">${coderRows}</div>
            ${inviteControl}
            <div style="margin-top: 4px;">${resultsLine}</div>
        </div>`;
    }

    async function hevResendInvite(btn, runId, taskType, username) {
        try {
            btn.disabled = true;
            await _postJson(
                `${BASE}/tasks/${encodeURIComponent(runId)}/${encodeURIComponent(taskType)}/notify`,
                { username });
            _status(`Invitation sent to ${username}.`);
            hevRefreshTasks();
        } catch (e) {
            btn.disabled = false;
            _status(`Invite failed: ${e.message}`, true);
        }
    }

    async function hevInviteCoder(btn, runId, taskType, selectId) {
        const sel = document.getElementById(selectId);
        if (!sel || !sel.value) return;
        try {
            await _busy(btn, "Inviting…", async () => {
                await _postJson(
                    `${BASE}/tasks/${encodeURIComponent(runId)}/${encodeURIComponent(taskType)}/coders`,
                    { coders: [sel.value] });
                _status(`${sel.value} invited.`);
                await hevRefreshTasks();
            });
        } catch (e) {
            _status(`Invite failed: ${e.message}`, true);
        }
    }

    async function hevRecompute(btn, runId, taskType) {
        try {
            await _busy(btn, "Recomputing…", async () => {
                await _postJson(`${BASE}/tasks/${encodeURIComponent(runId)}/${encodeURIComponent(taskType)}/recompute`);
                _status(`Results recomputed for ${runId}.`);
                await hevRefreshTasks();
            });
        } catch (e) {
            _status(`Recompute failed: ${e.message}`, true);
        }
    }

    async function hevDeleteTask(btn, runId, taskType) {
        if (!_armTwoClick(btn, "Really delete?")) return;
        try {
            await _busy(btn, "Deleting…", async () => {
                await _postJson(`${BASE}/tasks/${encodeURIComponent(runId)}/${encodeURIComponent(taskType)}`,
                    undefined, "DELETE");
                _status(`Task deleted from ${runId}.`);
                await Promise.all([hevRefreshTasks(), loadRuns()]);
            });
        } catch (e) {
            _status(`Delete failed: ${e.message}`, true);
        }
    }

    // ---------- bootstrap (lazy, on first sub-page activation) ----------

    let bootstrapped = false;

    function _maybeBootstrap() {
        const page = document.getElementById("admin-page-humaneval");
        if (!page || bootstrapped || !page.classList.contains("active")) return;
        bootstrapped = true;
        loadRuns();
        loadUsers();
        hevRefreshTasks();
    }

    function _watchForActivation() {
        const page = document.getElementById("admin-page-humaneval");
        if (!page) return;
        const observer = new MutationObserver(_maybeBootstrap);
        observer.observe(page, { attributes: true, attributeFilter: ["class"] });
        _maybeBootstrap();
    }

    function hevToggleAllVars(master) {
        document.querySelectorAll(".hev-var-cb").forEach(c => { c.checked = master.checked; });
    }

    // Read-only rendition of the coder form for the currently selected
    // variables — mirrors human_coding.js's widget choices (radio ≤5 values,
    // else select; checkboxes for closed lists; number / free text otherwise).
    function hevPreviewTask() {
        const modal = document.getElementById("hev-preview-modal");
        const body = document.getElementById("hev-preview-body");
        if (!modal || !body) return;
        const selected = new Set(Array.from(document.querySelectorAll(".hev-var-cb:checked"))
            .map(c => c.value));
        const vars = st.variables.filter(v => selected.has(v.name));
        if (!vars.length) {
            body.innerHTML = '<span class="text-sm" style="color: var(--color-text-muted);">'
                + 'No variables selected — tick some in the list first.</span>';
            modal.style.display = "flex";
            return;
        }
        const inputStyle = 'padding: 5px 10px; border: 1px solid var(--color-border);'
            + ' border-radius: 4px; background: var(--color-bg-input);'
            + ' color: var(--color-text-primary);';
        body.innerHTML = vars.map(v => {
            const values = v.values || [];
            let widget;
            if (v.kind === "enum" && values.length && values.length <= 5) {
                widget = values.map(val => `<label class="text-sm" style="display: inline-flex;
                        align-items: center; gap: 4px; margin-right: 12px;">
                    <input type="radio" name="hev-pv-${_esc(v.name)}" disabled> ${_esc(val)}</label>`).join("");
            } else if (v.kind === "enum" && values.length) {
                widget = `<select disabled class="text-sm" style="${inputStyle} min-width: 220px;">
                    <option>(select…)</option>
                    ${values.map(val => `<option>${_esc(val)}</option>`).join("")}</select>`;
            } else if (v.kind === "list" && values.length) {
                widget = values.map(val => `<label class="text-sm" style="display: inline-flex;
                        align-items: center; gap: 4px; margin-right: 12px;">
                    <input type="checkbox" disabled> ${_esc(val)}</label>`).join("");
            } else if (v.kind === "numeric") {
                widget = `<input type="number" disabled placeholder="number" class="text-sm"
                    style="${inputStyle} width: 140px;">`;
            } else {
                widget = `<textarea disabled rows="2" placeholder="free text${v.kind === "list" ? " — one per line" : ""}"
                    class="text-sm" style="${inputStyle} width: 100%; resize: vertical;"></textarea>`;
            }
            return `<div style="margin-bottom: 14px;">
                <div class="font-semibold" style="margin-bottom: 2px;">${_esc(v.label || v.name)}
                    <span class="text-xxs font-normal" style="color: var(--color-text-muted);">(${_esc(v.kind)})</span></div>
                ${v.description ? `<div class="text-xs" style="color: var(--color-text-muted); margin-bottom: 4px;">${_esc(v.description)}</div>` : ""}
                ${widget}
            </div>`;
        }).join("");
        modal.style.display = "flex";
    }

    function hevClosePreview() {
        const modal = document.getElementById("hev-preview-modal");
        if (modal) modal.style.display = "none";
    }

    window.hevOnRunChange = hevOnRunChange;
    window.hevOnTypeChange = hevOnTypeChange;
    window.hevToggleAllVars = hevToggleAllVars;
    window.hevPreviewTask = hevPreviewTask;
    window.hevClosePreview = hevClosePreview;
    window.hevCreateTask = hevCreateTask;
    window.hevRefreshTasks = hevRefreshTasks;
    window.hevRecompute = hevRecompute;
    window.hevDeleteTask = hevDeleteTask;
    window.hevResendInvite = hevResendInvite;
    window.hevInviteCoder = hevInviteCoder;

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", _watchForActivation);
    } else {
        _watchForActivation();
    }
})();
