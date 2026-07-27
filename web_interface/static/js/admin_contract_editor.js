/**
 * Annotation-contract form editor (full-screen modal on the Annotation
 * versions page).
 *
 * Hydrates from GET /api/manage/annotation-contract/parsed (parsed contract
 * dict + help texts + role/scale vocabularies), edits the dict in place, live
 * previews via POST .../preview, and saves by POSTing the JSON contract into
 * the SAME two-step dry-run → confirm flow as a TOML upload (the server
 * serializes with tomlkit against the current effective text, so comments on
 * untouched keys survive). The global fetch wrapper in main.js injects CSRF.
 */
(function () {
    "use strict";

    const ENDPOINT = "/api/manage/annotation-contract";

    const st = {
        contract: null,       // working copy of the parsed contract dict
        etag: null,           // effective-contract etag at hydration (optimistic concurrency)
        help: {},
        roles: [],
        scales: [],
        dirty: false,
        previewTab: "prompt",
        lastPreview: { prompt: "", schema: "" },
        previewTimer: null,
        stagedImpact: null,
        // When set (a candidate name), Save posts to /api/manage/ab-candidates
        // instead of the active contract flow — the Contracts page's "Edit" path.
        saveTarget: null,
    };

    // ---------- small utils ----------

    function _esc(v) {
        const div = document.createElement("div");
        div.textContent = v == null ? "" : String(v);
        return div.innerHTML;
    }

    function _escAttr(v) {
        return String(v == null ? "" : v)
            .replace(/&/g, "&amp;").replace(/"/g, "&quot;")
            .replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    function _status(msg, color) {
        const el = document.getElementById("ace-status");
        if (el) {
            el.textContent = msg || "";
            el.style.color = color || "var(--color-text-muted)";
        }
    }

    function _markDirty() {
        st.dirty = true;
        const el = document.getElementById("ace-dirty");
        if (el) el.style.display = "inline";
        _schedulePreview();
    }

    // Help tooltip for a dotted contract path (from annotation_contract_help.toml).
    function _hlp(path) {
        const text = st.help[path];
        if (!text) return "";
        return ` <span class="meta-tooltip text-xxs" data-tooltip="${_escAttr(text)}"` +
            ` style="color: var(--color-text-muted); cursor: help;">ⓘ</span>`;
    }

    // Two-click confirmation for destructive buttons — native confirm() is
    // blocked in embedded preview browsers. First click arms/relabels; a
    // second click within 4s confirms.
    function _armTwoClick(btn, confirmLabel) {
        if (!btn) return true;
        if (btn.dataset.armed === "1") {
            btn.dataset.armed = "";
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

    function _setIf(obj, key, value) {
        // Contract keys are optional: write non-empty values, delete empties
        // (an empty role/scale/enum would fail validation server-side).
        if (value === "" || value == null) delete obj[key];
        else obj[key] = value;
    }

    // ---------- sub-key spec strings (mirror fyp.annotation_contract) ----------

    const INT_SPEC_RE = /^int\s*(?:\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\))?\s*:\s*([\s\S]*)$/;

    function parseSpec(specVal) {
        const s = (typeof specVal === "object" && specVal !== null) ? (specVal.spec || "") : (specVal || "");
        const meta = (typeof specVal === "object" && specVal !== null) ? specVal : {};
        let p;
        if (s.startsWith("enum:")) p = { kind: "enum", enum: s.slice(5).trim(), desc: "" };
        else if (s.startsWith("list:")) p = { kind: "list", desc: s.slice(5).trim() };
        else {
            const m = INT_SPEC_RE.exec(s);
            if (m) {
                p = { kind: "int", desc: (m[3] || "").trim() };
                if (m[1] !== undefined && m[1] !== null) { p.min = parseInt(m[1], 10); p.max = parseInt(m[2], 10); }
            } else p = { kind: "text", desc: s };
        }
        p.role = meta.role || ""; p.scale = meta.scale || "";
        p.display_name = meta.display_name || ""; p.description = meta.description || "";
        return p;
    }

    function formatSpec(p) {
        const desc = (p.desc || "").trim();
        if (p.kind === "enum") return "enum:" + (p.enum || "").trim();
        if (p.kind === "list") return desc ? "list: " + desc : "list:";
        if (p.kind === "int") {
            const b = (p.min !== undefined && p.min !== "" && p.max !== undefined && p.max !== "")
                ? `(${parseInt(p.min, 10)},${parseInt(p.max, 10)})` : "";
            return desc ? `int${b}: ${desc}` : `int${b}:`;
        }
        return desc;
    }

    function specValFromParts(p) {
        const spec = formatSpec(p);
        if (!(p.role || p.scale || p.display_name || p.description)) return spec;
        const o = { spec };
        if (p.role) o.role = p.role;
        if (p.scale) o.scale = p.scale;
        if (p.display_name) o.display_name = p.display_name;
        if (p.description) o.description = p.description;
        return o;
    }

    // ---------- form rendering ----------

    const PANEL_STYLE = "margin-bottom: 10px; border: 1px solid var(--color-border);" +
        " border-radius: 6px; background: var(--color-bg-elevated); padding: 4px 12px 10px 12px;";
    const SUMMARY_STYLE = "cursor: pointer; padding: 6px 0; color: var(--color-text-heading);";
    const CARD_STYLE = "margin: 6px 0; border: 1px solid var(--color-border); border-radius: 6px;" +
        " padding: 4px 10px 8px 10px;";
    const IN = "padding: 4px 8px; border: 1px solid var(--color-border); border-radius: 4px;" +
        " background: var(--color-bg-input); color: var(--color-text-primary); font-size: var(--text-sm);";
    const ROW = "display: flex; gap: 8px; align-items: center; margin-top: 6px; flex-wrap: wrap;";
    const LBL = "color: var(--color-text-muted); min-width: 90px; display: inline-block;";

    function _select(attrs, options, current, emptyLabel) {
        let html = `<select ${attrs} style="${IN}">`;
        if (emptyLabel !== undefined) {
            html += `<option value="" ${current ? "" : "selected"}>${_esc(emptyLabel)}</option>`;
        }
        for (const o of options) {
            html += `<option value="${_escAttr(o)}" ${o === current ? "selected" : ""}>${_esc(o)}</option>`;
        }
        return html + "</select>";
    }

    function _btn(act, extra, label) {
        return `<button data-act="${act}" ${extra} class="btn-discreet text-xs"` +
            ` style="padding: 2px 8px;">${label}</button>`;
    }

    function _panelPrompt(c) {
        const p = c.prompt || {};
        return `<details open style="${PANEL_STYLE}">
            <summary style="${SUMMARY_STYLE}" class="font-semibold">Prompt${_hlp("prompt")}</summary>
            <div style="${ROW}"><span style="${LBL}">header${_hlp("prompt.header")}</span></div>
            <textarea data-act="prompt" data-key="header" rows="4"
                style="${IN} width: 100%; resize: vertical;">${_esc(p.header || "")}</textarea>
            <div style="${ROW}"><span style="${LBL}">footer${_hlp("prompt.footer")}</span></div>
            <textarea data-act="prompt" data-key="footer" rows="2"
                style="${IN} width: 100%; resize: vertical;">${_esc(p.footer || "")}</textarea>
        </details>`;
    }

    function _panelSections(c) {
        const sections = c.section || [];
        let rows = sections.map((s, i) => `<div style="${CARD_STYLE}">
            <div style="${ROW}">
                <input data-act="section" data-i="${i}" data-key="name" value="${_escAttr(s.name || "")}"
                    placeholder="name" style="${IN} width: 110px;" class="font-mono">
                <input data-act="section" data-i="${i}" data-key="title" value="${_escAttr(s.title || "")}"
                    placeholder="title" style="${IN} flex: 1; min-width: 140px;">
                <span style="flex-basis: 100%;"></span>
                <input data-act="section" data-i="${i}" data-key="intro" value="${_escAttr(s.intro || "")}"
                    placeholder="intro sentence" style="${IN} flex: 1; min-width: 200px;">
                ${_btn("section-move", `data-i="${i}" data-dir="-1"`, "↑")}
                ${_btn("section-move", `data-i="${i}" data-dir="1"`, "↓")}
                ${_btn("section-del", `data-i="${i}"`, "✕")}
            </div>
        </div>`).join("");
        return `<details open style="${PANEL_STYLE}">
            <summary style="${SUMMARY_STYLE}" class="font-semibold">Sections (${sections.length})${_hlp("section")}</summary>
            ${rows}
            <div style="${ROW}">${_btn("section-add", "", "+ Add section")}</div>
        </details>`;
    }

    function _keyRow(fieldIdx, keyName, specVal, ki, c) {
        const p = parseSpec(specVal);
        const enumNames = Object.keys(c.enums || {});
        const kindSel = _select(`data-act="key" data-i="${fieldIdx}" data-k="${ki}" data-part="kind"`,
            ["enum", "list", "int", "text"], p.kind);
        let kindCtl = "";
        if (p.kind === "enum") {
            kindCtl = _select(`data-act="key" data-i="${fieldIdx}" data-k="${ki}" data-part="enum"`,
                enumNames, p.enum, "(pick enum)");
        } else if (p.kind === "int") {
            kindCtl = `<input data-act="key" data-i="${fieldIdx}" data-k="${ki}" data-part="min" type="number"
                    value="${p.min !== undefined ? p.min : ""}" placeholder="min" style="${IN} width: 64px;">
                <input data-act="key" data-i="${fieldIdx}" data-k="${ki}" data-part="max" type="number"
                    value="${p.max !== undefined ? p.max : ""}" placeholder="max" style="${IN} width: 64px;">`;
        }
        const descCtl = p.kind === "enum" ? "" :
            `<input data-act="key" data-i="${fieldIdx}" data-k="${ki}" data-part="desc"
                value="${_escAttr(p.desc)}" placeholder="description (feeds the schema)"
                style="${IN} flex: 1; min-width: 160px;">`;
        return `<div style="${CARD_STYLE} background: var(--color-bg-elevated);">
            <div style="${ROW}">
                <input data-act="key" data-i="${fieldIdx}" data-k="${ki}" data-part="name"
                    value="${_escAttr(keyName)}" placeholder="sub-key" style="${IN} width: 120px;" class="font-mono">
                ${kindSel} ${kindCtl} ${descCtl}
                ${_btn("key-del", `data-i="${fieldIdx}" data-k="${ki}"`, "✕")}
            </div>
            <div style="${ROW}">
                <span class="text-xxs" style="${LBL}">var_schema:</span>
                ${_select(`data-act="key" data-i="${fieldIdx}" data-k="${ki}" data-part="role"`, st.roles, p.role, "(role)")}
                ${_select(`data-act="key" data-i="${fieldIdx}" data-k="${ki}" data-part="scale"`, st.scales, p.scale, "(scale)")}
                <input data-act="key" data-i="${fieldIdx}" data-k="${ki}" data-part="display_name"
                    value="${_escAttr(p.display_name)}" placeholder="display name" style="${IN} width: 170px;">
                <input data-act="key" data-i="${fieldIdx}" data-k="${ki}" data-part="description"
                    value="${_escAttr(p.description)}" placeholder="web-UI description" style="${IN} flex: 1; min-width: 160px;">
            </div>
        </div>`;
    }

    function _fieldCard(f, i, c) {
        const sectionNames = (c.section || []).map(s => s.name);
        const enumNames = Object.keys(c.enums || {});
        const type = f.type || "string";
        const arrayMode = f.array === true ? "true" : (typeof f.array === "number" ? "cap" : "");
        const badge = `<span class="text-xxs font-mono" style="color: var(--color-text-muted);">` +
            `${type}${f.array ? "[]" : ""}${f.enum ? " · " + _esc(f.enum) : ""}</span>`;

        let typeCtls = "";
        if (type === "int") {
            typeCtls = `<span style="${LBL}">min / max${_hlp("fields.min")}</span>
                <input data-act="field-min" data-i="${i}" type="number" value="${f.min !== undefined ? f.min : ""}" style="${IN} width: 70px;">
                <input data-act="field-max" data-i="${i}" type="number" value="${f.max !== undefined ? f.max : ""}" style="${IN} width: 70px;">`;
        } else if (type === "string") {
            typeCtls = `<span style="${LBL}">enum${_hlp("fields.enum")}</span>
                ${_select(`data-act="field-enum" data-i="${i}"`, enumNames, f.enum || "", "(open text)")}`;
        }

        let keysHtml = "";
        if (type === "object") {
            const keys = f.keys || {};
            keysHtml = `<div style="${ROW}"><span style="${LBL}">keys${_hlp("fields.keys")}</span></div>` +
                Object.entries(keys).map(([k, v], ki) => _keyRow(i, k, v, ki, c)).join("") +
                `<div style="${ROW}">${_btn("key-add", `data-i="${i}"`, "+ Add sub-key")}</div>`;
        }

        return `<details style="${CARD_STYLE}" ${st.openField === i ? "open" : ""} data-field-card="${i}">
            <summary style="${SUMMARY_STYLE}">
                <span class="font-mono font-semibold">${_esc(f.name || "(unnamed)")}</span>
                &nbsp;${badge}
                <span class="text-xxs" style="color: var(--color-text-muted);">· ${_esc(f.section || "?")}</span>
            </summary>
            <div style="${ROW}">
                <span style="${LBL}">name${_hlp("fields.name")}</span>
                <input data-act="field" data-i="${i}" data-key="name" value="${_escAttr(f.name || "")}"
                    style="${IN} width: 180px;" class="font-mono">
                <span style="${LBL}">section${_hlp("fields.section")}</span>
                ${_select(`data-act="field" data-i="${i}" data-key="section"`, sectionNames, f.section || "")}
                <span style="${LBL}">type${_hlp("fields.type")}</span>
                ${_select(`data-act="field-type" data-i="${i}"`, ["string", "int", "object"], type)}
            </div>
            <div style="${ROW}">
                <span style="${LBL}">list${_hlp("fields.array")}</span>
                ${_arraySelect(f, i)}
            </div>
            <div style="${ROW}">${typeCtls}</div>
            <div style="${ROW}"><span style="${LBL}">desc (prompt)${_hlp("fields.desc")}</span></div>
            <textarea data-act="field" data-i="${i}" data-key="desc" rows="2"
                style="${IN} width: 100%; resize: vertical;">${_esc(f.desc || "")}</textarea>
            ${keysHtml}
            <div style="${ROW}">
                <span class="text-xxs" style="${LBL}">var_schema:</span>
                ${_select(`data-act="field" data-i="${i}" data-key="role"`, st.roles, f.role || "", "(role)")}${_hlp("fields.role")}
                ${_select(`data-act="field" data-i="${i}" data-key="scale"`, st.scales, f.scale || "", "(scale)")}${_hlp("fields.scale")}
                <input data-act="field" data-i="${i}" data-key="display_name" value="${_escAttr(f.display_name || "")}"
                    placeholder="display name" style="${IN} width: 170px;">${_hlp("fields.display_name")}
            </div>
            <div style="${ROW}">
                <span class="text-xxs" style="${LBL}">description</span>
                <input data-act="field" data-i="${i}" data-key="description" value="${_escAttr(f.description || "")}"
                    placeholder="web-UI description (falls back to desc)" style="${IN} flex: 1; min-width: 200px;">${_hlp("fields.description")}
            </div>
            <div style="${ROW}">
                ${_btn("field-move", `data-i="${i}" data-dir="-1"`, "↑ move up")}
                ${_btn("field-move", `data-i="${i}" data-dir="1"`, "↓ move down")}
                ${_btn("field-del", `data-i="${i}"`, "✕ delete field")}
            </div>
        </details>`;
    }

    // The list-mode select needs the current value — build it via a tiny post-pass.
    function _arraySelect(f, i) {
        const mode = f.array === true ? "true" : (typeof f.array === "number" && typeof f.array !== "boolean" ? "cap" : "");
        const capInput = mode === "cap"
            ? `<input data-act="field-arraycap" data-i="${i}" type="number" min="1" value="${f.array}" style="${IN} width: 64px;">`
            : "";
        return `<select data-act="field-array" data-i="${i}" style="${IN}">
            <option value="" ${mode === "" ? "selected" : ""}>single value</option>
            <option value="true" ${mode === "true" ? "selected" : ""}>list (uncapped)</option>
            <option value="cap" ${mode === "cap" ? "selected" : ""}>list (max N)</option>
        </select> ${capInput}`;
    }

    function _panelFields(c) {
        const fields = c.fields || [];
        const cards = fields.map((f, i) => _fieldCard(f, i, c)).join("");
        return `<details open style="${PANEL_STYLE}">
            <summary style="${SUMMARY_STYLE}" class="font-semibold">Fields (${fields.length})${_hlp("fields")}</summary>
            ${cards}
            <div style="${ROW}">${_btn("field-add", "", "+ Add field")}</div>
        </details>`;
    }

    function _panelEnums(c) {
        const enums = c.enums || {};
        const blocks = Object.entries(enums).map(([name, val], ei) => {
            const isTable = !Array.isArray(val);
            const entries = isTable ? Object.entries(val) : val.map(v => [v, ""]);
            const rows = entries.map(([v, d], vi) => `<div style="${ROW}">
                <input data-act="enum-val" data-e="${_escAttr(name)}" data-v="${vi}" data-part="value"
                    value="${_escAttr(v)}" style="${IN} width: 200px;">
                <input data-act="enum-val" data-e="${_escAttr(name)}" data-v="${vi}" data-part="desc"
                    value="${_escAttr(d)}" placeholder="(description — optional)" style="${IN} flex: 1; min-width: 200px;">
                ${_btn("enum-val-del", `data-e="${_escAttr(name)}" data-v="${vi}"`, "✕")}
            </div>`).join("");
            return `<details style="${CARD_STYLE}">
                <summary style="${SUMMARY_STYLE}">
                    <span class="font-mono font-semibold">${_esc(name)}</span>
                    <span class="text-xxs" style="color: var(--color-text-muted);">(${entries.length} values${isTable ? ", described" : ""})</span>
                </summary>
                <div style="${ROW}">
                    <span style="${LBL}">name</span>
                    <input data-act="enum-name" data-e="${_escAttr(name)}" value="${_escAttr(name)}"
                        style="${IN} width: 180px;" class="font-mono">
                    ${_btn("enum-del", `data-e="${_escAttr(name)}"`, "✕ delete enum")}
                </div>
                ${rows}
                <div style="${ROW}">${_btn("enum-val-add", `data-e="${_escAttr(name)}"`, "+ Add value")}</div>
            </details>`;
        }).join("");
        return `<details style="${PANEL_STYLE}">
            <summary style="${SUMMARY_STYLE}" class="font-semibold">Enums (${Object.keys(enums).length})${_hlp("enums")}</summary>
            ${blocks}
            <div style="${ROW}">${_btn("enum-add", "", "+ Add enum")}</div>
        </details>`;
    }

    function _panelDrop(c) {
        const drop = (c.recode || {}).drop || {};
        const rows = Object.entries(drop).map(([col, words]) => `<div style="${ROW}">
            <input data-act="drop-col" data-d="${_escAttr(col)}" value="${_escAttr(col)}"
                placeholder="output column" style="${IN} width: 180px;" class="font-mono">
            <input data-act="drop-words" data-d="${_escAttr(col)}" value="${_escAttr(words.join(", "))}"
                placeholder="comma-separated stop words" style="${IN} flex: 1; min-width: 220px;">
            ${_btn("drop-del", `data-d="${_escAttr(col)}"`, "✕")}
        </div>`).join("");
        return `<details style="${PANEL_STYLE}">
            <summary style="${SUMMARY_STYLE}" class="font-semibold">Recode drop-words${_hlp("recode.drop")}</summary>
            ${rows}
            <div style="${ROW}">${_btn("drop-add", "", "+ Add column")}</div>
        </details>`;
    }

    function _renderForm() {
        const el = document.getElementById("ace-form");
        if (!el || !st.contract) return;
        el.innerHTML = _panelPrompt(st.contract) + _panelSections(st.contract)
            + _panelFields(st.contract) + _panelEnums(st.contract) + _panelDrop(st.contract);
    }

    // ---------- model mutation (delegated events) ----------

    function _renameObjectKey(obj, oldKey, newKey) {
        // Rebuild preserving insertion order so document order is stable.
        const out = {};
        for (const [k, v] of Object.entries(obj)) out[k === oldKey ? newKey : k] = v;
        return out;
    }

    function _keyEntries(f) { return Object.entries(f.keys || {}); }

    function _onInput(ev) {
        const t = ev.target;
        const act = t.dataset && t.dataset.act;
        if (!act) return;
        const c = st.contract;
        const i = t.dataset.i !== undefined ? parseInt(t.dataset.i, 10) : null;

        if (act === "prompt") {
            c.prompt = c.prompt || {};
            _setIf(c.prompt, t.dataset.key, t.value);
        } else if (act === "section") {
            _setIf(c.section[i], t.dataset.key, t.value);
        } else if (act === "field") {
            _setIf(c.fields[i], t.dataset.key, t.value);
        } else if (act === "field-min" || act === "field-max") {
            const key = act === "field-min" ? "min" : "max";
            if (t.value === "") delete c.fields[i][key];
            else c.fields[i][key] = parseInt(t.value, 10);
        } else if (act === "field-arraycap") {
            const n = parseInt(t.value, 10);
            if (n >= 1) c.fields[i].array = n;
        } else if (act === "key") {
            const entries = _keyEntries(c.fields[i]);
            const ki = parseInt(t.dataset.k, 10);
            const [keyName, specVal] = entries[ki];
            const part = t.dataset.part;
            if (part === "name") {
                c.fields[i].keys = _renameObjectKey(c.fields[i].keys, keyName, t.value);
                return _markDirty();
            }
            const p = parseSpec(specVal);
            if (part === "min" || part === "max") p[part] = t.value === "" ? undefined : parseInt(t.value, 10);
            else p[part] = t.value;
            c.fields[i].keys[Object.keys(c.fields[i].keys)[ki]] = specValFromParts(p);
        } else if (act === "enum-name") {
            const oldName = t.dataset.e;
            if (t.value && t.value !== oldName) {
                c.enums = _renameObjectKey(c.enums, oldName, t.value);
                // Update every reference so the contract stays valid.
                for (const f of c.fields || []) {
                    if (f.enum === oldName) f.enum = t.value;
                    for (const [k, v] of Object.entries(f.keys || {})) {
                        const p = parseSpec(v);
                        if (p.kind === "enum" && p.enum === oldName) {
                            p.enum = t.value;
                            f.keys[k] = specValFromParts(p);
                        }
                    }
                }
                // Keep data-e attributes in sync without a re-render (focus survives).
                document.querySelectorAll(`#ace-form [data-e="${CSS.escape(oldName)}"]`)
                    .forEach(el => { el.dataset.e = t.value; });
            }
        } else if (act === "enum-val") {
            const name = t.dataset.e, vi = parseInt(t.dataset.v, 10);
            let val = c.enums[name];
            const isTable = !Array.isArray(val);
            const entries = isTable ? Object.entries(val) : val.map(v => [v, ""]);
            if (t.dataset.part === "value") entries[vi][0] = t.value;
            else entries[vi][1] = t.value;
            const anyDesc = entries.some(([, d]) => (d || "").trim() !== "");
            c.enums[name] = anyDesc ? Object.fromEntries(entries) : entries.map(([v]) => v);
        } else if (act === "drop-col") {
            const oldCol = t.dataset.d;
            if (t.value && t.value !== oldCol) {
                c.recode.drop = _renameObjectKey(c.recode.drop, oldCol, t.value);
                document.querySelectorAll(`#ace-form [data-d="${CSS.escape(oldCol)}"]`)
                    .forEach(el => { el.dataset.d = t.value; });
            }
        } else if (act === "drop-words") {
            c.recode = c.recode || {}; c.recode.drop = c.recode.drop || {};
            c.recode.drop[t.dataset.d] = t.value.split(",").map(w => w.trim()).filter(Boolean);
        } else {
            return;
        }
        _markDirty();
    }

    function _onChange(ev) {
        // Structural <select> changes re-render the form (focus loss is fine).
        const t = ev.target;
        const act = t.dataset && t.dataset.act;
        if (!act) return;
        const c = st.contract;
        const i = t.dataset.i !== undefined ? parseInt(t.dataset.i, 10) : null;

        if (act === "field-type") {
            const f = c.fields[i];
            f.type = t.value;
            if (t.value !== "object") delete f.keys;
            else f.keys = f.keys || { new_key: "description of the sub-key" };
            if (t.value !== "int") { delete f.min; delete f.max; }
            if (t.value !== "string") delete f.enum;
            if (t.value === "string") delete f.type;   // string is the default — keep files minimal
            st.openField = i;
        } else if (act === "field-enum") {
            _setIf(c.fields[i], "enum", t.value);
            st.openField = i;
        } else if (act === "field-array") {
            const f = c.fields[i];
            if (t.value === "") delete f.array;
            else if (t.value === "true") f.array = true;
            else f.array = typeof f.array === "number" && f.array > 0 ? f.array : 2;
            st.openField = i;
        } else if (act === "key" && t.dataset.part === "kind") {
            const entries = _keyEntries(c.fields[i]);
            const ki = parseInt(t.dataset.k, 10);
            const p = parseSpec(entries[ki][1]);
            p.kind = t.value;
            if (t.value !== "enum") p.enum = "";
            if (t.value !== "int") { delete p.min; delete p.max; }
            c.fields[i].keys[entries[ki][0]] = specValFromParts(p);
            st.openField = i;
        } else {
            return;   // non-structural selects are handled by _onInput
        }
        _markDirty();
        _renderForm();
    }

    function _onClick(ev) {
        const t = ev.target.closest("[data-act]");
        if (!t || t.tagName !== "BUTTON") return;
        ev.preventDefault();
        const act = t.dataset.act;
        const c = st.contract;
        const i = t.dataset.i !== undefined ? parseInt(t.dataset.i, 10) : null;

        if (act === "section-add") {
            c.section = c.section || [];
            c.section.push({ name: `section_${c.section.length + 1}`, title: "New section", intro: "" });
        } else if (act === "section-del") {
            c.section.splice(i, 1);
        } else if (act === "section-move") {
            const j = i + parseInt(t.dataset.dir, 10);
            if (j < 0 || j >= c.section.length) return;
            [c.section[i], c.section[j]] = [c.section[j], c.section[i]];
        } else if (act === "field-add") {
            c.fields = c.fields || [];
            const firstSection = (c.section && c.section[0] && c.section[0].name) || "";
            c.fields.push({ name: `new_field_${c.fields.length + 1}`, section: firstSection, desc: "" });
            st.openField = c.fields.length - 1;
        } else if (act === "field-del") {
            if (!_armTwoClick(t, "✕ delete — sure?")) return;
            c.fields.splice(i, 1);
            st.openField = null;
        } else if (act === "field-move") {
            const j = i + parseInt(t.dataset.dir, 10);
            if (j < 0 || j >= c.fields.length) return;
            [c.fields[i], c.fields[j]] = [c.fields[j], c.fields[i]];
            st.openField = j;
        } else if (act === "key-add") {
            c.fields[i].keys = c.fields[i].keys || {};
            c.fields[i].keys[`key_${Object.keys(c.fields[i].keys).length + 1}`] = "description of the sub-key";
            st.openField = i;
        } else if (act === "key-del") {
            const entries = _keyEntries(c.fields[i]);
            delete c.fields[i].keys[entries[parseInt(t.dataset.k, 10)][0]];
            st.openField = i;
        } else if (act === "enum-add") {
            c.enums = c.enums || {};
            c.enums[`new_enum_${Object.keys(c.enums).length + 1}`] = ["Value A", "Value B"];
        } else if (act === "enum-del") {
            // Fields referencing the enum will fail validation until updated.
            if (!_armTwoClick(t, "✕ delete — sure?")) return;
            delete c.enums[t.dataset.e];
        } else if (act === "enum-val-add") {
            const name = t.dataset.e, val = c.enums[name];
            if (Array.isArray(val)) val.push("");
            else val[""] = "";
        } else if (act === "enum-val-del") {
            const name = t.dataset.e, vi = parseInt(t.dataset.v, 10), val = c.enums[name];
            if (Array.isArray(val)) val.splice(vi, 1);
            else delete val[Object.keys(val)[vi]];
        } else if (act === "drop-add") {
            c.recode = c.recode || {}; c.recode.drop = c.recode.drop || {};
            c.recode.drop["column_name"] = [];
        } else if (act === "drop-del") {
            delete c.recode.drop[t.dataset.d];
        } else {
            return;
        }
        _markDirty();
        _renderForm();
    }

    // ---------- live preview ----------

    function _schedulePreview() {
        clearTimeout(st.previewTimer);
        st.previewTimer = setTimeout(_refreshPreview, 600);
    }

    async function _refreshPreview() {
        if (!st.contract) return;
        const statusEl = document.getElementById("ace-preview-status");
        const errEl = document.getElementById("ace-preview-errors");
        if (statusEl) statusEl.textContent = "rendering…";
        try {
            const res = await fetch(`${ENDPOINT}/preview`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ contract: st.contract }),
            });
            const body = await res.json();
            if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
            if (!body.valid) {
                if (errEl) {
                    errEl.style.display = "block";
                    errEl.innerHTML = `<strong>${body.errors.length} validation error${body.errors.length === 1 ? "" : "s"}</strong>`
                        + '<ul style="margin: 4px 0 0 18px; padding: 0;">'
                        + body.errors.slice(0, 30).map(e => `<li>${_esc(e)}</li>`).join("") + "</ul>";
                }
                if (statusEl) statusEl.textContent = "invalid — preview is stale";
                return;
            }
            if (errEl) errEl.style.display = "none";
            st.lastPreview.prompt = body.prompt || "";
            st.lastPreview.schema = JSON.stringify(body.schema, null, 2);
            _renderPreview();
            if (statusEl) statusEl.textContent = "";
        } catch (e) {
            if (statusEl) statusEl.textContent = `preview failed: ${e.message}`;
        }
    }

    function _renderPreview() {
        const pre = document.getElementById("ace-preview");
        if (pre) pre.textContent = st.lastPreview[st.previewTab] || "";
        const pb = document.getElementById("ace-tab-prompt");
        const sb = document.getElementById("ace-tab-schema");
        if (pb) pb.className = st.previewTab === "prompt" ? "btn-primary text-xs" : "btn-discreet text-xs";
        if (sb) sb.className = st.previewTab === "schema" ? "btn-primary text-xs" : "btn-discreet text-xs";
    }

    // ---------- open / close / save ----------

    async function aceOpen(opts) {
        const modal = document.getElementById("ace-modal");
        if (!modal) return;
        st.saveTarget = (opts && opts.candidate) || null;
        modal.style.display = "flex";
        _status("Loading contract…");
        try {
            const res = await fetch(`${ENDPOINT}/parsed`);
            const body = await res.json();
            if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
            st.contract = body.contract;
            st.etag = body.etag;
            st.help = body.help || {};
            st.roles = body.roles || [];
            st.scales = body.scales || [];
            if (st.saveTarget) {
                // Candidate mode: the /parsed call above supplied help/vocabs;
                // the contract itself comes from the candidate store.
                const cres = await fetch(`/api/manage/ab-candidates/${encodeURIComponent(st.saveTarget)}`);
                const cbody = await cres.json();
                if (!cres.ok) throw new Error(cbody.error || `HTTP ${cres.status}`);
                st.contract = cbody.contract;
            }
            st.dirty = false;
            st.openField = null;
            const dirtyEl = document.getElementById("ace-dirty");
            if (dirtyEl) dirtyEl.style.display = "none";
            const ov = document.getElementById("ace-help-overview");
            if (ov && st.help.overview) ov.setAttribute("data-tooltip", st.help.overview);
            const note = document.getElementById("ace-footer-note");
            const saveBtn = document.getElementById("ace-save-btn");
            if (st.saveTarget) {
                if (note) note.textContent = `Editing candidate '${st.saveTarget}' — saving updates the `
                    + "candidate only (activate it from the Contracts page).";
                if (saveBtn) saveBtn.textContent = "Save candidate";
            } else {
                if (note) note.textContent = `Editing the ${body.source} contract (etag ${String(st.etag).slice(0, 18)}…). ` +
                    "Saving runs the same dry-run → confirm flow as a TOML upload.";
                if (saveBtn) saveBtn.textContent = "Review & activate…";
            }
            _status("");
            _renderForm();
            _refreshPreview();
        } catch (e) {
            _status(`Failed to load: ${e.message}`, "var(--color-danger)");
        }
    }

    let closeArmed = false;

    function aceClose() {
        if (st.dirty && !closeArmed) {
            closeArmed = true;
            _status("Unsaved changes — click Close/Cancel again to discard them.", "var(--color-warning)");
            setTimeout(() => { closeArmed = false; }, 4000);
            return;
        }
        closeArmed = false;
        const modal = document.getElementById("ace-modal");
        if (modal) modal.style.display = "none";
        st.contract = null;
        st.dirty = false;
    }

    function aceShowPreview(tab) {
        st.previewTab = tab;
        _renderPreview();
    }

    async function aceSave() {
        if (!st.contract) return;
        if (st.saveTarget) return _aceSaveCandidate();
        _status("Validating…");
        try {
            const res = await fetch(ENDPOINT, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ contract: st.contract }),
            });
            const body = await res.json();
            if (res.status === 400 && body.errors) {
                _status(`${body.errors.length} validation error(s) — see preview panel.`, "var(--color-danger)");
                const errEl = document.getElementById("ace-preview-errors");
                if (errEl) {
                    errEl.style.display = "block";
                    errEl.innerHTML = "<strong>Save rejected:</strong><ul style='margin: 4px 0 0 18px; padding: 0;'>"
                        + body.errors.slice(0, 30).map(e => `<li>${_esc(e)}</li>`).join("") + "</ul>";
                }
                return;
            }
            if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
            st.stagedImpact = body.impact;
            _status("");
            _showImpact(body.impact);
        } catch (e) {
            _status(`Error: ${e.message}`, "var(--color-danger)");
        }
    }

    async function _aceSaveCandidate() {
        _status("Saving candidate…");
        try {
            const res = await fetch("/api/manage/ab-candidates", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: st.saveTarget, contract: st.contract, overwrite: true }),
            });
            const body = await res.json();
            if (res.status === 400 && body.errors) {
                _status(`${body.errors.length} validation error(s) — see preview panel.`, "var(--color-danger)");
                const errEl = document.getElementById("ace-preview-errors");
                if (errEl) {
                    errEl.style.display = "block";
                    errEl.innerHTML = "<strong>Save rejected:</strong><ul style='margin: 4px 0 0 18px; padding: 0;'>"
                        + body.errors.slice(0, 30).map(e => `<li>${_esc(e)}</li>`).join("") + "</ul>";
                }
                return;
            }
            if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
            st.dirty = false;
            const modal = document.getElementById("ace-modal");
            if (modal) modal.style.display = "none";
            document.dispatchEvent(new CustomEvent("fyp:candidates-changed"));
        } catch (e) {
            _status(`Error: ${e.message}`, "var(--color-danger)");
        }
    }

    function _showImpact(impact) {
        const body = document.getElementById("ace-impact-body");
        if (!body) return;
        const rows = [];
        if (impact.metadata_only) {
            rows.push(`<div style="color: var(--color-success); margin-bottom: 10px;">`
                + `✓ Metadata-only change — <strong>no new annotation version</strong>. `
                + `Existing annotations stay valid.</div>`);
        } else {
            rows.push(`<div style="color: var(--color-warning); margin-bottom: 10px;">`
                + `⚠ This changes the ${impact.prompt_changed && impact.schema_changed ? "prompt and response schema"
                    : impact.prompt_changed ? "prompt" : "response schema"}. `
                + `A new annotation version <span class="font-mono">${_esc(impact.candidate_version)}</span> `
                + `will be registered and become the <strong>active</strong> version `
                + `(replacing <span class="font-mono">${_esc(impact.active_version)}</span>). `
                + `Studies keep using the preferred version until you promote it.</div>`);
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
            + '<ul style="margin: 6px 0 0 18px; padding: 0;" class="text-sm">'
            + detail.map(d => `<li>${d}</li>`).join("") + "</ul>";
        const m = document.getElementById("ace-impact-modal");
        if (m) m.style.display = "flex";
    }

    function aceCloseImpact() {
        const m = document.getElementById("ace-impact-modal");
        if (m) m.style.display = "none";
    }

    async function aceConfirmSave() {
        const btn = document.getElementById("ace-impact-confirm-btn");
        if (btn) { btn.disabled = true; btn.textContent = "Activating…"; }
        try {
            const res = await fetch(ENDPOINT, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ contract: st.contract, confirm: true, expected_etag: st.etag }),
            });
            const body = await res.json();
            if (res.status === 409) {
                _status(`Rejected: ${body.message || "the contract changed"}. Reload the editor.`, "var(--color-danger)");
                aceCloseImpact();
                return;
            }
            if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
            st.dirty = false;
            st.etag = body.etag;
            aceCloseImpact();
            const modal = document.getElementById("ace-modal");
            if (modal) modal.style.display = "none";
            document.dispatchEvent(new CustomEvent("fyp:contract-changed"));
        } catch (e) {
            _status(`Error: ${e.message}`, "var(--color-danger)");
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = "Activate contract"; }
        }
    }

    // ---------- wiring ----------

    function _bindDelegates() {
        const form = document.getElementById("ace-form");
        if (!form) return;
        form.addEventListener("input", _onInput);
        form.addEventListener("change", _onChange);
        form.addEventListener("click", _onClick);
    }

    window.aceOpen = aceOpen;
    window.aceClose = aceClose;
    window.aceSave = aceSave;
    window.aceShowPreview = aceShowPreview;
    window.aceCloseImpact = aceCloseImpact;
    window.aceConfirmSave = aceConfirmSave;

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", _bindDelegates);
    } else {
        _bindDelegates();
    }
})();
