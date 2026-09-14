// Shared UI for the per-variable filter blocks ("groups") inside the
// Explore and Video Analysis filter panels.
//
// A group is a collapsible sub-section: a compact header row (arrow, variable
// name, an active-state badge, optional extra controls) and a body that is
// built lazily on first expand. Expanded groups are remembered in
// localStorage under the caller's key.
//
// Both tabs keep their own filter state objects; this module only reads them
// (through describeFilterEntry) to paint the badge, so it stays in sync with
// resets and drill-down restores without any DOM bookkeeping.
(function () {
    'use strict';

    const ARROW_OPEN = '▾';   // ▾
    const ARROW_CLOSED = '▸'; // ▸

    function loadList(storageKey) {
        try {
            const v = JSON.parse(localStorage.getItem(storageKey) || '[]');
            return Array.isArray(v) ? v : [];
        } catch (e) { return []; }
    }

    function saveList(storageKey, list) {
        try { localStorage.setItem(storageKey, JSON.stringify(list)); } catch (e) { /* ignore */ }
    }

    // Text shown in the badge next to a variable name, or '' when the filter
    // is inactive. Checkbox filters show how many values are ticked; range
    // filters show the bound(s) that bind.
    function describeFilterEntry(entry) {
        if (!entry) return '';
        const fmt = (typeof formatMetricNumber === 'function') ? formatMetricNumber : (n) => String(n);
        const parts = [];
        if (entry.type === 'number') {
            const v = entry.value || {};
            const hasMin = v.min !== undefined && v.min !== null;
            const hasMax = v.max !== undefined && v.max !== null;
            if (hasMin && hasMax) parts.push(`${fmt(v.min)}–${fmt(v.max)}`);
            else if (hasMin) parts.push(`≥ ${fmt(v.min)}`);
            else if (hasMax) parts.push(`≤ ${fmt(v.max)}`);
        } else if (Array.isArray(entry.value) && entry.value.length > 0) {
            parts.push(String(entry.value.length));
        }
        if (entry.na) parts.push('NA');
        return parts.join(', ');
    }

    // Builds a collapsible group. `build(body)` is called once, on first
    // expand, to fill the body with the slider / checkbox list.
    //   col, displayName, storageKey, build
    //   headerExtra (optional): element placed at the right end of the header
    function buildFilterGroup(opts) {
        const { col, displayName, storageKey, build } = opts;
        const expanded = loadList(storageKey);

        const wrapper = document.createElement('div');
        wrapper.className = 'filter-group';
        wrapper.dataset.column = col;

        const header = document.createElement('div');
        header.className = 'filter-group-header';
        header.setAttribute('role', 'button');
        header.setAttribute('tabindex', '0');

        const arrow = document.createElement('span');
        arrow.className = 'filter-group-arrow';

        const name = document.createElement('span');
        name.className = 'filter-group-name';
        name.textContent = displayName;

        const badge = document.createElement('span');
        badge.className = 'filter-group-badge';

        header.appendChild(arrow);
        header.appendChild(name);
        header.appendChild(badge);

        const body = document.createElement('div');
        body.className = 'filter-group-body';

        const setOpen = (open) => {
            if (open && body.dataset.populated !== '1') {
                body.dataset.populated = '1';
                build(body);
            }
            body.hidden = !open;
            arrow.textContent = open ? ARROW_OPEN : ARROW_CLOSED;
            header.setAttribute('aria-expanded', open ? 'true' : 'false');
            wrapper.classList.toggle('is-open', open);
        };

        const toggle = () => {
            const open = body.hidden;
            setOpen(open);
            const list = loadList(storageKey);
            const idx = list.indexOf(col);
            if (open && idx === -1) list.push(col);
            if (!open && idx > -1) list.splice(idx, 1);
            saveList(storageKey, list);
        };

        header.onclick = (ev) => {
            // Clicks on controls placed in the header (sort toggle) don't fold.
            if (ev.target.closest('button, input, select')) return;
            toggle();
        };
        header.onkeydown = (ev) => {
            if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); toggle(); }
        };

        if (opts.headerExtra) {
            opts.headerExtra.classList.add('filter-group-header-extra');
            header.appendChild(opts.headerExtra);
        }

        wrapper.appendChild(header);
        wrapper.appendChild(body);
        setOpen(expanded.includes(col));
        return wrapper;
    }

    // Repaints every group's badge and active colour inside `container` from
    // the tab's filter map, and the parent section header's count of active
    // variables. Call after any filter change or reset.
    function paintFilterState(container, filters) {
        if (!container) return;
        container.querySelectorAll('.filter-group').forEach(g => {
            const text = describeFilterEntry(filters[g.dataset.column]);
            const badge = g.querySelector('.filter-group-badge');
            if (badge) badge.textContent = text ? `(${text})` : '';
            g.classList.toggle('has-active-filter', !!text);
        });
        container.querySelectorAll('.filter-section').forEach(sec => {
            let cols = [];
            try { cols = JSON.parse(sec.dataset.columns || '[]'); } catch (e) { cols = []; }
            const n = cols.filter(col => !!describeFilterEntry(filters[col])).length;
            const header = sec.querySelector('.filter-section-header');
            if (!header) return;
            header.classList.toggle('has-active-filter', n > 0);
            const count = header.querySelector('.filter-section-count');
            if (count) count.textContent = n > 0 ? `(${n})` : '';
        });
    }

    // Section (top-level) header markup shared by both tabs.
    function sectionHeaderHtml(name, isExpanded) {
        const arrow = isExpanded ? ARROW_OPEN : ARROW_CLOSED;
        return `<span class="filter-section-arrow">${arrow}</span>`
            + `<span class="filter-section-name"></span>`
            + `<span class="filter-section-count"></span>`;
    }

    function fillSectionHeader(header, name, isExpanded) {
        header.innerHTML = sectionHeaderHtml(name, isExpanded);
        header.querySelector('.filter-section-name').textContent = name;
    }

    function setSectionArrow(header, isExpanded) {
        const a = header.querySelector('.filter-section-arrow');
        if (a) a.textContent = isExpanded ? ARROW_OPEN : ARROW_CLOSED;
    }

    window.FilterGroupUI = {
        buildFilterGroup,
        describeFilterEntry,
        paintFilterState,
        fillSectionHeader,
        setSectionArrow,
    };
})();
