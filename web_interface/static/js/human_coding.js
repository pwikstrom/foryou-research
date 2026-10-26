/**
 * Coder-facing "My Tasks" page (My stuff tab): blind human coding of
 * annotation test runs.
 *
 * Talks only to the invitation-gated /api/human-eval endpoints; the payload
 * never contains machine annotation values. The sidebar item (hidden by
 * default) is unhidden when /api/human-eval/my-tasks returns work — which is
 * also the in-app "you have been invited" surface. Videos stream through the
 * existing /api/video route (the study segment is ignored by the server).
 * The global fetch wrapper in main.js injects the CSRF header.
 */
(function () {
    "use strict";

    const BASE = "/api/human-eval";

    // A coder's explicit "can't tell" (human_eval.CANT_TELL).
    const CANT_TELL = "can't tell";
    // Closed lists up to this size render as radio buttons, so every option,
    // "Can't tell" included, is visible without opening a menu.
    const RADIO_MAX = 12;
    const INPUT_STYLE = "padding: 5px 10px; border: 1px solid var(--color-border);"
        + " border-radius: 4px; background: var(--color-bg-input); color: var(--color-text-primary);";

    const st = {
        tasks: [],
        task: null,        // the open task's payload
        responses: {},     // coding: {item_id: {var: value}}; vote: {item_id: {choice}}
        mode: "coding",    // the open task's type
        idx: 0,
        dirty: false,
        readOnly: false,
        revealed: {},      // rating: item_id -> answers shown this session
        guessing: false,   // rating: the closing "who wrote these?" screen is open
    };

    function _esc(v) {
        const div = document.createElement("div");
        div.textContent = v == null ? "" : String(v);
        return div.innerHTML;
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
            body = await getJSON(`${BASE}/my-tasks`);
        } catch (e) {
            return;   // not logged in / endpoint unavailable — keep the tab hidden
        }
        st.tasks = body.tasks || [];
        const item = document.querySelector('#my_stuff .dm-sidebar-item[data-page="my-stuff-page-tasks"]');
        if (item) {
            item.style.display = st.tasks.length ? "" : "none";
            _updateTabBadge(item);
            // Refresh the mobile subnav so the item (dis)appears there too.
            if (typeof _buildTabSubnavs === 'function') _buildTabSubnavs();
        }
        _updateTopLevelBadge();
        renderTaskList();
    }

    // Small count pill on the sidebar item — the in-app "you have pending
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

    // Dot on the top-level "My stuff" tab button — visible from any tab when
    // the user has unfinished coding/vote work.
    function _updateTopLevelBadge() {
        const btn = document.querySelector('button[data-subpages-for="my_stuff"]');
        if (!btn) return;
        const pending = st.tasks.filter(t => t.my_status !== "submitted").length;
        let dot = btn.querySelector(".hc-top-badge");
        if (!pending) {
            if (dot) dot.remove();
            return;
        }
        if (!dot) {
            dot = document.createElement("span");
            dot.className = "hc-top-badge";
            dot.style.cssText = "display: inline-block; width: 8px; height: 8px;" +
                "margin-left: 5px; border-radius: 50%; background: var(--color-accent);" +
                "vertical-align: super;";
            dot.title = "You have unfinished tasks under My stuff → My Tasks";
            btn.appendChild(dot);
        }
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
            const typeLabel = { vote: "preference vote", rating: "quality rating" }[t.task_type] || "coding";
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
            const body = await _busy(btn, "Opening…", () => getJSON(
                `${BASE}/tasks/${encodeURIComponent(runId)}/${encodeURIComponent(taskType)}`));
            if (body.not_a_rater) {
                await showAppAlert("You are not one of the raters on this task.");
                return;
            }
            st.task = body;
            st.mode = body.task_type || "coding";
            st.revealed = {};
            st.guessing = false;
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
                { vote: `${runId} — preference vote`, rating: `${runId} — quality rating` }[st.mode] || runId;
            const submitBtn = document.getElementById("hc-submit-btn");
            submitBtn.style.display = st.readOnly ? "none" : "";
            submitBtn.textContent = { vote: "Submit votes", rating: "Submit ratings" }[st.mode] || "Submit coding";
            renderItem();
        } catch (e) {
            await showAppAlert(`Could not open task: ${e.message}`);
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
        const ratingForm = document.getElementById("hc-rating-form");
        const guide = document.getElementById("hc-coding-guide");
        const ratingGuide = document.getElementById("hc-rating-guide");
        if (guide) guide.style.display = st.mode === "coding" ? "" : "none";
        if (ratingGuide) ratingGuide.style.display = st.mode === "rating" ? "" : "none";
        codingForm.style.display = st.mode === "coding" ? "" : "none";
        voteForm.style.display = st.mode === "vote" ? "" : "none";
        if (ratingForm) ratingForm.style.display = st.mode === "rating" ? "" : "none";
        _watchForReveal(video, st.mode === "rating");
        if (st.mode === "vote") {
            renderVoteForm();
        } else if (st.mode === "rating") {
            renderRatingForm();
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
        if (st.mode === "rating") {
            const v = r.values || {};
            return !!(v.cant_rate || Object.keys(v.ratings || {}).length);
        }
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
            await postJSON(
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

    // ---------- rating mode ----------

    // The answers stay hidden until the video has been watched (ended, or 80%
    // played); a video that won't load unlocks them too, so the rater can say
    // so. Other modes clear the handlers.
    function _watchForReveal(video, active) {
        video.dataset.watched = "";
        const mark = () => {
            if (video.dataset.watched === "1") return;
            video.dataset.watched = "1";
            const btn = document.getElementById("hc-reveal-btn");
            if (btn) btn.disabled = false;
        };
        video.onended = active ? mark : null;
        video.onerror = active ? mark : null;
        video.ontimeupdate = active
            ? () => { if (video.duration && video.currentTime >= 0.8 * video.duration) mark(); }
            : null;
    }

    function hcRevealAnswers() {
        st.revealed[_currentItem().item_id] = true;
        renderRatingForm();
    }

    // An answer as raters see it: the same shape whatever its source.
    function _answerHtml(value, spec) {
        const box = "padding: 8px 10px; border-radius: 6px; background: var(--color-bg-elevated);"
            + " border: 1px solid var(--color-border);";
        if (Array.isArray(value)) {
            const chips = value.map(v => `<span style="display: inline-block; margin: 2px 4px 2px 0;
                padding: 1px 8px; border-radius: 10px; border: 1px solid var(--color-border);">${_esc(v)}</span>`).join("");
            return `<div style="${box}">${chips || '<em style="color: var(--color-text-muted);">nothing listed</em>'}</div>`;
        }
        const range = Array.isArray(spec.range) ? spec.range : null;
        const scale = range && typeof value === "number"
            ? ` <span class="text-xs" style="color: var(--color-text-muted);">on a ${_esc(range[0])}–${_esc(range[1])} scale</span>` : "";
        return `<div style="${box}">${_esc(value)}${scale}</div>`;
    }

    function _questionsHtml(name, spec, answer, rating, disabled) {
        const kind = spec.rate_kind;
        const group = (gname, options, current, extra = "") => options.map(([v, l]) =>
            `<label style="display: inline-flex; align-items: center; gap: 4px; margin-right: 10px;">
                <input type="radio" name="${_esc(gname)}" value="${_esc(v)}" ${extra} ${disabled}
                    ${current === v ? "checked" : ""} onchange="hcMarkDirty()"> ${_esc(l)}</label>`).join("");
        let html = "";
        if (kind === "list") {
            const marks = rating.items || {};
            html += (Array.isArray(answer) ? answer : []).map((entry, i) => `<div style="margin: 2px 0;">
                <span class="font-medium" style="display: inline-block; min-width: 140px;">${_esc(entry)}</span>
                ${group(`hr-${name}-item-${i}`, st.task.list_item_options || [], marks[entry],
                    `data-list-of="${_esc(name)}" data-entry="${_esc(entry)}"`)}</div>`).join("");
        }
        html += ((st.task.questions || {})[kind] || []).map(q => `<div style="margin-top: 6px;">
            <div class="text-xs font-semibold" style="color: var(--color-text-muted);">${_esc(q.label)}</div>
            ${group(`hr-${name}-${q.id}`, q.options, rating[q.id])}</div>`).join("");
        return html;
    }

    function _cantRateControl() {
        const reasons = st.task.cant_rate_reasons || [];
        return `<details class="text-xs" style="margin-top: 12px;">
            <summary style="cursor: pointer; color: var(--color-text-muted);">Can't rate this video</summary>
            <div style="display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-top: 6px;">
                ${reasons.map(([v, l]) => `<label style="display: flex; align-items: center; gap: 4px;">
                    <input type="radio" name="hc-cant-rate" value="${_esc(v)}"> ${_esc(l)}</label>`).join("")}
                <button class="btn-discreet btn-compact" onclick="hcCantRate(this)">Mark as can't rate</button>
            </div></details>`;
    }

    function _finalQuestionPrompt() {
        const guesses = st.task.guesses || [];
        if (!guesses.length || st.readOnly) return "";
        if (!st.task.items.every(it => _isAnswered(it.item_id))) return "";
        const answered = guesses.filter(g => (st.task.my_guesses || {})[g.key]).length;
        return `<div style="margin-top: 16px; padding: 10px 12px; border: 1px solid var(--color-accent);
                border-radius: 6px;">
            <div class="font-medium">All videos rated. One last question.</div>
            <div class="text-xs" style="color: var(--color-text-muted); margin: 2px 0 6px 0;">
                ${answered === guesses.length
                    ? "Answered. You can look at it again, or submit."
                    : `For ${guesses.length} of the answers you rated, guess whether a person or the model wrote it.`}</div>
            <button class="btn-primary btn-compact" onclick="hcOpenGuesses()">Who wrote these?</button>
        </div>`;
    }

    function renderRatingForm() {
        const box = document.getElementById("hc-rating-form");
        if (st.guessing) { renderGuessForm(box); return; }
        const item = _currentItem();
        const values = (st.responses[item.item_id] || {}).values || {};
        const disabled = st.readOnly ? "disabled" : "";
        const practice = item.practice
            ? `<div class="text-xs" style="margin-bottom: 10px; padding: 6px 10px; border-radius: 6px;
                border: 1px dashed var(--color-border-strong); color: var(--color-text-muted);">
                Practice video: not counted. Use it to get a feel for the questions.</div>` : "";
        if (values.cant_rate) {
            const reason = (st.task.cant_rate_reasons || []).find(([v]) => v === values.cant_rate_reason);
            box.innerHTML = practice + `<div style="padding: 10px 12px; border: 1px solid var(--color-border);
                    border-radius: 6px;">Marked as can't rate${reason ? `: ${_esc(reason[1].toLowerCase())}` : ""}.
                ${st.readOnly ? "" : '<button class="btn-discreet btn-compact" onclick="hcCantRateUndo(this)">Rate it after all</button>'}
                </div>` + _finalQuestionPrompt();
            return;
        }
        const revealed = st.readOnly || st.revealed[item.item_id] || _isAnswered(item.item_id);
        if (!revealed) {
            const ready = document.getElementById("hc-video").dataset.watched === "1";
            box.innerHTML = practice + `<div style="padding: 14px; border: 1px dashed var(--color-border-strong);
                    border-radius: 6px;">
                <div style="margin-bottom: 8px;">Watch the video first. The answers appear once you have watched it.</div>
                <button id="hc-reveal-btn" class="btn-primary btn-compact" ${ready ? "" : "disabled"}
                    onclick="hcRevealAnswers()">Show the answers</button>
                ${_cantRateControl()}
            </div>`;
            return;
        }
        const ratings = values.ratings || {};
        const cards = st.task.variables.map(name => {
            const answer = (item.answers || {})[name];
            if (answer === undefined || answer === null) return "";
            const spec = st.task.field_specs[name] || {};
            const description = spec.description
                ? `<div class="text-xs" style="color: var(--color-text-muted); margin-bottom: 6px;">${_esc(spec.description)}</div>` : "";
            return `<div style="margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid var(--color-border);">
                <div class="font-medium" style="margin-bottom: 2px;">${_esc(spec.label || name)}</div>
                ${description}
                ${_answerHtml(answer, spec)}
                ${_questionsHtml(name, spec, answer, ratings[name] || {}, disabled)}
            </div>`;
        }).join("");
        box.innerHTML = practice + cards + (st.readOnly ? "" : _cantRateControl()) + _finalQuestionPrompt();
    }

    function collectRatingValues() {
        const item = _currentItem();
        const box = document.getElementById("hc-rating-form");
        const previous = (st.responses[item.item_id] || {}).values || {};
        if (previous.cant_rate) return previous;   // saved by its own control
        const ratings = {};
        for (const name of st.task.variables) {
            const spec = st.task.field_specs[name] || {};
            const out = {};
            for (const q of ((st.task.questions || {})[spec.rate_kind] || [])) {
                const checked = box.querySelector(`input[name="hr-${CSS.escape(name)}-${q.id}"]:checked`);
                if (checked) out[q.id] = checked.value;
            }
            if (spec.rate_kind === "list") {
                const marks = {};
                box.querySelectorAll(`input[data-list-of="${CSS.escape(name)}"]:checked`)
                    .forEach(c => { marks[c.dataset.entry] = c.value; });
                if (Object.keys(marks).length) out.items = marks;
            }
            if (Object.keys(out).length) ratings[name] = out;
        }
        return Object.keys(ratings).length ? { cant_rate: false, ratings } : {};
    }

    async function _postRatingValues(btn, values) {
        const item = _currentItem();
        const note = _currentNote();
        try {
            await _busy(btn, "Saving…", () => postJSON(
                `${BASE}/tasks/${encodeURIComponent(st.task.run_id)}/rating/responses`,
                { item_id: item.item_id, values, note }));
            st.responses[item.item_id] = { values: values.cant_rate ? { ...values, ratings: {} } : {}, note };
            st.dirty = false;
            _setSaveState("Saved");
            renderRatingForm();
            renderDots();
            renderProgress();
        } catch (e) {
            _setSaveState(`Save failed: ${e.message}`, true);
        }
    }

    function hcCantRate(btn) {
        const reason = (document.querySelector('#hc-rating-form input[name="hc-cant-rate"]:checked') || {}).value || "";
        return _postRatingValues(btn, { cant_rate: true, cant_rate_reason: reason });
    }

    function hcCantRateUndo(btn) {
        st.revealed[_currentItem().item_id] = true;
        return _postRatingValues(btn, {});
    }

    async function hcOpenGuesses() {
        if (!(await saveCurrent())) return;
        st.guessing = true;
        renderRatingForm();
    }

    function hcCloseGuesses() {
        st.guessing = false;
        renderRatingForm();
    }

    function renderGuessForm(box) {
        const mine = st.task.my_guesses || {};
        const disabled = st.readOnly ? "disabled" : "";
        const options = [["person", "A person"], ["model", "The model"], ["not_sure", "Not sure"]];
        box.innerHTML = `<div class="font-medium">Who wrote these?</div>
            <div class="text-xs" style="color: var(--color-text-muted); margin: 2px 0 8px 0;">
                Some of the answers you rated were written by a person, others by the model.
                For each one, give your best guess.</div>`
            + (st.task.guesses || []).map((g, i) => {
                const spec = st.task.field_specs[g.variable] || {};
                return `<div style="margin: 10px 0; padding: 8px 10px; border: 1px solid var(--color-border);
                        border-radius: 6px;">
                    <div class="text-xxs" style="color: var(--color-text-muted); margin-bottom: 4px;">${_esc(spec.label || g.variable)}</div>
                    ${_answerHtml(g.value, spec)}
                    <div style="display: flex; gap: 14px; margin-top: 6px;">${options.map(([v, l]) =>
                        `<label style="display: flex; align-items: center; gap: 4px;">
                            <input type="radio" name="hc-guess-${i}" data-key="${_esc(g.key)}" value="${v}"
                                ${mine[g.key] === v ? "checked" : ""} ${disabled}> ${l}</label>`).join("")}</div>
                </div>`;
            }).join("")
            + (st.readOnly ? "" : '<button class="btn-primary btn-compact" onclick="hcSaveGuesses(this)">Save my guesses</button> ')
            + '<button class="btn-discreet btn-compact" onclick="hcCloseGuesses()">Back to the videos</button>';
    }

    async function hcSaveGuesses(btn) {
        const guesses = {};
        document.querySelectorAll("#hc-rating-form input[data-key]:checked")
            .forEach(c => { guesses[c.dataset.key] = c.value; });
        try {
            await _busy(btn, "Saving…", () => postJSON(
                `${BASE}/tasks/${encodeURIComponent(st.task.run_id)}/rating/guesses`, { guesses }));
            st.task.my_guesses = { ...(st.task.my_guesses || {}), ...guesses };
            _setSaveState("Saved. You can now submit.");
        } catch (e) {
            _setSaveState(`Save failed: ${e.message}`, true);
        }
    }

    async function hcPickOption(value) {
        if (st.readOnly) return;
        const item = _currentItem();
        const voteButtons = document.querySelectorAll("#hc-vote-form button");
        voteButtons.forEach(b => { b.disabled = true; });
        try {
            await postJSON(
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
            const widget = _fieldWidget(name, spec, value, disabled);
            const description = spec.description
                ? `<div class="text-xs" style="color: var(--color-text-muted); margin-bottom: 6px;">
                    ${_esc(spec.description)}</div>` : "";
            return `<div style="margin-bottom: 16px;">
                <div class="font-medium" style="margin-bottom: 2px;">${_esc(spec.label || name)}</div>
                ${description}
                ${widget}
            </div>`;
        }).join("");
    }

    // One variable's input, from its task field spec. Shared with the admin
    // task preview (window.hcFieldWidget), which passes its own `prefix` so
    // its radio groups never collide with an open coding form.
    function _fieldWidget(name, spec, value, disabled, prefix = "hc") {
        if (spec.kind === "enum" && spec.open) return _openWidget(name, spec, value, disabled, prefix);
        if (spec.kind === "enum" && spec.values && spec.values.length) {
            return spec.values.length <= RADIO_MAX
                ? _radioWidget(name, spec, value, disabled, prefix)
                : _selectWidget(name, spec, value, disabled);
        }
        if (spec.kind === "list") return _listWidget(name, spec, value, disabled);
        if (spec.kind === "numeric") return _numberWidget(name, spec, value, disabled);
        return `<textarea rows="3" data-var="${_esc(name)}" ${disabled}
            onchange="hcMarkDirty()" class="text-sm" style="${INPUT_STYLE} width: 100%;
            padding: 6px 10px; resize: vertical;">${value == null ? "" : _esc(value)}</textarea>`;
    }

    // A codebook answer as the coder sees it ("-" spelled out per field).
    function _optionLabel(spec, v) {
        return (spec.option_labels || {})[v] || v;
    }

    // A label's class attribute plus, when the codebook defines this answer
    // (e.g. a content category), that definition as a hover tooltip.
    function _definitionAttrs(spec, v, baseClass = "") {
        const definition = (spec.value_descriptions || {})[v];
        const classes = [baseClass, definition ? "meta-tooltip" : ""].filter(Boolean).join(" ");
        return (classes ? ` class="${classes}"` : "")
            + (definition ? ` data-tooltip="${_esc(definition)}"` : "");
    }

    function _radio(group, v, label, checked, disabled, muted, attrs = "") {
        return `<label${attrs} style="display: flex; align-items: center; gap: 5px;${muted
            ? " color: var(--color-text-muted);" : ""}">
            <input type="radio" name="${_esc(group)}" value="${_esc(v)}" ${disabled}
                ${checked ? "checked" : ""} onchange="hcMarkDirty()"> ${_esc(label)}
        </label>`;
    }

    // Closed list: the codebook's answers, "Can't tell" where the codebook has
    // no such answer, and "Skip" (saved blank, never counted as an answer).
    function _radioWidget(name, spec, value, disabled, prefix) {
        const group = `${prefix}-radio-${name}`;
        const current = value == null ? "" : String(value);
        const options = spec.values.map(v => _radio(group, v, _optionLabel(spec, v),
            current === String(v), disabled, false, _definitionAttrs(spec, v)));
        if (spec.cant_tell) {
            options.push(_radio(group, CANT_TELL, "Can't tell", current === CANT_TELL, disabled));
        }
        options.push(_radio(group, "", "Skip (not coded)", current === "", disabled, true));
        return `<div style="display: flex; gap: 12px; flex-wrap: wrap;">${options.join("")}</div>`;
    }

    function _selectWidget(name, spec, value, disabled) {
        const current = value == null ? "" : String(value);
        const option = (v, label) =>
            `<option value="${_esc(v)}" ${current === String(v) ? "selected" : ""}>${_esc(label)}</option>`;
        const options = [option("", "— skip (not coded) —")]
            .concat(spec.values.map(v => option(v, _optionLabel(spec, v))))
            .concat(spec.cant_tell ? [option(CANT_TELL, "Can't tell")] : []);
        return `<select data-var="${_esc(name)}" ${disabled} onchange="hcMarkDirty()" class="text-sm"
            style="${INPUT_STYLE} min-width: 220px;">${options.join("")}</select>`;
    }

    // Open category (no codebook list): a typed answer, or one of the
    // field's fixed choices. Typing clears the choice and picking one clears
    // the text, so exactly one of them is the answer.
    function _openWidget(name, spec, value, disabled, prefix) {
        const group = `${prefix}-open-${name}`;
        const current = value == null ? "" : String(value);
        const fixed = [];
        if (spec.dash_label) fixed.push(["-", spec.dash_label]);
        if (spec.cant_tell) fixed.push([CANT_TELL, "Can't tell"]);
        const isFixed = fixed.some(([v]) => v === current);
        const radios = fixed.map(([v, label]) => `<label style="display: flex; align-items: center; gap: 5px;">
                <input type="radio" name="${_esc(group)}" value="${_esc(v)}" ${disabled}
                    ${current === v ? "checked" : ""} onchange="hcOpenPicked(this)"> ${_esc(label)}
            </label>`).join("");
        return `<div data-open-var="${_esc(name)}" style="display: flex; gap: 12px; flex-wrap: wrap;
                align-items: center;">
            <input type="text" data-var="${_esc(name)}" ${disabled} oninput="hcOpenTyped(this)"
                value="${isFixed ? "" : _esc(current)}" placeholder="an English name"
                class="text-sm" style="${INPUT_STYLE} width: 200px;">
            ${radios}
            <span class="text-xs" style="color: var(--color-text-muted);">leave empty to skip</span>
        </div>`;
    }

    function hcOpenTyped(input) {
        const box = input.closest("[data-open-var]");
        if (box && input.value) {
            box.querySelectorAll('input[type="radio"]').forEach(r => { r.checked = false; });
        }
        hcMarkDirty();
    }

    function hcOpenPicked(radio) {
        const box = radio.closest("[data-open-var]");
        const text = box && box.querySelector('input[type="text"]');
        if (text) text.value = "";
        hcMarkDirty();
    }

    // A number on the codebook's own scale (e.g. 0-100), or "Can't tell".
    function _numberWidget(name, spec, value, disabled) {
        const cantTell = value === CANT_TELL;
        const range = Array.isArray(spec.range) ? spec.range : null;
        const bounds = range ? `min="${_esc(range[0])}" max="${_esc(range[1])}"` : "";
        const muted = 'class="text-xs" style="color: var(--color-text-muted);"';
        return `<div data-num-var="${_esc(name)}" style="display: flex; gap: 12px; flex-wrap: wrap;
                align-items: center;">
            <input type="number" step="any" ${bounds} data-var="${_esc(name)}"
                ${disabled || (cantTell ? "disabled" : "")}
                value="${cantTell || value == null ? "" : _esc(value)}" onchange="hcMarkDirty()"
                placeholder="${range ? `${_esc(range[0])}–${_esc(range[1])}` : "number"}"
                class="text-sm" style="${INPUT_STYLE} width: 120px;">
            ${range ? `<span ${muted}>on a ${_esc(range[0])}–${_esc(range[1])} scale</span>` : ""}
            ${spec.cant_tell ? `<label style="display: flex; align-items: center; gap: 5px;">
                <input type="checkbox" data-cant-tell="${_esc(name)}" ${disabled}
                    ${cantTell ? "checked" : ""} onchange="hcNumberCantTell(this)"> Can't tell</label>` : ""}
            <span ${muted}>leave empty to skip</span>
        </div>`;
    }

    function hcNumberCantTell(checkbox) {
        const box = checkbox.closest("[data-num-var]");
        const input = box && box.querySelector('input[type="number"]');
        if (input) {
            input.disabled = checkbox.checked;
            if (checkbox.checked) input.value = "";
        }
        hcMarkDirty();
    }

    function _listWidget(name, spec, value, disabled) {
        const accepted = spec.values;
        const selected = Array.isArray(value) ? value.map(String) : [];
        if (accepted && accepted.length) {
            // Closed list: one checkbox chip per accepted value.
            const chips = accepted.map(v => `<label${_definitionAttrs(spec, v, "text-sm")} style="display: inline-flex;
                    align-items: center; gap: 5px; border: 1px solid var(--color-border);
                    border-radius: 12px; padding: 2px 10px;">
                <input type="checkbox" data-var="${_esc(name)}" value="${_esc(v)}" ${disabled}
                    ${selected.includes(String(v)) ? "checked" : ""} onchange="hcMarkDirty()">
                ${_esc(_optionLabel(spec, v))}
            </label>`).join(" ");
            return `<div data-list-var="${_esc(name)}" data-closed="1"
                style="display: flex; gap: 6px; flex-wrap: wrap;">${chips}</div>`;
        }
        // Open list: free-text entry, one value per line.
        return `<textarea rows="3" data-var="${_esc(name)}" data-open-list="1" ${disabled}
            onchange="hcMarkDirty()" placeholder="one value per line; leave empty for none" class="text-sm"
            style="${INPUT_STYLE} width: 100%; padding: 6px 10px; resize: vertical;">${_esc(selected.join("\n"))}</textarea>`;
    }

    function collectValues() {
        const box = document.getElementById("hc-form");
        const find = (selector) => (box ? box.querySelector(selector) : null);
        const values = {};
        for (const name of st.task.variables) {
            const spec = st.task.field_specs[name] || {};
            const key = CSS.escape(name);
            if (spec.kind === "list") {
                const closed = find(`[data-list-var="${key}"]`);
                if (closed) {
                    values[name] = Array.from(
                        closed.querySelectorAll("input:checked")).map(c => c.value);
                } else {
                    const area = find(`textarea[data-var="${key}"][data-open-list="1"]`);
                    values[name] = area
                        ? area.value.split("\n").map(s => s.trim()).filter(Boolean) : [];
                }
            } else if (spec.kind === "enum" && spec.open) {
                const wrap = find(`[data-open-var="${key}"]`);
                const typed = wrap ? wrap.querySelector('input[type="text"]').value.trim() : "";
                const picked = wrap && wrap.querySelector('input[type="radio"]:checked');
                values[name] = typed || (picked ? picked.value : "");
            } else if (spec.kind === "enum" && spec.values && spec.values.length <= RADIO_MAX) {
                const checked = find(`input[name="hc-radio-${key}"]:checked`);
                values[name] = checked ? checked.value : "";
            } else if (spec.kind === "numeric") {
                const cantTell = find(`input[data-cant-tell="${key}"]`);
                const input = find(`input[type="number"][data-var="${key}"]`);
                values[name] = cantTell && cantTell.checked ? CANT_TELL : (input ? input.value : "");
            } else {
                const el = find(`[data-var="${key}"]`);
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
        if (st.guessing) return true;          // guesses save with their own button
        const item = _currentItem();
        const values = st.mode === "rating" ? collectRatingValues() : collectValues();
        const note = _currentNote();
        const hadResponse = !!st.responses[item.item_id];
        const hasValues = st.mode === "rating"
            ? Object.keys(values).length > 0 : _hasAnyValue(values);
        // Nothing filled in and nothing saved before — nothing to persist.
        if (!st.dirty && !hadResponse) return true;
        if (!hasValues && !note && !hadResponse) return true;
        try {
            await postJSON(
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
        st.guessing = false;
        if (st.idx < st.task.items.length - 1) {
            st.idx += 1;
            renderItem();
        }
    }

    async function hcPrev() {
        if (!(await saveCurrent())) return;
        st.guessing = false;
        if (st.idx > 0) {
            st.idx -= 1;
            renderItem();
        }
    }

    async function hcJump(index) {
        if (!(await saveCurrent())) return;
        st.guessing = false;
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
        const done = st.mode === "rating" ? "rated" : "coded";
        document.getElementById("hc-progress").textContent = st.readOnly
            ? `Submitted — ${answered}/${st.task.items.length} ${done} (read-only)`
            : `${answered}/${st.task.items.length} ${done}`;
    }

    async function hcSubmit(btn) {
        if (!(await saveCurrent())) return;
        const answered = st.task.items.filter(it => _isAnswered(it.item_id)).length;
        const total = st.task.items.length;
        const guesses = st.mode === "rating" ? (st.task.guesses || []) : [];
        const guessed = guesses.filter(g => (st.task.my_guesses || {})[g.key]).length;
        const label = answered < total
            ? `Submit with ${total - answered} ${st.mode === "rating" ? "unrated" : "uncoded"}?`
            : (guessed < guesses.length ? "Submit without the last question?" : "Really submit?");
        if (!_armTwoClick(btn, label)) return;
        try {
            await _busy(btn, "Submitting…", () => postJSON(
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
    window.hcOpenTyped = hcOpenTyped;
    window.hcOpenPicked = hcOpenPicked;
    window.hcNumberCantTell = hcNumberCantTell;
    window.hcFieldWidget = _fieldWidget;
    window.hcRevealAnswers = hcRevealAnswers;
    window.hcCantRate = hcCantRate;
    window.hcCantRateUndo = hcCantRateUndo;
    window.hcOpenGuesses = hcOpenGuesses;
    window.hcCloseGuesses = hcCloseGuesses;
    window.hcSaveGuesses = hcSaveGuesses;

    function _bootstrap() {
        refreshTasks();
        // A task assigned mid-session must show up without a full reload:
        // re-check whenever the My stuff tab is opened, plus a slow poll so
        // the top-level marker appears even while working in another tab.
        const tabBtn = document.querySelector('button[data-subpages-for="my_stuff"]');
        if (tabBtn) tabBtn.addEventListener("click", () => refreshTasks());
        setInterval(refreshTasks, 120000);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", _bootstrap);
    } else {
        _bootstrap();
    }
})();
