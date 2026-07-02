// Per-user variable preferences ("Customize variables").
//
// Each user can include/exclude variables per surface (filter / display /
// timeline / viz) on top of the admin-set global defaults. Preferences are
// stored as deltas in user.settings.variable_prefs via /api/user/settings:
//   { surface: { include: [names], exclude: [names] } }
// Composition everywhere: effective = (global ∪ include) − exclude, ordered
// by the canonical derived order (all_variables_order). Unknown names are
// ignored so stored prefs survive schema evolution; an absent key means the
// user sees the global defaults.
window.VariablePrefs = (function () {
    'use strict';

    function _prefs(surface) {
        const all = (window.userSettings || {}).variable_prefs || {};
        return all[surface] || {};
    }

    function isCustomized(surface) {
        const p = _prefs(surface);
        return !!((p.include || []).length || (p.exclude || []).length);
    }

    // effective(surface, allOrder, globalList) -> ordered effective list.
    // Items in globalList that aren't schema variables (e.g. dynamic user-tag
    // columns prepended by the overlay, or the synthetic machine_state) are
    // preserved ahead of the canonical ordering and are never excludable.
    function effective(surface, allOrder, globalList) {
        const p = _prefs(surface);
        const order = allOrder || [];
        const global = globalList || [];
        const allSet = new Set(order);
        const base = new Set(global);
        (p.include || []).forEach(v => { if (allSet.has(v)) base.add(v); });
        (p.exclude || []).forEach(v => { if (allSet.has(v)) base.delete(v); });
        const extras = global.filter(v => !allSet.has(v));
        return extras.concat(order.filter(v => base.has(v)));
    }

    // Broadcast a preference change so tabs that aren't currently rendered
    // still refresh the affected surface next time (and immediately if they're
    // already mounted). The 'filter' surface is shared by the Explore and Video
    // Analysis tabs, so a change in one must reach the other. detail.surface is
    // null when every surface was reset at once.
    function _broadcast(surface) {
        try {
            window.dispatchEvent(new CustomEvent('fyp:variable-prefs-changed',
                { detail: { surface: surface || null } }));
        } catch (e) { /* CustomEvent unsupported — callers still re-render via onApply */ }
    }

    async function save(surface, delta) {
        const all = Object.assign({}, (window.userSettings || {}).variable_prefs || {});
        const empty = !delta || (!(delta.include || []).length && !(delta.exclude || []).length);
        if (empty) delete all[surface];
        else all[surface] = { include: delta.include || [], exclude: delta.exclude || [] };
        await saveUserSettings({ variable_prefs: all });
        _broadcast(surface);
        return true;
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

    // openPanel({surface, title, allOrder, globalList, schemaMap, sectionOrder,
    //            coveredSet, onApply})
    // Modal checkbox list grouped by section. coveredSet (optional): variables
    // with aggregated data — an unchecked->checked var outside it gets a
    // "first load may take a while" note (timelines only). onApply() fires
    // after a successful save so the caller can re-render.
    function openPanel(opts) {
        closePanel();
        const surface = opts.surface;
        const order = (opts.allOrder || []).filter(v => v !== 'machine_state');
        const globalSet = new Set(opts.globalList || []);
        const effSet = new Set(effective(surface, opts.allOrder || [], opts.globalList || []));
        const schemaMap = opts.schemaMap || {};
        const covered = opts.coveredSet ? new Set(opts.coveredSet) : null;

        // Group in canonical order by section.
        const sections = new Map();
        order.forEach(v => {
            const sec = (schemaMap[v] && schemaMap[v].section) || 'General';
            if (!sections.has(sec)) sections.set(sec, []);
            sections.get(sec).push(v);
        });

        let body = '';
        sections.forEach((vars, sec) => {
            const rows = vars.map(v => {
                const meta = schemaMap[v] || {};
                const label = meta.display_name || v;
                const checked = effSet.has(v) ? 'checked' : '';
                const isDefault = globalSet.has(v) === effSet.has(v);
                const dot = isDefault ? '' :
                    '<span class="text-xxs" style="color: var(--color-accent); margin-left: 4px;">●</span>';
                const slow = (covered && !covered.has(v) && !globalSet.has(v)) ?
                    '<span class="text-xxs" style="color: var(--color-text-muted); margin-left: 6px;">first load may take a while</span>' : '';
                const desc = meta.description ?
                    ` data-tooltip="${_esc(meta.description)}"` : '';
                return `<label class="text-sm${meta.description ? ' meta-tooltip' : ''}"${desc}
                    style="display: flex; align-items: center; gap: 8px; padding: 3px 2px; cursor: pointer;">
                    <input type="checkbox" data-vp-var="${_esc(v)}" ${checked}>
                    <span>${_esc(label)}</span>${dot}${slow}
                </label>`;
            }).join('');
            body += `<div style="margin-bottom: 10px;">
                <div class="text-xs font-semibold" style="color: var(--color-text-muted);
                    text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 4px;">${_esc(sec)}</div>
                ${rows}
            </div>`;
        });

        const overlay = document.createElement('div');
        overlay.id = 'variable-prefs-overlay';
        overlay.style.cssText = 'position: fixed; inset: 0; z-index: 2000;' +
            'background: var(--color-overlay, rgba(0,0,0,0.5)); display: flex;' +
            'align-items: center; justify-content: center;';
        overlay.innerHTML = `
            <div style="background: var(--color-bg-elevated); border: 1px solid var(--color-border);
                border-radius: 8px; width: min(440px, 92vw); max-height: 80vh; display: flex;
                flex-direction: column; box-shadow: var(--shadow-lg, 0 8px 30px rgba(0,0,0,0.4));">
                <div style="display: flex; align-items: center; justify-content: space-between;
                    padding: 12px 16px; border-bottom: 1px solid var(--color-border);">
                    <div>
                        <div class="text-body font-semibold">${_esc(opts.title || 'Customize variables')}</div>
                        <div class="text-xs" style="color: var(--color-text-muted);">
                            Your personal selection — the dot marks changes from the defaults.</div>
                    </div>
                    <button id="vp-close" class="btn-discreet" style="padding: 2px 8px;">✕</button>
                </div>
                <div id="vp-body" style="overflow-y: auto; padding: 12px 16px;">${body}</div>
                <div style="display: flex; align-items: center; justify-content: space-between;
                    padding: 10px 16px; border-top: 1px solid var(--color-border);">
                    <button id="vp-reset" class="btn-discreet text-sm">Reset to defaults</button>
                    <div style="display: flex; gap: 8px;">
                        <button id="vp-cancel" class="btn-discreet text-sm">Cancel</button>
                        <button id="vp-save" class="btn-primary text-sm">Save</button>
                    </div>
                </div>
            </div>`;
        document.body.appendChild(overlay);

        overlay.addEventListener('click', ev => { if (ev.target === overlay) closePanel(); });
        overlay.querySelector('#vp-close').onclick = closePanel;
        overlay.querySelector('#vp-cancel').onclick = closePanel;
        overlay.querySelector('#vp-reset').onclick = async () => {
            await save(surface, null);
            closePanel();
            if (opts.onApply) opts.onApply();
        };
        overlay.querySelector('#vp-save').onclick = async () => {
            const checked = new Set();
            overlay.querySelectorAll('input[data-vp-var]').forEach(cb => {
                if (cb.checked) checked.add(cb.getAttribute('data-vp-var'));
            });
            const include = order.filter(v => checked.has(v) && !globalSet.has(v));
            const exclude = order.filter(v => !checked.has(v) && globalSet.has(v));
            await save(surface, { include, exclude });
            closePanel();
            if (opts.onApply) opts.onApply();
        };
    }

    function closePanel() {
        const el = document.getElementById('variable-prefs-overlay');
        if (el) el.remove();
    }

    // A clear, readable gear icon (Material "settings" glyph). Uses currentColor
    // so it inherits the button's themed text colour. Sized to match the control
    // bar icons (18px) — the old ⚙ emoji rendered far too small to read.
    const _GEAR_SVG =
        '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" ' +
        'aria-hidden="true" focusable="false" style="display: block;">' +
        '<path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61' +
        'l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41' +
        'h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87' +
        'c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61' +
        'l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84' +
        'c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32' +
        'c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6' +
        '-1.62 3.6-3.6 3.6z"/></svg>';

    // Gear button factory for surface headers. Marked with a dot when the user
    // has an active customization on that surface.
    function gearButton(surface, onClick) {
        const btn = document.createElement('button');
        btn.className = 'btn-discreet';
        btn.style.cssText = 'display: inline-flex; align-items: center; gap: 4px; ' +
            'padding: 3px 7px; margin-left: 6px;';
        btn.setAttribute('data-vp-gear', surface);
        btn.title = 'Customize variables';
        const dot = isCustomized(surface)
            ? '<span class="text-xs" style="color: var(--color-accent);">●</span>' : '';
        btn.innerHTML = _GEAR_SVG + dot;
        btn.onclick = onClick;
        return btn;
    }

    return { effective, isCustomized, save, resetAll, openPanel, closePanel, gearButton };
})();
