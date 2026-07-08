/* Admin → Variable Schema editor.
 *
 * Loads /api/manage/schema, renders an inline-edit table, and POSTs back
 * to the same endpoint.  Optimistic concurrency via the etag returned on
 * GET.  Validation is performed server-side; the UI surfaces returned
 * errors row-by-row.
 *
 * No build step; this file is served as-is.  Styling uses CSS custom
 * properties from style.css (see CLAUDE.md "Frontend Styling Rules").
 */

(function () {
    'use strict';

    const SCHEMA_ENDPOINT = '/api/manage/schema';
    const PRESENTATION_ENDPOINT = '/api/manage/presentation';

    // The four web-surface membership columns — the only editable payload left
    // (metadata is contract-owned). Rendered as checkboxes; saved as
    // per-surface variable lists to the presentation store.
    const PRIO_COLUMNS = {
        web_filter_prio: 'filter',
        web_timeline_prio: 'timeline',
        web_viz_prio: 'viz',
        web_display_prio: 'display',
    };

    // Module state — bound once when the schema tab is first opened.
    const state = {
        rows: [],            // server rows
        edits: {},           // {rowIndex: {column: newValue}}
        columns: [],         // ordered list from server
        semanticColumns: new Set(),
        enums: {},
        recodeFuncs: [],
        etag: null,
        currentHash: null,
        filters: { group: 'all', source: 'all', role: 'all', search: '' },
        hiddenColumns: new Set(),   // user-hidden via the Columns dropdown
        sort: { col: null, dir: 1 },  // dir: 1 = asc, -1 = desc
        loaded: false,
        // {variable_name: {metadata: bool, section: bool}} — which cells the
        // annotation contract owns (so the editor renders them read-only).
        contractLocked: {},
        contractPath: 'config/annotation_contract.toml',
    };

    // localStorage key for the user's hidden-column choices.
    const HIDDEN_COLS_KEY = 'vsHiddenColumns';

    // Columns whose role is more bookkeeping than user-facing.  Hidden by
    // default to keep the table readable; toggled via the "All columns"
    // filter group setting.
    const ALWAYS_VISIBLE = new Set([
        'variable_name', 'display_name', 'section', 'source', 'role', 'scale',
    ]);

    // Columns that are derived/overlaid in memory rather than stored in the CSV:
    // the annotation contract (config/annotation_contract.toml) is the source of
    // truth, the column is rebuilt at load, and any edit is stripped before save.
    // Shown read-only here for context — editing happens in the contract, not here.
    const READONLY_COLUMNS = new Set(['accepted_labels']);

    const READONLY_TOOLTIP = 'Derived from the annotation contract '
        + '(config/annotation_contract.toml) — read-only here. Edit the contract '
        + 'to change the accepted labels.';

    // Metadata columns the annotation contract owns for its Gemini output
    // variables. Locked per-row (not per-column) via state.contractLocked.
    const META_LOCK_COLS = new Set(['role', 'scale', 'display_name', 'description']);

    // ---------- helpers ----------

    function _columnGroup(col) {
        if (state.semanticColumns.has(col)) return 'semantic';
        if (col === 'variable_name' || col === 'source' || col === 'section') return 'identity';
        return 'presentation';
    }

    function _rowMatchesFilters(row) {
        const f = state.filters;
        if (f.source !== 'all' && String(row.source || '') !== f.source) return false;
        if (f.role !== 'all' && String(row.role || '') !== f.role) return false;
        if (f.search) {
            const needle = f.search.toLowerCase();
            const name = String(row.variable_name || '').toLowerCase();
            if (!name.includes(needle)) return false;
        }
        return true;
    }

    function _visibleColumns() {
        const f = state.filters;
        let cols = state.columns;
        if (f.group === 'semantic') {
            cols = cols.filter(c =>
                ALWAYS_VISIBLE.has(c) || state.semanticColumns.has(c));
        } else if (f.group === 'presentation') {
            cols = cols.filter(c =>
                ALWAYS_VISIBLE.has(c) || _columnGroup(c) === 'presentation');
        }
        // variable_name is the row key and can never be hidden.
        return cols.filter(c => c === 'variable_name' || !state.hiddenColumns.has(c));
    }




    function _loadHiddenColumns() {
        try {
            const stored = JSON.parse(localStorage.getItem(HIDDEN_COLS_KEY) || '[]');
            state.hiddenColumns = new Set(
                stored.filter(c => state.columns.includes(c) && c !== 'variable_name'));
        } catch (e) {
            state.hiddenColumns = new Set();
        }
    }




    function _saveHiddenColumns() {
        try {
            localStorage.setItem(HIDDEN_COLS_KEY,
                JSON.stringify(Array.from(state.hiddenColumns)));
        } catch (e) { /* storage unavailable — selection just won't persist */ }
    }




    function _sortedRowIndices() {
        const indices = [];
        state.rows.forEach((row, i) => {
            if (_rowMatchesFilters(row)) indices.push(i);
        });
        const col = state.sort.col;
        if (!col) return indices;
        const dir = state.sort.dir;
        indices.sort((a, b) => {
            const va = String(_effectiveValue(a, col) ?? '').trim();
            const vb = String(_effectiveValue(b, col) ?? '').trim();
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

    function _isSemanticDirty() {
        for (const [rowIdx, cols] of Object.entries(state.edits)) {
            for (const col of Object.keys(cols)) {
                if (state.semanticColumns.has(col)) return true;
            }
        }
        return false;
    }

    function _dirtyCount() {
        let n = 0;
        for (const cols of Object.values(state.edits)) {
            n += Object.keys(cols).length;
        }
        return n;
    }

    function _effectiveValue(rowIdx, col) {
        if (state.edits[rowIdx] && col in state.edits[rowIdx]) {
            return state.edits[rowIdx][col];
        }
        const row = state.rows[rowIdx];
        return row && col in row ? row[col] : '';
    }

    function _setEdit(rowIdx, col, value) {
        const original = state.rows[rowIdx][col] || '';
        if (String(value) === String(original)) {
            if (state.edits[rowIdx]) {
                delete state.edits[rowIdx][col];
                if (Object.keys(state.edits[rowIdx]).length === 0) {
                    delete state.edits[rowIdx];
                }
            }
        } else {
            if (!state.edits[rowIdx]) state.edits[rowIdx] = {};
            state.edits[rowIdx][col] = value;
        }
        _renderSaveBar();
    }

    function _payloadRows() {
        return state.rows.map((row, i) => {
            const out = {};
            for (const col of state.columns) {
                out[col] = _effectiveValue(i, col);
            }
            return out;
        });
    }

    // ---------- rendering ----------

    function _renderFilters() {
        const sources = Array.from(new Set(state.rows.map(r => r.source).filter(Boolean))).sort();
        const roles = Array.from(new Set(state.rows.map(r => r.role).filter(Boolean))).sort();
        const sourceSel = document.getElementById('vs-filter-source');
        const roleSel = document.getElementById('vs-filter-role');
        if (sourceSel) {
            sourceSel.innerHTML = '<option value="all">All sources</option>'
                + sources.map(s => `<option value="${_esc(s)}">${_esc(s)}</option>`).join('');
        }
        if (roleSel) {
            roleSel.innerHTML = '<option value="all">All roles</option>'
                + roles.map(r => `<option value="${_esc(r)}">${_esc(r)}</option>`).join('');
        }
    }

    function _renderTable() {
        const thead = document.getElementById('vs-thead');
        const tbody = document.getElementById('vs-tbody');
        if (!thead || !tbody) return;

        const cols = _visibleColumns();

        thead.innerHTML = '<tr>' + cols.map(col => {
            const group = _columnGroup(col);
            let marker = '';
            if (READONLY_COLUMNS.has(col)) {
                marker = `<span class="meta-tooltip" data-tooltip="${_esc(READONLY_TOOLTIP)}" style="color: var(--color-text-muted); margin-left: 4px;">🔒</span>`;
            } else if (group === 'semantic') {
                marker = '<span class="meta-tooltip" data-tooltip="Semantic column — editing this rebuilds study caches." style="color: var(--color-warning); margin-left: 4px;">⚠</span>';
            }
            const isSorted = state.sort.col === col;
            const arrow = isSorted
                ? `<span style="margin-left: 4px;">${state.sort.dir === 1 ? '▲' : '▼'}</span>`
                : '';
            const sortColor = isSorted ? 'var(--color-text-primary)' : 'var(--color-text-muted)';
            return `<th onclick="vsSort('${_esc(col)}')"
                class="meta-tooltip" data-tooltip="Click to sort"
                style="text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--color-border); white-space: nowrap; color: ${sortColor}; font-weight: var(--weight-semibold); cursor: pointer; user-select: none;">${_esc(col)}${marker}${arrow}</th>`;
        }).join('') + '</tr>';

        const fragments = [];
        _sortedRowIndices().forEach(rowIdx => {
            const cells = cols.map(col => _renderCell(rowIdx, col)).join('');
            fragments.push(`<tr data-row-index="${rowIdx}" style="border-bottom: 1px solid var(--color-border);">${cells}</tr>`);
        });
        tbody.innerHTML = fragments.join('');

        const visible = tbody.querySelectorAll('tr').length;
        const status = document.getElementById('vs-status');
        if (status) {
            status.textContent = `${visible} of ${state.rows.length} rows visible.`;
        }
    }

    // Return the tooltip for a contract-owned (read-only) cell, or null when the
    // cell is editable. accepted_labels is always derived; for a Gemini variable
    // the contract owns role/scale/display_name/description and the AI Annotations section.
    function _cellReadonlyTooltip(rowIdx, col) {
        if (READONLY_COLUMNS.has(col)) return READONLY_TOOLTIP;
        const row = state.rows[rowIdx];
        const lock = row && state.contractLocked[row.variable_name];
        if (!lock) return null;
        if (col === 'section' && lock.section) {
            return 'Gemini variables are grouped under "AI Annotations" automatically — '
                + 'this section is set by the app, not editable here.';
        }
        if (lock.metadata && META_LOCK_COLS.has(col)) {
            return `Owned by the annotation contract (${state.contractPath}) — `
                + `edit ${col.replace(/_/g, ' ')} there, not here.`;
        }
        return null;
    }

    function _renderCell(rowIdx, col) {
        const current = _effectiveValue(rowIdx, col);
        const isEdited = state.edits[rowIdx] && col in state.edits[rowIdx];
        const baseStyle = `padding: 4px 8px; vertical-align: top; ${isEdited ? 'background: var(--color-bg-input);' : ''}`;

        if (col === 'variable_name' || col === 'source') {
            let badge = '';
            if (col === 'variable_name') {
                const lock = state.contractLocked[String(current)];
                if (lock && lock.legacy) {
                    badge = ` <span class="meta-tooltip" data-tooltip="Legacy annotation field — owned by a past contract version and kept for older annotated rows; not editable here."`
                        + ` style="color: var(--color-text-muted); font-size: var(--text-xxs); border: 1px solid var(--color-border); border-radius: 3px; padding: 0 3px; white-space: nowrap;">legacy</span>`;
                }
            }
            return `<td class="font-mono text-xs" style="${baseStyle} color: var(--color-text-primary); white-space: nowrap;">${_esc(current)}${badge}</td>`;
        }

        // Contract-owned cells: display-only, greyed, with a tooltip that points
        // at the real source of truth instead of an editable input. Covers the
        // always-derived accepted_labels plus the per-row contract metadata /
        // forced AI Annotations section for Gemini variables.
        const lockTip = _cellReadonlyTooltip(rowIdx, col);
        if (lockTip !== null) {
            const shown = String(current).trim()
                ? _esc(current)
                : '<span style="opacity: 0.5;">—</span>';
            return `<td class="meta-tooltip font-mono text-xs" data-tooltip="${_esc(lockTip)}"
                style="${baseStyle} color: var(--color-text-muted);">${shown}</td>`;
        }

        // Presentation membership flags: ON/OFF checkboxes (the numeric value
        // is historical — any non-blank means ON).
        if (col in PRIO_COLUMNS) {
            const checked = String(current).trim() !== '' && String(current) !== '<NA>';
            return `<td style="${baseStyle} text-align: center;">
                <input type="checkbox" ${checked ? 'checked' : ''}
                    onchange="vsOnEdit(${rowIdx}, '${_esc(col)}', this.checked ? '1' : '')">
            </td>`;
        }

        // Enums via <select>
        const enumChoices = _enumForColumn(col);
        if (enumChoices) {
            const opts = ['<option value=""></option>']
                .concat(enumChoices.map(v =>
                    `<option value="${_esc(v)}" ${String(current) === v ? 'selected' : ''}>${_esc(v)}</option>`));
            return `<td style="${baseStyle}">
                <select onchange="vsOnEdit(${rowIdx}, '${_esc(col)}', this.value)"
                    style="padding: 2px 4px; border: 1px solid var(--color-border); border-radius: 3px; background: var(--color-bg-input); color: var(--color-text-primary); font-size: inherit;">
                    ${opts.join('')}
                </select>
            </td>`;
        }

        // Free-text fallback
        const isLong = String(current).length > 30 || col === 'description';
        const widthStyle = isLong ? 'min-width: 240px;' : 'min-width: 80px;';
        return `<td style="${baseStyle}">
            <input type="text" value="${_esc(current)}"
                oninput="vsOnEditDebounced(${rowIdx}, '${_esc(col)}', this.value)"
                onchange="vsOnEdit(${rowIdx}, '${_esc(col)}', this.value)"
                style="padding: 2px 6px; border: 1px solid var(--color-border); border-radius: 3px; background: var(--color-bg-input); color: var(--color-text-primary); width: 100%; ${widthStyle} font-family: inherit; font-size: inherit;">
        </td>`;
    }

    function _renderColumnsMenu() {
        const menu = document.getElementById('vs-columns-menu');
        if (!menu) return;
        const items = state.columns.map(col => {
            const locked = col === 'variable_name';
            const checked = !state.hiddenColumns.has(col);
            return `<label class="text-xs" style="display: flex; align-items: center; gap: 6px;
                    padding: 3px 4px; cursor: ${locked ? 'default' : 'pointer'};
                    color: ${locked ? 'var(--color-text-muted)' : 'var(--color-text-primary)'};">
                <input type="checkbox" ${checked ? 'checked' : ''} ${locked ? 'disabled' : ''}
                    onchange="vsToggleColumn('${_esc(col)}', this.checked)">
                <span class="font-mono">${_esc(col)}</span>
            </label>`;
        });
        const actions = `<div style="display: flex; gap: 8px; padding: 4px 4px 6px 4px;
                margin-bottom: 4px; border-bottom: 1px solid var(--color-border);">
            <button onclick="vsShowAllColumns()" class="btn-discreet text-xs"
                style="padding: 2px 8px;">Show all</button>
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




    function _renderSaveBar() {
        const bar = document.getElementById('vs-save-bar');
        const count = _dirtyCount();
        const dirtyEl = document.getElementById('vs-dirty-count');
        const hashWarn = document.getElementById('vs-hash-warning');
        if (dirtyEl) dirtyEl.textContent = String(count);
        if (hashWarn) hashWarn.style.display = _isSemanticDirty() ? 'inline' : 'none';
        if (bar) bar.style.display = count > 0 ? 'block' : 'none';
    }

    function _enumForColumn(col) {
        if (col === 'role' || col === 'scale' || col === 'unable_to_detect_policy') {
            return state.enums[col] || [];
        }
        if (col === 'recode_func') {
            return state.recodeFuncs;
        }
        return null;
    }

    // ---------- network ----------

    async function _load(forceDiskReload) {
        const status = document.getElementById('vs-status');
        if (status) status.textContent = forceDiskReload ? 'Re-reading from disk…' : 'Loading…';
        try {
            const url = forceDiskReload ? `${SCHEMA_ENDPOINT}?force_reload=1` : SCHEMA_ENDPOINT;
            const res = await fetch(url);
            if (!res.ok) {
                throw new Error(`Server returned ${res.status}`);
            }
            const body = await res.json();
            state.rows = body.rows || [];
            state.columns = body.columns || [];
            state.semanticColumns = new Set(body.semantic_columns || []);
            state.enums = body.enums || {};
            state.recodeFuncs = body.recode_funcs || [];
            state.contractLocked = body.contract_locked || {};
            state.contractPath = body.contract_path || state.contractPath;
            state.etag = body.etag;
            state.currentHash = body.current_hash;
            state.edits = {};
            _loadHiddenColumns();
            _renderFilters();
            _renderColumnsMenu();
            _renderTable();
            _renderSaveBar();
            state.loaded = true;
            if (status) status.textContent = `${state.rows.length} variables loaded. Etag ${state.etag.slice(0, 8)}.`;
        } catch (e) {
            if (status) status.textContent = `Error: ${e.message}`;
        }
    }

    async function _validate() {
        // Metadata is contract-owned and presentation flags are excluded from
        // the study hash, so there is no semantic impact to preview anymore.
        const out = document.getElementById('vs-validation-output');
        if (out) {
            out.innerHTML = '<div style="color: var(--color-text-muted);">'
                + 'Metadata is owned by the contract TOMLs (read-only here); the '
                + 'on/off surface flags never affect cached study data — nothing to preview.</div>';
        }
    }

    function _renderValidation(body) {
        const out = document.getElementById('vs-validation-output');
        if (!out) return;
        const errors = body.errors || [];
        let html = '';
        if (errors.length) {
            html += `<div style="color: var(--color-danger);">${errors.length} validation error(s):</div>`;
            html += '<ul style="margin: 4px 0 0 18px; padding: 0;">'
                + errors.slice(0, 20).map(e =>
                    `<li><span class="font-mono">${_esc(e.variable_name || '?')}</span> · <span style="color: var(--color-text-muted);">${_esc(e.column)}</span>: ${_esc(e.message)}</li>`)
                  .join('')
                + (errors.length > 20 ? `<li>… and ${errors.length - 20} more</li>` : '')
                + '</ul>';
        } else {
            html += '<div style="color: var(--color-success);">Validation passed.</div>';
        }
        if (body.hash_changed) {
            const affected = body.affected_studies || [];
            html += `<div style="margin-top: 6px;">⚠ Semantic change detected. New hash ${_esc(body.new_hash || '').slice(0, 16)}…<br>`
                + `Studies that would be marked for rebuild: ${affected.length === 0 ? '<em>(none currently — first build will use new hash)</em>' : affected.map(_esc).join(', ')}.</div>`;
        }
        out.innerHTML = html;
    }

    let _editDebounceTimer = null;

    function _setEditDebounced(rowIdx, col, value) {
        if (_editDebounceTimer) clearTimeout(_editDebounceTimer);
        _editDebounceTimer = setTimeout(() => {
            _editDebounceTimer = null;
            _setEdit(rowIdx, col, value);
        }, 150);
    }

    async function _save() {
        // Flush a focused-but-uncommitted cell: text inputs only commit on
        // change (blur), so a click straight onto Save would otherwise see
        // zero dirty edits and silently no-op.
        const active = document.activeElement;
        if (active && typeof active.blur === 'function') active.blur();
        if (_editDebounceTimer) {
            clearTimeout(_editDebounceTimer);
            _editDebounceTimer = null;
        }
        if (_dirtyCount() === 0) return;
        const out = document.getElementById('vs-validation-output');
        if (out) out.textContent = 'Saving…';
        try {
            // Only the presentation flags are editable — build the per-surface
            // membership lists from the effective (edited) checkbox values.
            const surfaces = {};
            for (const [col, surface] of Object.entries(PRIO_COLUMNS)) {
                surfaces[surface] = state.rows
                    .map((row, i) => ({ name: row.variable_name, v: _effectiveValue(i, col) }))
                    .filter(x => String(x.v).trim() !== '' && String(x.v) !== '<NA>')
                    .map(x => x.name);
            }
            const res = await fetch(PRESENTATION_ENDPOINT, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ surfaces, etag: state.etag }),
            });
            const body = await res.json();
            if (res.status === 409) {
                if (out) out.innerHTML = `<div style="color: var(--color-danger);">Save rejected: ${_esc(body.message || 'etag mismatch')}. Reloading.</div>`;
                await _load();
                return;
            }
            if (!res.ok) {
                throw new Error(body.error || `HTTP ${res.status}`);
            }
            // Re-fetch from the server so state.rows reflects what was
            // actually persisted — without this, the table re-renders from
            // pre-save in-memory data and the edit appears to revert until
            // the page is reloaded.
            await _load();
            if (out) out.innerHTML = `<div style="color: var(--color-success);">Saved.</div>`;
        } catch (e) {
            if (out) out.textContent = `Error: ${e.message}`;
        }
    }

    function _cancel() {
        state.edits = {};
        _renderTable();
        _renderSaveBar();
        const out = document.getElementById('vs-validation-output');
        if (out) out.textContent = '';
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

    function _bindFilters() {
        const g = document.getElementById('vs-filter-group');
        const s = document.getElementById('vs-filter-source');
        const r = document.getElementById('vs-filter-role');
        const q = document.getElementById('vs-filter-search');
        if (g) g.addEventListener('change', () => { state.filters.group = g.value; _renderTable(); });
        if (s) s.addEventListener('change', () => { state.filters.source = s.value; _renderTable(); });
        if (r) r.addEventListener('change', () => { state.filters.role = r.value; _renderTable(); });
        if (q) {
            let timer = null;
            q.addEventListener('input', () => {
                clearTimeout(timer);
                timer = setTimeout(() => {
                    state.filters.search = q.value.trim();
                    _renderTable();
                }, 150);
            });
        }
        // Close the columns dropdown when clicking anywhere outside it.
        document.addEventListener('click', (ev) => {
            const wrap = document.getElementById('vs-columns-wrap');
            if (wrap && !wrap.contains(ev.target)) {
                _toggleColumnsMenu(true);
            }
        });
    }

    // ---------- annotation contract card ----------

    const AC_ENDPOINT = '/api/manage/annotation-contract';
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
            const badge = document.getElementById('ac-source-badge');
            if (badge) badge.textContent = 'unknown';
        }
    }

    function _acRenderStatus(s) {
        const badge = document.getElementById('ac-source-badge');
        const meta = document.getElementById('ac-meta');
        const err = document.getElementById('ac-error');
        const revert = document.getElementById('ac-revert-btn');
        const isRuntime = s.source === 'runtime';
        acState.etag = s.etag || null;
        if (badge) {
            badge.textContent = isRuntime ? 'runtime' : 'baked';
            badge.style.color = isRuntime ? 'var(--color-success)' : 'var(--color-text-muted)';
            badge.style.borderColor = isRuntime ? 'var(--color-success)' : 'var(--color-border)';
        }
        if (meta) {
            const parts = [];
            if (s.current_version) parts.push(`Active version: <span class="font-mono">${_esc(s.current_version)}</span>`);
            if (s.etag) parts.push(`Etag: <span class="font-mono">${_esc(String(s.etag).slice(0, 20))}</span>`);
            if (isRuntime && s.updated_by) parts.push(`Uploaded by ${_esc(s.updated_by)}`);
            if (isRuntime && s.updated_at) parts.push(_esc(s.updated_at));
            meta.innerHTML = parts.join(' · ');
        }
        if (err) {
            if (s.error) {
                err.style.display = 'block';
                err.textContent = `⚠ Runtime contract ignored: ${s.error} — using the baked contract.`;
            } else {
                err.style.display = 'none';
                err.textContent = '';
            }
        }
        if (revert) revert.style.display = isRuntime ? 'inline-block' : 'none';
    }

    function _acStatus(msg, color) {
        const el = document.getElementById('ac-card-status');
        if (el) {
            el.textContent = msg || '';
            el.style.color = color || 'var(--color-text-muted)';
        }
    }

    function _acDownload() {
        window.location.href = `${AC_ENDPOINT}/download`;
    }

    async function _acOnFileChosen(input) {
        const file = input.files && input.files[0];
        input.value = '';  // allow re-selecting the same file later
        if (!file) return;
        _acStatus('Validating…');
        let text;
        try {
            text = await file.text();
        } catch (e) {
            _acStatus('Could not read the file.', 'var(--color-danger)');
            return;
        }
        try {
            const fd = new FormData();
            fd.append('text', text);
            const res = await fetch(AC_ENDPOINT, { method: 'POST', body: fd });
            const body = await res.json();
            if (res.status === 400 && body.errors) {
                _acStatus('', 'var(--color-text-muted)');
                _acShowValidationErrors(file.name, body.errors);
                return;
            }
            if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
            acState.staged = { text, filename: file.name, impact: body.impact };
            _acStatus('');
            _acShowImpactModal(file.name, body.impact);
        } catch (e) {
            _acStatus(`Error: ${e.message}`, 'var(--color-danger)');
        }
    }

    function _acShowValidationErrors(filename, errors) {
        const body = document.getElementById('ac-modal-body');
        const confirmBtn = document.getElementById('ac-confirm-btn');
        if (confirmBtn) confirmBtn.style.display = 'none';
        if (body) {
            body.innerHTML = `<div style="color: var(--color-danger); margin-bottom: 8px;">`
                + `<span class="font-mono">${_esc(filename)}</span> is not a valid contract `
                + `(${errors.length} error${errors.length === 1 ? '' : 's'}):</div>`
                + '<ul style="margin: 0 0 0 18px; padding: 0;">'
                + errors.slice(0, 30).map(e => `<li>${_esc(e)}</li>`).join('')
                + (errors.length > 30 ? `<li>… and ${errors.length - 30} more</li>` : '')
                + '</ul>';
        }
        _acOpenModal();
    }

    function _acShowImpactModal(filename, impact) {
        const body = document.getElementById('ac-modal-body');
        const confirmBtn = document.getElementById('ac-confirm-btn');
        if (confirmBtn) confirmBtn.style.display = 'inline-block';
        if (!body) return;
        const rows = [];
        if (impact.metadata_only) {
            rows.push(`<div style="color: var(--color-success); margin-bottom: 10px;">`
                + `✓ Metadata-only change — <strong>no new annotation version</strong>. `
                + `Existing annotations stay valid.</div>`);
        } else {
            rows.push(`<div style="color: var(--color-warning); margin-bottom: 10px;">`
                + `⚠ This changes the ${impact.prompt_changed && impact.schema_changed ? 'prompt and response schema'
                    : impact.prompt_changed ? 'prompt' : 'response schema'}. `
                + `A new annotation version <span class="font-mono">${_esc(impact.candidate_version)}</span> `
                + `will be minted on the next annotation run (current: `
                + `<span class="font-mono">${_esc(impact.current_version)}</span>). `
                + `It won&rsquo;t become active until you promote it under `
                + `<em>Annotation versions</em>.</div>`);
        }
        const detail = [];
        detail.push(`Prompt changed: <strong>${impact.prompt_changed ? 'yes' : 'no'}</strong>`);
        detail.push(`Schema changed: <strong>${impact.schema_changed ? 'yes' : 'no'}</strong>`);
        if (impact.fields_added && impact.fields_added.length) {
            detail.push(`Fields added: <span class="font-mono">${impact.fields_added.map(_esc).join(', ')}</span>`);
        }
        if (impact.fields_removed && impact.fields_removed.length) {
            detail.push(`Fields removed: <span class="font-mono">${impact.fields_removed.map(_esc).join(', ')}</span>`);
        }
        body.innerHTML = rows.join('')
            + `<div class="text-xs" style="color: var(--color-text-muted); margin-bottom: 4px;">`
            + `Uploading <span class="font-mono">${_esc(filename)}</span></div>`
            + '<ul style="margin: 6px 0 0 18px; padding: 0;" class="text-sm">'
            + detail.map(d => `<li>${d}</li>`).join('')
            + '</ul>';
        _acOpenModal();
    }

    async function _acConfirmUpload() {
        if (!acState.staged) { _acCloseModal(); return; }
        const confirmBtn = document.getElementById('ac-confirm-btn');
        if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = 'Activating…'; }
        try {
            const fd = new FormData();
            fd.append('text', acState.staged.text);
            fd.append('confirm', '1');
            if (acState.etag) fd.append('expected_etag', acState.etag);
            const res = await fetch(AC_ENDPOINT, { method: 'POST', body: fd });
            const body = await res.json();
            if (res.status === 409) {
                _acStatus(`Rejected: ${body.message || 'the contract changed'}.`, 'var(--color-danger)');
                _acCloseModal();
                await _acLoadStatus();
                return;
            }
            if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
            acState.staged = null;
            _acCloseModal();
            _acStatus(body.note || 'Contract activated.', 'var(--color-success)');
            await _acLoadStatus();
            // The contract drives var_schema metadata — re-read the table too.
            await _load();
        } catch (e) {
            _acStatus(`Error: ${e.message}`, 'var(--color-danger)');
        } finally {
            if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = 'Activate contract'; }
        }
    }

    async function _acRevert() {
        if (!window.confirm('Revert to the baked contract? The current runtime contract will be archived.')) return;
        _acStatus('Reverting…');
        try {
            const res = await fetch(`${AC_ENDPOINT}/revert`, { method: 'POST' });
            const body = await res.json();
            if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
            _acStatus(body.note || 'Reverted.', 'var(--color-success)');
            await _acLoadStatus();
            await _load();
        } catch (e) {
            _acStatus(`Error: ${e.message}`, 'var(--color-danger)');
        }
    }

    function _acOpenModal() {
        const m = document.getElementById('ac-modal');
        if (m) m.style.display = 'flex';
    }

    function _acCloseModal() {
        const m = document.getElementById('ac-modal');
        if (m) m.style.display = 'none';
    }

    // Defer until the schema page becomes active.  Avoids loading the
    // payload (and the etag) before the admin even opens the tab.
    function _maybeBootstrap() {
        const page = document.getElementById('admin-page-schema');
        if (!page) return;
        if (state.loaded) return;
        if (!page.classList.contains('active')) return;
        _bindFilters();
        _load();
        _acLoadStatus();
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
    window.vsOnEdit = _setEdit;
    window.vsOnEditDebounced = _setEditDebounced;
    window.vsSort = _onSort;
    window.vsToggleColumn = _toggleColumn;
    window.vsToggleColumnsMenu = () => _toggleColumnsMenu(false);
    window.vsShowAllColumns = _showAllColumns;
    window.vsReload = () => _load(true);
    window.vsValidate = _validate;
    window.vsSave = _save;
    window.vsCancel = _cancel;
    // Annotation contract card handlers.
    window.acDownload = _acDownload;
    window.acOnFileChosen = _acOnFileChosen;
    window.acConfirmUpload = _acConfirmUpload;
    window.acRevert = _acRevert;
    window.acCloseModal = _acCloseModal;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _watchForActivation);
    } else {
        _watchForActivation();
    }
})();
