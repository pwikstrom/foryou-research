/**
 * Admin "Annotation Versions" panel.
 *
 * Lists recorded annotation versions (GET /api/manage/annotation-versions),
 * shows a version's prompt/schema snapshot, and activates a version
 * (POST .../activate). Also hosts the annotation-contract card (upload /
 * dry-run impact / confirm / revert against /api/manage/annotation-contract)
 * — the contract mints the versions listed below it. The global fetch
 * wrapper in main.js injects the CSRF header, so plain fetch is sufficient
 * (matches admin_var_schema.js).
 */
(function () {
    "use strict";

    const LIST = "/api/manage/annotation-versions";
    // Matches annotation_versioning.LEGACY_VERSION. The legacy version is a
    // synthetic, snapshot-less entry that only owns pre-versioning fields, so it
    // can never be activated (the backend rejects it too).
    const LEGACY_VERSION = "v0_legacy";

    function _status(msg, isError) {
        const el = document.getElementById("avStatus");
        if (!el) return;
        el.textContent = msg || "";
        el.style.color = isError ? "var(--btn-danger-bg)" : "var(--color-text-muted)";
    }

    function _esc(value) {
        const div = document.createElement("div");
        div.textContent = value == null ? "" : String(value);
        return div.innerHTML;
    }

    // Key of the version whose snapshot is shown below the table ("__current__"
    // for the unminted live contract), or null when the snapshot is hidden.
    // Drives the depressed state of the matching View button.
    let viewedKey = null;

    function _snapshotKeyFor(btn) {
        return btn.id === "avViewCurrent" ? "__current__" : btn.dataset.v;
    }

    function _syncViewButtons() {
        document.querySelectorAll("#avTableBody .av-view, #avViewCurrent").forEach(function (b) {
            b.classList.toggle("btn-pressed", _snapshotKeyFor(b) === viewedKey);
        });
    }

    function _hideSnapshot() {
        viewedKey = null;
        const snap = document.getElementById("avSnapshot");
        if (snap) snap.style.display = "none";
        _syncViewButtons();
    }

    // Render the version's model + generation settings into the snapshot.
    // schema_hash doubles as the "response format" id shown in the old table
    // column; gen_params carries temperature/thinking budget/etc.
    function _renderSnapSettings(rec) {
        const el = document.getElementById("avSnapSettings");
        if (!el) return;
        const pairs = [];
        pairs.push(["Model", rec.model]);
        if (rec.backend) pairs.push(["Backend", rec.backend]);
        if (rec.variant) pairs.push(["Variant", rec.variant]);
        pairs.push(["Response format", rec.schema_hash]);
        const params = rec.gen_params || {};
        Object.keys(params).sort().forEach(function (key) {
            pairs.push([key, params[key]]);
        });
        el.innerHTML = pairs.map(function (p) {
            const value = p[1] == null || p[1] === "" ? "—" : String(p[1]);
            return '<span style="white-space: nowrap;"><span style="color: var(--color-text-muted);">'
                + _esc(p[0]) + ':</span> <span class="font-mono">' + _esc(value) + "</span></span>";
        }).join("");
    }


    async function load() {
        _status("Loading…");
        try {
            const res = await fetch(LIST);
            const body = await res.json();
            if (!res.ok) throw new Error(body.error || res.statusText);
            render(body);
            _status("");
        } catch (err) {
            _status("Failed to load versions: " + err.message, true);
        }
    }

    function render(body) {
        // Most recent first; the synthetic legacy version (epoch created_at)
        // sinks to the bottom naturally.
        const versions = (body.versions || []).slice().sort(function (a, b) {
            return String(b.created_at || "").localeCompare(String(a.created_at || ""));
        });
        const current = body.current;

        const tbody = document.getElementById("avTableBody");
        if (!tbody) return;

        const cell = 'padding: 8px; border-bottom: 1px solid var(--color-border);';
        const mono = cell + ' font-family: var(--font-mono);';
        // The current version's row is shaded — no label needed.
        const currentTr = '<tr style="background: var(--color-bg-elevated);">';

        // Pinned row for the live contract when it has no minted version yet
        // (versions are only registered when annotation runs) — its View
        // renders the generated prompt/schema from the current contract, so
        // they can be inspected before the first run.
        const currentIsMinted = versions.some(function (v) { return v.annotation_version === current; });
        let currentRow = "";
        if (current && !currentIsMinted) {
            currentRow = currentTr +
                '<td style="' + cell + '"></td>' +
                '<td style="' + mono + '">' + _esc(current) + "</td>" +
                '<td style="' + cell + ' color: var(--color-text-muted);">Version for new annotations — none saved yet</td>' +
                '<td style="' + cell + '"></td>' +
                '<td style="' + cell + '">' +
                    '<button class="btn-discreet btn-compact av-view" id="avViewCurrent">View</button>' +
                "</td>" +
                "</tr>";
        }

        if (!versions.length && !currentRow) {
            tbody.innerHTML =
                '<tr><td colspan="5" class="text-sm" style="color: var(--color-text-muted); padding: 12px;">' +
                "No versions recorded yet. Activating a contract (or running annotation) registers one." +
                "</td></tr>";
            return;
        }

        tbody.innerHTML = currentRow + versions.map(function (v) {
            const isActive = !!v.active;
            const isLegacy = v.annotation_version === LEGACY_VERSION;
            const isCurrent = v.annotation_version === current;
            const activateBtn = (isActive || isLegacy) ? "" :
                '<button class="btn-primary btn-compact av-activate" data-v="' + _esc(v.annotation_version) + '">Prefer</button>';
            return (isCurrent ? currentTr : "<tr>") +
                '<td style="' + cell + '">' + (isActive ? "✓" : "") + "</td>" +
                '<td style="' + mono + '">' + _esc(v.annotation_version) + "</td>" +
                '<td style="' + cell + '">' + _esc(v.label) + "</td>" +
                '<td style="' + cell + '">' + _esc(v.created_at) + "</td>" +
                '<td style="' + cell + '">' +
                    '<button class="btn-discreet btn-compact av-view" data-v="' + _esc(v.annotation_version) + '">View</button> ' +
                    activateBtn +
                "</td>" +
                "</tr>";
        }).join("");

        // Every View button toggles: clicking the depressed one hides the
        // snapshot, clicking another switches it.
        tbody.querySelectorAll(".av-view").forEach(function (b) {
            b.addEventListener("click", function () {
                const key = _snapshotKeyFor(b);
                if (key === viewedKey) { _hideSnapshot(); return; }
                if (key === "__current__") viewCurrentRendered();
                else viewSnapshot(key);
            });
        });
        tbody.querySelectorAll(".av-activate").forEach(function (b) {
            b.addEventListener("click", function () { activate(b.dataset.v, b); });
        });
        _syncViewButtons();
    }

    async function viewSnapshot(version) {
        _status("Loading snapshot…");
        try {
            const res = await fetch(LIST + "/" + encodeURIComponent(version));
            const body = await res.json();
            if (!res.ok) throw new Error(body.error || res.statusText);
            const rec = body.record || {};
            document.getElementById("avSnapVersion").textContent = version;
            _renderSnapSettings(rec);
            document.getElementById("avSnapPrompt").textContent = rec.prompt_text || "(none)";
            document.getElementById("avSnapSchema").textContent =
                rec.schema_json ? JSON.stringify(rec.schema_json, null, 2) : "(none / free-text)";
            document.getElementById("avSnapshot").style.display = "block";
            viewedKey = version;
            _syncViewButtons();
            _status("");
        } catch (err) {
            _status("Failed to load snapshot: " + err.message, true);
        }
    }

    async function viewCurrentRendered() {
        _status("Rendering current contract…");
        try {
            const res = await fetch("/api/manage/annotation-contract/rendered");
            const body = await res.json();
            if (!res.ok) throw new Error(body.error || res.statusText);
            document.getElementById("avSnapVersion").textContent =
                (body.version || "current") + " (version for new annotations — none saved yet)";
            _renderSnapSettings(body.descriptor || {});
            document.getElementById("avSnapPrompt").textContent = body.prompt || "(none)";
            document.getElementById("avSnapSchema").textContent =
                body.schema ? JSON.stringify(body.schema, null, 2) : "(none / free-text)";
            document.getElementById("avSnapshot").style.display = "block";
            viewedKey = "__current__";
            _syncViewButtons();
            _status("");
        } catch (err) {
            _status("Failed to render current contract: " + err.message, true);
        }
    }

    // ---------- post-promotion staleness banner ----------

    // After a preferred-version promotion, per-study datasets stay on the
    // previous version until recode_refresh_studies runs. The server keeps a
    // persistent marker (auto-cleared once the refresh succeeds); this banner
    // surfaces it with a one-click refresh for role-admins.
    async function loadStaleness() {
        const banner = document.getElementById("avStaleBanner");
        if (!banner) return;
        try {
            const res = await fetch("/api/manage/refresh/staleness");
            if (!res.ok) return;
            const body = await res.json();
            renderStaleBanner((body.version_promotion || {}).stale ? body.version_promotion : null);
        } catch (err) { /* banner is best-effort */ }
    }

    function renderStaleBanner(promotion) {
        const banner = document.getElementById("avStaleBanner");
        const text = document.getElementById("avStaleText");
        const btn = document.getElementById("avStaleRefreshBtn");
        if (!banner || !text || !btn) return;
        if (!promotion) {
            banner.style.display = "none";
            return;
        }
        const canStart = banner.dataset.canStart === "1";
        const version = promotion.impact ? promotion.impact.version : (promotion.version || "");
        text.textContent = "Studies are still built from the previous preferred version"
            + (version ? " (now preferred: " + version + ")" : "")
            + " — a study refresh is needed."
            + (canStart ? "" : " Ask an administrator to run a study refresh.");
        btn.style.display = canStart ? "inline-block" : "none";
        banner.style.display = "flex";
    }

    async function startStudyRefresh(btn) {
        btn.disabled = true;
        const prev = btn.textContent;
        btn.textContent = "Starting…";
        try {
            // Non-forced is sufficient: the promote already rewrote the active
            // annotation parquet, so every study's fingerprint check rebuilds.
            const res = await fetch("/api/start/recode_refresh_studies", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({}),
            });
            const body = await res.json().catch(function () { return {}; });
            if (res.status === 409 || (body.error || "").indexOf("already running") !== -1) {
                _status("A study refresh is already running — the banner clears when it succeeds.");
            } else if (!res.ok) {
                throw new Error(body.error || res.statusText);
            } else {
                _status("Study refresh started — the banner clears once it completes. "
                    + "Track it under Data Management → Refresh Caches.");
            }
        } catch (err) {
            _status("Could not start the study refresh: " + err.message, true);
        } finally {
            btn.disabled = false;
            btn.textContent = prev;
        }
    }




    async function activate(version, btn) {
        // Two-click confirm (native confirm() is blocked in embedded preview
        // browsers): first click arms the button, second within 4s activates.
        if (btn && btn.dataset.armed !== "1") {
            btn.dataset.armed = "1";
            btn.dataset.prevHtml = btn.innerHTML;
            btn.innerHTML = "Prefer — sure?";
            _status("Making " + version + " the preferred version — annotation datasets "
                + "use it after a study refresh. Click again to confirm.");
            setTimeout(function () {
                if (btn.dataset.armed === "1") {
                    btn.dataset.armed = "";
                    btn.innerHTML = btn.dataset.prevHtml;
                    _status("");
                }
            }, 4000);
            return;
        }
        if (btn) btn.dataset.armed = "";
        _status("Applying…");
        try {
            const res = await fetch(LIST + "/activate", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ version: version }),
            });
            const body = await res.json();
            if (!res.ok) throw new Error(body.error || res.statusText);
            _status(version + " is now the preferred version (rows: " + (body.active_rows ?? "—") + "). " + (body.note || ""));
            load();
            loadStaleness();
        } catch (err) {
            _status("Could not set the preferred version: " + err.message, true);
        }
    }

    // ---------- annotation contract card ----------

    const AC_ENDPOINT = "/api/manage/annotation-contract";
    // Staged upload awaiting confirmation: {text, filename, impact}; etag is the
    // last-loaded contract etag, sent back on confirm for optimistic concurrency.
    const acState = { staged: null, etag: null };

    async function _acLoadStatus() {
        try {
            const res = await fetch(AC_ENDPOINT);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const s = await res.json();
            _acRenderStatus(s);
        } catch (e) {
            const badge = document.getElementById("ac-source-badge");
            if (badge) badge.textContent = "unknown";
        }
    }

    function _acRenderStatus(s) {
        const badge = document.getElementById("ac-source-badge");
        const meta = document.getElementById("ac-meta");
        const err = document.getElementById("ac-error");
        const revert = document.getElementById("ac-revert-btn");
        const isRuntime = s.source === "runtime";
        // The etag is kept for optimistic concurrency on upload, never shown —
        // it is a storage implementation detail with no meaning to admins.
        acState.etag = s.etag || null;
        if (badge) {
            badge.textContent = isRuntime ? "custom upload" : "default";
            badge.style.color = isRuntime ? "var(--color-success)" : "var(--color-text-muted)";
            badge.style.borderColor = isRuntime ? "var(--color-success)" : "var(--color-border)";
        }
        if (meta) {
            const parts = [];
            if (s.current_version) parts.push(`New annotations made with version: <span class="font-mono">${_esc(s.current_version)}</span>`);
            if (isRuntime && s.updated_by) parts.push(`Uploaded by ${_esc(s.updated_by)}`);
            if (isRuntime && s.updated_at) parts.push(_esc(s.updated_at));
            meta.innerHTML = parts.join(" · ");
        }
        if (err) {
            if (s.error) {
                err.style.display = "block";
                err.textContent = `⚠ The uploaded contract could not be used: ${s.error} — using the default contract instead.`;
            } else {
                err.style.display = "none";
                err.textContent = "";
            }
        }
        if (revert) revert.style.display = isRuntime ? "inline-block" : "none";
    }

    function _acStatus(msg, color) {
        const el = document.getElementById("ac-card-status");
        if (el) {
            el.textContent = msg || "";
            el.style.color = color || "var(--color-text-muted)";
        }
    }

    function _acDownload() {
        window.location.href = `${AC_ENDPOINT}/download`;
    }

    async function _acOnFileChosen(input) {
        const file = input.files && input.files[0];
        input.value = "";  // allow re-selecting the same file later
        if (!file) return;
        _acStatus("Validating…");
        let text;
        try {
            text = await file.text();
        } catch (e) {
            _acStatus("Could not read the file.", "var(--color-danger)");
            return;
        }
        try {
            const fd = new FormData();
            fd.append("text", text);
            const res = await fetch(AC_ENDPOINT, { method: "POST", body: fd });
            const body = await res.json();
            if (res.status === 400 && body.errors) {
                _acStatus("", "var(--color-text-muted)");
                _acShowValidationErrors(file.name, body.errors);
                return;
            }
            if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
            acState.staged = { text, filename: file.name, impact: body.impact };
            _acStatus("");
            _acShowImpactModal(file.name, body.impact);
        } catch (e) {
            _acStatus(`Error: ${e.message}`, "var(--color-danger)");
        }
    }

    function _acShowValidationErrors(filename, errors) {
        const body = document.getElementById("ac-modal-body");
        const confirmBtn = document.getElementById("ac-confirm-btn");
        if (confirmBtn) confirmBtn.style.display = "none";
        if (body) {
            body.innerHTML = `<div style="color: var(--color-danger); margin-bottom: 8px;">`
                + `<span class="font-mono">${_esc(filename)}</span> is not a valid contract `
                + `(${errors.length} error${errors.length === 1 ? "" : "s"}):</div>`
                + '<ul style="margin: 0 0 0 18px; padding: 0;">'
                + errors.slice(0, 30).map(e => `<li>${_esc(e)}</li>`).join("")
                + (errors.length > 30 ? `<li>… and ${errors.length - 30} more</li>` : "")
                + "</ul>";
        }
        _acOpenModal();
    }

    function _acShowImpactModal(filename, impact) {
        const body = document.getElementById("ac-modal-body");
        const confirmBtn = document.getElementById("ac-confirm-btn");
        if (confirmBtn) confirmBtn.style.display = "inline-block";
        if (!body) return;
        const rows = [];
        if (impact.metadata_only) {
            rows.push(`<div style="color: var(--color-success); margin-bottom: 10px;">`
                + `✓ Metadata-only change — <strong>no new annotation version</strong>. `
                + `Existing annotations stay valid.</div>`);
        } else {
            rows.push(`<div style="color: var(--color-warning); margin-bottom: 10px;">`
                + `⚠ This changes the ${impact.prompt_changed && impact.schema_changed ? "prompt and response format"
                    : impact.prompt_changed ? "prompt" : "response format"}. `
                + `Future annotations will be made with a new version `
                + `<span class="font-mono">${_esc(impact.candidate_version)}</span> `
                + `(instead of <span class="font-mono">${_esc(impact.current_version)}</span>). `
                + `Studies keep showing what they show now until you make the new version preferred below.</div>`);
        }
        const detail = [];
        detail.push(`Prompt changed: <strong>${impact.prompt_changed ? "yes" : "no"}</strong>`);
        detail.push(`Schema changed: <strong>${impact.schema_changed ? "yes" : "no"}</strong>`);
        if (impact.fields_added && impact.fields_added.length) {
            detail.push(`Fields added: <span class="font-mono">${impact.fields_added.map(_esc).join(", ")}</span>`);
        }
        if (impact.fields_removed && impact.fields_removed.length) {
            detail.push(`Fields removed: <span class="font-mono">${impact.fields_removed.map(_esc).join(", ")}</span>`);
        }
        body.innerHTML = rows.join("")
            + `<div class="text-xs" style="color: var(--color-text-muted); margin-bottom: 4px;">`
            + `Uploading <span class="font-mono">${_esc(filename)}</span></div>`
            + '<ul style="margin: 6px 0 0 18px; padding: 0;" class="text-sm">'
            + detail.map(d => `<li>${d}</li>`).join("")
            + "</ul>";
        _acOpenModal();
    }

    // The contract drives var_schema metadata and the pending annotation
    // version — tell the other admin panels (var-schema table) to refetch.
    function _acAnnounceChange() {
        document.dispatchEvent(new CustomEvent("fyp:contract-changed"));
    }

    async function _acConfirmUpload() {
        if (!acState.staged) { _acCloseModal(); return; }
        const confirmBtn = document.getElementById("ac-confirm-btn");
        if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = "Activating…"; }
        try {
            const fd = new FormData();
            fd.append("text", acState.staged.text);
            fd.append("confirm", "1");
            if (acState.etag) fd.append("expected_etag", acState.etag);
            const res = await fetch(AC_ENDPOINT, { method: "POST", body: fd });
            const body = await res.json();
            if (res.status === 409) {
                _acStatus(`Rejected: ${body.message || "the contract changed"}.`, "var(--color-danger)");
                _acCloseModal();
                await _acLoadStatus();
                return;
            }
            if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
            acState.staged = null;
            _acCloseModal();
            _acStatus(body.note || "Contract activated.", "var(--color-success)");
            _acAnnounceChange();
        } catch (e) {
            _acStatus(`Error: ${e.message}`, "var(--color-danger)");
        } finally {
            if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = "Activate contract"; }
        }
    }

    async function _acRevert() {
        // Two-click confirm (native confirm() is blocked in embedded preview
        // browsers): first click arms the button, second within 4s reverts.
        const btn = document.getElementById("ac-revert-btn");
        if (btn && btn.dataset.armed !== "1") {
            btn.dataset.armed = "1";
            btn.dataset.prevHtml = btn.innerHTML;
            btn.innerHTML = "Revert — sure?";
            _acStatus("Your uploaded contract will be archived and the default contract restored. Click again to revert.");
            setTimeout(function () {
                if (btn.dataset.armed === "1") {
                    btn.dataset.armed = "";
                    btn.innerHTML = btn.dataset.prevHtml;
                    _acStatus("");
                }
            }, 4000);
            return;
        }
        if (btn) {
            btn.dataset.armed = "";
            btn.innerHTML = btn.dataset.prevHtml || btn.innerHTML;
        }
        _acStatus("Reverting…");
        try {
            const res = await fetch(`${AC_ENDPOINT}/revert`, { method: "POST" });
            const body = await res.json();
            if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
            _acStatus(body.note || "Reverted.", "var(--color-success)");
            _acAnnounceChange();
        } catch (e) {
            _acStatus(`Error: ${e.message}`, "var(--color-danger)");
        }
    }

    function _acOpenModal() {
        const m = document.getElementById("ac-modal");
        if (m) m.style.display = "flex";
    }

    function _acCloseModal() {
        const m = document.getElementById("ac-modal");
        if (m) m.style.display = "none";
    }

    // Public globals used by inline handlers in the template.
    window.acDownload = _acDownload;
    window.acOnFileChosen = _acOnFileChosen;
    window.acConfirmUpload = _acConfirmUpload;
    window.acRevert = _acRevert;
    window.acCloseModal = _acCloseModal;

    // ---------- bootstrap ----------

    let bootstrapped = false;

    // Defer until the versions page becomes active — avoids fetching the
    // versions list and contract status before the admin opens the sub-page.
    function _maybeBootstrap() {
        const page = document.getElementById("admin-page-versions");
        if (!page) return;
        if (bootstrapped) return;
        if (!page.classList.contains("active")) return;
        bootstrapped = true;
        const btn = document.getElementById("avRefreshBtn");
        if (btn) btn.addEventListener("click", load);
        const staleBtn = document.getElementById("avStaleRefreshBtn");
        if (staleBtn) staleBtn.addEventListener("click", function () { startStudyRefresh(staleBtn); });
        if (document.getElementById("avTableBody")) load();
        _acLoadStatus();
        loadStaleness();
    }

    // Hook the admin sidebar's openAdminPage flow — it just toggles the
    // .active class, so observe DOM mutations rather than monkey-patching
    // the function (which the embedded admin.html script defines).
    function _watchForActivation() {
        const page = document.getElementById("admin-page-versions");
        if (!page) return;
        const observer = new MutationObserver(_maybeBootstrap);
        observer.observe(page, { attributes: true, attributeFilter: ["class"] });
        // Also try once on script load in case the page is already active
        // (e.g. user refreshes the browser while on this tab).
        _maybeBootstrap();
    }

    // Any contract activation/revert (the card's upload flow or the form
    // editor) announces itself; refresh the card status + versions list —
    // a changed contract can shift the "current config" version label.
    document.addEventListener("fyp:contract-changed", function () {
        if (!bootstrapped) return;
        _acLoadStatus();
        load();
        loadStaleness();
    });

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", _watchForActivation);
    } else {
        _watchForActivation();
    }
})();
