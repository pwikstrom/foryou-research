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

    // ---------- setup form ----------

    async function loadRuns() {
        try {
            const body = await getJSON(`${BASE}/runs`);
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
            const body = await getJSON(`${BASE}/users`);
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
            const body = await getJSON(`${BASE}/runs/${encodeURIComponent(sel.value)}/variables`);
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
    // the contracts-to-vote-between row (all of the run's arms, all checked);
    // rating tasks their settings row and the run's submitted coders.
    function hevOnTypeChange() {
        const type = _selectedTaskType();
        const title = document.getElementById("hev-var-title");
        if (title) {
            title.textContent = {
                vote: "Fields shown to voters (side-by-side, blind)",
                rating: "Variables whose answers are rated",
            }[type] || "Variables the coders provide input for";
        }
        const userTitle = document.getElementById("hev-user-title");
        if (userTitle && userTitle.firstChild) {
            userTitle.firstChild.textContent = type === "rating" ? "Raters " : "Coders to invite ";
        }
        const createBtn = document.getElementById("hev-create-btn");
        if (createBtn) createBtn.textContent = type === "rating" ? "Create draft" : "Create task";
        const ratingRow = document.getElementById("hev-rating-row");
        if (ratingRow) ratingRow.style.display = type === "rating" ? "" : "none";
        const videosRow = document.getElementById("hev-videos-row");
        if (videosRow) videosRow.style.display = type === "vote" ? "none" : "";
        if (type === "rating") _loadRatingSources();
        if (type === "vote") {
            document.querySelectorAll(".hev-var-cb").forEach(c => { c.checked = true; });
            const master = document.getElementById("hev-var-check-all");
            if (master) master.checked = true;
        }
        const armRow = document.getElementById("hev-arm-row");
        if (armRow) armRow.style.display = type === "vote" ? "" : "none";
        if (type === "vote") _renderArmList();
    }

    // The contract picker and the run's submitted coders (sources of the
    // human-written answers) for a rating task.
    async function _loadRatingSources() {
        const runId = (document.getElementById("hev-run-select") || {}).value;
        const armSel = document.getElementById("hev-rating-arm");
        const box = document.getElementById("hev-rating-coders");
        if (!armSel || !box) return;
        const run = st.runs.find(r => r.run_id === runId);
        armSel.innerHTML = ((run && run.arms) || [])
            .map(a => `<option value="${_esc(a)}">${_esc(a)}</option>`).join("");
        if (!runId) return;
        try {
            const body = await getJSON(`${BASE}/tasks/${encodeURIComponent(runId)}/coding`);
            const submitted = Object.entries(body.coder_status || {})
                .filter(([, s]) => s.status === "submitted").map(([u]) => u);
            box.innerHTML = submitted.length
                ? submitted.map(u => `<label style="display: flex; align-items: center; gap: 6px;">
                    <input type="checkbox" class="hev-rating-coder-cb" value="${_esc(u)}" checked>
                    <span>${_esc(u)}</span></label>`).join("")
                : '<span style="color: var(--color-text-muted);">No submitted coding yet: the task rates the contract\'s answers and traps only.</span>';
        } catch (e) {
            box.innerHTML = '<span style="color: var(--color-text-muted);">No coding task on this run: the task rates the contract\'s answers and traps only.</span>';
        }
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
        const num = (id) => {
            const el = document.getElementById(id);
            return el && el.value !== "" ? parseInt(el.value, 10) : null;
        };
        const payload = { run_id: runId, task_type: taskType, variables, coders, arms };
        if (taskType !== "vote") payload.n_videos = num("hev-n-videos");
        if (taskType === "rating") {
            Object.assign(payload, {
                arm: (document.getElementById("hev-rating-arm") || {}).value || null,
                coders_included: Array.from(
                    document.querySelectorAll(".hev-rating-coder-cb:checked")).map(c => c.value),
                n_practice: num("hev-n-practice") ?? 3,
                ratings_per_human: num("hev-ratings-per-human") ?? 2,
                swapped_per_rater: num("hev-swapped") ?? 3,
                exclusion_rule: (document.getElementById("hev-exclusion") || {}).value || "none",
            });
        }
        try {
            await _busy(btn, "Creating…", async () => {
                await postJSON(`${BASE}/tasks`, payload);
                _status(taskType === "rating"
                    ? `Rating draft created on ${runId}: write traps, then Start.`
                    : `${taskType === "vote" ? "Vote" : "Coding"} task created on ${runId}.`);
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
            const body = await getJSON(`${BASE}/tasks`);
            st.tasks = body.tasks || [];
            if (!st.tasks.length) {
                box.innerHTML = '<span class="text-sm" style="color: var(--color-text-muted);">No human tasks yet.</span>';
                return;
            }
            const cards = await Promise.all(st.tasks.map(async t => {
                let detail = null;
                try {
                    detail = await getJSON(
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
        const task = (detail || {}).task || {};
        const coderMeta = task.coders || {};
        const nItems = t.n_items || 0;
        const cardKey = `${t.run_id}::${t.task_type}`;
        const isRating = t.task_type === "rating";
        const isDraft = isRating && (task.status || t.status) === "draft";
        const coderRows = isDraft
            ? Object.keys(coderMeta).map(user => `<div class="text-xs" style="padding: 1px 0;">
                ${_esc(user)} <span class="text-xxs" style="color: var(--color-text-muted);">invited at Start</span></div>`)
                .join("") || '<div class="text-xs" style="color: var(--color-text-muted);">No raters yet.</div>'
            : Object.entries(status).map(([user, s]) => {
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
        const inviteControl = inviteOptions && (!isRating || isDraft)
            ? `<div style="margin-top: 6px; display: flex; gap: 8px; align-items: center;">
                <select id="hev-invite-${_esc(cardKey)}" class="text-xs"
                    style="padding: 3px 8px; border: 1px solid var(--color-border); border-radius: 4px;
                    background: var(--color-bg-input); color: var(--color-text-primary);">
                    ${inviteOptions}
                </select>
                <button class="btn-discreet btn-compact"
                    onclick="hevInviteCoder(this, '${_esc(t.run_id)}', '${_esc(t.task_type)}', 'hev-invite-${_esc(cardKey)}')"
                    >${isRating ? "Add rater" : "Invite more coders"}</button>
            </div>` : "";

        const results = (detail || {}).results;
        const nSubmitted = Object.values(status).filter(s => s.status === "submitted").length;
        const resultsLine = isDraft ? "" : results
            ? `<span class="text-xs" style="color: var(--color-text-muted);">Results computed ${_esc(fypFmtDateTime(results.computed_at))} — see the run's report under Annotation testing.</span>`
            : `<span class="text-xs" style="color: var(--color-text-muted);">${nSubmitted ? "" : "No results yet — waiting for submissions."}</span>`;
        const typeLabel = isRating ? `quality rating${isDraft ? " · draft" : ""}` : t.task_type;
        const csvTip = isRating
            ? "One row per rating: rater, video, the answer judged, its source (contract, coder or trap), every response, and the source guess."
            : "One row per video: each contract's answer next to every submitted coder's, plus coder notes, as given.";
        const csvLink = (t.task_type === "coding" || (isRating && !isDraft))
            ? `<a class="btn-discreet btn-compact meta-tooltip" style="text-decoration: none;"
                data-tooltip="${_esc(csvTip)}"
                href="${BASE}/tasks/${encodeURIComponent(t.run_id)}/${encodeURIComponent(t.task_type)}/export">Download CSV</a>`
            : "";

        return `<div style="border: 1px solid var(--color-border); border-radius: 6px;
                padding: 10px 12px; margin-bottom: 10px;">
            <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
                <span class="font-semibold">${_esc(_runName(t.run_id) || t.run_id)}</span>
                ${_runName(t.run_id) ? `<span class="text-xs font-mono" style="color: var(--color-text-muted);">${_esc(t.run_id)}</span>` : ""}
                <span class="text-xs" style="color: var(--color-text-muted);">
                    ${_esc(typeLabel)} · ${nItems} items · ${t.n_variables} variables ·
                    created ${_esc(fypFmtDateTime(t.created_at))} by ${_esc(t.created_by || "")}
                </span>
                <span style="flex: 1;"></span>
                ${csvLink}
                ${isDraft ? `<button class="btn-primary btn-compact meta-tooltip"
                    data-tooltip="Freezes every answer, plans which rater sees which, and sends the invitations. Traps can't be changed afterwards."
                    onclick="hevStartRating(this, '${_esc(t.run_id)}')">Start</button>` : `<button class="btn-discreet btn-compact"
                    onclick="hevRecompute(this, '${_esc(t.run_id)}', '${_esc(t.task_type)}')">Recompute</button>`}
                <button class="btn-danger btn-compact"
                    onclick="hevDeleteTask(this, '${_esc(t.run_id)}', '${_esc(t.task_type)}')">Delete</button>
            </div>
            <div style="margin-top: 6px;">${coderRows}</div>
            ${inviteControl}
            ${isDraft ? _trapPanel(t.run_id, task) : ""}
            ${isRating && !isDraft ? _planLines(task) : ""}
            <div style="margin-top: 4px;">${resultsLine}</div>
        </div>`;
    }

    // ---------- rating drafts: traps and Start ----------

    const _trapState = {};   // run_id -> the draft task (for the editor)

    // Per rater: how many contract, human and trap answers the plan gave them.
    function _planLines(task) {
        const answers = task.answers || {};
        const practice = new Set(task.practice_item_ids || []);
        const lines = Object.entries(task.assignment || {}).map(([rater, items]) => {
            const c = { model: 0, human: 0, swap: 0, trap: 0 };
            for (const [itemId, byVar] of Object.entries(items)) {
                if (practice.has(itemId)) continue;
                for (const answerId of Object.values(byVar)) {
                    const group = ((answers[answerId] || {}).source || "").split(":")[0];
                    c[group] = (c[group] || 0) + 1;
                }
            }
            return `${_esc(rater)}: ${c.model} contract · ${c.human} human · ${c.swap} swapped · ${c.trap} written traps`;
        });
        return lines.length ? `<div class="text-xxs" style="color: var(--color-text-muted); margin-top: 6px;">
            Plan (rated videos, all variables): ${lines.join(" &nbsp;|&nbsp; ")}</div>` : "";
    }

    function _trapPanel(runId, task) {
        _trapState[runId] = task;
        const key = _esc(runId);
        const specs = task.field_specs || {};
        const platforms = Object.fromEntries((task.items || []).map(i => [i.item_id, i.platform]));
        const traps = (task.traps || []).map(trap => {
            const shown = Array.isArray(trap.value) ? trap.value.join("; ") : String(trap.value);
            return `<div class="text-xs" style="display: flex; gap: 8px; align-items: baseline; padding: 2px 0;">
                <span class="font-mono">${_esc(trap.item_id)}</span>
                <span>${_esc((specs[trap.variable] || {}).label || trap.variable)}</span>
                <span style="color: var(--color-text-muted); flex: 1;">${_esc(shown.slice(0, 120))}${shown.length > 120 ? "…" : ""}
                    ${trap.note ? ` <em>(${_esc(trap.note)})</em>` : ""}</span>
                <button class="btn-discreet btn-compact"
                    onclick="hevDeleteTrap(this, '${key}', '${_esc(trap.trap_id)}')">Remove</button>
            </div>`;
        }).join("") || '<div class="text-xs" style="color: var(--color-text-muted);">No written traps yet.</div>';
        const videoOptions = (task.rated_item_ids || []).map(i =>
            `<option value="${_esc(i)}" data-platform="${_esc(platforms[i] || "")}">${_esc(i)}</option>`).join("");
        const varOptions = (task.variables || []).map(v =>
            `<option value="${_esc(v)}">${_esc((specs[v] || {}).label || v)}</option>`).join("");
        const input = "padding: 4px 8px; border: 1px solid var(--color-border); border-radius: 4px; background: var(--color-bg-input); color: var(--color-text-primary);";
        return `<details style="margin-top: 8px; border: 1px solid var(--color-border); border-radius: 6px; padding: 6px 10px;" open>
            <summary class="text-sm font-semibold" style="cursor: pointer;">Traps (${(task.traps || []).length} written)</summary>
            <div class="text-xxs" style="color: var(--color-text-muted); margin: 4px 0 6px 0;">
                Pick a video, watch it, and edit a copy of the contract's answer to plant one error.
                Each trap is shown to one rater instead of the real answer; the stored annotations
                are never changed. Swapped traps are added automatically at Start.</div>
            ${traps}
            <div style="display: flex; gap: 14px; flex-wrap: wrap; margin-top: 8px;">
                <video id="hev-trap-player-${key}" controls preload="metadata"
                    style="width: 200px; max-height: 320px; border-radius: 6px; background: var(--color-bg-elevated);"></video>
                <div style="flex: 1; min-width: 280px; display: flex; flex-direction: column; gap: 6px;">
                    <div style="display: flex; gap: 8px; flex-wrap: wrap; align-items: center;">
                        <select id="hev-trap-video-${key}" class="text-xs" style="${input}"
                            onchange="hevTrapLoad('${key}')">${videoOptions}</select>
                        <button class="btn-discreet btn-compact" onclick="hevTrapRandom('${key}')">Random video</button>
                        <select id="hev-trap-var-${key}" class="text-xs" style="${input}"
                            onchange="hevTrapLoad('${key}', true)">${varOptions}</select>
                    </div>
                    <div id="hev-trap-editor-${key}" class="text-xs" style="color: var(--color-text-muted);">
                        <button class="btn-discreet btn-compact" onclick="hevTrapLoad('${key}')">Load the contract's answer</button></div>
                    <input id="hev-trap-note-${key}" class="text-xs" style="${input}"
                        placeholder="What did you change? (raters never see this; it goes into the results)">
                    <div><button class="btn-primary btn-compact" onclick="hevSaveTrap(this, '${key}')">Save trap</button></div>
                </div>
            </div>
        </details>`;
    }

    function _trapSelection(runId) {
        const video = document.getElementById(`hev-trap-video-${runId}`);
        const variable = document.getElementById(`hev-trap-var-${runId}`);
        return { video, itemId: video ? video.value : "", variable: variable ? variable.value : "" };
    }

    // Load the chosen video into the player and the contract's answer for the
    // chosen variable into the editor, as raters would see it.
    async function hevTrapLoad(runId, answerOnly) {
        const task = _trapState[runId] || {};
        const { video, itemId, variable } = _trapSelection(runId);
        const editor = document.getElementById(`hev-trap-editor-${runId}`);
        if (!itemId || !editor) return;
        if (!answerOnly) {
            const player = document.getElementById(`hev-trap-player-${runId}`);
            const platform = video.selectedOptions[0] ? video.selectedOptions[0].dataset.platform : "";
            if (player) player.src = `/api/video/eval/${encodeURIComponent(itemId)}${platform ? `?platform=${encodeURIComponent(platform)}` : ""}`;
        }
        editor.textContent = "Loading…";
        try {
            const body = await getJSON(`${BASE}/tasks/${encodeURIComponent(runId)}/rating/model-answers?item_id=${encodeURIComponent(itemId)}`);
            const value = (body.answers || {})[variable];
            const kind = ((task.field_specs || {})[variable] || {}).rate_kind;
            const style = "width: 100%; padding: 5px 8px; border: 1px solid var(--color-border); border-radius: 4px; background: var(--color-bg-input); color: var(--color-text-primary);";
            if (value == null) {
                editor.innerHTML = '<span style="color: var(--color-warning);">The contract gave no answer here; pick another video.</span>';
            } else if (kind === "number") {
                editor.innerHTML = `<input type="number" step="any" data-kind="number" value="${_esc(value)}" class="text-sm" style="${style} width: 140px;">`;
            } else {
                const text = Array.isArray(value) ? value.join("\n") : String(value);
                const rows = kind === "text" ? 4 : (kind === "list" ? Math.min(Math.max(value.length, 3), 10) : 2);
                editor.innerHTML = `<textarea rows="${rows}" data-kind="${_esc(kind)}" class="text-sm" style="${style} resize: vertical;">${_esc(text)}</textarea>
                    ${kind === "list" ? '<div class="text-xxs" style="color: var(--color-text-muted);">One entry per line.</div>' : ""}`;
            }
        } catch (e) {
            editor.innerHTML = `<span style="color: var(--color-danger);">${_esc(e.message)}</span>`;
        }
    }

    function hevTrapRandom(runId) {
        const { video } = _trapSelection(runId);
        if (!video || !video.options.length) return;
        video.selectedIndex = Math.floor(Math.random() * video.options.length);
        hevTrapLoad(runId);
    }

    async function hevSaveTrap(btn, runId) {
        const { itemId, variable } = _trapSelection(runId);
        const field = document.querySelector(`#hev-trap-editor-${CSS.escape(runId)} [data-kind]`);
        if (!field) { _status("Load the contract's answer first.", true); return; }
        const kind = field.dataset.kind;
        const value = kind === "list"
            ? field.value.split("\n").map(s => s.trim()).filter(Boolean)
            : (kind === "number" ? parseFloat(field.value) : field.value);
        const note = (document.getElementById(`hev-trap-note-${runId}`) || {}).value || "";
        try {
            await _busy(btn, "Saving…", () => postJSON(
                `${BASE}/tasks/${encodeURIComponent(runId)}/rating/traps`,
                { item_id: itemId, variable, value, note }));
            _status(`Trap saved on ${itemId}.`);
            hevRefreshTasks();
        } catch (e) {
            _status(`Trap not saved: ${e.message}`, true);
        }
    }

    async function hevDeleteTrap(btn, runId, trapId) {
        try {
            await _busy(btn, "Removing…", () => postJSON(
                `${BASE}/tasks/${encodeURIComponent(runId)}/rating/traps/${encodeURIComponent(trapId)}`,
                undefined, "DELETE"));
            hevRefreshTasks();
        } catch (e) {
            _status(`Could not remove: ${e.message}`, true);
        }
    }

    async function hevStartRating(btn, runId) {
        if (!_armTwoClick(btn, "Freeze answers and invite raters?")) return;
        try {
            const body = await _busy(btn, "Starting…", () => postJSON(
                `${BASE}/tasks/${encodeURIComponent(runId)}/rating/start`, {}));
            const s = (body || {}).summary || {};
            const groups = Object.entries(s.by_source || {})
                .map(([g, c]) => `${g}: ${c.answers} answers, ${c.ratings} ratings`).join("; ");
            const warnings = (s.warnings || []).length ? ` Notes: ${s.warnings.join("; ")}.` : "";
            _status(`Rating task started. ${groups}.${warnings}`);
            hevRefreshTasks();
        } catch (e) {
            _status(`Start failed: ${e.message}`, true);
        }
    }

    async function hevResendInvite(btn, runId, taskType, username) {
        try {
            btn.disabled = true;
            await postJSON(
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
                await postJSON(
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
                await postJSON(`${BASE}/tasks/${encodeURIComponent(runId)}/${encodeURIComponent(taskType)}/recompute`);
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
                await postJSON(`${BASE}/tasks/${encodeURIComponent(runId)}/${encodeURIComponent(taskType)}`,
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
        // The coder form's own renderer and How to code panel (human_coding.js
        // and the My stuff template, on the same page), so the preview cannot
        // drift from what coders see.
        const guide = document.getElementById("hc-coding-guide");
        let guideHtml = "";
        if (guide) {
            const copy = guide.cloneNode(true);
            copy.removeAttribute("id");
            copy.style.display = "";  // hidden in the coder view after a vote task
            guideHtml = copy.outerHTML;
        }
        body.innerHTML = guideHtml + vars.map(v => {
            const widget = window.hcFieldWidget
                ? window.hcFieldWidget(v.name, v, null, "disabled", "hev-pv")
                : '<span class="text-xs" style="color: var(--color-text-muted);">(form unavailable)</span>';
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
    window.hevTrapLoad = hevTrapLoad;
    window.hevTrapRandom = hevTrapRandom;
    window.hevSaveTrap = hevSaveTrap;
    window.hevDeleteTrap = hevDeleteTrap;
    window.hevStartRating = hevStartRating;

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", _watchForActivation);
    } else {
        _watchForActivation();
    }
})();
