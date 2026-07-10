/**
 * Coder-facing "Coding" tab: blind human coding of annotation test runs.
 *
 * Talks only to the invitation-gated /api/human-eval endpoints; the payload
 * never contains machine annotation values. The tab button (hidden by
 * default) is unhidden when /api/human-eval/my-tasks returns work — which is
 * also the in-app "you have been invited" surface. Videos stream through the
 * existing /api/video route (the study segment is ignored by the server).
 * The global fetch wrapper in main.js injects the CSRF header.
 */
(function () {
    "use strict";

    const BASE = "/api/human-eval";

    const st = {
        tasks: [],
        task: null,        // the open task's payload
        responses: {},     // coding: {item_id: {var: value}}; vote: {item_id: {choice}}
        mode: "coding",    // the open task's type
        idx: 0,
        dirty: false,
        readOnly: false,
    };

    function _esc(v) {
        const div = document.createElement("div");
        div.textContent = v == null ? "" : String(v);
        return div.innerHTML;
    }

    async function _getJson(url) {
        const res = await fetch(url);
        const body = await res.json();
        if (!res.ok) throw new Error(body.error || body.message || res.statusText);
        return body;
    }

    async function _postJson(url, payload) {
        const res = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload || {}),
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(body.error || body.message || res.statusText);
        return body;
    }

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

    // ---------- task list ----------

    async function refreshTasks() {
        let body;
        try {
            body = await _getJson(`${BASE}/my-tasks`);
        } catch (e) {
            return;   // not logged in / endpoint unavailable — keep the tab hidden
        }
        st.tasks = body.tasks || [];
        const btn = document.querySelector('.tab-button[data-tab="coding"]');
        if (btn) {
            btn.style.display = st.tasks.length ? "" : "none";
            _updateTabBadge(btn);
        }
        renderTaskList();
    }

    // Small count pill on the tab button — the in-app "you have pending
    // coding work" signal (complements the invitation email).
    function _updateTabBadge(btn) {
        const pending = st.tasks.filter(t => t.my_status !== "submitted").length;
        let badge = btn.querySelector(".hc-tab-badge");
        if (!pending) {
            if (badge) badge.remove();
            return;
        }
        if (!badge) {
            badge = document.createElement("span");
            badge.className = "hc-tab-badge text-xxs font-semibold";
            badge.style.cssText = "margin-left: 6px; padding: 1px 6px; border-radius: 8px;" +
                "background: var(--color-accent); color: var(--color-bg-primary);";
            btn.appendChild(badge);
        }
        badge.textContent = String(pending);
    }

    function renderTaskList() {
        const box = document.getElementById("hc-task-list");
        if (!box) return;
        if (!st.tasks.length) {
            box.innerHTML = '<span class="text-sm" style="color: var(--color-text-muted);">No coding tasks.</span>';
            return;
        }
        box.innerHTML = st.tasks.map(t => {
            const done = t.my_status === "submitted";
            const pct = t.n_items ? Math.round(100 * (t.my_n_answered || 0) / t.n_items) : 0;
            const action = done ? "Review" : (t.my_status === "in_progress" ? "Continue" : "Start");
            const typeLabel = t.task_type === "vote" ? "preference vote" : "coding";
            return `<div style="border: 1px solid var(--color-border); border-radius: 6px;
                    padding: 12px 14px; margin-bottom: 10px; display: flex; gap: 14px;
                    align-items: center; flex-wrap: wrap;">
                <div style="flex: 1; min-width: 220px;">
                    <div class="font-semibold">${_esc(t.run_id)}</div>
                    <div class="text-xs" style="color: var(--color-text-muted);">
                        ${_esc(typeLabel)} · ${t.n_items} videos · ${t.n_variables} variables · ${_esc(t.my_status || "invited")}
                    </div>
                    <div style="height: 6px; border-radius: 3px; background: var(--color-bg-elevated);
                        margin-top: 6px; overflow: hidden;">
                        <div style="height: 100%; width: ${pct}%; background: var(--color-accent);"></div>
                    </div>
                </div>
                <button class="btn-primary btn-compact"
                    onclick="hcOpenTask('${_esc(t.run_id)}', '${_esc(t.task_type)}', this)">${action}</button>
            </div>`;
        }).join("");
    }

    // ---------- coding view ----------

    async function hcOpenTask(runId, taskType, btn) {
        try {
            const body = await _busy(btn, "Opening…", () => _getJson(
                `${BASE}/tasks/${encodeURIComponent(runId)}/${encodeURIComponent(taskType)}`));
            st.task = body;
            st.mode = body.task_type || "coding";
            st.responses = body.responses || {};
            st.readOnly = body.status === "submitted";
            // Resume at the first unanswered item (or the start when reviewing).
            st.idx = 0;
            if (!st.readOnly) {
                const firstOpen = body.items.findIndex(it => !_isAnswered(it.item_id));
                st.idx = firstOpen >= 0 ? firstOpen : 0;
            }
            document.getElementById("hc-list-view").style.display = "none";
            document.getElementById("hc-work-view").style.display = "";
            document.getElementById("hc-work-title").textContent =
                st.mode === "vote" ? `${runId} — preference vote` : runId;
            const submitBtn = document.getElementById("hc-submit-btn");
            submitBtn.style.display = st.readOnly ? "none" : "";
            submitBtn.textContent = st.mode === "vote" ? "Submit votes" : "Submit coding";
            renderItem();
        } catch (e) {
            alert(`Could not open task: ${e.message}`);
        }
    }

    async function hcBackToList() {
        await saveCurrent();
        const video = document.getElementById("hc-video");
        if (video) video.pause();
        document.getElementById("hc-work-view").style.display = "none";
        document.getElementById("hc-list-view").style.display = "";
        refreshTasks();
    }

    function _currentItem() {
        return st.task.items[st.idx];
    }

    function renderItem() {
        const item = _currentItem();
        const video = document.getElementById("hc-video");
        const platform = item.platform
            ? `?platform=${encodeURIComponent(item.platform)}` : "";
        video.src = `/api/video/eval/${encodeURIComponent(item.item_id)}${platform}`;
        document.getElementById("hc-video-note").textContent =
            `Video ${item.item_id}${item.platform ? ` (${item.platform})` : ""}`;
        document.getElementById("hc-item-pos").textContent =
            `Video ${st.idx + 1} of ${st.task.items.length}`;
        const codingForm = document.getElementById("hc-form");
        const voteForm = document.getElementById("hc-vote-form");
        codingForm.style.display = st.mode === "vote" ? "none" : "";
        voteForm.style.display = st.mode === "vote" ? "" : "none";
        if (st.mode === "vote") {
            renderVoteForm();
        } else {
            renderForm();
        }
        const noteEl = document.getElementById("hc-note");
        if (noteEl) {
            noteEl.value = (st.responses[item.item_id] || {}).note || "";
            noteEl.disabled = st.readOnly;
        }
        renderDots();
        renderProgress();
        _setSaveState("");
    }

    function _isAnswered(itemId) {
        const r = st.responses[itemId] || {};
        if (st.mode === "vote") return !!r.choice;
        return _hasAnyValue(r.values || {});
    }

    function _currentNote() {
        const noteEl = document.getElementById("hc-note");
        return noteEl ? noteEl.value.trim() : "";
    }

    // Note edits: votes save immediately (their values save on pick, so the
    // note needs its own trigger); coding notes ride the normal autosave.
    async function hcNoteChanged() {
        if (st.readOnly) return;
        if (st.mode !== "vote") { hcMarkDirty(); return; }
        const item = _currentItem();
        try {
            await _postJson(
                `${BASE}/tasks/${encodeURIComponent(st.task.run_id)}/` +
                `${encodeURIComponent(st.task.task_type)}/responses`,
                { item_id: item.item_id, note: _currentNote() });
            st.responses[item.item_id] = {
                ...(st.responses[item.item_id] || {}), note: _currentNote(),
            };
            _setSaveState("Saved");
        } catch (e) {
            _setSaveState(`Save failed: ${e.message}`, true);
        }
    }

    // ---------- vote mode ----------

    function renderVoteForm() {
        const box = document.getElementById("hc-vote-form");
        const item = _currentItem();
        const options = (st.task.options || {})[item.item_id] || [];
        const choice = (st.responses[item.item_id] || {}).choice || "";
        const disabled = st.readOnly ? "disabled" : "";

        const pickBtn = (value, label) => {
            const active = choice === value;
            const cls = active ? "btn-primary" : "btn-discreet";
            return `<button class="${cls} btn-compact" ${disabled}
                onclick="hcPickOption('${_esc(value)}')"
                style="min-width: 90px;">${_esc(label)}${active ? " ✓" : ""}</button>`;
        };

        const cell = "padding: 5px 8px; border-bottom: 1px solid var(--color-border); vertical-align: top;";
        const rows = (st.task.variables || []).map(name => {
            const spec = (st.task.field_specs || {})[name] || {};
            const cells = options.map(o =>
                `<td style="${cell}">${_esc(o.values[name] ?? "")}</td>`).join("");
            return `<tr>
                <td style="${cell}" class="text-xs font-medium meta-tooltip"
                    data-tooltip="${_esc(spec.description || name)}">${_esc(spec.label || name)}</td>
                ${cells}
            </tr>`;
        }).join("");

        box.innerHTML = `
            <div class="text-xs" style="color: var(--color-text-muted); margin-bottom: 8px;">
                Two or more anonymous annotation options for this video are shown
                side by side. Watch the video, compare them, and pick the better
                one — or a tie. Option order is randomized per video.
            </div>
            <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px;">
                ${options.map(o => pickBtn(o.option, `Option ${o.option}`)).join("")}
                ${pickBtn("tie", "Tie")}
            </div>
            <div style="overflow-x: auto; max-height: 60vh; overflow-y: auto;">
                <table style="border-collapse: collapse; width: 100%;" class="text-xs">
                    <thead><tr class="text-xxs" style="text-align: left; color: var(--color-text-muted);">
                        <th style="${cell}">field</th>
                        ${options.map(o => `<th style="${cell}">Option ${_esc(o.option)}</th>`).join("")}
                    </tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>`;
    }

    async function hcPickOption(value) {
        if (st.readOnly) return;
        const item = _currentItem();
        const voteButtons = document.querySelectorAll("#hc-vote-form button");
        voteButtons.forEach(b => { b.disabled = true; });
        try {
            await _postJson(
                `${BASE}/tasks/${encodeURIComponent(st.task.run_id)}/` +
                `${encodeURIComponent(st.task.task_type)}/responses`,
                { item_id: item.item_id, values: { choice: value }, note: _currentNote() });
            st.responses[item.item_id] = { choice: value, note: _currentNote() };
            _setSaveState("Saved");
            renderVoteForm();
            renderDots();
            renderProgress();
        } catch (e) {
            _setSaveState(`Save failed: ${e.message}`, true);
            voteButtons.forEach(b => { b.disabled = false; });
        }
    }

    function renderForm() {
        const box = document.getElementById("hc-form");
        const values = (st.responses[_currentItem().item_id] || {}).values || {};
        const disabled = st.readOnly ? "disabled" : "";
        box.innerHTML = st.task.variables.map(name => {
            const spec = st.task.field_specs[name] || {};
            const value = values[name];
            let widget;
            if (spec.kind === "enum" && spec.values && spec.values.length) {
                widget = spec.values.length <= 5
                    ? _radioWidget(name, spec.values, value, disabled)
                    : _selectWidget(name, spec.values, value, disabled);
            } else if (spec.kind === "list") {
                widget = _listWidget(name, spec.values, value, disabled);
            } else if (spec.kind === "numeric") {
                widget = `<input type="number" step="any" data-var="${_esc(name)}" ${disabled}
                    value="${value == null ? "" : _esc(value)}" onchange="hcMarkDirty()"
                    class="text-sm" style="padding: 5px 10px; border: 1px solid var(--color-border);
                    border-radius: 4px; background: var(--color-bg-input);
                    color: var(--color-text-primary); width: 120px;">`;
            } else {
                widget = `<textarea rows="3" data-var="${_esc(name)}" ${disabled}
                    onchange="hcMarkDirty()" class="text-sm" style="width: 100%; padding: 6px 10px;
                    border: 1px solid var(--color-border); border-radius: 4px;
                    background: var(--color-bg-input); color: var(--color-text-primary);
                    resize: vertical;">${value == null ? "" : _esc(value)}</textarea>`;
            }
            const tooltip = spec.description
                ? ` <span class="meta-tooltip" data-tooltip="${_esc(spec.description)}">&#9432;</span>` : "";
            return `<div style="margin-bottom: 14px;">
                <div class="font-medium" style="margin-bottom: 4px;">${_esc(spec.label || name)}${tooltip}</div>
                ${widget}
            </div>`;
        }).join("");
    }

    function _radioWidget(name, values, value, disabled) {
        const options = values.map(v => `<label style="display: flex; align-items: center; gap: 5px;">
            <input type="radio" name="hc-radio-${_esc(name)}" data-var="${_esc(name)}" ${disabled}
                value="${_esc(v)}" ${String(value) === String(v) ? "checked" : ""}
                onchange="hcMarkDirty()"> ${_esc(v)}
        </label>`).join("");
        const clear = `<label style="display: flex; align-items: center; gap: 5px;
                color: var(--color-text-muted);">
            <input type="radio" name="hc-radio-${_esc(name)}" data-var="${_esc(name)}" ${disabled}
                value="" ${value == null || value === "" ? "checked" : ""}
                onchange="hcMarkDirty()"> no value
        </label>`;
        return `<div style="display: flex; gap: 12px; flex-wrap: wrap;">${options}${clear}</div>`;
    }

    function _selectWidget(name, values, value, disabled) {
        const options = ['<option value="">— no value —</option>']
            .concat(values.map(v =>
                `<option value="${_esc(v)}" ${String(value) === String(v) ? "selected" : ""}>${_esc(v)}</option>`))
            .join("");
        return `<select data-var="${_esc(name)}" ${disabled} onchange="hcMarkDirty()" class="text-sm"
            style="padding: 5px 10px; border: 1px solid var(--color-border); border-radius: 4px;
            background: var(--color-bg-input); color: var(--color-text-primary);
            min-width: 220px;">${options}</select>`;
    }

    function _listWidget(name, accepted, value, disabled) {
        const selected = Array.isArray(value) ? value.map(String) : [];
        if (accepted && accepted.length) {
            // Closed list: one checkbox chip per accepted value.
            const chips = accepted.map(v => `<label class="text-sm" style="display: inline-flex;
                    align-items: center; gap: 5px; border: 1px solid var(--color-border);
                    border-radius: 12px; padding: 2px 10px;">
                <input type="checkbox" data-var="${_esc(name)}" value="${_esc(v)}" ${disabled}
                    ${selected.includes(String(v)) ? "checked" : ""} onchange="hcMarkDirty()">
                ${_esc(v)}
            </label>`).join(" ");
            return `<div data-list-var="${_esc(name)}" data-closed="1"
                style="display: flex; gap: 6px; flex-wrap: wrap;">${chips}</div>`;
        }
        // Open list: free-text entry, one value per line.
        return `<textarea rows="3" data-var="${_esc(name)}" data-open-list="1" ${disabled}
            onchange="hcMarkDirty()" placeholder="one value per line" class="text-sm"
            style="width: 100%; padding: 6px 10px; border: 1px solid var(--color-border);
            border-radius: 4px; background: var(--color-bg-input);
            color: var(--color-text-primary); resize: vertical;">${_esc(selected.join("\n"))}</textarea>`;
    }

    function collectValues() {
        const values = {};
        for (const name of st.task.variables) {
            const spec = st.task.field_specs[name] || {};
            if (spec.kind === "list") {
                const closed = document.querySelector(`[data-list-var="${CSS.escape(name)}"]`);
                if (closed) {
                    values[name] = Array.from(
                        closed.querySelectorAll("input:checked")).map(c => c.value);
                } else {
                    const area = document.querySelector(
                        `textarea[data-var="${CSS.escape(name)}"][data-open-list="1"]`);
                    values[name] = area
                        ? area.value.split("\n").map(s => s.trim()).filter(Boolean) : [];
                }
            } else if (spec.kind === "enum" && spec.values && spec.values.length <= 5) {
                const checked = document.querySelector(
                    `input[name="hc-radio-${CSS.escape(name)}"]:checked`);
                values[name] = checked ? checked.value : "";
            } else {
                const el = document.querySelector(`[data-var="${CSS.escape(name)}"]`);
                values[name] = el ? el.value : "";
            }
        }
        return values;
    }

    function _hasAnyValue(values) {
        return Object.values(values).some(v =>
            Array.isArray(v) ? v.length > 0 : v !== "" && v != null);
    }

    function hcMarkDirty() {
        st.dirty = true;
        _setSaveState("");
    }

    function _setSaveState(text, isError) {
        const el = document.getElementById("hc-save-state");
        if (!el) return;
        el.textContent = text;
        el.style.color = isError ? "var(--color-danger)" : "var(--color-text-muted)";
    }

    async function saveCurrent() {
        if (st.readOnly) return true;
        if (st.mode === "vote") return true;   // votes save on pick, not on navigation
        const item = _currentItem();
        const values = collectValues();
        const note = _currentNote();
        const hadResponse = !!st.responses[item.item_id];
        // Nothing filled in and nothing saved before — nothing to persist.
        if (!st.dirty && !hadResponse) return true;
        if (!_hasAnyValue(values) && !note && !hadResponse) return true;
        try {
            await _postJson(
                `${BASE}/tasks/${encodeURIComponent(st.task.run_id)}/` +
                `${encodeURIComponent(st.task.task_type)}/responses`,
                { item_id: item.item_id, values, note });
            st.responses[item.item_id] = { values, note };
            st.dirty = false;
            _setSaveState("Saved");
            renderDots();
            renderProgress();
            return true;
        } catch (e) {
            _setSaveState(`Save failed: ${e.message}`, true);
            return false;
        }
    }

    async function hcNext() {
        if (!(await saveCurrent())) return;
        if (st.idx < st.task.items.length - 1) {
            st.idx += 1;
            renderItem();
        }
    }

    async function hcPrev() {
        if (!(await saveCurrent())) return;
        if (st.idx > 0) {
            st.idx -= 1;
            renderItem();
        }
    }

    async function hcJump(index) {
        if (!(await saveCurrent())) return;
        st.idx = index;
        renderItem();
    }

    function renderDots() {
        const box = document.getElementById("hc-dots");
        box.innerHTML = st.task.items.map((it, i) => {
            const answered = _isAnswered(it.item_id);
            const current = i === st.idx;
            return `<span onclick="hcJump(${i})" title="Video ${i + 1}"
                style="width: 10px; height: 10px; border-radius: 50%; cursor: pointer;
                display: inline-block;
                border: 1px solid ${current ? "var(--color-accent)" : "var(--color-border-strong)"};
                background: ${answered ? "var(--color-accent)" : "transparent"};"></span>`;
        }).join("");
    }

    function renderProgress() {
        const answered = st.task.items.filter(it => _isAnswered(it.item_id)).length;
        document.getElementById("hc-progress").textContent = st.readOnly
            ? `Submitted — ${answered}/${st.task.items.length} coded (read-only)`
            : `${answered}/${st.task.items.length} coded`;
    }

    async function hcSubmit(btn) {
        if (!(await saveCurrent())) return;
        const answered = st.task.items.filter(it => _isAnswered(it.item_id)).length;
        const total = st.task.items.length;
        const label = answered < total
            ? `Submit with ${total - answered} uncoded?` : "Really submit?";
        if (!_armTwoClick(btn, label)) return;
        try {
            await _busy(btn, "Submitting…", () => _postJson(
                `${BASE}/tasks/${encodeURIComponent(st.task.run_id)}/` +
                `${encodeURIComponent(st.task.task_type)}/submit`, {}));
            st.readOnly = true;
            document.getElementById("hc-submit-btn").style.display = "none";
            renderItem();
            _setSaveState("Submitted — thank you!");
            refreshTasks();   // clears the tab badge
        } catch (e) {
            _setSaveState(`Submit failed: ${e.message}`, true);
        }
    }

    // ---------- bootstrap ----------

    window.hcOpenTask = hcOpenTask;
    window.hcBackToList = hcBackToList;
    window.hcNext = hcNext;
    window.hcPrev = hcPrev;
    window.hcJump = hcJump;
    window.hcSubmit = hcSubmit;
    window.hcMarkDirty = hcMarkDirty;
    window.hcPickOption = hcPickOption;
    window.hcNoteChanged = hcNoteChanged;

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", refreshTasks);
    } else {
        refreshTasks();
    }
})();
