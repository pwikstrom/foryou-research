// Per-variable value search for capped categorical/list filter dropdowns.
//
// The dropdown metadata only ships the top-200 count>1 values; this widget
// adds a search box that queries /api/explore/values/search, which scans the
// live column — so out-of-top-200 and single-occurrence values (e.g. an
// author with one video) become reachable. Shared by the Explore and Video
// Analysis filter builders, which pass in their own selection plumbing.
(function () {
    'use strict';

    // Attach a value-search box above `listContainer`. Options:
    //   wrapper:            the filter block element (search box is inserted
    //                       before listContainer inside it)
    //   listContainer:      the scrollable checkbox list; result items are
    //                       appended here with the same DOM shape as the
    //                       existing top-200 items, so the callers' existing
    //                       input:checked sweeps and sort toggle keep working
    //   column:             the variable name (server column)
    //   getStudy:           () => active study name
    //   isChecked:          (value) => whether the value is currently selected
    //   onSelectionChanged: () => void; caller re-collects checked values
    //   totalUnique:        selectable value count (for the placeholder)
    window.attachFilterValueSearch = function (opts) {
        const { wrapper, listContainer, column, getStudy,
                isChecked, onSelectionChanged, totalUnique } = opts;

        const searchWrap = document.createElement('div');
        searchWrap.className = 'filter-value-search-wrap';

        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'filter-value-search text-sm';
        input.placeholder = `Search all ${(totalUnique || 0).toLocaleString()} values…`;
        input.setAttribute('aria-label', `Search all values of ${column}`);

        const status = document.createElement('div');
        status.className = 'filter-value-search-status text-xs fvs-hidden';

        searchWrap.appendChild(input);
        searchWrap.appendChild(status);
        wrapper.insertBefore(searchWrap, listContainer);

        let debounceTimer = null;
        let controller = null;

        function showStatus(msg) {
            status.textContent = msg;
            status.classList.remove('fvs-hidden');
        }

        function hideStatus() {
            status.classList.add('fvs-hidden');
        }

        function resultItems() {
            return Array.from(listContainer.querySelectorAll('.fvs-result'));
        }

        // Build one result row with the exact DOM shape of the callers' own
        // items (class, dataset.rawValue, sortLabel/sortCount), so checked
        // collection, restore and the sort toggle treat it as a native item.
        function makeItem(value, count) {
            const item = document.createElement('div');
            item.style.display = 'flex';
            item.style.alignItems = 'center';
            item.className = 'filter-checkbox-item fvs-result';
            item.dataset.sortLabel = String(value).toLowerCase();
            item.dataset.sortCount = count;

            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.value = value;
            cb.dataset.rawValue = value;
            cb.style.marginRight = '5px';
            if (isChecked && isChecked(value)) cb.checked = true;
            cb.onchange = () => {
                if (!cb.checked) item.classList.remove('filter-checkbox-item--pinned');
                onSelectionChanged();
            };

            const span = document.createElement('span');
            span.innerText = `${value} (${Number(count).toLocaleString()})`;
            span.classList.add('text-sm');

            item.appendChild(cb);
            item.appendChild(span);
            return item;
        }

        // Show the given matches: hide the top-200 items and the notice, drop
        // unchecked leftovers from the previous query, then unhide existing
        // rows that match (dedupe) and append fresh rows for the rest.
        function renderResults(matches) {
            resultItems().forEach(el => {
                const cb = el.querySelector('input');
                if (!cb || !cb.checked) el.remove();
            });

            const known = new Map();
            Array.from(listContainer.children).forEach(el => {
                if (el.classList.contains('filter-checkbox-item')) {
                    const cb = el.querySelector('input[type=checkbox]');
                    if (cb) known.set(cb.dataset.rawValue, el);
                }
                el.classList.add('fvs-hidden');
            });

            matches.forEach(m => {
                const value = String(m.value);
                const existing = known.get(value);
                if (existing) existing.classList.remove('fvs-hidden');
                else listContainer.appendChild(makeItem(value, m.count));
            });
            listContainer.scrollTop = 0;
        }

        // Leave search mode: checked results become pinned rows at the top of
        // the restored top-200 view (so a searched-and-selected rare value
        // stays visible and uncheckable); unchecked results are dropped.
        function exitSearch() {
            if (controller) { controller.abort(); controller = null; }
            hideStatus();
            resultItems().forEach(el => {
                const cb = el.querySelector('input');
                if (cb && cb.checked) {
                    el.classList.add('filter-checkbox-item--pinned');
                    listContainer.insertBefore(el, listContainer.firstChild);
                } else {
                    el.remove();
                }
            });
            Array.from(listContainer.children).forEach(el => el.classList.remove('fvs-hidden'));
        }

        function runSearch(q) {
            const study = getStudy();
            if (!study) return;
            if (controller) controller.abort();
            controller = new AbortController();
            const url = '/api/explore/values/search'
                + `?study=${encodeURIComponent(study)}`
                + `&column=${encodeURIComponent(column)}`
                + `&q=${encodeURIComponent(q)}&limit=50`;
            fetch(url, { signal: controller.signal })
                .then(r => r.json().then(body => ({ ok: r.ok, body })))
                .then(({ ok, body }) => {
                    if (input.value.trim() !== q) return; // stale response
                    if (!ok) {
                        showStatus(body.error || 'Search failed');
                        return;
                    }
                    renderResults(body.matches || []);
                    const total = body.total_matches || 0;
                    if (total === 0) {
                        showStatus('No matches');
                    } else if (body.truncated) {
                        showStatus(`Showing top ${body.limit} of ${total.toLocaleString()} matches — refine your search`);
                    } else {
                        showStatus(`${total.toLocaleString()} ${total === 1 ? 'match' : 'matches'}`);
                    }
                })
                .catch(err => {
                    if (err && err.name === 'AbortError') return;
                    showStatus('Search failed');
                });
        }

        input.addEventListener('input', () => {
            clearTimeout(debounceTimer);
            const q = input.value.trim();
            if (q.length === 0) {
                exitSearch();
                return;
            }
            if (q.length < 2) {
                showStatus('Type at least 2 characters');
                return;
            }
            debounceTimer = setTimeout(() => runSearch(q), 300);
        });
    };
})();
