// Per-user variable preferences ("Customize variables") and the dual-list
// customizer that edits them.
//
// Each user can include/exclude variables per surface (filter / display /
// timeline / viz) on top of the admin-set global defaults, and rearrange them
// within their sections, in My Stuff -> Preferences -> Variable customizations.
// Preferences are stored as deltas in user.settings.variable_prefs via
// /api/user/settings:
//   { surface: { include: [names], exclude: [names], order: [names]? } }
// Composition everywhere: effective = (global ∪ include) − exclude, in the
// surface's default order (default_order[surface]: the computed order re-sorted
// within sections by the admins' arrangement), then re-sorted within sections
// by the user's own order. Unknown names are ignored so stored prefs survive
// schema evolution; an absent key means the user sees the global defaults.
// Tabs react to changes via the 'fyp:variable-prefs-changed' event this module
// broadcasts on save.
//
// The same customizer, in admin mode, edits the default layout itself
// (membership + order in var_presentation.json) from Admin -> Variable
// Visibility.
window.VariablePrefs = (function () {
    'use strict';

    const SURFACES = ['filter', 'viz', 'display', 'timeline'];
    const SURFACE_LABELS = {
        filter: 'Filters',
        viz: 'Visualized',
        display: 'Detail panel',
        timeline: 'Timelines',
    };
    const SURFACE_HINTS = {
        filter: 'Filter panels in Explore and Video Analysis.',
        viz: "Explore's distribution charts and the variables offered in Correlations.",
        display: 'Fields in the Video Analysis detail panel.',
        timeline: 'Series offered in Timelines. A variable that is not aggregated yet takes longer to load the first time.',
    };

    function _prefs(surface) {
        const all = (window.userSettings || {}).variable_prefs || {};
        return all[surface] || {};
    }

    function isCustomized(surface) {
        const p = _prefs(surface);
        return !!((p.include || []).length || (p.exclude || []).length || (p.order || []).length);
    }

    function _sectionOf(schemaMap) {
        if (!schemaMap) return () => null;
        return v => (schemaMap[v] && schemaMap[v].section) || 'General';
    }

    // Re-sort `names` by `order` without letting variables leave their section:
    // within each section, the positions held by variables listed in `order`
    // are refilled with those same variables in `order`'s sequence. Unlisted
    // variables keep their slot. Mirrors apply_section_order() in
    // web_interface/services/user_variables.py.
    function applySectionOrder(names, order, sectionOf) {
        const out = names.slice();
        if (!order || !order.length) return out;
        const rank = new Map();
        order.forEach((v, i) => { if (!rank.has(v)) rank.set(v, i); });
        const sec = sectionOf || (() => null);
        const slots = new Map();
        names.forEach((v, i) => {
            if (!rank.has(v)) return;
            const s = sec(v);
            if (!slots.has(s)) slots.set(s, []);
            slots.get(s).push(i);
        });
        slots.forEach(idxs => {
            const ranked = idxs.map(i => names[i]).sort((a, b) => rank.get(a) - rank.get(b));
            idxs.forEach((i, k) => { out[i] = ranked[k]; });
        });
        return out;
    }

    // effective(surface, allOrder, globalList, schemaMap) -> ordered effective
    // list. allOrder is the surface's default order (the full candidate list).
    // Items in globalList that aren't schema variables (e.g. dynamic user-tag
    // columns prepended by the overlay, or the synthetic machine_state) are
    // preserved ahead of the ordering and are never excludable.
    function effective(surface, allOrder, globalList, schemaMap) {
        const p = _prefs(surface);
        const order = allOrder || [];
        const global = globalList || [];
        const allSet = new Set(order);
        const base = new Set(global);
        (p.include || []).forEach(v => { if (allSet.has(v)) base.add(v); });
        (p.exclude || []).forEach(v => { if (allSet.has(v)) base.delete(v); });
        const extras = global.filter(v => !allSet.has(v));
        const ordered = applySectionOrder(order.filter(v => base.has(v)), p.order, _sectionOf(schemaMap));
        return extras.concat(ordered);
    }

    // effectiveFor(surface, metadata): the composed list for a surface, read
    // from any metadata payload carrying all_variables_order, default_order,
    // <surface>_priority and schema_map (Explore/Video Analysis metadata, the
    // Correlations meta, the My Stuff catalog).
    function effectiveFor(surface, md) {
        const all = md.all_variables_order || [];
        const def = (md.default_order && md.default_order[surface]) || all;
        return effective(surface, def, md[surface + '_priority'] || [], md.schema_map);
    }

    // Broadcast a preference change so tabs that aren't currently rendered
    // still refresh the affected surface next time (and immediately if they're
    // already mounted). The 'filter' surface is shared by the Explore and Video
    // Analysis tabs, so a change in one must reach the other. detail.surface is
    // null when several surfaces changed at once.
    function _broadcast(surface) {
        try {
            window.dispatchEvent(new CustomEvent('fyp:variable-prefs-changed',
                { detail: { surface: surface || null } }));
        } catch (e) { /* CustomEvent unsupported — callers still re-render via onApply */ }
    }

    function _cleanDelta(delta) {
        if (!delta) return null;
        const out = {};
        ['include', 'exclude', 'order'].forEach(k => {
            if ((delta[k] || []).length) out[k] = delta[k];
        });
        return Object.keys(out).length ? out : null;
    }

    // saveMany({surface: delta|null}) — one settings write; a null or empty
    // delta resets that surface to the defaults.
    async function saveMany(deltas) {
        const all = Object.assign({}, (window.userSettings || {}).variable_prefs || {});
        const changed = Object.keys(deltas);
        changed.forEach(s => {
            const d = _cleanDelta(deltas[s]);
            if (d) all[s] = d; else delete all[s];
        });
        await saveUserSettings({ variable_prefs: all });
        _broadcast(changed.length === 1 ? changed[0] : null);
        return true;
    }

    function save(surface, delta) {
        return saveMany({ [surface]: delta });
    }

    async function resetAll() {
        await saveUserSettings({ variable_prefs: {} });
        _broadcast(null);
        return true;
    }

    function _esc(s) {
        return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function _sameList(a, b) {
        return a.length === b.length && a.every((v, i) => v === b[i]);
    }

    // ------------------------------------------------------------------
    // The customizer
    // ------------------------------------------------------------------

    let _ui = null;   // state of the open customizer (null when closed)

    // openCustomizer({mode, catalog, surface, onApply, adminSave})
    //   mode: 'user' (default) edits the viewer's own prefs; 'admin' edits the
    //     default layout and calls adminSave({surfaces, order}) to persist it.
    //   catalog: {all_variables_order, default_order, <surface>_priority,
    //     schema_map, section_order} — /api/user/variable-catalog's shape.
    //   surface: the tab to open on.
    //   onApply(): fires after a successful save.
    function openCustomizer(opts) {
        closePanel();
        const catalog = opts.catalog || {};
        const mode = opts.mode === 'admin' ? 'admin' : 'user';
        const allOrder = catalog.all_variables_order || [];
        const schemaMap = catalog.schema_map || {};
        const sectionOf = _sectionOf(schemaMap);
        const allSet = new Set(allOrder);

        // Section order as the lists render it: first appearance in the
        // computed order (which is already section-sorted).
        const sectionRank = new Map();
        allOrder.forEach(v => {
            const s = sectionOf(v);
            if (!sectionRank.has(s)) sectionRank.set(s, sectionRank.size);
        });

        const perSurface = {};
        SURFACES.forEach(s => {
            const defOrder = (catalog.default_order && catalog.default_order[s]) || allOrder;
            const globals = (catalog[s + '_priority'] || []).filter(v => allSet.has(v));
            // In admin mode the "default" a tab resets to is the stored
            // membership in computed order; in user mode it is the admins'
            // default layout.
            const baseOrder = mode === 'admin' ? allOrder : defOrder;
            const globalSet = new Set(globals);
            const initial = mode === 'admin'
                ? defOrder.filter(v => globalSet.has(v))
                : effective(s, defOrder, globals, schemaMap).filter(v => allSet.has(v));
            perSurface[s] = { baseOrder, globals, globalSet, initial };
        });

        _ui = {
            mode, allOrder, schemaMap, sectionOf, sectionRank, perSurface,
            surface: SURFACES.includes(opts.surface) ? opts.surface : 'filter',
            drafts: Object.fromEntries(SURFACES.map(s => [s, perSurface[s].initial.slice()])),
            sel: null,              // {pane: 'avail'|'shown', name}
            section: '',            // left-pane section filter ('' = all)
            query: '',
            collapsed: new Set(),   // collapsed right-pane sections
            drag: null,             // {name, from}
            onApply: opts.onApply,
            adminSave: opts.adminSave,
            saving: false,
        };
        _renderShell(opts.title);
        _renderAll();
    }

    // Keep a draft grouped by section (stable within each section).
    function _normalize(list) {
        const r = v => _ui.sectionRank.has(_ui.sectionOf(v)) ? _ui.sectionRank.get(_ui.sectionOf(v)) : 1e9;
        return list.map((v, i) => [v, i]).sort((a, b) => (r(a[0]) - r(b[0])) || (a[1] - b[1])).map(x => x[0]);
    }

    function _isDirty(s) {
        return !_sameList(_ui.drafts[s], _ui.perSurface[s].initial);
    }

    function _anyDirty() {
        return SURFACES.some(_isDirty);
    }

    function _renderShell(title) {
        const admin = _ui.mode === 'admin';
        const overlay = document.createElement('div');
        overlay.id = 'variable-prefs-overlay';
        overlay.className = 'vc-overlay';
        overlay.innerHTML = `
            <div class="vc-dialog" role="dialog" aria-modal="true" aria-labelledby="vc-title">
                <div class="vc-header">
                    <div>
                        <div id="vc-title" class="text-body font-semibold">${_esc(title ||
                            (admin ? 'Default variable layout' : 'Customize variables'))}</div>
                        <div class="text-xs vc-muted">${admin
                            ? 'Applies to every user who has not customized a surface themselves.'
                            : 'Your personal selection, across all studies. A dot marks a change from the defaults.'}</div>
                    </div>
                    <button type="button" class="btn-discreet vc-close" data-vc="close" aria-label="Close">✕</button>
                </div>
                <div class="vc-tabs" role="tablist">
                    ${SURFACES.map(s => `<button type="button" role="tab" class="vc-tab" data-vc-tab="${s}">
                        ${_esc(SURFACE_LABELS[s])}<span class="vc-tab-dot" aria-hidden="true">●</span></button>`).join('')}
                </div>
                <div class="vc-hint text-xs vc-muted" id="vc-hint"></div>
                <div class="vc-panes">
                    <div class="vc-pane">
                        <label class="vc-pane-label text-sm font-semibold" for="vc-section">Choose variables from:</label>
                        <select id="vc-section" class="vc-select text-sm"></select>
                        <input id="vc-search" class="vc-search text-sm" type="search"
                            placeholder="Search variables" autocomplete="off" aria-label="Search variables">
                        <div id="vc-avail" class="vc-list" tabindex="0" role="listbox" aria-label="Available variables"></div>
                    </div>
                    <div class="vc-move">
                        <button type="button" class="btn-discreet vc-move-btn" data-vc="add"
                            title="Add (double-click or Enter)" aria-label="Add"><span class="vc-arrow">›</span></button>
                        <button type="button" class="btn-discreet vc-move-btn" data-vc="remove"
                            title="Remove (double-click or Delete)" aria-label="Remove"><span class="vc-arrow">‹</span></button>
                    </div>
                    <div class="vc-pane">
                        <div class="vc-pane-label text-sm font-semibold" id="vc-shown-label"></div>
                        <div id="vc-shown" class="vc-list vc-list-tall" tabindex="0" role="listbox"></div>
                        <div class="vc-toolbar">
                            <button type="button" class="btn-discreet text-sm" data-vc="up"
                                title="Move up (Alt+↑)" aria-label="Move up">↑</button>
                            <button type="button" class="btn-discreet text-sm" data-vc="down"
                                title="Move down (Alt+↓)" aria-label="Move down">↓</button>
                            <span class="vc-toolbar-sep"></span>
                            <button type="button" class="btn-discreet text-sm" data-vc="sort-section"
                                title="Put the selected variable's section back in its default order">Reset section order</button>
                        </div>
                    </div>
                </div>
                <div id="vc-desc" class="vc-desc text-xs"></div>
                <div class="vc-footer">
                    <div class="vc-footer-left">
                        <button type="button" class="btn-discreet text-sm" data-vc="reset-tab">Reset this tab</button>
                        <button type="button" class="btn-discreet text-sm" data-vc="reset-all">Reset all tabs</button>
                    </div>
                    <div class="vc-footer-right">
                        <button type="button" class="btn-discreet text-sm" data-vc="cancel">Cancel</button>
                        <button type="button" class="btn-primary text-sm" data-vc="save">Save</button>
                    </div>
                </div>
                <div class="vc-caption text-xs vc-muted">Drag to re-order variables within a section. Double-click to add or remove.</div>
            </div>`;
        document.body.appendChild(overlay);
        _wire(overlay);
    }

    function _renderAll() {
        _renderTabs();
        _renderSectionSelect();
        _renderAvail();
        _renderShown();
        _renderControls();
    }

    function _renderTabs() {
        const ov = document.getElementById('variable-prefs-overlay');
        ov.querySelectorAll('[data-vc-tab]').forEach(btn => {
            const s = btn.getAttribute('data-vc-tab');
            btn.classList.toggle('active', s === _ui.surface);
            btn.setAttribute('aria-selected', s === _ui.surface ? 'true' : 'false');
            btn.classList.toggle('dirty', _isDirty(s));
        });
        ov.querySelector('#vc-hint').textContent = SURFACE_HINTS[_ui.surface];
        ov.querySelector('#vc-shown-label').textContent = `Shown in ${SURFACE_LABELS[_ui.surface]}:`;
        ov.querySelector('#vc-shown').setAttribute('aria-label', `Shown in ${SURFACE_LABELS[_ui.surface]}`);
    }

    function _renderSectionSelect() {
        const sel = document.getElementById('vc-section');
        const opts = ['<option value="">All sections</option>'];
        _ui.sectionRank.forEach((_, s) => {
            opts.push(`<option value="${_esc(s)}"${s === _ui.section ? ' selected' : ''}>${_esc(s)}</option>`);
        });
        sel.innerHTML = opts.join('');
    }

    function _label(v) {
        const m = _ui.schemaMap[v] || {};
        return m.display_name || v;
    }

    function _rowHtml(v, pane) {
        const selected = _ui.sel && _ui.sel.pane === pane && _ui.sel.name === v;
        const ps = _ui.perSurface[_ui.surface];
        const shown = pane === 'shown';
        // User mode: mark membership that differs from the admins' default.
        const changed = _ui.mode === 'user' && (ps.globalSet.has(v) !== shown);
        const dot = changed ? '<span class="vc-changed" title="Changed from the default">●</span>' : '';
        const desc = (_ui.schemaMap[v] || {}).description;
        return `<div class="vc-row${selected ? ' selected' : ''}" role="option" draggable="true"
            aria-selected="${selected ? 'true' : 'false'}" data-vc-var="${_esc(v)}" data-vc-pane="${pane}"
            ${desc ? `title="${_esc(desc)}"` : ''}>
            ${shown ? '<span class="vc-grip" aria-hidden="true">⋮⋮</span>' : ''}
            <span class="vc-row-label">${_esc(_label(v))}</span>${dot}</div>`;
    }

    function _availList() {
        const shown = new Set(_ui.drafts[_ui.surface]);
        const q = _ui.query.trim().toLowerCase();
        return _ui.perSurface[_ui.surface].baseOrder.filter(v => {
            if (shown.has(v)) return false;
            if (_ui.section && _ui.sectionOf(v) !== _ui.section) return false;
            if (!q) return true;
            const m = _ui.schemaMap[v] || {};
            return _label(v).toLowerCase().includes(q) || v.toLowerCase().includes(q) ||
                (m.description || '').toLowerCase().includes(q);
        });
    }

    function _renderAvail() {
        const box = document.getElementById('vc-avail');
        const list = _availList();
        if (!list.length) {
            box.innerHTML = `<div class="vc-empty text-sm">${_ui.query
                ? 'No matching variables.'
                : 'Every variable here is already shown.'}</div>`;
            return;
        }
        let html = '';
        let cur = null;
        list.forEach(v => {
            const s = _ui.sectionOf(v);
            if (s !== cur) {
                cur = s;
                html += `<div class="vc-section-head">${_esc(s)}</div>`;
            }
            html += _rowHtml(v, 'avail');
        });
        box.innerHTML = html;
    }

    function _renderShown() {
        const box = document.getElementById('vc-shown');
        const draft = _ui.drafts[_ui.surface];
        if (!draft.length) {
            box.innerHTML = '<div class="vc-empty text-sm">Nothing shown. Add variables from the left.</div>';
            return;
        }
        const groups = new Map();
        draft.forEach(v => {
            const s = _ui.sectionOf(v);
            if (!groups.has(s)) groups.set(s, []);
            groups.get(s).push(v);
        });
        let html = '';
        groups.forEach((vars, s) => {
            const collapsed = _ui.collapsed.has(s);
            html += `<div class="vc-group" data-vc-section="${_esc(s)}">
                <button type="button" class="vc-group-head" data-vc-toggle="${_esc(s)}" aria-expanded="${collapsed ? 'false' : 'true'}">
                    <span class="vc-chevron${collapsed ? '' : ' open'}" aria-hidden="true">›</span>
                    <span>${_esc(s)}</span><span class="vc-count">${vars.length}</span>
                </button>
                ${collapsed ? '' : `<div class="vc-group-body">${vars.map(v => _rowHtml(v, 'shown')).join('')}</div>`}
            </div>`;
        });
        box.innerHTML = html;
    }

    function _renderControls() {
        const ov = document.getElementById('variable-prefs-overlay');
        const sel = _ui.sel;
        const inAvail = sel && sel.pane === 'avail';
        const inShown = sel && sel.pane === 'shown';
        const btn = k => ov.querySelector(`[data-vc="${k}"]`);
        btn('add').disabled = !inAvail;
        btn('remove').disabled = !inShown;
        const siblings = inShown ? _sectionSiblings(sel.name) : [];
        const idx = siblings.indexOf(inShown ? sel.name : null);
        btn('up').disabled = !inShown || idx <= 0;
        btn('down').disabled = !inShown || idx === -1 || idx >= siblings.length - 1;
        btn('sort-section').disabled = !inShown;
        btn('save').disabled = !_anyDirty() || _ui.saving;
        btn('reset-tab').disabled = _sameList(_ui.drafts[_ui.surface], _defaultDraft(_ui.surface));
        btn('reset-all').disabled = SURFACES.every(s => _sameList(_ui.drafts[s], _defaultDraft(s)));

        const desc = ov.querySelector('#vc-desc');
        if (sel) {
            const m = _ui.schemaMap[sel.name] || {};
            const ps = _ui.perSurface[_ui.surface];
            const defNote = _ui.mode === 'user'
                ? ` <span class="vc-muted">· ${ps.globalSet.has(sel.name) ? 'Shown' : 'Hidden'} by default</span>` : '';
            desc.innerHTML = `<span class="font-semibold">${_esc(_label(sel.name))}</span>` +
                ` <span class="vc-muted">(${_esc(_ui.sectionOf(sel.name))})</span>${defNote}` +
                (m.description ? `<div>${_esc(m.description)}</div>` : '');
        } else {
            desc.innerHTML = '<span class="vc-muted">Select a variable to see its description.</span>';
        }
    }

    // The default draft "Reset" returns to: the admins' layout (user mode) or
    // the stored membership in computed order (admin mode).
    function _defaultDraft(s) {
        const ps = _ui.perSurface[s];
        return ps.baseOrder.filter(v => ps.globalSet.has(v));
    }

    function _sectionSiblings(v) {
        const s = _ui.sectionOf(v);
        return _ui.drafts[_ui.surface].filter(x => _ui.sectionOf(x) === s);
    }

    function _changed(scrollTo) {
        _renderTabs();
        _renderAvail();
        _renderShown();
        _renderControls();
        if (scrollTo) {
            const el = document.querySelector(`#variable-prefs-overlay [data-vc-pane="${scrollTo.pane}"][data-vc-var="${CSS.escape(scrollTo.name)}"]`);
            if (el) el.scrollIntoView({ block: 'nearest' });
        }
    }

    function _add(v, beforeName) {
        const draft = _ui.drafts[_ui.surface];
        if (draft.includes(v)) return;
        const avail = _availList();
        const next = avail[avail.indexOf(v) + 1] || null;
        let list = draft.slice();
        if (beforeName && _ui.sectionOf(beforeName) === _ui.sectionOf(v)) {
            list.splice(list.indexOf(beforeName), 0, v);
        } else {
            list.push(v);   // _normalize moves it to the end of its section
        }
        _ui.drafts[_ui.surface] = _normalize(list);
        _ui.collapsed.delete(_ui.sectionOf(v));
        // Keep the keyboard flow going on the left: select the next candidate.
        _ui.sel = next ? { pane: 'avail', name: next } : { pane: 'shown', name: v };
        _changed({ pane: 'shown', name: v });
    }

    function _remove(v) {
        const draft = _ui.drafts[_ui.surface];
        const i = draft.indexOf(v);
        if (i === -1) return;
        const list = draft.slice();
        list.splice(i, 1);
        _ui.drafts[_ui.surface] = list;
        const next = list[i] || list[i - 1];
        _ui.sel = next ? { pane: 'shown', name: next } : null;
        _changed(next ? { pane: 'shown', name: next } : null);
    }

    function _move(v, delta) {
        const sib = _sectionSiblings(v);
        const j = sib.indexOf(v) + delta;
        if (j < 0 || j >= sib.length) return;
        _moveBefore(v, delta < 0 ? sib[j] : (sib[j + 1] || null));
    }

    // Move v directly before `target` within its section (or to the section's
    // end when target is null).
    function _moveBefore(v, target) {
        const list = _ui.drafts[_ui.surface].filter(x => x !== v);
        if (target && target !== v) {
            list.splice(list.indexOf(target), 0, v);
        } else {
            const s = _ui.sectionOf(v);
            let last = -1;
            list.forEach((x, i) => { if (_ui.sectionOf(x) === s) last = i; });
            list.splice(last + 1, 0, v);
        }
        _ui.drafts[_ui.surface] = _normalize(list);
        _ui.sel = { pane: 'shown', name: v };
        _changed({ pane: 'shown', name: v });
    }

    function _resetSectionOrder(v) {
        const s = _ui.sectionOf(v);
        const draft = _ui.drafts[_ui.surface];
        const members = new Set(draft.filter(x => _ui.sectionOf(x) === s));
        const sorted = _ui.perSurface[_ui.surface].baseOrder.filter(x => members.has(x));
        let k = 0;
        _ui.drafts[_ui.surface] = draft.map(x => (_ui.sectionOf(x) === s ? sorted[k++] : x));
        _changed({ pane: 'shown', name: v });
    }

    function _visibleRows(pane) {
        const box = document.getElementById(pane === 'avail' ? 'vc-avail' : 'vc-shown');
        return [...box.querySelectorAll('.vc-row')].map(r => r.getAttribute('data-vc-var'));
    }

    function _select(pane, name) {
        _ui.sel = name ? { pane, name } : null;
        document.querySelectorAll('#variable-prefs-overlay .vc-row').forEach(r => {
            const on = !!name && r.getAttribute('data-vc-pane') === pane && r.getAttribute('data-vc-var') === name;
            r.classList.toggle('selected', on);
            r.setAttribute('aria-selected', on ? 'true' : 'false');
            if (on) r.scrollIntoView({ block: 'nearest' });
        });
        _renderControls();
    }

    function _onListKey(ev, pane) {
        const rows = _visibleRows(pane);
        const cur = _ui.sel && _ui.sel.pane === pane ? rows.indexOf(_ui.sel.name) : -1;
        if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
            ev.preventDefault();
            if (pane === 'shown' && ev.altKey && cur !== -1) {
                _move(_ui.sel.name, ev.key === 'ArrowUp' ? -1 : 1);
                return;
            }
            const next = ev.key === 'ArrowDown' ? Math.min(rows.length - 1, cur + 1) : Math.max(0, cur - 1);
            if (rows[next]) _select(pane, rows[next]);
        } else if (ev.key === 'Enter' && pane === 'avail' && cur !== -1) {
            ev.preventDefault();
            _add(_ui.sel.name);
        } else if ((ev.key === 'Delete' || ev.key === 'Backspace') && pane === 'shown' && cur !== -1) {
            ev.preventDefault();
            _remove(_ui.sel.name);
        }
    }

    function _clearDropMarks() {
        document.querySelectorAll('#variable-prefs-overlay .vc-drop-before, #variable-prefs-overlay .vc-drop-after, #variable-prefs-overlay .vc-drop-target')
            .forEach(el => el.classList.remove('vc-drop-before', 'vc-drop-after', 'vc-drop-target'));
    }

    // Where a drag over the shown list would land: {target, before} for a
    // same-section row, null when the drop is not allowed there.
    function _dropSpot(ev) {
        const drag = _ui.drag;
        if (!drag) return null;
        const row = ev.target.closest('.vc-row[data-vc-pane="shown"]');
        const sec = _ui.sectionOf(drag.name);
        if (row) {
            const target = row.getAttribute('data-vc-var');
            if (_ui.sectionOf(target) !== sec) return null;
            const r = row.getBoundingClientRect();
            return { row, target, before: ev.clientY < r.top + r.height / 2 };
        }
        // Dropping an available variable on empty list space adds it at the
        // end of its section; reordering needs a row.
        return drag.from === 'avail' ? { row: null, target: null, before: false } : null;
    }

    function _wire(ov) {
        ov.addEventListener('click', ev => {
            if (ev.target === ov) { _requestClose(); return; }
            const tab = ev.target.closest('[data-vc-tab]');
            if (tab) {
                _ui.surface = tab.getAttribute('data-vc-tab');
                _ui.sel = null;
                _renderTabs();
                _renderAvail();
                _renderShown();
                _renderControls();
                return;
            }
            const toggle = ev.target.closest('[data-vc-toggle]');
            if (toggle) {
                const s = toggle.getAttribute('data-vc-toggle');
                if (_ui.collapsed.has(s)) _ui.collapsed.delete(s); else _ui.collapsed.add(s);
                _renderShown();
                return;
            }
            const row = ev.target.closest('.vc-row');
            if (row) {
                _select(row.getAttribute('data-vc-pane'), row.getAttribute('data-vc-var'));
                return;
            }
            const act = ev.target.closest('[data-vc]');
            if (!act || act.disabled) return;
            const sel = _ui.sel;
            switch (act.getAttribute('data-vc')) {
                case 'close':
                case 'cancel': _requestClose(); break;
                case 'add': if (sel && sel.pane === 'avail') _add(sel.name); break;
                case 'remove': if (sel && sel.pane === 'shown') _remove(sel.name); break;
                case 'up': if (sel) _move(sel.name, -1); break;
                case 'down': if (sel) _move(sel.name, 1); break;
                case 'sort-section': if (sel) _resetSectionOrder(sel.name); break;
                case 'reset-tab':
                    _ui.drafts[_ui.surface] = _defaultDraft(_ui.surface);
                    _ui.sel = null;
                    _changed();
                    break;
                case 'reset-all':
                    SURFACES.forEach(s => { _ui.drafts[s] = _defaultDraft(s); });
                    _ui.sel = null;
                    _changed();
                    break;
                case 'save': _save(); break;
            }
        });
        ov.addEventListener('dblclick', ev => {
            const row = ev.target.closest('.vc-row');
            if (!row) return;
            const v = row.getAttribute('data-vc-var');
            if (row.getAttribute('data-vc-pane') === 'avail') _add(v); else _remove(v);
        });
        ov.querySelector('#vc-section').addEventListener('change', ev => {
            _ui.section = ev.target.value;
            _renderAvail();
            _renderControls();
        });
        ov.querySelector('#vc-search').addEventListener('input', ev => {
            _ui.query = ev.target.value;
            _renderAvail();
        });
        ov.querySelector('#vc-avail').addEventListener('keydown', ev => _onListKey(ev, 'avail'));
        ov.querySelector('#vc-shown').addEventListener('keydown', ev => _onListKey(ev, 'shown'));

        // Drag and drop: reorder within a section on the right; drag an
        // available variable onto the right to add it, or a shown one onto the
        // left to remove it.
        ov.addEventListener('dragstart', ev => {
            const row = ev.target.closest('.vc-row');
            if (!row) return;
            _ui.drag = { name: row.getAttribute('data-vc-var'), from: row.getAttribute('data-vc-pane') };
            row.classList.add('dragging');
            ev.dataTransfer.effectAllowed = 'move';
            try { ev.dataTransfer.setData('text/plain', _ui.drag.name); } catch (e) { /* IE */ }
        });
        ov.addEventListener('dragend', () => {
            _ui.drag = null;
            _clearDropMarks();
            ov.querySelectorAll('.vc-row.dragging').forEach(r => r.classList.remove('dragging'));
        });
        const shown = ov.querySelector('#vc-shown');
        shown.addEventListener('dragover', ev => {
            const spot = _dropSpot(ev);
            _clearDropMarks();
            if (!spot) { ev.dataTransfer.dropEffect = 'none'; return; }
            ev.preventDefault();
            ev.dataTransfer.dropEffect = 'move';
            if (spot.row) spot.row.classList.add(spot.before ? 'vc-drop-before' : 'vc-drop-after');
            else shown.classList.add('vc-drop-target');
        });
        shown.addEventListener('dragleave', ev => {
            if (!shown.contains(ev.relatedTarget)) _clearDropMarks();
        });
        shown.addEventListener('drop', ev => {
            const spot = _dropSpot(ev);
            const drag = _ui.drag;
            _clearDropMarks();
            if (!spot || !drag) return;
            ev.preventDefault();
            let before = spot.target;
            if (spot.target && !spot.before) {
                const sib = _ui.drafts[_ui.surface].filter(x => _ui.sectionOf(x) === _ui.sectionOf(spot.target));
                before = sib[sib.indexOf(spot.target) + 1] || null;
                if (before === drag.name) before = sib[sib.indexOf(before) + 1] || null;
            }
            if (drag.from === 'avail') _add(drag.name, before);
            else if (before !== drag.name) _moveBefore(drag.name, before);
        });
        const avail = ov.querySelector('#vc-avail');
        avail.addEventListener('dragover', ev => {
            if (!_ui.drag || _ui.drag.from !== 'shown') return;
            ev.preventDefault();
            avail.classList.add('vc-drop-target');
        });
        avail.addEventListener('dragleave', ev => {
            if (!avail.contains(ev.relatedTarget)) avail.classList.remove('vc-drop-target');
        });
        avail.addEventListener('drop', ev => {
            avail.classList.remove('vc-drop-target');
            if (!_ui.drag || _ui.drag.from !== 'shown') return;
            ev.preventDefault();
            _remove(_ui.drag.name);
        });

        _ui.keyHandler = ev => {
            if (ev.key !== 'Escape' || ev.defaultPrevented) return;
            if (document.getElementById('app-dialog-overlay')?.classList.contains('visible')) return;
            ev.preventDefault();
            _requestClose();
        };
        document.addEventListener('keydown', _ui.keyHandler);
        ov.querySelector('#vc-search').focus();
    }

    async function _requestClose() {
        if (_ui && _anyDirty() && !_ui.saving) {
            const ok = await showAppConfirm('Discard your unsaved variable changes?',
                { okLabel: 'Discard', cancelLabel: 'Keep editing' });
            if (!ok) return;
        }
        closePanel();
    }

    // User mode: per dirty surface, include/exclude against the global list,
    // plus the order only when it differs from the default arrangement.
    function _userDeltas() {
        const deltas = {};
        SURFACES.filter(_isDirty).forEach(s => {
            const ps = _ui.perSurface[s];
            const draft = _ui.drafts[s];
            const set = new Set(draft);
            const include = draft.filter(v => !ps.globalSet.has(v));
            const exclude = ps.globals.filter(v => !set.has(v));
            const defaultArranged = ps.baseOrder.filter(v => set.has(v));
            const order = _sameList(draft, defaultArranged) ? [] : draft.slice();
            deltas[s] = { include, exclude, order };
        });
        return deltas;
    }

    // Admin mode: membership lists and orders for the dirty surfaces. An order
    // equal to the computed arrangement is sent as [] (back to computed).
    function _adminPayload() {
        const surfaces = {};
        const order = {};
        SURFACES.filter(_isDirty).forEach(s => {
            const draft = _ui.drafts[s];
            const set = new Set(draft);
            surfaces[s] = draft.slice();
            const computed = _ui.allOrder.filter(v => set.has(v));
            order[s] = _sameList(draft, computed) ? [] : draft.slice();
        });
        return { surfaces, order };
    }

    async function _save() {
        if (!_ui || _ui.saving || !_anyDirty()) return;
        _ui.saving = true;
        _renderControls();
        const onApply = _ui.onApply;
        try {
            if (_ui.mode === 'admin') await _ui.adminSave(_adminPayload());
            else await saveMany(_userDeltas());
        } catch (e) {
            console.error('Saving variable layout failed', e);
            if (_ui) { _ui.saving = false; _renderControls(); }
            showAppAlert((e && e.message) || 'Saving failed. Please try again.');
            return;
        }
        closePanel();
        if (onApply) onApply();
    }

    function closePanel() {
        const el = document.getElementById('variable-prefs-overlay');
        if (el) el.remove();
        if (_ui && _ui.keyHandler) document.removeEventListener('keydown', _ui.keyHandler);
        _ui = null;
    }

    return {
        SURFACES, SURFACE_LABELS,
        applySectionOrder, effective, effectiveFor, isCustomized,
        save, saveMany, resetAll, openCustomizer, closePanel,
    };
})();
