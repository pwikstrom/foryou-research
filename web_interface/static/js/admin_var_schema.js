/* Admin → Variable Visibility viewer.
 *
 * Loads /api/manage/schema and renders a read-only metadata table. The only
 * editable cells are the four presentation-surface checkboxes; toggling one
 * saves immediately (debounced) to /api/manage/presentation with optimistic
 * concurrency via the etag returned on GET.
 *
 * No build step; this file is served as-is.  Styling uses CSS custom
 * properties from style.css (see CLAUDE.md "Frontend Styling Rules").
 */

(function () {
    'use strict';

    const SCHEMA_ENDPOINT = '/api/manage/schema';
    const PRESENTATION_ENDPOINT = '/api/manage/presentation';

    // The four web-surface membership columns — the only editable payload
    // (metadata is contract-owned). Rendered as checkboxes; saved as
    // per-surface variable lists to the presentation store.
    const PRIO_COLUMNS = {
        web_filter_prio: 'filter',
        web_timeline_prio: 'timeline',
        web_viz_prio: 'viz',
        web_display_prio: 'display',
    };

    // Display-only header labels + hover help for the surface columns (the
    // underlying column names are unchanged in the data model).
    const SURFACE_HEADERS = {
        web_filter_prio: {
            label: 'Filters',
            tip: 'Offered as a filter (Explore and Video Analysis) by default.',
        },
        web_timeline_prio: {
            label: 'Timelines',
            tip: 'Aggregated and offered on the Timelines tab by default.',
        },
        web_viz_prio: {
            label: 'Explore',
            tip: 'Offered in Explore visualizations by default.',
        },
        web_display_prio: {
            label: 'Video Analysis',
            tip: 'Shown in the Video Analysis detail panel by default.',
        },
    };

    const SURFACE_GROUP_HEADING = 'Default show/hide of variables in the UI';

    // Module state — bound once when the schema tab is first opened.
    const state = {
        rows: [],            // server rows
        columns: [],         // ordered list from server
        etag: null,
        hiddenColumns: new Set(),   // user-hidden via the Columns dropdown
        sort: { col: null, dir: 1 },  // dir: 1 = asc, -1 = desc
        loaded: false,
        wired: false,        // document-level listeners attach once
        // {variable_name: {metadata: bool, section: bool, legacy?: bool}} —
        // which cells the contracts own (rendered read-only).
        contractLocked: {},
        contractPath: 'config/annotation_contract.toml',
    };

    // localStorage key for the user's hidden-column choices.
    const HIDDEN_COLS_KEY = 'vsHiddenColumns';

    // Bookkeeping columns hidden by default to keep the table readable;
    // individually toggleable via the Columns dropdown (choice persists).
    const DEFAULT_HIDDEN = [
        'role', 'scale', 'description', 'skip_recode', 'accepted_labels',
    ];

    // ---------- helpers ----------

    function _visibleColumns() {
        // variable_name is the row key and can never be hidden.
        return state.columns.filter(
            c => c === 'variable_name' || !state.hiddenColumns.has(c));
    }




    function _loadHiddenColumns() {
        let stored = null;
        try {
            stored = JSON.parse(localStorage.getItem(HIDDEN_COLS_KEY) || 'null');
        } catch (e) { /* fall through to the default */ }
        const chosen = Array.isArray(stored) ? stored : DEFAULT_HIDDEN;
        state.hiddenColumns = new Set(
            chosen.filter(c => state.columns.includes(c) && c !== 'variable_name'));
    }




    function _saveHiddenColumns() {
        try {
            localStorage.setItem(HIDDEN_COLS_KEY,
                JSON.stringify(Array.from(state.hiddenColumns)));
        } catch (e) { /* storage unavailable — selection just won't persist */ }
    }




    function _sortedRowIndices() {
        const indices = state.rows.map((_, i) => i);
        const col = state.sort.col;
        if (!col) return indices;
        const dir = state.sort.dir;
        indices.sort((a, b) => {
            const va = String(state.rows[a][col] ?? '').trim();
            const vb = String(state.rows[b][col] ?? '').trim();
            // Empty values always sort last, regardless of direction.
            if (va === '' && vb === '') return 0;
            if (va === '') return 1;
            if (vb === '') return -1;
            const na = Number(va);
            const nb = Number(vb);
            if (!Number.isNaN(na) && !Number.isNaN(nb)) {
                return (na - nb) * dir;
            }
            return va.localeCompare(vb, undefined, { sensitivity: 'base' }) * dir;
        });
        return indices;
    }

    // ---------- rendering ----------

    function _renderTable() {
        const thead = document.getElementById('vs-thead');
        const tbody = document.getElementById('vs-tbody');
        if (!thead || !tbody) return;

        const cols = _visibleColumns();

        // Group header row: one heading spanning the (always contiguous)
        // surface-checkbox columns; blank cells elsewhere.
        const prioCount = cols.filter(c => c in PRIO_COLUMNS).length;
        let groupRow = '';
        if (prioCount > 0) {
            const firstPrio = cols.findIndex(c => c in PRIO_COLUMNS);
            const after = cols.length - firstPrio - prioCount;
            groupRow = '<tr>'
                + (firstPrio > 0 ? `<th colspan="${firstPrio}"></th>` : '')
                + `<th colspan="${prioCount}" class="text-xs" style="text-align: center; padding: 6px 10px;
                       border-bottom: 1px solid var(--color-border); color: var(--color-text-muted);
                       font-weight: var(--weight-medium); white-space: nowrap;">${_esc(SURFACE_GROUP_HEADING)}</th>`
                + (after > 0 ? `<th colspan="${after}"></th>` : '')
                + '</tr>';
        }

        thead.innerHTML = groupRow + '<tr>' + cols.map(col => {
            const surface = SURFACE_HEADERS[col];
            const label = surface ? surface.label : col;
            const tipAttrs = surface
                ? ` class="meta-tooltip tooltip-below" data-tooltip="${_esc(surface.tip)}"`
                : '';
            const isSorted = state.sort.col === col;
            const arrow = isSorted
                ? `<span style="margin-left: 4px;">${state.sort.dir === 1 ? '▲' : '▼'}</span>`
                : '';
            const sortColor = isSorted ? 'var(--color-text-primary)' : 'var(--color-text-muted)';
            return `<th onclick="vsSort('${_esc(col)}')"${tipAttrs}
                style="text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--color-border); white-space: nowrap; color: ${sortColor}; font-weight: var(--weight-semibold); cursor: pointer; user-select: none;">${_esc(label)}${arrow}</th>`;
        }).join('') + '</tr>';

        const fragments = [];
        _sortedRowIndices().forEach(rowIdx => {
            const cells = cols.map(col => _renderCell(rowIdx, col)).join('');
            fragments.push(`<tr data-row-index="${rowIdx}" style="border-bottom: 1px solid var(--color-border);">${cells}</tr>`);
        });
        tbody.innerHTML = fragments.join('');

        _setStatus(`${state.rows.length} variables.`);
    }

    function _renderCell(rowIdx, col) {
        const row = state.rows[rowIdx];
        const current = row && col in row ? row[col] : '';
        const baseStyle = 'padding: 4px 8px; vertical-align: top;';

        // Presentation membership flags: ON/OFF checkboxes (the numeric value
        // is historical — any non-blank means ON). Toggling saves immediately.
        if (col in PRIO_COLUMNS) {
            const checked = String(current).trim() !== '' && String(current) !== '<NA>';
            return `<td style="${baseStyle} text-align: center;">
                <input type="checkbox" ${checked ? 'checked' : ''}
                    onchange="vsTogglePrio(${rowIdx}, '${_esc(col)}', this.checked)">
            </td>`;
        }

        if (col === 'variable_name') {
            const lock = state.contractLocked[String(current)];
            const badge = (lock && lock.legacy)
                ? ` <span class="meta-tooltip tooltip-below" data-tooltip="Legacy field — owned by a past contract version and kept for older rows."`
                    + ` style="color: var(--color-text-muted); font-size: var(--text-xxs); border: 1px solid var(--color-border); border-radius: 3px; padding: 0 3px; white-space: nowrap;">legacy</span>`
                : '';
            return `<td class="font-mono text-xs" style="${baseStyle} color: var(--color-text-primary); white-space: nowrap;">${_esc(current)}${badge}</td>`;
        }

        // Everything else is contract-owned metadata: display-only, greyed.
        // (Read-only-ness is explained once in the page intro — no per-cell
        // tooltips.)
        const shown = String(current).trim()
            ? _esc(current)
            : '<span style="opacity: 0.5;">—</span>';
        return `<td class="font-mono text-xs"
            style="${baseStyle} color: var(--color-text-muted);">${shown}</td>`;
    }

    function _renderColumnsMenu() {
        const menu = document.getElementById('vs-columns-menu');
        if (!menu) return;
        const items = state.columns.map(col => {
            const locked = col === 'variable_name';
            const checked = !state.hiddenColumns.has(col);
            const surface = SURFACE_HEADERS[col];
            const label = surface ? surface.label : col;
            return `<label class="text-xs" style="display: flex; align-items: center; gap: 6px;
                    padding: 3px 4px; cursor: ${locked ? 'default' : 'pointer'};
                    color: ${locked ? 'var(--color-text-muted)' : 'var(--color-text-primary)'};">
                <input type="checkbox" ${checked ? 'checked' : ''} ${locked ? 'disabled' : ''}
                    onchange="vsToggleColumn('${_esc(col)}', this.checked)">
                <span class="${surface ? '' : 'font-mono'}">${_esc(label)}</span>
            </label>`;
        });
        const actions = `<div style="display: flex; gap: 8px; padding: 4px 4px 6px 4px;
                margin-bottom: 4px; border-bottom: 1px solid var(--color-border);">
            <button onclick="vsShowAllColumns()" class="btn-discreet text-xs"
                style="padding: 2px 8px;">Show all</button>
            <button onclick="vsResetColumns()" class="btn-discreet text-xs"
                style="padding: 2px 8px;">Reset</button>
        </div>`;
        menu.innerHTML = actions + items.join('');
        const btn = document.getElementById('vs-columns-btn');
        if (btn) {
            const hidden = state.hiddenColumns.size;
            btn.textContent = hidden > 0 ? `Columns (${hidden} hidden) ▾` : 'Columns ▾';
        }
    }




    function _toggleColumnsMenu(forceClose) {
        const menu = document.getElementById('vs-columns-menu');
        if (!menu) return;
        const open = menu.style.display !== 'none';
        menu.style.display = (open || forceClose) ? 'none' : 'block';
    }




    function _toggleColumn(col, visible) {
        if (col === 'variable_name') return;
        if (visible) {
            state.hiddenColumns.delete(col);
        } else {
            state.hiddenColumns.add(col);
        }
        _saveHiddenColumns();
        _renderColumnsMenu();
        _renderTable();
    }




    function _showAllColumns() {
        state.hiddenColumns.clear();
        _saveHiddenColumns();
        _renderColumnsMenu();
        _renderTable();
    }




    function _resetColumns() {
        try { localStorage.removeItem(HIDDEN_COLS_KEY); } catch (e) { /* noop */ }
        _loadHiddenColumns();
        _renderColumnsMenu();
        _renderTable();
    }




    function _onSort(col) {
        if (state.sort.col === col) {
            // Cycle asc → desc → off.
            if (state.sort.dir === 1) {
                state.sort.dir = -1;
            } else {
                state.sort.col = null;
                state.sort.dir = 1;
            }
        } else {
            state.sort.col = col;
            state.sort.dir = 1;
        }
        _renderTable();
    }




    function _setStatus(text, tone) {
        const status = document.getElementById('vs-status');
        if (!status) return;
        status.textContent = text;
        status.style.color = tone === 'error' ? 'var(--color-danger)'
            : tone === 'ok' ? 'var(--color-success)'
            : 'var(--color-text-muted)';
    }

    // ---------- network ----------

    async function _load(forceDiskReload) {
        _setStatus(forceDiskReload ? 'Re-reading from disk…' : 'Loading…');
        try {
            const url = forceDiskReload ? `${SCHEMA_ENDPOINT}?force_reload=1` : SCHEMA_ENDPOINT;
            const res = await fetch(url);
            if (!res.ok) {
                throw new Error(`Server returned ${res.status}`);
            }
            const body = await res.json();
            state.rows = body.rows || [];
            state.columns = body.columns || [];
            state.contractLocked = body.contract_locked || {};
            state.contractPath = body.contract_path || state.contractPath;
            state.etag = body.etag;
            _loadHiddenColumns();
            _renderColumnsMenu();
            _renderTable();
            state.loaded = true;
        } catch (e) {
            _setStatus(`Error: ${e.message}`, 'error');
        }
    }

    // A checkbox toggle updates the row in place and schedules a debounced
    // save, so a burst of clicks lands as one POST. The presentation store is
    // the only editable payload; the payload is simply the per-surface
    // membership lists rebuilt from the current checkbox states.
    let _saveTimer = null;
    let _saving = false;

    function _togglePrio(rowIdx, col, checked) {
        const row = state.rows[rowIdx];
        if (!row || !(col in PRIO_COLUMNS)) return;
        row[col] = checked ? '1' : '';
        _setStatus('Saving…');
        if (_saveTimer) clearTimeout(_saveTimer);
        _saveTimer = setTimeout(_save, 400);
    }

    async function _save() {
        _saveTimer = null;
        if (_saving) {
            // A save is in flight — run again once it finishes so the latest
            // checkbox states always land.
            _saveTimer = setTimeout(_save, 400);
            return;
        }
        _saving = true;
        try {
            const surfaces = {};
            for (const [col, surface] of Object.entries(PRIO_COLUMNS)) {
                surfaces[surface] = state.rows
                    .filter(r => String(r[col] ?? '').trim() !== '' && String(r[col]) !== '<NA>')
                    .map(r => r.variable_name);
            }
            const res = await fetch(PRESENTATION_ENDPOINT, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ surfaces, etag: state.etag }),
            });
            const body = await res.json();
            if (res.status === 409) {
                _setStatus('Save rejected (someone else saved first) — reloading.', 'error');
                await _load();
                return;
            }
            if (!res.ok) {
                throw new Error(body.error || body.message || `HTTP ${res.status}`);
            }
            state.etag = body.etag || state.etag;
            _setStatus('Saved.', 'ok');
        } catch (e) {
            _setStatus(`Save failed: ${e.message} — reloading.`, 'error');
            await _load();
        } finally {
            _saving = false;
        }
    }

    function _esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // ---------- wiring ----------

    function _wireOnce() {
        if (state.wired) return;
        state.wired = true;
        // Close the columns dropdown when clicking anywhere outside it.
        document.addEventListener('click', (ev) => {
            const wrap = document.getElementById('vs-columns-wrap');
            if (wrap && !wrap.contains(ev.target)) {
                _toggleColumnsMenu(true);
            }
        });
    }

    // Defer until the schema page becomes active.  Avoids loading the
    // payload (and the etag) before the admin even opens the tab.
    function _maybeBootstrap() {
        const page = document.getElementById('admin-page-schema');
        if (!page) return;
        if (state.loaded) return;
        if (!page.classList.contains('active')) return;
        _wireOnce();
        _load();
    }

    // Hook the existing admin sidebar's openAdminPage flow — it just adds
    // the .active class, so observe DOM mutations rather than monkey-patching
    // the function (which the embedded admin.html script defines).
    function _watchForActivation() {
        const page = document.getElementById('admin-page-schema');
        if (!page) return;
        const observer = new MutationObserver(_maybeBootstrap);
        observer.observe(page, { attributes: true, attributeFilter: ['class'] });
        // Also try once on script load in case the page is already active
        // (e.g. user refreshes the browser while on this tab).
        _maybeBootstrap();
    }

    // Public globals used by inline handlers in the template.
    window.vsTogglePrio = _togglePrio;
    window.vsSort = _onSort;
    window.vsToggleColumn = _toggleColumn;
    window.vsToggleColumnsMenu = () => _toggleColumnsMenu(false);
    window.vsShowAllColumns = _showAllColumns;
    window.vsResetColumns = _resetColumns;
    window.vsReload = () => _load(true);

    // The annotation-contract card (now on the versions page) announces
    // activations/reverts; the contract drives var_schema metadata, so drop
    // the cached table and refetch (immediately if this page is active,
    // otherwise on its next activation).
    document.addEventListener('fyp:contract-changed', () => {
        state.loaded = false;
        _maybeBootstrap();
    });

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _watchForActivation);
    } else {
        _watchForActivation();
    }
})();
