// Admin → Data Contracts (read-only viewer for the scrape / activity / derived
// contracts + the scrape/activity version registries). Extracted pattern from
// admin_annotation_versions.js: plain fetch, refresh-button-driven, no state.

(function () {
    "use strict";

    const KINDS = [
        { kind: "scrape", title: "Scrape contract", versioned: true },
        { kind: "activity", title: "Activity contract", versioned: true },
        { kind: "derived", title: "Derived contract", versioned: false },
    ];

    const cell = "padding: 6px 8px; border-bottom: 1px solid var(--color-border); vertical-align: top;";
    const th = "padding: 6px 8px; border-bottom: 1px solid var(--color-border); text-align: left;";

    function _esc(s) {
        return String(s == null ? "" : s)
            .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
    }

    function _status(msg, isError) {
        const el = document.getElementById("dcStatus");
        if (!el) return;
        el.textContent = msg || "";
        el.style.color = isError ? "var(--color-danger)" : "var(--color-text-muted)";
    }

    function _fmtDT(v) {
        if (typeof fypFmtDateTime === "function") return fypFmtDateTime(v, "—");
        return v || "—";
    }

    function _badge(text) {
        return '<span class="text-xxs uppercase font-semibold" style="padding: 2px 8px; ' +
            'border-radius: 10px; border: 1px solid var(--color-border); ' +
            'color: var(--color-text-muted); white-space: nowrap;">' + _esc(text) + "</span>";
    }

    function _flagBadges(f) {
        const flags = [];
        if (f.required) flags.push("required");
        if (f.derived) flags.push("derived");
        if (f.skip_recode !== undefined) flags.push("skip_recode: " + f.skip_recode);
        return flags.map(_badge).join(" ");
    }

    function _fieldTable(payload) {
        const hasScope = payload.fields.some((f) => f.scope !== undefined);
        let html = '<table style="width: 100%; border-collapse: collapse; margin-bottom: 8px;">' +
            '<thead><tr class="text-sm" style="color: var(--color-text-muted);">' +
            '<th style="' + th + '">Name</th>' +
            (hasScope ? '<th style="' + th + '"><span class="meta-tooltip" data-tooltip="base = every platform emits this field; platform = owned by one platform.">Scope</span></th>' : "") +
            '<th style="' + th + '">Dtype</th>' +
            '<th style="' + th + '">Role</th>' +
            '<th style="' + th + '">Scale</th>' +
            '<th style="' + th + '">Section</th>' +
            '<th style="' + th + '">Display name</th>' +
            '<th style="' + th + '">Flags</th>' +
            "</tr></thead><tbody>";
        for (const f of payload.fields) {
            const scope = f.scope === "platform" && f.platform ? "platform: " + f.platform : (f.scope || "");
            html += '<tr class="text-sm">' +
                '<td style="' + cell + ' font-family: var(--font-mono);">' + _esc(f.name) + "</td>" +
                (hasScope ? '<td style="' + cell + '">' + _esc(scope) + "</td>" : "") +
                '<td style="' + cell + ' font-family: var(--font-mono);">' + _esc(f.dtype || "") + "</td>" +
                '<td style="' + cell + '">' + _esc(f.role || "") + "</td>" +
                '<td style="' + cell + '">' + _esc(f.scale || "") + "</td>" +
                '<td style="' + cell + '">' + _esc(f.section || "") + "</td>" +
                '<td style="' + cell + '">' +
                    (f.description
                        ? '<span class="meta-tooltip" data-tooltip="' + _esc(f.description) + '">' + _esc(f.display_name || "") + "</span>"
                        : _esc(f.display_name || "")) + "</td>" +
                '<td style="' + cell + '">' + _flagBadges(f) + "</td>" +
                "</tr>";
        }
        html += "</tbody></table>";
        return html;
    }

    function _versionTable(kind, data) {
        if (!data.versions.length) {
            return '<div class="text-sm" style="color: var(--color-text-muted); padding: 8px 0;">' +
                "No versions recorded yet.</div>";
        }
        let html = '<table style="width: 100%; border-collapse: collapse; margin-bottom: 8px;">' +
            '<thead><tr class="text-sm" style="color: var(--color-text-muted);">' +
            '<th style="' + th + '"><span class="meta-tooltip" data-tooltip="Automatically generated ID stamped on every row produced under this contract version.">Version</span></th>' +
            '<th style="' + th + '">Label</th>' +
            '<th style="' + th + '">Created</th>' +
            '<th style="' + th + '">Platforms</th>' +
            '<th style="' + th + '">Details</th>' +
            '<th style="' + th + '"><span class="meta-tooltip" data-tooltip="The version whose rows analyses prefer when an item exists under several. Changed from code/registry tooling, not from this page.">Preferred</span></th>' +
            '<th style="' + th + '"><span class="meta-tooltip" data-tooltip="The version the currently deployed code stamps on new rows.">Active</span></th>' +
            "</tr></thead><tbody>";
        for (const v of data.versions) {
            const platforms = Array.isArray(v.platforms) ? v.platforms.join(", ") : "";
            html += '<tr class="text-sm">' +
                '<td style="' + cell + ' font-family: var(--font-mono);">' + _esc(v.version) + "</td>" +
                '<td style="' + cell + '">' + _esc(v.label || "") + "</td>" +
                '<td style="' + cell + '">' + _esc(_fmtDT(v.created_at)) + "</td>" +
                '<td style="' + cell + '">' + _esc(platforms) + "</td>" +
                '<td style="' + cell + '"><button class="btn-discreet text-xs dc-details-btn" ' +
                    'data-kind="' + _esc(kind) + '" data-version="' + _esc(v.version) + '">View</button></td>' +
                '<td style="' + cell + '">' + (v.preferred ? "★" : "—") + "</td>" +
                '<td style="' + cell + '">' + (v.version === data.active ? "★" : "—") + "</td>" +
                "</tr>";
        }
        html += "</tbody></table>" +
            '<pre id="dcSnapshot-' + _esc(kind) + '" class="text-xs" style="display: none; max-height: 280px; ' +
            'overflow: auto; background: var(--color-bg-elevated); padding: 12px; border-radius: 6px;"></pre>';
        return html;
    }

    function _sectionHtml(spec, payload) {
        const active = payload.active_version;
        const headerBits = [
            '<span style="font-family: var(--font-mono);" class="text-xs">' + _esc(payload.path) + "</span>",
        ];
        if (payload.meta_version) headerBits.push(_badge("meta v" + payload.meta_version));
        if (Array.isArray(payload.platforms)) {
            for (const p of payload.platforms) headerBits.push(_badge(p));
        }
        if (active && active.version) {
            headerBits.push('<span class="text-xs" style="color: var(--color-text-muted);">active: ' +
                '<span style="font-family: var(--font-mono);">' + _esc(active.version) + "</span></span>");
        }

        let html = '<section style="margin-bottom: 28px;">' +
            '<h3 class="text-h3" style="margin: 0 0 6px 0; color: var(--color-text-heading);">' + _esc(spec.title) + "</h3>" +
            '<div style="display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center; margin-bottom: 10px;">' +
            headerBits.join(" ") +
            '<span style="flex: 1;"></span>' +
            '<button class="btn-discreet text-xs dc-toml-btn" data-kind="' + _esc(spec.kind) + '">View TOML</button>' +
            '<a class="btn-discreet text-xs" style="text-decoration: none;" ' +
                'href="/api/manage/data-contracts/' + _esc(spec.kind) + '/download">Download</a>' +
            "</div>";

        if (payload.validation_errors && payload.validation_errors.length) {
            html += '<div class="text-sm" style="margin: 0 0 10px 0; padding: 10px 14px; ' +
                'border: 1px solid var(--color-danger); border-radius: 8px; color: var(--color-danger);">' +
                payload.validation_errors.map(_esc).join("<br>") + "</div>";
        }

        html += '<pre id="dcRaw-' + _esc(spec.kind) + '" class="text-xs" style="display: none; max-height: 320px; ' +
            'overflow: auto; background: var(--color-bg-elevated); padding: 12px; border-radius: 6px; margin: 0 0 10px 0;"></pre>';

        if (payload.fields.length) {
            html += _fieldTable(payload);
        }

        if (spec.versioned) {
            html += '<div class="text-sm" style="color: var(--color-text-muted); margin: 12px 0 4px 0;">Version history</div>' +
                '<div id="dcVersions-' + _esc(spec.kind) + '"></div>';
        }

        html += "</section>";
        return html;
    }

    async function _getJSON(url) {
        const res = await fetch(url);
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(body.error || (url + " -> HTTP " + res.status));
        return body;
    }

    async function _toggleRaw(kind) {
        const pre = document.getElementById("dcRaw-" + kind);
        if (!pre) return;
        if (pre.style.display !== "none") { pre.style.display = "none"; return; }
        if (!pre.textContent) {
            try {
                const body = await _getJSON("/api/manage/data-contracts/" + kind + "/raw");
                pre.textContent = body.toml || "";
            } catch (e) {
                pre.textContent = "Failed to load TOML: " + e.message;
            }
        }
        pre.style.display = "block";
    }

    async function _showDetails(kind, version) {
        const pre = document.getElementById("dcSnapshot-" + kind);
        if (!pre) return;
        if (pre.dataset.version === version && pre.style.display !== "none") {
            pre.style.display = "none";
            return;
        }
        try {
            const body = await _getJSON("/api/manage/data-contracts/" + kind + "/versions/" +
                encodeURIComponent(version));
            delete body.field_metadata; // bulky per-field snapshot; the digest is the readable part
            pre.textContent = JSON.stringify(body, null, 2);
            pre.dataset.version = version;
            pre.style.display = "block";
        } catch (e) {
            pre.textContent = "Failed to load version: " + e.message;
            pre.style.display = "block";
        }
    }

    async function load() {
        const container = document.getElementById("dcSections");
        if (!container) return;
        _status("Loading contracts…");
        try {
            const payloads = await Promise.all(
                KINDS.map((s) => _getJSON("/api/manage/data-contracts/" + s.kind)),
            );
            container.innerHTML = KINDS.map((s, i) => _sectionHtml(s, payloads[i])).join("");
            _status("");
            for (const spec of KINDS.filter((s) => s.versioned)) {
                const slot = document.getElementById("dcVersions-" + spec.kind);
                try {
                    const data = await _getJSON("/api/manage/data-contracts/" + spec.kind + "/versions");
                    slot.innerHTML = _versionTable(spec.kind, data);
                } catch (e) {
                    slot.innerHTML = '<div class="text-sm" style="color: var(--color-danger);">' +
                        _esc("Failed to load version history: " + e.message) + "</div>";
                }
            }
        } catch (e) {
            _status("Failed to load contracts: " + e.message, true);
        }
    }

    document.addEventListener("click", (ev) => {
        const tomlBtn = ev.target.closest(".dc-toml-btn");
        if (tomlBtn) { _toggleRaw(tomlBtn.dataset.kind); return; }
        const detailsBtn = ev.target.closest(".dc-details-btn");
        if (detailsBtn) { _showDetails(detailsBtn.dataset.kind, detailsBtn.dataset.version); }
    });

    document.addEventListener("DOMContentLoaded", () => {
        const btn = document.getElementById("dcRefreshBtn");
        if (btn) btn.addEventListener("click", load);
    });
})();
