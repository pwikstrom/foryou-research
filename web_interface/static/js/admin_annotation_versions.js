/**
 * Admin "Annotation Versions" panel.
 *
 * Lists recorded annotation versions (GET /api/manage/annotation-versions),
 * shows a version's prompt/schema snapshot, and promotes a version to active
 * (POST .../promote). The global fetch wrapper in main.js injects the CSRF
 * header, so plain fetch is sufficient (matches admin_var_schema.js).
 */
(function () {
    "use strict";

    const LIST = "/api/manage/annotation-versions";
    // Matches annotation_versioning.LEGACY_VERSION. The legacy version is a
    // synthetic, snapshot-less entry that only owns pre-versioning fields, so it
    // can never be promoted (the backend rejects it too).
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
        const versions = body.versions || [];
        const active = body.active;
        const current = body.current;

        const activeLabel = document.getElementById("avActiveLabel");
        if (activeLabel) {
            activeLabel.textContent =
                "Active: " + (active || "none (latest-per-item)") +
                "  ·  current config: " + (current || "—");
        }

        const tbody = document.getElementById("avTableBody");
        if (!tbody) return;
        if (!versions.length) {
            tbody.innerHTML =
                '<tr><td colspan="7" class="text-sm" style="color: var(--color-text-muted); padding: 12px;">' +
                "No versions recorded yet. They appear once annotation runs under the current config." +
                "</td></tr>";
            return;
        }

        const cell = 'padding: 8px; border-bottom: 1px solid var(--color-border);';
        const mono = cell + ' font-family: var(--font-mono);';
        tbody.innerHTML = versions.map(function (v) {
            const isActive = !!v.active;
            const isLegacy = v.annotation_version === LEGACY_VERSION;
            const promoteBtn = (isActive || isLegacy) ? "" :
                '<button class="btn-primary av-promote" data-v="' + _esc(v.annotation_version) + '">Promote</button>';
            return "<tr>" +
                '<td style="' + cell + '">' + (isActive ? "✓" : "") + "</td>" +
                '<td style="' + mono + '">' + _esc(v.annotation_version) + "</td>" +
                '<td style="' + cell + '">' + _esc(v.label) + "</td>" +
                '<td style="' + cell + '">' + _esc(v.model) + "</td>" +
                '<td style="' + mono + '">' + _esc(v.schema_hash) + "</td>" +
                '<td style="' + cell + '">' + _esc(v.created_at) + "</td>" +
                '<td style="' + cell + '">' +
                    '<button class="btn-discreet av-view" data-v="' + _esc(v.annotation_version) + '">View</button> ' +
                    promoteBtn +
                "</td>" +
                "</tr>";
        }).join("");

        tbody.querySelectorAll(".av-view").forEach(function (b) {
            b.addEventListener("click", function () { viewSnapshot(b.dataset.v); });
        });
        tbody.querySelectorAll(".av-promote").forEach(function (b) {
            b.addEventListener("click", function () { promote(b.dataset.v); });
        });
    }

    async function viewSnapshot(version) {
        _status("Loading snapshot…");
        try {
            const res = await fetch(LIST + "/" + encodeURIComponent(version));
            const body = await res.json();
            if (!res.ok) throw new Error(body.error || res.statusText);
            const rec = body.record || {};
            document.getElementById("avSnapVersion").textContent = version;
            document.getElementById("avSnapPrompt").textContent = rec.prompt_text || "(none)";
            document.getElementById("avSnapSchema").textContent =
                rec.schema_json ? JSON.stringify(rec.schema_json, null, 2) : "(none / free-text)";
            document.getElementById("avSnapshot").style.display = "block";
            _status("");
        } catch (err) {
            _status("Failed to load snapshot: " + err.message, true);
        }
    }

    async function promote(version) {
        const ok = window.confirm(
            "Promote " + version + " to the active version?\n\n" +
            "This rebuilds the global active annotations dataset. Refresh studies " +
            "afterwards to apply it to per-study datasets."
        );
        if (!ok) return;
        _status("Promoting…");
        try {
            const res = await fetch(LIST + "/promote", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ version: version }),
            });
            const body = await res.json();
            if (!res.ok) throw new Error(body.error || res.statusText);
            _status("Promoted " + version + " (active rows: " + (body.active_rows ?? "—") + "). " + (body.note || ""));
            load();
        } catch (err) {
            _status("Promote failed: " + err.message, true);
        }
    }

    document.addEventListener("DOMContentLoaded", function () {
        const btn = document.getElementById("avRefreshBtn");
        if (btn) btn.addEventListener("click", load);
        // Auto-load only when the panel is present (rendered for permitted users).
        if (document.getElementById("avTableBody")) load();
    });
})();
