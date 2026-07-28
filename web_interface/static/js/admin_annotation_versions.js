/**
 * Admin "Versions" panel.
 *
 * Lists recorded annotation versions (GET /api/manage/annotation-versions),
 * shows a version's prompt/schema snapshot, promotes the PREFERRED version
 * (POST .../promote — the version studies read), and re-activates a recorded
 * version ("Activate": its contract snapshot re-applied through the normal
 * contract confirm flow at /api/manage/annotation-contract, making it the
 * version new annotations use). Contract AUTHORING lives on the Contracts
 * page; here only a slim read-only status strip reports which contract is
 * active (and whether an uploaded contract failed to parse). The global fetch wrapper in main.js injects the CSRF
 * header, so plain fetch is sufficient (matches admin_var_schema.js).
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

    // Key of the version whose snapshot is shown below the table ("__active__"
    // for an active contract with no minted version yet), or null when hidden.
    // Drives the depressed state of the matching View button.
    let viewedKey = null;

    function _snapshotKeyFor(btn) {
        return btn.id === "avViewActive" ? "__active__" : btn.dataset.v;
    }

    function _syncViewButtons() {
        document.querySelectorAll("#avTableBody .av-view, #avViewActive").forEach(function (b) {
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

    // Snapshot of the last-loaded list, for the Prefer confirm modal (current
    // preferred version + per-version annotated-video counts).
    let lastLoad = { preferred: null, counts: null };

    function render(body) {
        // Most recent first; the synthetic legacy version (epoch created_at)
        // sinks to the bottom naturally.
        const versions = (body.versions || []).slice().sort(function (a, b) {
            return String(b.created_at || "").localeCompare(String(a.created_at || ""));
        });
        const activeVersion = body.active;
        // {version: n annotated videos}; null when the archive was unreadable —
        // then show "—" instead of a misleading 0.
        const counts = body.counts;
        lastLoad = { preferred: body.preferred || null, counts: counts || null };

        const tbody = document.getElementById("avTableBody");
        if (!tbody) return;

        const cell = 'padding: 8px; border-bottom: 1px solid var(--color-border);';
        const mono = cell + ' font-family: var(--font-mono);';
        const num = cell + ' text-align: right; font-variant-numeric: tabular-nums;';

        function _countCell(version) {
            if (counts == null) {
                return '<td style="' + num + ' color: var(--color-text-muted);">—</td>';
            }
            const n = counts[version] || 0;
            return '<td style="' + num + (n ? '' : ' color: var(--color-text-muted);') + '">'
                + n.toLocaleString() + "</td>";
        }

        // One column per button kind, so the same button always sits in the
        // same place: Details (View), Preferred (Prefer / ✓ Preferred), Active
        // (Activate / ✓ Active). A row that already holds a state shows a
        // green, non-interactive button stating the fact; a row the button
        // does not apply to gets an empty cell.
        function _preferCell(v) {
            // The legacy version can be preferred too (its rows are in the
            // archive like any other version's) — only Activate is impossible
            // for it, because it has no contract snapshot.
            if (v.preferred) {
                return '<td style="' + cell + '"><button class="btn-compact btn-state btn-row-fixed">✓ Preferred</button></td>';
            }
            return '<td style="' + cell + '"><button class="btn-primary btn-compact btn-row-fixed av-prefer" data-v="'
                + _esc(v.annotation_version) + '">Prefer</button></td>';
        }

        function _activeCell(v, isActive) {
            if (v.annotation_version === LEGACY_VERSION) return '<td style="' + cell + '"></td>';
            if (isActive) {
                return '<td style="' + cell + '"><button class="btn-compact btn-state btn-row-fixed">✓ Active</button></td>';
            }
            if (!v.restorable) {
                return '<td style="' + cell + '"><button class="btn-discreet btn-compact btn-row-fixed meta-tooltip" disabled '
                    + 'data-tooltip="Recorded before contract snapshots — its contract file '
                    + 'was not saved, so it cannot be re-activated automatically.">Activate</button></td>';
            }
            return '<td style="' + cell + '"><button class="btn-primary btn-compact btn-row-fixed av-restore" data-v="'
                + _esc(v.annotation_version) + '">Activate</button></td>';
        }

        // Pinned row for the active contract when it has no minted version yet
        // (pre-existing deployments; new activations mint eagerly) — its View
        // renders the generated prompt/schema from the active contract.
        const activeIsMinted = versions.some(function (v) { return v.annotation_version === activeVersion; });
        let activeRow = "";
        if (activeVersion && !activeIsMinted) {
            activeRow = "<tr>" +
                '<td style="' + mono + '">' + _esc(activeVersion) + "</td>" +
                '<td style="' + cell + ' color: var(--color-text-muted);">Version for new annotations — none saved yet</td>' +
                '<td style="' + cell + '"></td>' +
                _countCell(activeVersion) +
                '<td style="' + cell + '"><button class="btn-discreet btn-compact av-view" id="avViewActive">View</button></td>' +
                '<td style="' + cell + '"></td>' +
                '<td style="' + cell + '"><button class="btn-compact btn-state btn-row-fixed">✓ Active</button></td>' +
                "</tr>";
        }

        if (!versions.length && !activeRow) {
            tbody.innerHTML =
                '<tr><td colspan="7" class="text-sm" style="color: var(--color-text-muted); padding: 12px;">' +
                "No versions recorded yet. Activating a contract (or running annotation) registers one." +
                "</td></tr>";
            return;
        }

        tbody.innerHTML = activeRow + versions.map(function (v) {
            const isActive = v.annotation_version === activeVersion;
            return "<tr>" +
                '<td style="' + mono + '">' + _esc(v.annotation_version) + "</td>" +
                '<td style="' + cell + '">' + _esc(v.label) + "</td>" +
                '<td style="' + cell + '">' + _esc(v.created_at) + "</td>" +
                _countCell(v.annotation_version) +
                '<td style="' + cell + '"><button class="btn-discreet btn-compact av-view" data-v="'
                    + _esc(v.annotation_version) + '">View</button></td>' +
                _preferCell(v) +
                _activeCell(v, isActive) +
                "</tr>";
        }).join("");

        // Every View button toggles: clicking the depressed one hides the
        // snapshot, clicking another switches it.
        tbody.querySelectorAll(".av-view").forEach(function (b) {
            b.addEventListener("click", function () {
                const key = _snapshotKeyFor(b);
                if (key === viewedKey) { _hideSnapshot(); return; }
                if (key === "__active__") viewActiveRendered();
                else viewSnapshot(key);
            });
        });
        tbody.querySelectorAll(".av-prefer").forEach(function (b) {
            b.addEventListener("click", function () { promote(b.dataset.v, b); });
        });
        tbody.querySelectorAll(".av-restore").forEach(function (b) {
            b.addEventListener("click", function () { makeActive(b.dataset.v, b); });
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

    async function viewActiveRendered() {
        _status("Rendering active contract…");
        try {
            const res = await fetch("/api/manage/annotation-contract/rendered");
            const body = await res.json();
            if (!res.ok) throw new Error(body.error || res.statusText);
            document.getElementById("avSnapVersion").textContent =
                (body.version || "active") + " (version for new annotations — none saved yet)";
            _renderSnapSettings(body.descriptor || {});
            document.getElementById("avSnapPrompt").textContent = body.prompt || "(none)";
            document.getElementById("avSnapSchema").textContent =
                body.schema ? JSON.stringify(body.schema, null, 2) : "(none / free-text)";
            document.getElementById("avSnapshot").style.display = "block";
            viewedKey = "__active__";
            _syncViewButtons();
            _status("");
        } catch (err) {
            _status("Failed to render active contract: " + err.message, true);
        }
    }

    // ---------- re-activate a recorded version ----------

    // Re-apply a version's contract snapshot as the active contract (and,
    // when it was recorded under a different, still-switchable backend, offer
    // to switch that too) via the NORMAL contract confirm flow — the predicted
    // version hash tells us honestly whether this re-activates av_X exactly or
    // mints a new version based on it.
    async function makeActive(version, btn) {
        _status("Preparing activation…");
        btn.disabled = true;
        try {
            const res = await fetch(LIST + "/" + encodeURIComponent(version));
            const body = await res.json();
            if (!res.ok) throw new Error(body.error || res.statusText);
            const rec = body.record || {};
            const restore = body.restore || {};
            const be = restore.backend || {};
            if (!rec.contract_text) throw new Error("this version has no contract snapshot");

            const wantSwitch = !!(be.mismatch && be.can_switch_backend && be.target_available);

            // Dry-run the contract upload to get the honest impact report.
            // When a backend switch is on offer, dry-run BOTH checkbox states
            // so the modal can show the true outcome of either choice.
            async function dryRun(withSwitch) {
                const payload = { text: rec.contract_text };
                if (withSwitch) payload.switch_backend = be.target;
                const dres = await fetch(AC_ENDPOINT, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
                const dbody = await dres.json();
                if (!dres.ok) {
                    throw new Error((dbody.errors || []).join("; ") || dbody.error || dres.statusText);
                }
                return dbody.impact || {};
            }
            const impactNoSwitch = await dryRun(false);
            const impactSwitch = wantSwitch ? await dryRun(true) : null;

            // One self-contained outcome statement per checkbox state: whether
            // this is an exact re-activation or mints a new version, and which
            // backend + model new annotations then run on.
            function outcomeHtml(impact) {
                const backendPart = "new annotations will run on backend <strong>"
                    + _esc(impact.target_backend || "gemini") + "</strong>"
                    + (impact.target_model
                        ? ' · <span class="font-mono">' + _esc(impact.target_model) + "</span>" : "");
                if (impact.candidate_version === version) {
                    return '<div style="color: var(--color-success); margin-bottom: 10px;">'
                        + '✓ This re-activates version <span class="font-mono">' + _esc(version)
                        + "</span> exactly — " + backendPart + ", as recorded.</div>";
                }
                return '<div style="color: var(--color-warning); margin-bottom: 10px;">'
                    + '⚠ This does not bring back <span class="font-mono">' + _esc(version)
                    + "</span> exactly: its contract is re-applied, but under the current "
                    + "model and settings that creates a new version <span class=\"font-mono\">"
                    + _esc(impact.candidate_version) + "</span>, and " + backendPart + ".</div>";
            }

            const rows = [];
            if (wantSwitch) {
                // Context → choice → outcome, with the outcome re-rendered
                // whenever the checkbox changes so the two never disagree.
                rows.push('<div style="margin-bottom: 10px;">This version was recorded under backend '
                    + "<strong>" + _esc(be.target) + "</strong>; the active backend is currently "
                    + "<strong>" + _esc(be.active) + "</strong>.</div>");
                rows.push('<div style="margin-bottom: 10px;">'
                    + '<label class="text-sm" style="display: flex; gap: 8px; align-items: baseline; cursor: pointer;">'
                    + '<input type="checkbox" id="av-restore-switch" checked> '
                    + "<span>Switch the active annotation backend back to <strong>" + _esc(be.target)
                    + "</strong> as part of the activation.</span></label></div>");
                rows.push('<div id="av-restore-outcome">' + outcomeHtml(impactSwitch) + "</div>");
            } else {
                rows.push(outcomeHtml(impactNoSwitch));
                if (be.mismatch && !be.target_available) {
                    rows.push('<div style="color: var(--color-warning); margin-bottom: 10px;">'
                        + "⚠ Recorded under backend <strong>" + _esc(be.target)
                        + "</strong>, which is not available here ("
                        + _esc(be.target_unavailable_reason || "unavailable")
                        + ") — activation runs on <strong>" + _esc(be.active) + "</strong> instead.</div>");
                } else if (be.mismatch && !be.can_switch_backend) {
                    rows.push('<div style="color: var(--color-warning); margin-bottom: 10px;">'
                        + "⚠ Recorded under backend <strong>" + _esc(be.target)
                        + "</strong>; switching backends requires the Backends admin permission — "
                        + "activation runs on <strong>" + _esc(be.active) + "</strong> instead.</div>");
                }
            }
            rows.push('<div class="text-xs" style="color: var(--color-text-muted);">'
                + "Studies keep using the preferred version — activating a version only "
                + "affects how new annotations are produced.</div>");

            acState.mode = "activate";
            acState.staged = {
                text: rec.contract_text,
                filename: version + " snapshot",
                switchBackend: wantSwitch ? be.target : null,
            };
            const modalTitle = document.getElementById("ac-modal-title");
            if (modalTitle) modalTitle.textContent = "Activate this version";
            const modalBody = document.getElementById("ac-modal-body");
            const confirmBtn = document.getElementById("ac-confirm-btn");
            if (confirmBtn) {
                confirmBtn.style.display = "inline-block";
                confirmBtn.textContent = "Activate";
            }
            if (modalBody) modalBody.innerHTML = rows.join("");
            const switchCb = document.getElementById("av-restore-switch");
            if (switchCb) {
                switchCb.addEventListener("change", function () {
                    const out = document.getElementById("av-restore-outcome");
                    if (out) out.innerHTML = outcomeHtml(switchCb.checked ? impactSwitch : impactNoSwitch);
                });
            }
            _acOpenModal();
            _status("");
        } catch (err) {
            _status("Activation failed: " + err.message, true);
        } finally {
            btn.disabled = false;
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




    // Open the shared confirm modal with a plain-language account of what
    // preferring this version means; the POST happens in _confirmPrefer once
    // the admin confirms.
    function promote(version) {
        const current = lastLoad.preferred;
        const counts = lastLoad.counts;
        const isLegacy = version === LEGACY_VERSION;
        const label = isLegacy
            ? '<span class="font-mono">' + _esc(version) + "</span> (the pre-versioning annotations)"
            : '<span class="font-mono">' + _esc(version) + "</span>";

        const rows = [];
        rows.push('<div style="margin-bottom: 10px;">The <strong>preferred</strong> version is the '
            + "one analyses and study datasets read. This makes " + label
            + " the preferred version"
            + (current && current !== version
                ? " — currently it is <span class=\"font-mono\">" + _esc(current) + "</span>"
                : "")
            + ".</div>");
        rows.push('<div style="margin-bottom: 10px;">For every video annotated under several '
            + "versions, its " + '<span class="font-mono">' + _esc(version) + "</span> annotation "
            + "is then the one analyses use; videos without one fall back to their most recent "
            + "annotation. Nothing is changed or deleted in the annotations themselves.</div>");
        if (counts) {
            const n = counts[version] || 0;
            rows.push('<div style="margin-bottom: 10px;">' + n.toLocaleString()
                + " video(s) currently have an annotation made with this version.</div>");
        }
        rows.push('<div class="text-xs" style="color: var(--color-text-muted);">'
            + "When you confirm, the shared annotation dataset is rebuilt right away. "
            + "Per-study datasets follow on their next study refresh — a banner above "
            + "the table will offer to start one.</div>");

        acState.mode = "prefer";
        acState.promoteVersion = version;
        acState.staged = null;
        const title = document.getElementById("ac-modal-title");
        if (title) title.textContent = "Make this version preferred";
        const confirmBtn = document.getElementById("ac-confirm-btn");
        if (confirmBtn) {
            confirmBtn.style.display = "inline-block";
            confirmBtn.textContent = "Prefer";
        }
        const modalBody = document.getElementById("ac-modal-body");
        if (modalBody) modalBody.innerHTML = rows.join("");
        _acOpenModal();
    }




    async function _confirmPrefer() {
        const version = acState.promoteVersion;
        if (!version) { _acCloseModal(); return; }
        const confirmBtn = document.getElementById("ac-confirm-btn");
        if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = "Applying…"; }
        try {
            const res = await fetch(LIST + "/promote", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ version: version }),
            });
            const body = await res.json();
            if (!res.ok) throw new Error(body.error || res.statusText);
            acState.promoteVersion = null;
            _acCloseModal();
            // preferred_rows counts the whole rebuilt dataset — including
            // fallback rows and failed annotations — so it is larger than the
            // table's "annotated videos" count; say so to avoid alarm.
            const rows = body.preferred_rows != null ? body.preferred_rows.toLocaleString() : "—";
            _status(version + " is now the preferred version. The shared annotation dataset was "
                + "rebuilt with " + rows + " rows — each video's " + version + " annotation where "
                + "one exists, otherwise its most recent. Refresh studies to apply it to study datasets.");
            load();
            loadStaleness();
        } catch (err) {
            _acCloseModal();
            _status("Could not set the preferred version: " + err.message, true);
        } finally {
            if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = "Prefer"; }
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
            if (s.active_version) parts.push(`Active version (used for new annotations): <span class="font-mono">${_esc(s.active_version)}</span>`);
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
    }

    function _acStatus(msg, color) {
        const el = document.getElementById("ac-card-status");
        if (el) {
            el.textContent = msg || "";
            el.style.color = color || "var(--color-text-muted)";
        }
    }

    // The contract drives var_schema metadata and the pending annotation
    // version — tell the other admin panels (var-schema table) to refetch.
    function _acAnnounceChange() {
        document.dispatchEvent(new CustomEvent("fyp:contract-changed"));
    }

    async function _acConfirmUpload() {
        // The shared modal serves both per-version actions — dispatch on mode.
        if (acState.mode === "prefer") return _confirmPrefer();
        if (!acState.staged) { _acCloseModal(); return; }
        const confirmBtn = document.getElementById("ac-confirm-btn");
        if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = "Applying…"; }
        try {
            const fd = new FormData();
            fd.append("text", acState.staged.text);
            fd.append("confirm", "1");
            if (acState.etag) fd.append("expected_etag", acState.etag);
            // Restore flow: carry the backend switch unless its (pre-checked)
            // opt-out checkbox was unticked.
            const switchCb = document.getElementById("av-restore-switch");
            if (acState.staged.switchBackend && (!switchCb || switchCb.checked)) {
                fd.append("switch_backend", acState.staged.switchBackend);
            }
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
            if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = "Activate"; }
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

    // Public globals used by inline handlers in the template (the confirm
    // modal, which the "Activate" re-activation flow drives).
    window.acConfirmUpload = _acConfirmUpload;
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

    // Any contract activation/revert (from the Playground, the form
    // editor, or a restore here) announces itself; refresh the status strip
    // + versions list — a changed contract shifts the active version.
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
