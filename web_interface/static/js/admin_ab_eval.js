/**
 * Admin "Annotation testing" sub-page.
 *
 * Manages candidate contracts (/api/manage/ab-candidates), the named
 * evaluation sets (/api/manage/ab-eval-sets + /api/manage/ab-eval-set), run
 * launching (/api/manage/ab-eval/run + /api/status polling), and result
 * comparison (/api/manage/ab-eval/runs). Candidate activation funnels through
 * the NORMAL contract flow: the activate endpoint returns the candidate text +
 * impact, and this module drives the standard confirm POST to
 * /api/manage/annotation-contract. The global fetch wrapper in main.js injects
 * the CSRF header.
 *
 * On the wire a contract under test is still called an "arm" (report.arms,
 * manifest.arms) — in the UI it is always a "candidate contract".
 */
(function () {
    "use strict";

    const CAND = "/api/manage/ab-candidates";
    const EVALSET = "/api/manage/ab-eval-set";
    const EVALSETS = "/api/manage/ab-eval-sets";
    const EVAL = "/api/manage/ab-eval";
    const AC = "/api/manage/annotation-contract";

    // How each column kind is scored, for the results legend + table grouping.
    // Order = display order (most interpretable metric first).
    const KINDS = [
        {
            key: "enum", label: "Categorical",
            blurb: "Exact match after lower-casing. Values meaning “nothing found” " +
                "(none, unknown, unclear, other, -, unable to detect, blank) are treated as " +
                "no value — “no” itself is a real answer.",
        },
        {
            key: "list", label: "List / set",
            blurb: "Jaccard overlap of the two value sets (|A∩B| / |A∪B|). " +
                "Two empty sets score 1.0 — see the “both empty” column. Lists of " +
                "free-text phrases (e.g. main_activity) are shown but excluded from the " +
                "summary means — exact-phrase matching reads wording variation as disagreement.",
        },
        {
            key: "numeric", label: "Numeric",
            blurb: "Exact agreement (share of items where both contracts gave the same " +
                "number) plus the mean absolute difference (Δ̄, in the field's own units), " +
                "over the items both contracts scored. Pearson r is secondary: with " +
                "near-constant scores it tracks rounding noise, not disagreement, and is " +
                "flagged/excluded from the summary in that case.",
        },
        {
            key: "freetext", label: "Free text",
            blurb: "Prose is never string-equal, so no agreement score is computed — " +
                "only how often each contract returned a substantive answer. " +
                "Use the disagreement table below to read the text side by side.",
        },
    ];

    const CAVEATS = {
        both_arms_empty: "Both contracts returned no value for every item — the 1.00 " +
            "agreement means they agreed on finding nothing, not on a value.",
        free_text_elements: "The list elements are free-text phrases, so the Jaccard " +
            "overlap is exact-phrase matching. A low score here can still mean the " +
            "two contracts described the same thing in different words — this field is " +
            "excluded from the summary means.",
        constant: "Both contracts answered the same constant for every item, so Pearson r " +
            "is undefined (0/0). The mean absolute difference is the metric to read.",
        too_few: "Fewer than 3 items were scored by both contracts — too few for a " +
            "meaningful correlation.",
        low_variance: "The paired scores barely vary, so Pearson r reflects rounding " +
            "noise rather than real disagreement — read exact agreement and Δ̄ instead " +
            "(this r is excluded from the summary).",
    };

    const st = {
        candidates: [],
        backends: [],            // [{name, active, availability:{ok,...}}] for the per-arm picker
        // Contracts explicitly added to the current test. Each entry is one
        // arm: {label (unique arm key), source: 'live'|'candidate', name
        // (candidate name, '' for live), backend ('' = gemini default)}.
        // The same contract may be added multiple times (e.g. once per
        // backend); labels get a ~2 / ~3 suffix to stay unique.
        testArms: [{ label: "live", source: "live", name: "", backend: "" }],
        evalSet: { item_ids: [], resolved: [], max_items: 50, name: "" },
        evalSets: { active: "", sets: [] },
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

    // Display name for an arm key: the "live" arm reads "active contract"
    // everywhere the user sees it, and the ~N multi-instance suffix becomes
    // a "(N)" marker. Candidate names pass through unchanged.
    function _armLabel(arm) {
        const m = String(arm).match(/^(.*?)(?:~(\d+))?$/);
        const base = m[1] === "live" ? "active contract" : m[1];
        return m[2] ? `${base} (${m[2]})` : base;
    }

    // The arm's backend from the current run's manifest ("gemini" default).
    function _armBackendOf(arm) {
        const meta = (((st.currentRun || {}).manifest || {}).arms || [])
            .find(a => a.name === arm) || {};
        return meta.backend || "gemini";
    }

    function _status(msg, isError) {
        const el = document.getElementById("abe-status");
        if (!el) return;
        el.textContent = msg || "";
        el.style.color = isError ? "var(--color-danger)" : "var(--color-text-muted)";
    }

    // Status line inside the "Available contracts" card — contract actions
    // (save/duplicate/activate/delete) report here, next to their buttons;
    // the evaluation workflow (sets, runs, results) uses _status further down.
    function _statusContracts(msg, isError) {
        const el = document.getElementById("abe-cand-status");
        if (!el) { _status(msg, isError); return; }
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

    // ---------- candidates ----------

    async function loadCandidates() {
        try {
            const body = await _getJson(CAND);
            st.candidates = body.candidates || [];
            st.defaultContract = body.default_contract || null;
            renderCandidates();
            renderArmPicker();
        } catch (e) {
            _statusContracts(`Failed to load candidates: ${e.message}`, true);
        }
    }

    // Per-backend availability for the per-arm backend picker. A failure just
    // leaves the picker at its gemini-only default (the worker validates the
    // backend again at run start regardless).
    async function loadBackends() {
        try {
            const body = await _getJson("/api/manage/annotation/backends");
            st.backends = body.backends || [];
            renderArmPicker();
        } catch (e) {
            console.warn("ab_eval: backend list unavailable:", e.message);
        }
    }

    // Small pill stating what a row IS (active / built-in).
    function _rowBadge(label, isActive) {
        return '<span class="row-badge text-xxs uppercase font-semibold'
            + (isActive ? " row-badge-active" : "") + '">' + _esc(label) + "</span>";
    }

    // Trailing action: the contract already in use shows a non-interactive
    // state button (same pattern as the Versions page), everything else an
    // Activate button.
    function _activateCell(name, isActive) {
        if (isActive) {
            return '<button class="btn-compact btn-state btn-row-fixed">✓ Active</button>';
        }
        return '<button class="btn-primary btn-compact btn-row-fixed abe-activate" data-n="'
            + _esc(name) + '">Activate</button>';
    }

    function renderCandidates() {
        const tbody = document.getElementById("abe-cand-tbody");
        if (!tbody) return;
        const cell = "padding: 6px 8px; border-bottom: 1px solid var(--color-border); vertical-align: top;";
        const muted = ' style="color: var(--color-text-muted);"';
        const active = st.activeContract || {};
        const activeVersion = active.version || null;
        const d = st.defaultContract;
        const defaultShadowed = !!(d && st.candidates.some(m => m.name === d.name));

        // Does a real row already represent the active contract? If so it just
        // gets the badge; only an otherwise-unrepresented active contract
        // (uploaded directly, or from a since-edited candidate) needs a row.
        const builtinIsActive = !!(d && !defaultShadowed && activeVersion && d.version === activeVersion);
        const candidateIsActive = st.candidates.some(m => activeVersion && m.version === activeVersion);
        const rows = [];

        if (activeVersion && !builtinIsActive && !candidateIsActive) {
            const who = active.updated_by ? _esc(active.updated_by) : "";
            const when = active.updated_at ? _esc(String(active.updated_at).replace("T", " ")) : "";
            rows.push(`<tr>
                <td style="${cell}"><span${muted}>active contract</span> ${_rowBadge("active", true)}
                    <div class="text-xxs" style="color: var(--color-text-muted); min-width: 220px;
                        max-width: 300px;">Uploaded contract, not saved as a candidate</div></td>
                <td style="${cell}" class="font-mono text-xs">${_esc(activeVersion)}</td>
                <td style="${cell}">${_esc(active.n_fields ?? "—")}</td>
                <td style="${cell}" class="text-xs">${when}<br><span${muted}>${who}</span></td>
                <td style="${cell}">
                    <div style="display: flex; flex-wrap: wrap; align-items: center; gap: 4px; min-width: 300px;">
                        <button class="btn-discreet btn-compact abe-view-active">View</button>
                        <button class="btn-discreet btn-compact abe-dl-active">Download</button>
                        <button class="btn-discreet btn-compact abe-dup-active">Duplicate</button>
                        <span style="flex: 1; min-width: 16px;"></span>
                        <button class="btn-compact btn-state btn-row-fixed">✓ Active</button>
                    </div>
                </td>
            </tr>`);
        }

        // The shipped default: inspectable and duplicatable like any contract,
        // and activating it restores the built-in default.
        if (d && !defaultShadowed) {
            rows.push(`<tr>
                <td style="${cell}"><span class="font-mono font-semibold">${_esc(d.name)}</span>
                    ${_rowBadge("built-in", false)}${builtinIsActive ? " " + _rowBadge("active", true) : ""}
                    <div class="text-xxs" style="color: var(--color-text-muted); min-width: 220px;
                        max-width: 300px;">The standard contract that comes with the Data Hub —
                        always listed here, so you can return to it at any time by pressing
                        Activate.</div></td>
                <td style="${cell}" class="font-mono text-xs">${_esc(d.version || "—")}</td>
                <td style="${cell}">${_esc(d.n_fields ?? "—")}</td>
                <td style="${cell} color: var(--color-text-muted);" class="text-xs">—</td>
                <td style="${cell}">
                    <div style="display: flex; flex-wrap: wrap; align-items: center; gap: 4px; min-width: 300px;">
                        <button class="btn-discreet btn-compact abe-view" data-n="${_esc(d.name)}">View</button>
                        <button class="btn-discreet btn-compact abe-dl" data-n="${_esc(d.name)}">Download</button>
                        <button class="btn-discreet btn-compact abe-dup" data-n="${_esc(d.name)}">Duplicate</button>
                        <span style="flex: 1; min-width: 16px;"></span>
                        ${_activateCell(d.name, builtinIsActive)}
                    </div>
                </td>
            </tr>`);
        }

        for (const m of st.candidates) {
            const isActive = !!(activeVersion && m.version === activeVersion);
            rows.push(`<tr>
                <td style="${cell}"><span class="font-mono font-semibold">${_esc(m.name)}</span>
                    ${isActive ? " " + _rowBadge("active", true) : ""}</td>
                <td style="${cell}" class="font-mono text-xs">${_esc(m.version || "—")}</td>
                <td style="${cell}">${_esc(m.n_fields ?? "—")}</td>
                <td style="${cell}" class="text-xs">${_esc((m.created_at || "").replace("T", " "))}<br>
                    <span${muted}>${_esc(m.created_by || "")}</span></td>
                <td style="${cell}">
                    <div style="display: flex; flex-wrap: wrap; align-items: center; gap: 4px; min-width: 300px;">
                        <button class="btn-discreet btn-compact abe-view" data-n="${_esc(m.name)}">View</button>
                        <button class="btn-discreet btn-compact abe-dl" data-n="${_esc(m.name)}">Download</button>
                        <button class="btn-discreet btn-compact abe-edit" data-n="${_esc(m.name)}">Edit</button>
                        <button class="btn-discreet btn-compact abe-dup" data-n="${_esc(m.name)}">Duplicate</button>
                        <button class="btn-discreet btn-compact abe-add" data-n="${_esc(m.name)}">Add to test</button>
                        <span style="flex: 1; min-width: 16px;"></span>
                        ${_activateCell(m.name, isActive)}
                        <button class="btn-danger btn-compact abe-del" data-n="${_esc(m.name)}">✕</button>
                    </div>
                </td>
            </tr>`);
        }

        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="5" class="text-sm" style="padding: 10px 8px;'
                + ' color: var(--color-text-muted);">No contracts to show — upload one to get started.</td></tr>';
            return;
        }
        tbody.innerHTML = rows.join("");

        tbody.querySelectorAll(".abe-view").forEach(b =>
            b.addEventListener("click", () => viewContract(b.dataset.n, b)));
        tbody.querySelectorAll(".abe-view-active").forEach(b =>
            b.addEventListener("click", () => viewActiveContract(b)));
        tbody.querySelectorAll(".abe-dl").forEach(b =>
            b.addEventListener("click", () => downloadCandidate(b.dataset.n, b)));
        tbody.querySelectorAll(".abe-edit").forEach(b =>
            b.addEventListener("click", () => editCandidate(b.dataset.n)));
        tbody.querySelectorAll(".abe-dup").forEach(b =>
            b.addEventListener("click", () => duplicateCandidate(b.dataset.n, b)));
        tbody.querySelectorAll(".abe-add").forEach(b =>
            b.addEventListener("click", () => abeAddToTest(b.dataset.n)));
        tbody.querySelectorAll(".abe-activate").forEach(b =>
            b.addEventListener("click", () => activateCandidate(b.dataset.n, b)));
        tbody.querySelectorAll(".abe-del").forEach(b =>
            b.addEventListener("click", () => deleteCandidate(b.dataset.n, b)));
        // The pinned active-contract row has no candidate name behind it: it
        // reads the live contract endpoints instead.
        tbody.querySelectorAll(".abe-dl-active").forEach(b =>
            b.addEventListener("click", () => abeDownloadActive()));
        tbody.querySelectorAll(".abe-dup-active").forEach(b =>
            b.addEventListener("click", () => duplicateActiveContract(b)));
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

    async function abeConfirmName(btn) {
        const input = document.getElementById("abe-name-input");
        const err = document.getElementById("abe-name-error");
        const name = (input && input.value || "").trim();
        if (!/^[a-z0-9_\-]{1,40}$/.test(name)) {
            if (err) err.textContent = "1-40 chars: lowercase letters, digits, '_' or '-'.";
            return;
        }
        if (!nameFlow.text) { abeCancelName(); return; }
        try {
            await _busy(btn, "Saving…", () =>
                _postJson(CAND, { name, text: nameFlow.text, overwrite: nameFlow.overwrite }));
            abeCancelName();
            _statusContracts(`Candidate '${name}' saved.`);
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

    // Duplicate an existing candidate: fetch its text and stage it in the
    // shared naming row — same save path as the other create-candidate flows,
    // including the two-click overwrite handling.
    async function duplicateCandidate(name, btn) {
        try {
            const body = await _busy(btn, "…", () => _getJson(`${CAND}/${encodeURIComponent(name)}`));
            nameFlow.text = body.text;
            _showNameRow(`Duplicate '${name}' as:`, `${name}-copy`.slice(0, 40));
        } catch (e) {
            _statusContracts(`Duplicate failed: ${e.message}`, true);
        }
    }

    async function duplicateActiveContract(btn) {
        try {
            const dl = await _busy(btn, "Reading…", () => fetch(`${AC}/download`));
            if (!dl.ok) throw new Error("could not read the active contract");
            nameFlow.text = await dl.text();
            _showNameRow("Duplicate the active contract as:", "active-copy");
        } catch (e) {
            _statusContracts(`Duplicate failed: ${e.message}`, true);
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
            _statusContracts(`Upload failed: ${e.message}`, true);
        }
    }

    // Save the candidate's TOML text as a local <name>.toml file.
    async function downloadCandidate(name, btn) {
        try {
            const body = await _busy(btn, "…", () => _getJson(`${CAND}/${encodeURIComponent(name)}`));
            const blob = new Blob([body.text || ""], { type: "application/toml" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `${name}.toml`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
        } catch (e) {
            _statusContracts(`Download failed: ${e.message}`, true);
        }
    }

    // ---------- contract preview ("View") ----------

    // Read-only preview of any contract in the table — including the built-in
    // default and the pinned active contract. Shows the response fields and
    // the exact instructions (prompt) the AI model would receive, rendered by
    // the same code path the annotator uses.
    function _showContractModal(title, contract, prompt, schema) {
        const modal = document.getElementById("abe-item-modal");
        const body = document.getElementById("abe-item-body");
        if (!modal || !body) return;
        _setItemModalChrome(false);
        document.getElementById("abe-item-id").textContent = title;
        const fields = (contract && contract.fields) || [];
        const pre = 'max-height: 45vh; overflow: auto; background: var(--color-bg-elevated);'
            + ' padding: 12px; border-radius: 6px; white-space: pre-wrap; margin: 4px 0 0 0;';
        const parts = [];
        parts.push(`<div class="text-sm" style="margin-bottom: 10px;">`
            + `<span style="color: var(--color-text-muted);">Response fields (${fields.length}):</span> `
            + `<span class="font-mono">${fields.map(f => _esc(f.name)).join(", ") || "—"}</span></div>`);
        parts.push(`<div class="text-sm" style="color: var(--color-text-muted);">`
            + `Instructions sent to the AI model for every video:</div>`);
        parts.push(`<pre class="text-xs" style="${pre}">${_esc(prompt || "(none)")}</pre>`);
        if (schema) {
            parts.push(`<details style="margin-top: 10px;"><summary class="text-sm"`
                + ` style="color: var(--color-text-muted); cursor: pointer;">`
                + `Required answer format (technical)</summary>`
                + `<pre class="text-xs" style="${pre}">${_esc(JSON.stringify(schema, null, 2))}</pre></details>`);
        }
        body.innerHTML = parts.join("");
        modal.style.display = "flex";
    }

    async function viewContract(name, btn) {
        try {
            const cand = await _busy(btn, "…", () => _getJson(`${CAND}/${encodeURIComponent(name)}`));
            const rendered = await _postJson(`${AC}/preview`, { contract: cand.contract });
            if (rendered.valid === false) {
                throw new Error((rendered.errors || []).join("; ") || "contract does not validate");
            }
            _showContractModal(`Contract '${name}'`, cand.contract, rendered.prompt, rendered.schema);
        } catch (e) {
            _statusContracts(`Could not show '${name}': ${e.message}`, true);
        }
    }

    async function viewActiveContract(btn) {
        try {
            const parsed = await _busy(btn, "…", () => _getJson(`${AC}/parsed`));
            const rendered = await _getJson(`${AC}/rendered`);
            _showContractModal("Active contract", parsed.contract, rendered.prompt, rendered.schema);
        } catch (e) {
            _statusContracts(`Could not show the active contract: ${e.message}`, true);
        }
    }

    function editCandidate(name) {
        // The Phase-2 form editor supports a candidate save target: it hydrates
        // from the candidate and saves back via POST /api/manage/ab-candidates.
        if (typeof window.aceOpen === "function") {
            window.aceOpen({ candidate: name });
        } else {
            _statusContracts("The contract form editor is not loaded on this page.", true);
        }
    }

    // Staged activation: the activate endpoint's dry-run result, awaiting the
    // modal's confirm click. switchBackend carries the tested backend when the
    // "also switch" checkbox applies.
    let pendingActivate = null;

    // Activate a contract: dry-run the impact (optionally against the backend
    // it was tested on), confirm in the modal, then drive the normal contract
    // upload flow. `testedBackend` comes from the run manifest's arm.
    async function activateCandidate(name, btn, testedBackend) {
        try {
            const body = await _busy(btn, "Checking…", () =>
                _postJson(`${CAND}/${encodeURIComponent(name)}/activate`,
                    testedBackend ? { backend: testedBackend } : {}));
            const impact = body.impact || {};
            const be = body.backend || {};
            const offerSwitch = !!(be.mismatch && be.can_switch_backend && be.target_available);
            // When a backend switch is on offer, also dry-run the no-switch
            // outcome so the modal can show the truth for either checkbox state.
            let impactNoSwitch = impact;
            if (offerSwitch) {
                try {
                    const alt = await _postJson(`${CAND}/${encodeURIComponent(name)}/activate`, {});
                    impactNoSwitch = alt.impact || impact;
                } catch (e) { /* fall back to the switch impact */ }
            }
            pendingActivate = { name, text: body.text, etag: body.current_etag,
                                builtinDefault: !!body.builtin_default };

            // One self-contained outcome statement per checkbox state: version
            // consequence plus the backend + model new annotations run on.
            function outcomeHtml(im) {
                const backendPart = `new annotations will run on backend `
                    + `<strong>${_esc(im.target_backend || "gemini")}</strong>`
                    + (im.target_model ? ` · <span class="font-mono">${_esc(im.target_model)}</span>` : "");
                if (im.metadata_only) {
                    return `<div style="color: var(--color-success); margin-bottom: 10px;">`
                        + `✓ Metadata-only change — <strong>no new annotation version</strong>; `
                        + `existing annotations stay valid, and ${backendPart}.</div>`;
                }
                return `<div style="color: var(--color-warning); margin-bottom: 10px;">`
                    + `⚠ A new annotation version <span class="font-mono">${_esc(im.candidate_version)}</span> `
                    + `will be created and become the <strong>active</strong> version (replacing `
                    + `<span class="font-mono">${_esc(im.active_version)}</span>), and ${backendPart}. `
                    + `Studies keep using the preferred version until you promote it under <em>Versions</em>.</div>`;
            }

            const rows = [];
            if (offerSwitch) {
                // Context → choice → outcome, with the outcome re-rendered on
                // checkbox change so the two never disagree.
                rows.push(`<div style="margin-bottom: 10px;">This contract was tested on backend `
                    + `<strong>${_esc(be.target)}</strong>; the active backend is currently `
                    + `<strong>${_esc(be.active)}</strong>.</div>`);
                rows.push(`<div style="margin-bottom: 10px;">`
                    + `<label class="text-sm" style="display: flex; gap: 8px; align-items: baseline; cursor: pointer;">`
                    + `<input type="checkbox" id="abe-switch-backend" checked> `
                    + `<span>Switch the active annotation backend to `
                    + `<strong>${_esc(be.target)}</strong> as part of the activation.</span></label></div>`);
                rows.push(`<div id="abe-activate-outcome">${outcomeHtml(impact)}</div>`);
            } else {
                rows.push(outcomeHtml(impact));
                if (be.mismatch && !be.can_switch_backend) {
                    rows.push(`<div style="color: var(--color-warning); margin-bottom: 10px;">`
                        + `⚠ This contract was tested on <strong>${_esc(be.target)}</strong>, but switching `
                        + `backends requires the Backends admin permission — after activation it will run on `
                        + `<strong>${_esc(be.active)}</strong>.</div>`);
                } else if (be.mismatch && !be.target_available) {
                    rows.push(`<div style="color: var(--color-warning); margin-bottom: 10px;">`
                        + `⚠ This contract was tested on <strong>${_esc(be.target)}</strong>, which is not `
                        + `available here (${_esc(be.target_unavailable_reason || "unavailable")}) — after `
                        + `activation it will run on <strong>${_esc(be.active)}</strong>.</div>`);
                }
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
            _setItemModalChrome(false);
            document.getElementById("abe-item-id").textContent = `Activate contract '${name}'`;
            document.getElementById("abe-item-body").innerHTML = rows.join("")
                + '<ul style="margin: 6px 0 0 18px; padding: 0;" class="text-sm">'
                + detail.map(d => `<li>${d}</li>`).join("") + "</ul>"
                + `<div style="display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px;
                        padding-top: 12px; border-top: 1px solid var(--color-border);">
                    <button onclick="abeCloseItemModal()" class="btn-discreet btn-compact">Cancel</button>
                    <button onclick="abeConfirmActivate(this)" class="btn-save btn-compact">Activate contract</button>
                </div>`;
            pendingActivate.switchBackend = offerSwitch ? be.target : null;
            const switchCb = document.getElementById("abe-switch-backend");
            if (switchCb) {
                switchCb.addEventListener("change", () => {
                    const out = document.getElementById("abe-activate-outcome");
                    if (out) out.innerHTML = outcomeHtml(switchCb.checked ? impact : impactNoSwitch);
                });
            }
            if (modal) modal.style.display = "flex";
        } catch (e) {
            _statusContracts(`Activate failed: ${e.message}`, true);
        }
    }

    async function abeConfirmActivate(btn) {
        if (!pendingActivate) { abeCloseItemModal(); return; }
        const { name, text, etag, switchBackend, builtinDefault } = pendingActivate;
        // Respect the (pre-checked) opt-out checkbox when it was offered.
        const checkbox = document.getElementById("abe-switch-backend");
        const doSwitch = switchBackend && (!checkbox || checkbox.checked);
        try {
            // Activating the shipped default = revert: remove the runtime
            // override (so future shipped contract updates apply) instead of
            // uploading identical text as a new runtime contract.
            const payload = { text, confirm: true, expected_etag: etag };
            if (doSwitch) payload.switch_backend = switchBackend;
            const res = await _busy(btn, "Activating…", () =>
                builtinDefault ? _postJson(`${AC}/revert`, {}) : _postJson(AC, payload));
            pendingActivate = null;
            abeCloseItemModal();
            _statusContracts(res.note || `'${name}' is now the active contract.`);
            document.dispatchEvent(new CustomEvent("fyp:contract-changed"));
        } catch (e) {
            abeCloseItemModal();
            if (e.status === 409) {
                _statusContracts("Rejected: the active contract changed underneath — reload and retry.", true);
            } else if (e.status === 403) {
                _statusContracts("Rejected: switching the annotation backend requires the Backends admin permission.", true);
            } else {
                _statusContracts(`Activate failed: ${e.message}`, true);
            }
        }
    }

    async function deleteCandidate(name, btn) {
        if (!_armTwoClick(btn, "sure?")) return;
        try {
            await _busy(btn, "…", () =>
                _postJson(`${CAND}/${encodeURIComponent(name)}`, undefined, "DELETE"));
            _statusContracts(`Candidate '${name}' deleted.`);
            // Drop any test arms that referenced the deleted candidate.
            st.testArms = st.testArms.filter(a => a.source !== "candidate" || a.name !== name);
            await loadCandidates();
        } catch (e) {
            _statusContracts(`Delete failed: ${e.message}`, true);
        }
    }

    // ---------- test sets ----------

    async function loadEvalSets() {
        try {
            st.evalSets = await _getJson(EVALSETS);
            renderSetPicker();
        } catch (e) {
            _status(`Failed to load test sets: ${e.message}`, true);
        }
    }

    function renderSetPicker() {
        const picker = document.getElementById("abe-set-picker");
        const del = document.getElementById("abe-set-delete");
        const sets = st.evalSets.sets || [];
        if (picker) {
            picker.innerHTML = sets.map(s =>
                `<option value="${_esc(s.name)}">${_esc(s.name)} (${s.n_items})</option>`).join("");
            picker.value = st.evalSets.active || "";
        }
        // The last remaining set has nothing to fall back to, so deleting it is
        // refused server-side; don't offer the button either.
        if (del) del.disabled = sets.length < 2;
    }

    async function loadEvalSet(name) {
        try {
            const body = await _getJson(name ? `${EVALSET}?name=${encodeURIComponent(name)}` : EVALSET);
            st.evalSet = body;
            st.setDirty = false;
            renderEvalSet();
        } catch (e) {
            _status(`Failed to load test set: ${e.message}`, true);
        }
    }

    async function abeSelectSet(name) {
        if (!name || name === st.evalSet.name) return;
        if (st.setDirty && !_confirmDiscard()) {
            const picker = document.getElementById("abe-set-picker");
            if (picker) picker.value = st.evalSet.name || "";
            return;
        }
        try {
            // Selecting a set also makes it the one a run will use.
            st.evalSet = await _postJson(`${EVALSETS}/${encodeURIComponent(name)}/activate`);
            st.setDirty = false;
            st.evalSets.active = name;
            renderEvalSet();
            refreshEstimate();
            _status(`Test set '${name}' selected.`);
        } catch (e) {
            _status(`Select failed: ${e.message}`, true);
        }
    }

    // Two-step guard for discarding unsaved pill edits (no native confirm()).
    let discardArmed = false;
    function _confirmDiscard() {
        if (discardArmed) { discardArmed = false; return true; }
        discardArmed = true;
        setTimeout(() => { discardArmed = false; }, 4000);
        _status("You have unsaved changes to this set. Repeat the action within 4s to discard them.", true);
        return false;
    }

    // Inline naming row for the three set-creation flows.
    const setNameFlow = { mode: null };

    function _showSetNameRow(mode, purpose, defaultName) {
        setNameFlow.mode = mode;
        const row = document.getElementById("abe-setname-row");
        const purposeEl = document.getElementById("abe-setname-purpose");
        const input = document.getElementById("abe-setname-input");
        const err = document.getElementById("abe-setname-error");
        if (purposeEl) purposeEl.textContent = purpose;
        if (err) err.textContent = "";
        if (row) row.style.display = "flex";
        if (input) { input.value = defaultName || ""; input.focus(); input.select(); }
    }

    function abeCancelSetName() {
        setNameFlow.mode = null;
        const row = document.getElementById("abe-setname-row");
        if (row) row.style.display = "none";
    }

    function abeNewSet() {
        if (st.setDirty && !_confirmDiscard()) return;
        _showSetNameRow("new", "Name the new (empty) test set:", "");
    }

    function abeDuplicateSet() {
        if (st.setDirty && !_confirmDiscard()) return;
        _showSetNameRow("duplicate", `Copy '${st.evalSet.name}' into a new set named:`,
            `${(st.evalSet.name || "set")}-copy`.slice(0, 40));
    }

    function abeRenameSet() {
        _showSetNameRow("rename", `Rename '${st.evalSet.name}' to:`, st.evalSet.name || "");
    }

    async function abeConfirmSetName(btn) {
        const input = document.getElementById("abe-setname-input");
        const err = document.getElementById("abe-setname-error");
        const name = (input && input.value || "").trim();
        if (!/^[a-z0-9_\-]{1,40}$/.test(name)) {
            if (err) err.textContent = "1-40 chars: lowercase letters, digits, '_' or '-'.";
            return;
        }
        if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
        try {
            if (setNameFlow.mode === "rename") {
                await _postJson(`${EVALSETS}/${encodeURIComponent(st.evalSet.name)}/rename`,
                    { new_name: name });
            } else {
                await _postJson(EVALSETS, {
                    name,
                    copy_from: setNameFlow.mode === "duplicate" ? st.evalSet.name : undefined,
                });
            }
            abeCancelSetName();
            await loadEvalSets();
            await loadEvalSet(name);
            refreshEstimate();
            _status(`Test set '${name}' ready.`);
        } catch (e) {
            if (err) err.textContent = e.message;
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = "Save"; }
        }
    }

    async function abeDeleteSet(btn) {
        const name = st.evalSet.name;
        if (!name) return;
        if (!_armTwoClick(btn, "Delete — sure?")) return;
        try {
            await _busy(btn, "Deleting…", () =>
                _postJson(`${EVALSETS}/${encodeURIComponent(name)}`, undefined, "DELETE"));
            st.setDirty = false;
            await loadEvalSets();
            await loadEvalSet(st.evalSets.active);
            refreshEstimate();
            _status(`Test set '${name}' deleted.`);
        } catch (e) {
            _status(`Delete failed: ${e.message}`, true);
        }
    }

    function renderEvalSet() {
        const pills = document.getElementById("abe-set-pills");
        const count = document.getElementById("abe-set-count");
        const save = document.getElementById("abe-set-save");
        const picker = document.getElementById("abe-set-picker");
        const ids = st.evalSet.item_ids || [];
        const resolved = {};
        (st.evalSet.resolved || []).forEach(r => { resolved[r.item_id] = r; });
        if (picker && st.evalSet.name) picker.value = st.evalSet.name;
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
        _status("Saving test set…");
        try {
            const body = await _postJson(EVALSET, {
                item_ids: st.evalSet.item_ids || [], name: st.evalSet.name || undefined,
            });
            st.evalSet = { ...st.evalSet, ...body };
            st.setDirty = false;
            const notDownloaded = (body.not_downloaded || []).length;
            const unknown = (body.resolved || []).filter(r => r.platform == null && r.downloaded == null)
                .map(r => r.item_id);
            const warnings = [];
            if (unknown.length) warnings.push(`${unknown.length} id(s) not found in the dataset: ${unknown.join(", ")}`);
            if (notDownloaded) warnings.push(`${notDownloaded} item(s) have no downloaded media`);
            _status(warnings.length ? `Saved — warning: ${warnings.join("; ")}.` : "Test set saved.", warnings.length > 0);
            await loadEvalSets();
            renderEvalSet();
            refreshEstimate();
        } catch (e) {
            _status(`Save failed: ${e.message}`, true);
        } finally {
            if (btn) { btn.textContent = "Save"; btn.disabled = !st.setDirty; }
        }
    }

    // ---------- run ----------

    // Per-arm backend selector (the only per-arm setting; run-scoped and
    // recorded in the run manifest — production config untouched).
    function _armBackendSelect(arm) {
        const selectStyle = 'padding: 2px 6px; border: 1px solid var(--color-border);'
            + ' border-radius: 4px; background: var(--color-bg-input);'
            + ' color: var(--color-text-primary); width: 150px;';
        const opt = (value, label, disabled, selected) =>
            `<option value="${_esc(value)}" ${disabled ? "disabled" : ""}
                ${selected ? "selected" : ""}>${_esc(label)}</option>`;
        const options = [opt("", "gemini (default)", false, !arm.backend)].concat(
            st.backends
                .filter(b => b.name !== "gemini")
                .map(b => {
                    const ok = b.availability && b.availability.ok;
                    // Config-declared variants show their label + implementation.
                    const isVariant = b.backend && b.backend !== b.name;
                    const display = (b.label && b.label !== b.name) ? b.label : b.name;
                    return opt(b.name,
                        display + (isVariant ? ` [${b.backend}]` : "")
                            + (ok ? "" : " (unavailable)"),
                        !ok, arm.backend === b.name);
                }));
        return `<select class="abe-arm-backend text-xs" data-arm="${_esc(arm.label)}"
            title="Annotation backend for this contract"
            style="${selectStyle}" onchange="abeArmBackendChanged(this)">${options.join("")}</select>`;
    }

    // Mint a unique arm label for one more instance of a contract.
    function _newArmLabel(base) {
        const taken = new Set(st.testArms.map(a => a.label));
        if (!taken.has(base)) return base;
        let i = 2;
        while (taken.has(`${base}~${i}`)) i++;
        return `${base}~${i}`;
    }

    function abeAddToTest(name) {
        st.testArms.push({ label: _newArmLabel(name), source: "candidate", name, backend: "" });
        renderArmPicker();
    }

    function abeAddLiveToTest() {
        st.testArms.push({ label: _newArmLabel("live"), source: "live", name: "", backend: "" });
        renderArmPicker();
    }

    function abeRemoveFromTest(label) {
        st.testArms = st.testArms.filter(a => a.label !== label);
        renderArmPicker();
    }

    function abeArmBackendChanged(select) {
        const arm = st.testArms.find(a => a.label === select.dataset.arm);
        if (arm) arm.backend = select.value;
        refreshEstimate();
    }

    function renderArmPicker() {
        const el = document.getElementById("abe-arm-picker");
        if (!el) return;
        // The "Contracts in this test run / AI model" column titles only make
        // sense above actual rows.
        const header = document.getElementById("abe-arm-header");
        if (header) header.style.display = st.testArms.length ? "flex" : "none";
        if (!st.testArms.length) {
            el.innerHTML = '<div class="text-sm" style="color: var(--color-text-muted);">'
                + 'No contracts in this test run yet — press “Add to test” on a contract'
                + ' in the table above (or “+ active contract” below).</div>';
            refreshEstimate();
            return;
        }
        el.innerHTML = st.testArms.map(arm => {
            const display = arm.source === "live"
                ? "<span>active contract</span>"
                : `<span class="font-mono">${_esc(arm.name)}</span>`;
            const suffix = arm.label.includes("~")
                ? ` <span class="text-xs" style="color: var(--color-text-muted);">(${_esc(arm.label.split("~")[1])})</span>`
                : "";
            return `<div style="display: flex; align-items: center; gap: 8px;">
                <span class="text-sm" style="min-width: 180px;">${display}${suffix}</span>
                ${_armBackendSelect(arm)}
                <button class="btn-discreet btn-compact" title="Remove from this test run"
                    onclick="abeRemoveFromTest('${_esc(arm.label)}')">&times;</button>
            </div>`;
        }).join("");
        refreshEstimate();
    }

    // The arms_spec payload for /run: one entry per test arm, labels unique.
    function _armsSpec() {
        return st.testArms.map(a => ({
            source: a.source,
            name: a.name || undefined,
            label: a.label,
            backend: a.backend || undefined,
        }));
    }

    function refreshEstimate() {
        const el = document.getElementById("abe-estimate");
        if (!el) return;
        const nArms = st.testArms.length;
        const nItems = (st.evalSet.item_ids || []).length;
        const unsaved = st.setDirty ? " (using the SAVED set — you have unsaved set edits)" : "";
        const setName = st.evalSet.name ? ` from set '${st.evalSet.name}'` : "";
        el.textContent = nArms && nItems
            ? `${nArms} contract(s) × ${nItems} video(s)${setName} = `
                + `${nArms * nItems} annotation calls in this test run${unsaved}`
            : "Add at least one contract to the test run (step 2) and choose at least one test video (step 1).";
    }

    async function abeStartRun() {
        if (!st.testArms.length) { _status("Add at least one contract to the test run.", true); return; }
        const runBtn = document.getElementById("abe-run-btn");
        try {
            // First click: fetch the authoritative estimate (visible feedback
            // while it loads), then arm the button with the real call count.
            if (!runBtn || runBtn.dataset.armed !== "1") {
                if (runBtn) { runBtn.disabled = true; runBtn.textContent = "Checking cost…"; }
                _status("Checking the cost of this test run…");
                let est;
                try {
                    est = await _postJson(`${EVAL}/estimate`, { n_arms: st.testArms.length });
                } finally {
                    if (runBtn) { runBtn.disabled = false; runBtn.textContent = "Start test run…"; }
                }
                if (!est.n_items) { _status("The saved test set is empty — save it first.", true); return; }
                _armTwoClick(runBtn, `Confirm: ${est.n_calls} annotation calls?`);
                _status(st.setDirty
                    ? "Note: the test run uses the last SAVED test videos — you have unsaved edits. Click again to start."
                    : "Click again to start the test run.", st.setDirty);
                return;
            }
            // Second click (armed): lock the button through the whole start
            // request so a double-click can never dispatch two runs.
            _armTwoClick(runBtn, "");
            _setRunning(true);
            _status("Starting test run…");
            const nameInput = document.getElementById("abe-run-name");
            const body = await _postJson(`${EVAL}/run`, {
                arms_spec: _armsSpec(),
                eval_set: st.evalSet.name || undefined,
                name: (nameInput && nameInput.value.trim()) || undefined,
            });
            _status(`Test run ${body.run_id} started.`);
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
                    _status(`Test run failed: ${entry.last_message || "see logs"}`, true);
                } else {
                    _status(`Test run ${runId} finished.`);
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

    async function abeCancelRun(btn) {
        try {
            await _busy(btn, "Cancelling…", () => _postJson("/api/stop/ab_eval", {}));
            _status("Cancel requested — the test run stops after the in-flight items.");
        } catch (e) {
            _status(`Cancel failed: ${e.message}`, true);
        }
    }

    // ---------- results ----------

    async function abeRefreshRuns(btn) {
        try {
            const body = await _busy(btn && btn.tagName === "BUTTON" ? btn : null,
                "Refreshing…", () => _getJson(`${EVAL}/runs`));
            st.runs = body.runs || [];
            const picker = document.getElementById("abe-run-picker");
            if (picker) {
                picker.innerHTML = st.runs.length
                    ? st.runs.map(r => {
                        const set = r.eval_set ? ` · set ${r.eval_set}` : "";
                        const label = r.name ? `${r.name} · ` : "";
                        return `<option value="${_esc(r.run_id)}">${_esc(label)}${_esc(r.run_id)} · `
                            + `${_esc((r.arms || []).map(_armLabel).join(" vs "))}${_esc(set)} · ${_esc(r.status)}</option>`;
                    }).join("")
                    : '<option value="">(no runs yet)</option>';
                // Auto-load a run so results are visible without touching the
                // dropdown (a single-run picker fires no change event at all).
                if (st.runs.length) {
                    const keep = st.currentRunId
                        && st.runs.some(r => r.run_id === st.currentRunId);
                    const target = keep ? st.currentRunId : st.runs[0].run_id;
                    picker.value = target;
                    if (!keep || !st.currentRun) abeLoadRun(target);
                }
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
        const picker = document.getElementById("abe-run-picker");
        if (picker) picker.disabled = true;
        try {
            st.currentRun = await _getJson(`${EVAL}/runs/${encodeURIComponent(runId)}`);
            renderRun();
        } catch (e) {
            container.innerHTML = `<span class="text-sm" style="color: var(--color-danger);">${_esc(e.message)}</span>`;
        } finally {
            if (picker) picker.disabled = false;
        }
    }

    async function abeDeleteRun() {
        if (!st.currentRunId) return;
        const btn = document.getElementById("abe-run-delete");
        if (!_armTwoClick(btn, "Delete — sure?")) return;
        try {
            await _busy(btn, "Deleting…", () =>
                _postJson(`${EVAL}/runs/${encodeURIComponent(st.currentRunId)}`, undefined, "DELETE"));
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

    function _pct(x) {
        return (x == null || Number.isNaN(x)) ? "—" : `${Math.round(Number(x) * 100)}%`;
    }

    // Explicit input/output token breakdown for an arm's cost dict — priced
    // API calls bill input and output separately, so both matter. Thinking
    // tokens (billed as output on Gemini) are broken out when present.
    function _tokensLine(c) {
        const n = (x) => (typeof x === "number") ? x.toLocaleString("en-US") : "—";
        const thoughts = (typeof c.thoughts_tokens === "number" && c.thoughts_tokens > 0)
            ? ` (+${n(c.thoughts_tokens)} thinking)` : "";
        return `tokens: ${n(c.prompt_tokens)} in · ${n(c.candidates_tokens)} out${thoughts}`
            + ` · ${n(c.total_tokens)} total`;
    }

    // Approximate dollar spend from the config-maintained [machine.pricing]
    // table (computed server-side per run). Empty when no price is configured
    // for the arm's model; a "+" marks partially-priced runs (some rows'
    // models missing from the table).
    function _costLine(c) {
        if (typeof c.cost_usd !== "number") return "";
        const digits = c.cost_usd >= 1 ? 2 : 4;
        const plus = (c.unpriced_rows > 0) ? "+" : "";
        return `cost ≈ $${c.cost_usd.toFixed(digits)}${plus} · `;
    }

    // The headline metric of a column, used ONLY to sort within its own kind.
    // Correlation (−1..1), agreement (0..1) and Jaccard (0..1) are not on a
    // common scale, which is exactly why the table is grouped by kind rather
    // than sorted across kinds.
    function _metricOf(colMeta) {
        if (colMeta.kind === "numeric") {
            // Pre-fix reports have no exact_agreement — fall back to r.
            return colMeta.exact_agreement != null ? colMeta.exact_agreement : colMeta.correlation;
        }
        if (colMeta.kind === "enum") return colMeta.agreement;
        if (colMeta.kind === "list") return colMeta.mean_jaccard;
        return null;
    }

    function _caveatIcon(caveat) {
        if (!caveat || !CAVEATS[caveat]) return "";
        return ` <span class="meta-tooltip" data-tooltip="${_esc(CAVEATS[caveat])}"
            style="color: var(--color-warning); cursor: help;">⚠</span>`;
    }

    function renderRun() {
        const container = document.getElementById("abe-results");
        if (!container || !st.currentRun) return;
        const manifest = st.currentRun.manifest || {};
        const report = st.currentRun.report;
        const arms = (manifest.arms || []).map(a => a.name);

        const human = st.currentRun.human || {};

        // Consolidated "About this run" panel — run identity plus the one
        // how-to-read note, gathered from what used to be scattered across the
        // top line, the metrics summary, and the human-input section.
        const statusColor = manifest.status === "complete" ? "var(--color-success)"
            : manifest.status === "failed" ? "var(--color-danger)" : "var(--color-warning)";
        let html = `<div style="border: 1px solid var(--color-border-strong); border-radius: 8px;
                padding: 12px 16px; margin-bottom: 14px;">
            <div style="display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;">
                <h4 class="text-h3 font-semibold" style="margin: 0; color: var(--color-text-heading);">
                    ${manifest.name ? _esc(manifest.name) : "Test run"}</h4>
                <span class="text-xxs font-semibold" style="padding: 2px 8px; border-radius: 10px;
                    color: ${statusColor}; border: 1px solid ${statusColor};">${_esc(manifest.status || "—")}</span>
                <span style="flex: 1;"></span>
                <span class="text-xxs font-mono" style="color: var(--color-text-muted);">${_esc(manifest.run_id || "")}</span>
            </div>
            <div class="text-xs" style="color: var(--color-text-muted); margin-top: 6px;
                    display: flex; gap: 18px; flex-wrap: wrap;">
                <span>Started ${_esc((manifest.started_at || "—").replace("T", " "))}
                    by ${_esc(manifest.started_by || "?")}</span>
                <span>Test videos <span class="font-mono" style="color: var(--color-text-primary);">${_esc(manifest.eval_set || "—")}</span></span>
                <span>${_esc(String(manifest.n_items ?? "?"))} videos</span>
            </div>
            ${manifest.error ? `<div class="text-xs" style="color: var(--color-danger); margin-top: 6px;">${_esc(manifest.error)}</div>` : ""}
            <div class="text-xxs" style="color: var(--color-text-muted); margin-top: 8px;
                    line-height: var(--leading-relaxed);">
                Test runs make real annotation calls, but their results are stored separately for
                comparison only — they never enter your research datasets. Every score below is
                pairwise agreement across the contracts, not a verdict on which contract is correct.
            </div>
        </div>`;

        if (!report) {
            container.innerHTML = html +
                '<div class="text-sm" style="color: var(--color-text-muted);">No report (run incomplete).</div>';
            return;
        }

        // Activate button for a candidate arm's cost card. The manifest's
        // `candidate` field (recorded per arm) is the real candidate name;
        // older manifests lack it — fall back to the arm label unless it's a
        // duplicated arm (`name~2`), where the label is not a candidate name.
        function _armActivateButton(arm, meta, isCandidate) {
            if (!isCandidate) return "";
            const candidate = meta.candidate || (arm.includes("~") ? null : arm);
            if (!candidate) {
                return `<button class="btn-primary btn-compact meta-tooltip" disabled
                    style="margin-top: 6px;"
                    data-tooltip="This run predates candidate tracking — re-run the test to enable activation.">Activate</button>`;
            }
            return `<button class="btn-primary btn-compact abe-activate-arm"
                data-n="${_esc(candidate)}" data-backend="${_esc(meta.backend || "gemini")}"
                style="margin-top: 6px;">Activate</button>`;
        }

        // Per-contract cost cards.
        html += '<div style="display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 14px;">';
        for (const arm of report.arms || arms) {
            const c = (report.costs || {})[arm] || {};
            const meta = (manifest.arms || []).find(a => a.name === arm) || {};
            const isCandidate = meta.source === "candidate";
            const ov = meta.gen_overrides || {};
            const ovParts = [`backend ${meta.backend || "gemini"}`];
            if (ov.model) ovParts.push(`model ${ov.model}`);
            if (ov.temperature !== undefined) ovParts.push(`temp ${ov.temperature}`);
            html += `<div style="border: 1px solid var(--color-border); border-radius: 6px;
                    padding: 10px 14px; min-width: 190px;">
                <div class="font-semibold font-mono">${_esc(_armLabel(arm))}</div>
                <div class="text-xxs" style="color: var(--color-text-muted); margin-top: 2px;">${_esc(ovParts.join(" · "))}</div>
                <div class="text-xs" style="color: var(--color-text-muted); margin-top: 4px;">
                    ${_tokensLine(c)}</div>
                <div class="text-xs" style="color: var(--color-text-muted); margin-top: 2px;">
                    ${_costLine(c)}${_esc(String(c.n_errors ?? "—"))} errors ·
                    ${_fmt(c.mean_inference_duration, 1)}s mean</div>
                ${_armActivateButton(arm, meta, isCandidate)}
            </div>`;
        }
        html += _humanInputCard(human);
        html += "</div>";

        // Every item in the test, whether or not it has disagreements — each
        // opens the same field-by-field side-by-side modal as the
        // disagreement table's item links. Sits directly below the contract
        // cost cards so the full test set is the first thing after them.
        const itemIds = (manifest.item_ids || []).map(String);
        if (itemIds.length) {
            html += `<h4 class="text-sm font-semibold" style="margin: 0 0 2px 0;
                color: var(--color-text-heading);">Check all items field-by-field</h4>
            <div class="text-xxs" style="color: var(--color-text-muted); margin: 0 0 6px 0;">
                Click any id for the full field-by-field view across every contract (and human coders)</div>
            <div style="display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px;">
                ${itemIds.map(i => `<a href="#" class="abe-item-any font-mono text-xs"
                    data-i="${_esc(i)}" style="color: var(--color-accent); padding: 2px 8px;
                    border: 1px solid var(--color-border); border-radius: 10px;
                    text-decoration: none;">${_esc(i)}</a>`).join("")}
            </div>`;
        }

        // N-way agreement view: overview metric tiles, then a collapsible
        // "Detailed metrics" sub-section holding the cross-pair per-field
        // tables (each field kind is itself collapsible). Submitted human
        // coders join as contracts.
        if (Object.keys(report.comparisons || {}).length) {
            html += `<div id="abe-nway-summary" style="margin-bottom: 12px;"></div>
                <details style="margin-bottom: 14px;">
                    <summary style="cursor: pointer;">
                        <h4 class="text-sm font-semibold" style="display: inline; margin: 0;
                            color: var(--color-text-heading);">Detailed metrics</h4>
                        <span class="text-xxs" style="color: var(--color-text-muted);">— per-field
                            agreement, expandable to the full pairwise matrix</span>
                    </summary>
                    <div id="abe-nway-table" style="overflow-x: auto; margin-top: 8px;"></div>
                </details>`;
        }

        // Adjudication (per-item disagreements; items a contract failed
        // entirely are excluded server-side) — a collapsible sub-section whose
        // rows are grouped into one collapsible sub-sub-section per item.
        const adj = report.adjudication || [];
        html += `<details style="margin-bottom: 4px;">
            <summary style="cursor: pointer;">
                <h4 class="text-sm font-semibold" style="display: inline; margin: 0;
                    color: var(--color-text-heading);">Disagreements
                    (${adj.length}${adj.length >= 3000 ? "+, capped" : ""})</h4>
            </summary>
            <div class="text-xxs" style="color: var(--color-text-muted); margin: 8px 0 6px 0;">
                Free-text fields differ on almost every item by nature; expand an item for its
                differing fields, or open the full field-by-field view.
            </div>
            <div id="abe-adj-table" style="max-height: 50vh; overflow-y: auto;"></div>
        </details>
        <div id="abe-human-section" style="margin-top: 18px;"></div>`;

        container.innerHTML = html;
        container.querySelectorAll(".abe-activate-arm").forEach(b =>
            b.addEventListener("click", () =>
                activateCandidate(b.dataset.n, b, b.dataset.backend || null)));
        container.querySelectorAll(".abe-item-any").forEach(a =>
            a.addEventListener("click", (ev) => { ev.preventDefault(); openItemView(a.dataset.i); }));
        // Human value distributions need the coders' rows — render the N-way
        // view immediately, then once more after the prefetch fills them in.
        _renderNwayView();
        _prefetchHumanRows().then(_renderNwayView);
        abeRenderAdjudication();
        abeRenderHuman();
    }

    // A card summarizing the run's human input, shown alongside the per-
    // contract cost cards. Counts each task's variables and submitted coders;
    // reads "no human input" when the run has no Human testing task.
    function _humanInputCard(human) {
        const line = (task, unit) => {
            const t = (human || {})[task];
            if (!t) return null;
            const entries = Object.values(t.coder_status || {});
            const done = entries.filter(s => s.status === "submitted").length;
            const n = (t.variables || []).length;
            const label = task === "coding" ? "coding" : "votes";
            return `${label} · ${n} ${unit}${n === 1 ? "" : "s"} · ${done}/${entries.length} submitted`;
        };
        const lines = [line("coding", "variable"), line("vote", "field")].filter(Boolean);
        const body = lines.length
            ? lines.map(l => `<div class="text-xs" style="color: var(--color-text-muted);
                    margin-top: 4px;">${_esc(l)}</div>`).join("")
                + `<div class="text-xxs" style="color: var(--color-text-muted); margin-top: 6px;">
                    managed under <em>Admin &rarr; Human testing</em></div>`
            : `<div class="text-xs" style="color: var(--color-text-muted); margin-top: 4px;">
                No human input for this run.</div>`;
        return `<div style="border: 1px solid var(--color-border); border-radius: 6px;
                padding: 10px 14px; min-width: 190px;">
            <div class="font-semibold">Human input</div>${body}</div>`;
    }

    function _renderNwayView() {
        const unified = _unifiedComparisons();
        if (!Object.keys(unified.pairs).length) return;
        const cols = _aggregateColumns(unified);
        _renderRunSummary(unified, cols);
        abeRenderNway(unified, cols);
    }

    // ---------- human input (ICR) ----------

    // Renders the run's human-coding block (st.currentRun.human, attached by
    // the run endpoint when a Human testing task exists on the run): task
    // status, per-coder progress, and — once coders have submitted — the
    // human-vs-machine and human-vs-human agreement tables with Cohen's κ.
    function abeRenderHuman() {
        const el = document.getElementById("abe-human-section");
        const human = st.currentRun && st.currentRun.human;
        if (!el) return;
        if (!human) { el.innerHTML = ""; return; }
        el.innerHTML = _renderCodingSection(human.coding)
            + _renderVoteSection(human.vote)
            + _renderNotesSection(human);
        el.querySelectorAll(".abe-note-item").forEach(a =>
            a.addEventListener("click", (ev) => { ev.preventDefault(); openItemView(a.dataset.i); }));
    }

    // Free-text notes coders attached to individual videos, across both task
    // types — one place to read them all when adjudicating.
    function _renderNotesSection(human) {
        const notes = [];
        for (const [type, label] of [["coding", "coding"], ["vote", "vote"]]) {
            for (const note of ((human[type] || {}).notes) || []) {
                notes.push({ ...note, type: label });
            }
        }
        if (!notes.length) return "";
        const byItem = {};
        for (const note of notes) (byItem[note.item_id] = byItem[note.item_id] || []).push(note);
        const cell = "padding: 4px 8px; border-bottom: 1px solid var(--color-border); vertical-align: top;";
        const rows = Object.entries(byItem).map(([itemId, itemNotes]) => itemNotes.map((n, i) => `<tr>
            <td style="${cell}">${i === 0
                ? `<a href="#" class="abe-note-item font-mono" data-i="${_esc(itemId)}"
                    style="color: var(--color-accent);">${_esc(itemId)}</a>` : ""}</td>
            <td style="${cell}">${_esc(n.username)}</td>
            <td style="${cell}">${_esc(n.type)}</td>
            <td style="${cell}">${_esc(n.note)}</td>
        </tr>`).join("")).join("");
        return `<h3 class="text-sm font-semibold" style="margin: 18px 0 6px 0;
                color: var(--color-text-heading);">Coder notes (${notes.length})</h3>
            <details>
                <summary class="text-xxs" style="cursor: pointer; color: var(--color-text-muted);">
                    free-text notes coders attached to individual videos — click an item id for the
                    full side-by-side view</summary>
                <div style="overflow-x: auto; margin-top: 6px;">
                <table style="border-collapse: collapse; width: 100%;" class="text-xs">
                <thead><tr class="text-xxs" style="text-align: left; color: var(--color-text-muted);">
                    <th style="${cell}">item</th><th style="${cell}">coder</th>
                    <th style="${cell}">task</th><th style="${cell}">note</th>
                </tr></thead><tbody>${rows}</tbody></table></div>
            </details>`;
    }

    function _renderCodingSection(coding) {
        if (!coding) return "";

        let html = `<h3 class="text-sm font-semibold" style="margin: 0 0 6px 0;
                color: var(--color-text-heading);">Human input (ICR)</h3>`;

        const results = coding.results;
        if (!results || (!Object.keys(results.human_vs_machine || {}).length
                && !Object.keys(results.human_vs_human || {}).length)) {
            return html + `<div class="text-xs" style="color: var(--color-text-muted);">
                No submitted codings yet — once a coder submits, they appear as an extra
                contract in the agreement tables and the per-item view above.</div>`;
        }
        return html + `<div class="text-xs" style="color: var(--color-text-muted);">
            Submitted coders are included as contracts in the agreement tables, the
            pairwise matrices (with Cohen's κ on categorical fields) and the per-item
            view above.</div>`;
    }

    // The "Preference votes" block: per-arm pooled win-rate bars, per-coder
    // tallies, and (for two-arm runs) a binomial sign test over non-tie votes.
    function _renderVoteSection(vote) {
        if (!vote) return "";

        let html = `<h3 class="text-sm font-semibold" style="margin: 18px 0 6px 0;
                color: var(--color-text-heading);">Preference votes</h3>`;

        const results = vote.results;
        if (!results || !Object.keys(results.per_coder || {}).length) {
            return html + `<div class="text-xs" style="color: var(--color-text-muted);">
                No submitted votes yet — win rates appear here after the first coder submits.</div>`;
        }

        const pooled = results.pooled || {};
        const arms = results.arms || [];
        const bars = arms.map(arm => {
            const rate = (pooled.win_rates || {})[arm];
            const pct = rate == null ? 0 : Math.round(rate * 100);
            return `<div style="display: flex; align-items: center; gap: 10px; margin-bottom: 4px;">
                <span class="font-mono text-xs" style="min-width: 120px;">${_esc(arm)}</span>
                <div style="flex: 1; max-width: 340px; height: 12px; border-radius: 6px;
                    background: var(--color-bg-elevated); overflow: hidden;">
                    <div style="height: 100%; width: ${pct}%; background: var(--color-accent);"></div>
                </div>
                <span class="text-xs">${(pooled.wins || {})[arm] ?? 0} wins
                    (${rate == null ? "—" : _fmt(rate)})</span>
            </div>`;
        }).join("");
        html += `<div style="margin-bottom: 6px;">${bars}
            <div class="text-xxs" style="color: var(--color-text-muted);">
                ties: ${pooled.ties ?? 0} of ${pooled.n_votes ?? 0} votes
                ${results.tie_rate != null ? ` (${_fmt(results.tie_rate)})` : ""} ·
                win rates are over non-tie votes only</div></div>`;

        const cell = "padding: 4px 8px; border-bottom: 1px solid var(--color-border);";
        const coderRows = Object.entries(results.per_coder).map(([user, r]) => `<tr>
            <td style="${cell}">${_esc(user)}</td>
            ${arms.map(arm => `<td style="${cell}">${(r.wins || {})[arm] ?? 0}</td>`).join("")}
            <td style="${cell}">${r.ties ?? 0}</td>
            <td style="${cell}">${r.n_votes ?? 0}</td>
        </tr>`).join("");
        html += `<div style="overflow-x: auto;">
            <table style="border-collapse: collapse; width: 100%;" class="text-xs">
            <thead><tr class="text-xxs" style="text-align: left; color: var(--color-text-muted);">
                <th style="${cell}">coder</th>
                ${arms.map(arm => `<th style="${cell}" class="font-mono">${_esc(arm)}</th>`).join("")}
                <th style="${cell}">ties</th>
                <th style="${cell}">votes</th>
            </tr></thead><tbody>${coderRows}</tbody></table></div>`;

        if (results.sign_test && results.sign_test.p_value != null) {
            const t = results.sign_test;
            html += `<div class="text-xs" style="margin-top: 6px;">
                <span class="meta-tooltip" data-tooltip="Binomial sign test: under the null that neither contract is preferred, wins split 50/50 over the non-tie votes. A small p suggests a real preference.">
                Sign test over ${t.n_non_tie} non-tie vote(s):
                p = ${_fmt(t.p_value, 3)}</span></div>`;
        }
        return html;
    }

    // Runs made before the 2026-07 metric fix stored a summary without per-kind
    // column counts (and classified `text` fields as categorical). Back-fill the
    // counts so the tiles still render, and flag the run as stale.
    function _isLegacyReport(comp) {
        return (comp.summary || {}).n_enum_columns === undefined;
    }

    // ---------- N-way agreement view ----------

    // Merge the machine pairwise comparisons with the human-coding results:
    // both share the compare_arms shape, so a submitted coder is literally
    // just another contract. Human arm rows are fetched as "human:<username>".
    function _unifiedComparisons() {
        const report = st.currentRun && st.currentRun.report;
        if (!report) return { machineArms: [], humanArms: [], pairs: {} };
        const pairs = { ...(report.comparisons || {}) };
        const results = (((st.currentRun.human || {}).coding) || {}).results || {};
        const humanArms = results.coders || [];
        for (const [key, comp] of Object.entries(results.human_vs_machine || {})) pairs[key] = comp;
        for (const [key, comp] of Object.entries(results.human_vs_human || {})) pairs[key] = comp;
        return { machineArms: report.arms || [], humanArms, pairs };
    }

    function _allArms(unified) {
        return [...unified.machineArms, ...unified.humanArms];
    }

    // Per-column cross-pair aggregation: kind, per-pair metrics (for the
    // matrix), the mean over pairs (the collapsed row), per-arm coverage,
    // and the union of caveats. Human pairs cover only the coded variables —
    // absent columns are simply skipped for that pair.
    function _aggregateColumns(unified) {
        const cols = {};
        for (const [key, comp] of Object.entries(unified.pairs)) {
            const [a, b] = key.split("|");
            for (const [name, m] of Object.entries(comp.columns || {})) {
                const c = cols[name] || (cols[name] = {
                    kind: m.kind, pairMeta: {}, coverage: {}, caveats: new Set(),
                });
                c.pairMeta[key] = m;
                if (m.coverage_a != null) c.coverage[a] = m.coverage_a;
                if (m.coverage_b != null) c.coverage[b] = m.coverage_b;
                if (m.caveat) c.caveats.add(m.caveat);
            }
        }
        for (const c of Object.values(cols)) {
            const vals = Object.values(c.pairMeta).map(_metricOf).filter(v => v != null);
            c.mean = vals.length ? vals.reduce((s, v) => s + v, 0) / vals.length : null;
            c.nPairs = vals.length;
        }
        return cols;
    }

    // Prefetch each submitted coder's rows once (≤ a few coders × ≤50 items)
    // and derive their per-column value counts — the human analogue of the
    // report's stored distributions. Also warms the per-item modal cache.
    async function _prefetchHumanRows() {
        st.humanDist = {};
        const results = (((st.currentRun || {}).human || {}).coding || {}).results;
        if (!results || !(results.coders || []).length) return;
        await Promise.all(results.coders.map(async (coder) => {
            try {
                const rows = await _armRows(`human:${coder}`);
                const dist = {};
                for (const row of rows) {
                    for (const [col, value] of Object.entries(row)) {
                        if (col === "item_id" || col === "note") continue;
                        const v = String(value ?? "");
                        if (!v) continue;
                        (dist[col] = dist[col] || {})[v] = (dist[col][v] || 0) + 1;
                    }
                }
                st.humanDist[coder] = dist;
            } catch (e) { /* rows unavailable — the matrix still renders */ }
        }));
    }

    // Headline tiles: cross-pair, cross-column means per kind (each kind keeps
    // its own tile — r, agreement and Jaccard are not on a common scale).
    function _renderRunSummary(unified, cols) {
        const el = document.getElementById("abe-nway-summary");
        if (!el) return;
        const tile = (title, value, sub, tip) => `
            <div class="meta-tooltip" data-tooltip="${_esc(tip)}"
                style="border: 1px solid var(--color-border); border-radius: 6px;
                       padding: 8px 12px; min-width: 168px; cursor: help;">
                <div class="text-xxs" style="color: var(--color-text-muted);">${_esc(title)}</div>
                <div class="text-h3 font-bold" style="color: var(--color-text-primary);">${value}</div>
                <div class="text-xxs" style="color: var(--color-text-muted);">${sub}</div>
            </div>`;
        const meanOf = (kind) => {
            const vals = Object.values(cols).filter(c => c.kind === kind && c.mean != null)
                .map(c => c.mean);
            return vals.length ? vals.reduce((s, v) => s + v, 0) / vals.length : null;
        };
        const countOf = (kind) => Object.values(cols).filter(c => c.kind === kind).length;

        const tiles = [];
        if (countOf("enum")) {
            const kappas = [];
            for (const c of Object.values(cols)) {
                if (c.kind !== "enum") continue;
                for (const m of Object.values(c.pairMeta)) {
                    if (m.kappa != null) kappas.push(m.kappa);
                }
            }
            const kappaBit = kappas.length
                ? ` · κ ${_fmt(kappas.reduce((s, v) => s + v, 0) / kappas.length)}` : "";
            tiles.push(tile("Categorical agreement", _fmt(meanOf("enum")),
                `${countOf("enum")} fields, all pairs${kappaBit}`, KINDS[0].blurb));
        }
        if (countOf("list")) {
            tiles.push(tile("List overlap (Jaccard)", _fmt(meanOf("list")),
                `${countOf("list")} fields, all pairs`, KINDS[1].blurb));
        }
        if (countOf("numeric")) {
            tiles.push(tile("Numeric agreement", _fmt(meanOf("numeric")),
                `${countOf("numeric")} fields, all pairs`, KINDS[2].blurb));
        }
        // Items one arm failed are excluded from all pairwise metrics.
        const excluded = Math.max(0, ...Object.values(unified.pairs)
            .map(p => p.n_items_excluded || 0));
        if (excluded) {
            tiles.push(tile("Excluded items", String(excluded), "failed in ≥1 contract",
                "Items without a usable annotation from every contract in a pair are "
                + "excluded from that pair's metrics — only items both sides "
                + "annotated successfully are compared."));
        }
        // Annotation success per machine arm (humans have no failure mode here).
        const okRates = {};
        for (const [key, comp] of Object.entries(unified.pairs)) {
            const [a, b] = key.split("|");
            const s = comp.summary || {};
            if (s.annotated_ok_rate_a != null && unified.machineArms.includes(a)) okRates[a] = s.annotated_ok_rate_a;
            if (s.annotated_ok_rate_b != null && unified.machineArms.includes(b)) okRates[b] = s.annotated_ok_rate_b;
        }
        if (Object.keys(okRates).length) {
            tiles.push(tile("Annotation succeeded",
                Object.values(okRates).map(_pct).join(" / "),
                Object.keys(okRates).map(a => _esc(_armLabel(a))).join(" / "),
                "Share of test-set videos each contract returned a usable annotation for."));
        }

        const legend = KINDS.map(k =>
            `<li><strong>${_esc(k.label)}</strong> — ${_esc(k.blurb)}</li>`).join("");
        const machinePairs = Object.entries(unified.pairs).filter(([key]) => {
            const [a, b] = key.split("|");
            return unified.machineArms.includes(a) && unified.machineArms.includes(b);
        });
        const stale = machinePairs.some(([, comp]) => _isLegacyReport(comp))
            ? `<div class="text-xxs" style="color: var(--color-warning); margin-bottom: 8px;">
                ⚠ This run predates the metric fix: free-text fields were scored with exact-string
                agreement, list fields joined with “|” were scored as single strings, and “nothing
                found” answers written with an en dash counted as answers. Re-run to get corrected
                scores.</div>`
            : "";

        const contracts = _allArms(unified);
        el.innerHTML = stale + `
            <div style="display: flex; gap: 10px; flex-wrap: wrap;">${tiles.join("")}</div>
            <div class="text-xxs" style="color: var(--color-text-muted); margin-top: 8px;">
                ${contracts.length} contracts compared${unified.humanArms.length
                    ? ` (incl. ${unified.humanArms.length} human coder(s))` : ""}:
                <span class="font-mono">${contracts.map(a => _esc(unified.machineArms.includes(a) ? `${_armLabel(a)} [${_armBackendOf(a)}]` : _armLabel(a))).join(", ")}</span>.
                Expand a field for the full pairwise matrix.
            </div>
            <details style="margin-top: 6px;">
                <summary class="text-xxs" style="cursor: pointer; color: var(--color-text-muted);">
                    How each metric is computed</summary>
                <ul class="text-xxs" style="margin: 6px 0 0 18px; padding: 0;
                    color: var(--color-text-muted); line-height: var(--leading-relaxed);">
                    ${legend}
                    <li><strong>Answered</strong> — share of items where that contract returned a
                        substantive value. A field where two contracts both answered
                        &ldquo;no&rdquo; every time shows 1.00 agreement but 0% answered.</li>
                    <li><strong>κ</strong> — Cohen's kappa (chance-corrected agreement), computed
                        for pairs involving a human coder over the items where both sides gave a
                        real value.</li>
                </ul>
            </details>`;
    }

    // The metric of one pair for one column, formatted for a matrix cell.
    function _pairCellText(m) {
        if (!m) return `<span style="color: var(--color-text-muted);">—</span>`;
        if (m.kind === "numeric") {
            if (m.exact_agreement == null) return `r ${_fmt(m.correlation)}`;  // pre-fix report
            const r = (m.correlation != null && m.caveat !== "low_variance")
                ? ` <span style="color: var(--color-text-muted);">r ${_fmt(m.correlation)}</span>` : "";
            return `${_fmt(m.exact_agreement)} <span style="color: var(--color-text-muted);">Δ̄ ${_fmt(m.mean_abs_diff)}</span>${r}`;
        }
        if (m.kind === "enum") {
            const kappa = m.kappa != null ? ` <span style="color: var(--color-text-muted);">κ ${_fmt(m.kappa)}</span>` : "";
            return `${_fmt(m.agreement)}${kappa}`;
        }
        if (m.kind === "list") return _fmt(m.mean_jaccard);
        return `<span style="color: var(--color-text-muted);">—</span>`;
    }

    function _pairMatrix(colAgg, unified) {
        const arms = _allArms(unified);
        const cell = "padding: 3px 8px; border-bottom: 1px solid var(--color-border);";
        const header = arms.map(a => `<th style="${cell}" class="font-mono">${_esc(_armLabel(a))}</th>`).join("");
        const rows = arms.map((rowArm, i) => {
            const cells = arms.map((colArm, j) => {
                if (i === j) return `<td style="${cell}"></td>`;
                const m = colAgg.pairMeta[`${rowArm}|${colArm}`] || colAgg.pairMeta[`${colArm}|${rowArm}`];
                return `<td style="${cell}">${_pairCellText(m)}</td>`;
            }).join("");
            return `<tr><td style="${cell}" class="font-mono text-xxs">${_esc(_armLabel(rowArm))}</td>${cells}</tr>`;
        }).join("");
        return `<table style="border-collapse: collapse;" class="text-xxs">
            <thead><tr class="text-xxs" style="text-align: left; color: var(--color-text-muted);">
                <th style="${cell}"></th>${header}</tr></thead>
            <tbody>${rows}</tbody></table>`;
    }

    function _coverageLine(colAgg, unified) {
        const parts = _allArms(unified)
            .filter(a => colAgg.coverage[a] != null)
            .map(a => `<span class="font-mono">${_esc(a)}</span> ${_pct(colAgg.coverage[a])}`);
        return parts.length
            ? `<div class="text-xxs" style="color: var(--color-text-muted); margin: 6px 0;">
                Answered: ${parts.join(" · ")}</div>`
            : "";
    }

    function _distBlock(name, unified) {
        const machine = ((st.currentRun.report || {}).distributions || {})[name];
        const blocks = [];
        for (const [arm, counts] of Object.entries((machine || {}).arms || {})) {
            blocks.push([arm, counts]);
        }
        for (const [coder, dist] of Object.entries(st.humanDist || {})) {
            if (dist[name]) blocks.push([coder, dist[name]]);
        }
        if (!blocks.length) return "";
        const inner = blocks.map(([arm, counts]) =>
            `<div style="min-width: 160px;"><span class="font-mono font-semibold">${_esc(arm)}</span><br>`
            + Object.entries(counts).map(([v, n]) => `${_esc(v)}: ${n}`).join("<br>") + "</div>"
        ).join("");
        return `<div class="text-xxs" style="color: var(--color-text-muted); margin-top: 6px;">Values</div>
            <div style="display: flex; gap: 16px; padding: 4px 0; flex-wrap: wrap;" class="text-xxs">${inner}</div>`;
    }

    // The main per-field table: collapsed row = cross-pair mean; expanded =
    // pairwise matrix + per-contract answered% + value distributions.
    function abeRenderNway(unified, cols) {
        const el = document.getElementById("abe-nway-table");
        if (!el) return;
        const cell = "padding: 4px 8px; border-bottom: 1px solid var(--color-border);";
        const entries = Object.entries(cols);

        const blocks = KINDS.map(kind => {
            const kindCols = entries
                .filter(([, c]) => c.kind === kind.key)
                .sort((x, y) => {
                    const mx = x[1].mean, my = y[1].mean;
                    return (mx == null ? 2 : mx) - (my == null ? 2 : my);   // lowest agreement first
                });
            if (!kindCols.length) return "";
            const rows = kindCols.map(([name, c]) => {
                const caveats = [...c.caveats].map(_caveatIcon).join("");
                const meanText = kind.key === "freetext"
                    ? `<span style="color: var(--color-text-muted);">not scored</span>`
                    : `${_fmt(c.mean)} <span style="color: var(--color-text-muted);">(${c.nPairs} pair${c.nPairs === 1 ? "" : "s"})</span>`;
                return `<tr>
                    <td style="${cell}" class="font-mono">${_esc(name)}${caveats}</td>
                    <td style="${cell}">${meanText}</td>
                </tr>
                <tr><td colspan="2" style="border-bottom: 1px solid var(--color-border); padding: 0 8px 6px 8px;">
                    <details>
                        <summary class="text-xxs" style="cursor: pointer; color: var(--color-text-muted);">
                            pairwise &amp; per-contract detail</summary>
                        <div style="margin-top: 6px; overflow-x: auto;">${
                            kind.key === "freetext" ? "" : _pairMatrix(c, unified)
                        }</div>
                        ${_coverageLine(c, unified)}
                        ${_distBlock(name, unified)}
                    </details>
                </td></tr>`;
            }).join("");
            const metricHeader = kind.key === "numeric" ? "mean exact agreement across pairs (lowest first)"
                : kind.key === "enum" ? "mean agreement across pairs (lowest first)"
                : kind.key === "list" ? "mean Jaccard across pairs (lowest first)"
                : "not scored (prose is never string-equal)";
            return `<details style="margin: 12px 0 4px 0;">
                <summary style="cursor: pointer;">
                    <h5 class="text-xs font-semibold" style="display: inline; margin: 0;
                        color: var(--color-text-heading);">${_esc(kind.label)}</h5>
                    <span class="text-xxs font-normal meta-tooltip tooltip-below" data-tooltip="${_esc(kind.blurb)}"
                        style="color: var(--color-text-muted); cursor: help;">&#9432;</span>
                </summary>
                <table style="border-collapse: collapse; width: 100%; margin-top: 4px;" class="text-xs">
                <thead><tr class="text-xxs" style="text-align: left; color: var(--color-text-muted);">
                    <th style="${cell}">field</th>
                    <th style="${cell}">${_esc(metricHeader)}</th>
                </tr></thead><tbody>${rows}</tbody></table>
            </details>`;
        }).join("");

        el.innerHTML = blocks || '<span class="text-xs" style="color: var(--color-text-muted);">No comparable columns.</span>';
    }

    function abeRenderAdjudication() {
        const el = document.getElementById("abe-adj-table");
        const report = st.currentRun && st.currentRun.report;
        if (!el || !report) return;
        const arms = report.arms || [];
        const rows = report.adjudication || [];
        if (!rows.length) {
            el.innerHTML = '<span class="text-xs" style="color: var(--color-text-muted);">No disagreements 🎉</span>';
            return;
        }
        // Group disagreement rows by item so each item is one collapsible
        // sub-sub-section listing only the fields where the contracts differ.
        const byItem = new Map();
        for (const r of rows) {
            if (!byItem.has(r.item_id)) byItem.set(r.item_id, []);
            byItem.get(r.item_id).push(r);
        }
        const ITEM_CAP = 500;
        const items = [...byItem.entries()];
        const shown = items.slice(0, ITEM_CAP);
        const cell = "padding: 4px 8px; border-bottom: 1px solid var(--color-border); vertical-align: top;";
        el.innerHTML = shown.map(([itemId, itemRows]) => `
            <details style="border-bottom: 1px solid var(--color-border);">
                <summary style="cursor: pointer; padding: 5px 0;">
                    <h5 class="text-xs font-semibold font-mono" style="display: inline; margin: 0;
                        color: var(--color-text-heading);">${_esc(itemId)}</h5>
                    <span class="text-xxs" style="color: var(--color-text-muted);">— ${itemRows.length}
                        field${itemRows.length === 1 ? "" : "s"} differ</span>
                    <a href="#" class="abe-item text-xxs" data-i="${_esc(itemId)}"
                        style="color: var(--color-accent); margin-left: 8px;">full view &rarr;</a>
                </summary>
                <div style="overflow-x: auto; padding: 4px 0 8px 0;">
                    <table style="border-collapse: collapse; width: 100%;" class="text-xs">
                    <thead><tr class="text-xxs" style="text-align: left; color: var(--color-text-muted);">
                        <th style="${cell}">field</th>
                        ${arms.map(a => `<th style="${cell}" class="font-mono">${_esc(_armLabel(a))}</th>`).join("")}
                    </tr></thead>
                    <tbody>${itemRows.map(r => `<tr>
                        <td style="${cell}" class="font-mono">${_esc(r.column)}</td>
                        ${arms.map(a => `<td style="${cell}">${_esc((r.values || {})[a] ?? "")}</td>`).join("")}
                    </tr>`).join("")}</tbody></table>
                </div>
            </details>`).join("")
            + (items.length > ITEM_CAP ? `<div class="text-xxs" style="color: var(--color-text-muted); padding: 6px 0;">Showing ${ITEM_CAP} of ${items.length} items.</div>` : "");
        el.querySelectorAll(".abe-item").forEach(a =>
            a.addEventListener("click", (ev) => {
                ev.preventDefault(); ev.stopPropagation(); openItemView(a.dataset.i);
            }));
    }

    async function _armRows(arm) {
        if (!st.rowsCache[arm]) {
            const body = await _getJson(
                `${EVAL}/runs/${encodeURIComponent(st.currentRunId)}/rows?arm=${encodeURIComponent(arm)}`);
            st.rowsCache[arm] = body.rows || [];
        }
        return st.rowsCache[arm];
    }

    // Ordered item list for the modal's prev/next navigation (the run's
    // manifest order — same order as the "All items" chips).
    function _modalItems() {
        return (((st.currentRun || {}).manifest || {}).item_ids || []).map(String);
    }

    function abeItemNav(step) {
        const items = _modalItems();
        const current = document.getElementById("abe-item-id");
        const idx = items.indexOf(current ? current.textContent : "");
        if (idx < 0 || !items.length) return;
        const next = (idx + step + items.length) % items.length;
        openItemView(items[next]);
    }

    function abeToggleItemVideo() {
        const video = document.getElementById("abe-item-video");
        const label = document.getElementById("abe-item-video-toggle-label");
        if (!video) return;
        const hidden = video.style.display === "none";
        video.style.display = hidden ? "block" : "none";
        if (!hidden) video.pause();
        if (label) label.textContent = hidden ? "Hide video" : "Show video";
    }

    // The item modal doubles as a plain dialog (the Activate confirm). Its
    // item-review chrome — video panel, prev/next, position — only makes
    // sense in the per-item view, so each opener sets the mode explicitly.
    function _setItemModalChrome(show) {
        ["abe-item-video-panel", "abe-item-prev", "abe-item-next", "abe-item-pos"]
            .forEach(function (id) {
                const el = document.getElementById(id);
                if (el) el.style.display = show ? "" : "none";
            });
        // The header's static "Item … — field by field" framing text.
        document.querySelectorAll("#abe-item-modal .abe-item-chrome").forEach(function (el) {
            el.style.display = show ? "" : "none";
        });
    }

    async function openItemView(itemId) {
        const modal = document.getElementById("abe-item-modal");
        const body = document.getElementById("abe-item-body");
        const report = st.currentRun && st.currentRun.report;
        if (!modal || !body || !report) return;
        _setItemModalChrome(true);
        document.getElementById("abe-item-id").textContent = itemId;
        body.innerHTML = '<span style="color: var(--color-text-muted);">Loading…</span>';
        modal.style.display = "flex";

        // Position indicator + mini player (platform resolved server-side).
        const items = _modalItems();
        const pos = document.getElementById("abe-item-pos");
        const idx = items.indexOf(String(itemId));
        if (pos) pos.textContent = idx >= 0 ? `${idx + 1} / ${items.length}` : "";
        const video = document.getElementById("abe-item-video");
        if (video) {
            const src = `/api/video/eval/${encodeURIComponent(itemId)}`;
            if (video.getAttribute("src") !== src) {
                video.pause();
                video.setAttribute("src", src);
            }
        }
        try {
            // Human coders join the side-by-side as extra contract columns.
            const unified = _unifiedComparisons();
            const arms = [
                ...unified.machineArms,
                ...unified.humanArms.map(u => `human:${u}`),
            ];
            const label = (a) => a.startsWith("human:") ? a.slice("human:".length) : `${_armLabel(a)} [${_armBackendOf(a)}]`;
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
                    ${arms.map(a => `<th style="${cell}" class="font-mono">${_esc(label(a))}</th>`).join("")}
                </tr></thead>
                <tbody>${columns.map(c => {
                    const values = arms.map(a => String(rowByArm[a][c] ?? ""));
                    // Blanks (a coder who skipped this field / a note only one
                    // coder wrote) don't count as disagreement.
                    const nonEmpty = values.filter(v => v !== "");
                    const differs = new Set(nonEmpty).size > 1;
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
        const video = document.getElementById("abe-item-video");
        if (video) {
            video.pause();
            video.removeAttribute("src");
            video.load();
        }
    }

    // ---------- bootstrap ----------

    let bootstrapped = false;

    function _maybeBootstrap() {
        const page = document.getElementById("admin-page-abeval");
        if (!page || bootstrapped || !page.classList.contains("active")) return;
        bootstrapped = true;
        loadCandidates();
        loadBackends();
        loadEvalSets().then(() => loadEvalSet()).then(refreshEstimate);
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

    // The active contract or backend changed (an activation here or on the
    // Versions page, or a backend switch on the Backends page) — recompute the
    // live version ids, the "active" badges, and the per-arm backend list.
    document.addEventListener("fyp:contract-changed", function () {
        if (bootstrapped) {
            loadCandidates();
            loadBackends();
        }
    });

    window.abeOnCandidateFile = abeOnCandidateFile;

    function abeDownloadActive() {
        window.location.href = `${AC}/download`;
    }
    window.abeConfirmName = abeConfirmName;
    window.abeCancelName = abeCancelName;
    window.abeConfirmActivate = abeConfirmActivate;
    window.abeAddIds = abeAddIds;
    window.abeSample = abeSample;
    window.abeSaveEvalSet = abeSaveEvalSet;
    window.abeSelectSet = abeSelectSet;
    window.abeNewSet = abeNewSet;
    window.abeRenameSet = abeRenameSet;
    window.abeDuplicateSet = abeDuplicateSet;
    window.abeDeleteSet = abeDeleteSet;
    window.abeConfirmSetName = abeConfirmSetName;
    window.abeCancelSetName = abeCancelSetName;
    window.abeStartRun = abeStartRun;
    window.abeCancelRun = abeCancelRun;
    window.abeRefreshRuns = abeRefreshRuns;
    window.abeLoadRun = abeLoadRun;
    window.abeDeleteRun = abeDeleteRun;
    window.abeCloseItemModal = abeCloseItemModal;
    window.abeItemNav = abeItemNav;
    window.abeToggleItemVideo = abeToggleItemVideo;
    window.abeRefreshEstimate = refreshEstimate;
    window.abeAddLiveToTest = abeAddLiveToTest;
    window.abeRemoveFromTest = abeRemoveFromTest;
    window.abeArmBackendChanged = abeArmBackendChanged;

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", _watchForActivation);
    } else {
        _watchForActivation();
    }
})();
