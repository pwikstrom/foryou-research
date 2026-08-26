// My Collections — the participant-facing "My Short-Video Personality" page.
// Everything rendered here comes from /api/my/* endpoints, which serve donated
// activity data only (ownership-gated; no scrape, no annotation).
//
// window.mycRenderPersonality(containerEl, bundle) renders one personality
// bundle into any container (chart ids are scoped per render), so the Edit
// Collections modal in data_management.js reuses the exact same view.
(function () {
    'use strict';

    let mycCollections = null;       // cached list for this page load
    let mycActiveSelection = null;   // collection_id, 'combined' or 'subset'
    let mycCheckedSet = null;        // Set of ready cids ticked for the combined persona
    let mycPersonaDebounce = null;   // pending checkbox-change timer
    let mycPersonaReqSeq = 0;        // guards against out-of-order persona responses
    let mycRenderSeq = 0;            // unique id prefix per render
    const mycMounted = [];           // {el, bundle, prefix} for theme re-renders
    let mycSources = null;           // donation upload sources (registry-driven)
    const mycPendingMap = {};        // cid -> {raw_path, filename} for pending uploads
    let mycUpload = null;            // {source, file} for the open upload modal
    let mycProcessPoll = null;       // interval id while watching ingest_refresh

    const PLATFORM_LABELS = { tiktok: 'TikTok', instagram: 'Instagram', youtube: 'YouTube' };
    // Chart series colors for platform overlays (same approach as the
    // timelines categorical palette; TikTok takes the app accent).
    function platformColor(p) {
        return { instagram: '#E91E63', youtube: '#EF5350' }[p] || getCSSVar('--color-accent');
    }
    const AXIS_LABELS = {
        patience: 'patience', binge: 'binge', consistency: 'consistency',
        chattiness: 'chattiness', enthusiasm: 'enthusiasm',
    };
    const WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];

    function platformLabel(p) {
        return PLATFORM_LABELS[p] || (p ? p.charAt(0).toUpperCase() + p.slice(1) : 'short-video');
    }

    function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }

    // "2 days, 3 hours, 14 minutes" from seconds.
    function humanizeDuration(totalSeconds) {
        const s = Math.round(totalSeconds);
        if (s < 60) return `${s} second${s === 1 ? '' : 's'}`;
        const days = Math.floor(s / 86400);
        const hours = Math.floor((s % 86400) / 3600);
        const minutes = Math.floor((s % 3600) / 60);
        const parts = [];
        if (days) parts.push(`${days} day${days === 1 ? '' : 's'}`);
        if (hours) parts.push(`${hours} hour${hours === 1 ? '' : 's'}`);
        if (minutes || !parts.length) parts.push(`${minutes} minute${minutes === 1 ? '' : 's'}`);
        return parts.join(', ');
    }

    function fmtInt(n) { return Number(n).toLocaleString(); }

    function chartFont() {
        return { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text') };
    }

    function baseLayout(extra) {
        return Object.assign({
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            font: chartFont(),
            margin: { l: 45, r: 20, t: 10, b: 40 },
            showlegend: false,
        }, extra || {});
    }

    const PLOT_CONFIG = { displayModeBar: false, responsive: true };

    // ------------------------------------------------------------------
    // Entry point (called from my_stuff_tab.js renderSettingsUI)
    // ------------------------------------------------------------------

    window.loadMyCollections = function (opts) {
        const force = !!(opts && opts.force);
        if (mycCollections !== null && !force) return;  // already loaded this page-load
        if (!document.getElementById('myc-table-body')) return;
        const listUrl = force ? '/api/my/collections?fresh=1' : '/api/my/collections';
        const sourcesPromise = mycSources !== null
            ? Promise.resolve(mycSources)
            : fetch('/api/my/collections/upload/sources').then(r => r.json())
                .then(d => (d && d.sources) || []).catch(() => []);
        Promise.all([fetch(listUrl).then(r => r.json()), sourcesPromise])
            .then(([data, sources]) => {
                mycCollections = (data && data.collections) || [];
                mycSources = sources;
                setLoadingNote('');
                syncMyCollectionsMenuLabel();
                renderTable();
                renderProcessButton();
                // Deep link from the participation wizard
                // (#my_stuff/my-collections/upload): open the upload modal for
                // the platform the visitor picked on /participate/start.
                if (window.PENDING_SUBPAGE_ACTION === 'upload') {
                    window.PENDING_SUBPAGE_ACTION = null;
                    let wizPlatform = null;
                    try { wizPlatform = localStorage.getItem('fyp_funnel_platform'); } catch (e) { /* private mode */ }
                    let idx = (mycSources || []).findIndex(s => s.source_platform === wizPlatform);
                    if (idx === -1 && (mycSources || []).length) idx = 0;
                    if (idx !== -1) mycOpenUploadModal(idx);
                }
                // The persona is always visible below the table whenever any
                // data exists: combined when several collections are in the
                // Hub, the single one otherwise, the pending preview when only
                // an unprocessed upload exists.
                const autoOpen = opts && opts.open;
                if (autoOpen) {
                    openMyCollection(autoOpen);
                } else {
                    const ready = mycCollections.filter(c => c.status === 'ready');
                    const pending = mycCollections.filter(c => c.status === 'pending');
                    if (ready.length > 1) {
                        openMyCollection('combined');
                    } else if (ready.length === 1) {
                        openMyCollection(ready[0].collection_id);
                    } else if (pending.length) {
                        openMyCollection(pending[0].collection_id);
                    } else {
                        personaBox(false);
                    }
                }
            })
            .catch(() => {
                setLoadingNote('Could not load your collections. Try reloading the page.');
            });
    };

    // ------------------------------------------------------------------
    // Section visibility — a section with nothing in it is hidden entirely,
    // headline and blurb included.
    // ------------------------------------------------------------------

    function showSection(id, visible) {
        const el = document.getElementById(id);
        if (el) el.style.display = visible ? '' : 'none';
    }

    // The one-line placeholder above the sections; '' removes it.
    function setLoadingNote(text) {
        const el = document.getElementById('myc-loading');
        if (!el) return;
        el.textContent = text;
        el.style.display = text ? '' : 'none';
    }

    // The persona container, revealing or hiding its section as it goes.
    function personaBox(visible) {
        showSection('myc-section-persona', visible);
        return document.getElementById('myc-personality');
    }

    // Someone with nothing donated yet has no "collections" to speak of, so
    // the menu item states the invitation instead of naming an empty page.
    function syncMyCollectionsMenuLabel() {
        const label = document.querySelector('#my_stuff .js-my-collections-label');
        if (!label) return;
        const has = Array.isArray(mycCollections) && mycCollections.length > 0;
        const text = has ? 'My Collections' : 'Share your data';
        label.textContent = text;
        // Keep the mobile subnav copy in step.
        if (typeof _buildTabSubnavs === 'function') _buildTabSubnavs();
    }

    // ------------------------------------------------------------------
    // The collections table: one row per collection (ready, pending and
    // withdrawn alike), then one inviting "share row" per platform. Clicking
    // a collection row shows its persona below; clicking a share row opens
    // the upload modal.
    // ------------------------------------------------------------------

    const COL_COUNT = 8;

    function statusBadge(c) {
        if (c.status === 'pending') {
            return '<span class="myc-badge myc-badge--wait">awaiting processing</span>';
        }
        if (c.status === 'withdrawn') {
            const until = (c.restorable_until || '').slice(0, 10);
            return `<span class="myc-badge myc-badge--gone" title="Restorable until ${escapeHtml(until)}">deleted</span>`;
        }
        return '<span class="myc-badge myc-badge--ok">In the Hub</span>';
    }

    function coverageCell(c) {
        // Stage 2 fills pct_scraped / pct_annotated; until then (and for
        // pending/withdrawn rows) an em-dash.
        if (c.pct_scraped == null || c.pct_annotated == null) {
            return '<span class="myc-cell-muted">&mdash;</span>';
        }
        return `${Math.round(c.pct_scraped * 100)}% / ${Math.round(c.pct_annotated * 100)}%`;
    }

    function collectionRow(c) {
        const cid = escapeHtml(c.collection_id);
        const pending = c.status === 'pending';
        const withdrawn = c.status === 'withdrawn';
        if (pending) mycPendingMap[c.collection_id] = { raw_path: c.raw_path, filename: c.filename };

        const range = (c.first_event_ts && c.last_event_ts)
            ? `${escapeHtml(c.first_event_ts.slice(0, 10))} &rarr; ${escapeHtml(c.last_event_ts.slice(0, 10))}` : '';
        const added = c.ts_added_to_dataset ? escapeHtml(c.ts_added_to_dataset.slice(0, 10)) : '';
        const events = c.total_events != null ? fmtInt(c.total_events) : '';

        // Persona checkboxes: ready rows drive the combined persona below
        // (debounced live updates); pending/withdrawn rows cannot take part.
        const ticked = mycCheckedSet && mycCheckedSet.has(c.collection_id);
        const personaBoxHtml = withdrawn ? '' : (pending
            ? '<input type="checkbox" disabled title="Available once processed">'
            : `<input type="checkbox" class="myc-persona-check" data-cid="${cid}"
                   ${ticked ? 'checked' : ''} title="Include in your combined persona"
                   onclick="event.stopPropagation()" onchange="mycPersonaToggled(this)">`);

        const action = withdrawn
            ? `<button class="myc-actbtn myc-actbtn--accent" onclick="event.stopPropagation(); mycRestore('${cid}', this)">Restore</button>`
            : (pending
                ? `<button class="myc-actbtn myc-actbtn--danger" data-myc-cancel="${cid}"
                       onclick="event.stopPropagation(); mycDeletePending('${cid}', this)">Cancel</button>`
                : `<button class="myc-actbtn myc-actbtn--danger"
                       onclick="event.stopPropagation(); mycOpenWithdrawModal('${cid}')">Delete</button>`);

        const clickable = withdrawn ? '' : ` class="myc-row" onclick="openMyCollection('${cid}')"`;
        return `
            <tr${withdrawn ? ' class="myc-row-withdrawn"' : clickable} data-myc-select="${cid}">
                <td>
                    <div class="myc-cell-platform">${escapeHtml(platformLabel(c.source_platform))}</div>
                    <div class="myc-cell-id text-xs">${escapeHtml(c.display_id)}</div>
                </td>
                <td>${range}</td>
                <td class="myc-num">${events}</td>
                <td class="myc-num">${coverageCell(c)}</td>
                <td class="myc-cell-muted">${added}</td>
                <td>${statusBadge(c)}</td>
                <td class="myc-cell-center">${personaBoxHtml}</td>
                <td class="myc-cell-actions">${action}</td>
            </tr>`;
    }

    function shareRow(s, i) {
        const plat = escapeHtml(platformLabel(s.source_platform));
        return `
            <tr class="myc-share-row" onclick="mycOpenUploadModal(${i})">
                <td colspan="${COL_COUNT}">
                    <div class="myc-share-flex">
                        <span class="myc-share-label"><span class="myc-share-plus">+</span>Share your ${plat} data</span>
                        <a class="myc-howto-link text-xs" href="#"
                           onclick="event.preventDefault(); event.stopPropagation(); mycOpenHowtoModal('${escapeHtml(s.source_platform)}')">How do I get my data?</a>
                    </div>
                </td>
            </tr>`;
    }

    function renderPersonaFooter() {
        const el = document.getElementById('myc-persona-footer');
        if (!el) return;
        const ready = mycCollections.filter(c => c.status === 'ready');
        if (!ready.length) { el.textContent = ''; el.style.display = 'none'; return; }
        el.style.display = '';
        // A clicked row overrides the ticked-set view until the next toggle.
        if (mycActiveSelection && mycActiveSelection !== 'combined' && mycActiveSelection !== 'subset') {
            const c = mycCollections.find(x => x.collection_id === mycActiveSelection);
            if (c) {
                el.innerHTML = c.status === 'ready'
                    ? `Showing <strong>${escapeHtml(c.display_id)}</strong> only &mdash; tick the Persona boxes to build a combined persona.`
                    : 'Showing a preview of your unprocessed upload.';
                return;
            }
        }
        const n = mycCheckedSet ? mycCheckedSet.size : ready.length;
        el.innerHTML = n
            ? `Your persona below is built from the <strong>${n} ticked collection${n === 1 ? '' : 's'}</strong>.`
            : 'Tick at least one collection to build your combined persona.';
    }

    function renderTable() {
        const body = document.getElementById('myc-table-body');
        if (!body) return;
        Object.keys(mycPendingMap).forEach(k => delete mycPendingMap[k]);
        // The ticked set defaults to every ready collection and survives
        // re-renders; collections that left the list fall out of it.
        const readyIds = mycCollections.filter(c => c.status === 'ready').map(c => c.collection_id);
        if (mycCheckedSet === null) {
            mycCheckedSet = new Set(readyIds);
        } else {
            mycCheckedSet = new Set(readyIds.filter(id => mycCheckedSet.has(id)));
            if (!mycCheckedSet.size && readyIds.length) mycCheckedSet = new Set(readyIds);
        }
        const rows = mycCollections.map(collectionRow);
        (mycSources || []).forEach((s, i) => rows.push(shareRow(s, i)));
        body.innerHTML = rows.join('');
        showSection('myc-section-collections', rows.length > 0);
        renderPersonaFooter();
        markActiveRow();
    }

    // A Persona checkbox changed: update the set and footer immediately, then
    // fetch the subset persona after a short debounce so a burst of toggles
    // costs one request.
    window.mycPersonaToggled = function (cb) {
        if (!mycCheckedSet) mycCheckedSet = new Set();
        if (cb.checked) mycCheckedSet.add(cb.dataset.cid);
        else mycCheckedSet.delete(cb.dataset.cid);
        mycActiveSelection = 'subset';
        markActiveRow();
        renderPersonaFooter();
        if (mycPersonaDebounce) clearTimeout(mycPersonaDebounce);
        mycPersonaDebounce = setTimeout(() => {
            mycPersonaDebounce = null;
            refreshSubsetPersona();
        }, 300);
    };

    function refreshSubsetPersona() {
        const ids = Array.from(mycCheckedSet || []).sort();
        if (!ids.length) return;  // footer already says "tick at least one"
        const el = personaBox(true);
        if (!el) return;
        const seq = ++mycPersonaReqSeq;
        el.classList.add('myc-persona-loading');
        fetch(`/api/my/collections/combined/personality?collections=${encodeURIComponent(ids.join(','))}`)
            .then(r => r.json().then(data => ({ ok: r.ok, data })))
            .then(({ ok, data }) => {
                if (seq !== mycPersonaReqSeq) return;  // a newer request superseded this one
                el.classList.remove('myc-persona-loading');
                if (!ok) {
                    el.innerHTML = `<p class="text-sm" style="color: var(--color-text-muted);">${escapeHtml((data && data.error) || 'Something went wrong.')}</p>`;
                    return;
                }
                mycRenderPersonality(el, data);
                mycResizeCharts();
            })
            .catch(() => {
                if (seq !== mycPersonaReqSeq) return;
                el.classList.remove('myc-persona-loading');
                el.innerHTML = '<p class="text-sm" style="color: var(--color-text-muted);">Something went wrong while computing your persona. Try again in a moment.</p>';
            });
    }

    function markActiveRow() {
        document.querySelectorAll('#myc-table-body tr[data-myc-select]').forEach(tr => {
            tr.classList.toggle('myc-row-active',
                tr.getAttribute('data-myc-select') === mycActiveSelection);
        });
    }

    // ------------------------------------------------------------------
    // Personality view
    // ------------------------------------------------------------------

    window.openMyCollection = function (selection) {
        mycActiveSelection = selection;
        markActiveRow();
        renderPersonaFooter();
        const el = personaBox(true);
        if (!el) return;
        const seq = ++mycPersonaReqSeq;  // shared with refreshSubsetPersona: last request wins
        el.innerHTML = '<p class="text-sm" style="color: var(--color-text-muted);">Crunching your numbers&hellip;</p>';
        const pend = mycPendingMap[selection];
        const url = pend
            ? `/api/my/collections/pending/personality?raw_path=${encodeURIComponent(pend.raw_path)}&filename=${encodeURIComponent(pend.filename)}`
            : (selection === 'combined'
                ? '/api/my/collections/combined/personality'
                : `/api/my/collections/${encodeURIComponent(selection)}/personality`);
        fetch(url)
            .then(r => r.json().then(data => ({ ok: r.ok, data })))
            .then(({ ok, data }) => {
                if (seq !== mycPersonaReqSeq) return;  // superseded by a newer request
                if (!ok) {
                    if (data && data.rejected) {
                        // QA rejection: the file was removed server-side.
                        el.innerHTML = `<p class="text-sm" style="color: var(--color-text-muted);">${escapeHtml(data.error)}</p>`;
                        loadMyCollections({ force: true });
                        return;
                    }
                    el.innerHTML = `<p class="text-sm" style="color: var(--color-text-muted);">${escapeHtml((data && data.error) || 'Something went wrong.')}</p>`;
                    return;
                }
                if (data.pending) {
                    el.innerHTML = `<p class="text-sm" style="border: 1px solid var(--color-border); border-radius: 8px; padding: 8px 12px; color: var(--color-text-muted); margin: 0 0 12px 0;">
                        Preview computed from your raw upload. It hasn't been merged into the Hub yet;
                        the final numbers may shift slightly after processing.</p><div></div>`;
                    mycRenderPersonality(el.lastElementChild, data);
                    return;
                }
                mycRenderPersonality(el, data);
            })
            .catch(() => {
                if (seq !== mycPersonaReqSeq) return;
                el.innerHTML = '<p class="text-sm" style="color: var(--color-text-muted);">Something went wrong while computing your personality. Try again in a moment.</p>';
            });
    };

    function sectionCard(inner, maxWidth) {
        return `<div style="border: 1px solid var(--color-border); border-radius: 8px; padding: 16px 20px; background: var(--color-bg-elevated); flex: 1 1 340px; min-width: 300px; ${maxWidth ? `max-width: ${maxWidth};` : ''}">${inner}</div>`;
    }

    function cardTitle(text) {
        return `<h3 style="margin: 0 0 10px 0; font-size: 1rem;">${text}</h3>`;
    }

    // Public: render one personality bundle into any container. Used by this
    // page and by the Edit Collections modal (data_management.js).
    window.mycRenderPersonality = function (el, b) {
        const prefix = `myc-r${++mycRenderSeq}`;
        renderBundle(el, b, prefix);
        // Keep for theme re-renders; drop entries whose container left the DOM.
        for (let i = mycMounted.length - 1; i >= 0; i--) {
            if (!document.body.contains(mycMounted[i].el) || mycMounted[i].el === el) mycMounted.splice(i, 1);
        }
        mycMounted.push({ el, bundle: b, prefix });
    };

    function renderBundle(el, b, prefix) {
        const singlePlat = b.platforms && b.platforms.length === 1;
        const plat = singlePlat ? platformLabel(b.platforms[0]) : 'short-video';
        const html = [];

        // --- Persona moniker. It heads the scores card, so it sits right next
        //     to the five axes it is derived from. With no scores card to head
        //     it (too little signal), it stands on its own above the row.
        const persona = b.persona || {};
        const axes = persona.axes || {};
        const presentAxes = Object.keys(AXIS_LABELS).filter(a => axes[a] && axes[a].score != null);
        // The moniker is the card's own headline, so the line it used to share
        // the card with ("The five scores behind it") drops to a caption.
        const moniker = persona.statement
            ? `<p style="font-size: 1.15rem; font-weight: 700; line-height: 1.3; margin: 0 0 2px 0;">${escapeHtml(persona.statement)}</p>`
            : '';
        const monikerCaption = caption => moniker
            ? `${moniker}<p class="text-xs" style="color: var(--color-text-muted); margin: 0 0 12px 0;">${caption}</p>`
            : cardTitle(caption);

        if (moniker && !presentAxes.length) {
            html.push(`<div style="margin-bottom: 16px;">${moniker}</div>`);
        }

        html.push('<div style="display: flex; flex-wrap: wrap; gap: 14px;">');

        // --- Radar (or chips)
        if (presentAxes.length >= 3) {
            html.push(sectionCard(monikerCaption('The five scores behind it') + `<div id="${prefix}-chart-radar" style="height: 300px;"></div>`, '460px'));
        } else if (presentAxes.length) {
            const chips = presentAxes.map(a =>
                `<span style="border: 1px solid var(--color-border); border-radius: 16px; padding: 4px 12px; display: inline-block; margin: 0 6px 6px 0;">${AXIS_LABELS[a]}: <strong>${Math.round(axes[a].score)}</strong></span>`).join('');
            html.push(sectionCard(monikerCaption('Your scores') + `<div>${chips}</div><p class="text-xs" style="color: var(--color-text-faint); margin: 8px 0 0 0;">This donation doesn't carry enough signal for the full radar. Here's what we could measure.</p>`, '460px'));
        }

        // --- Your data vs the rest of the Hub
        if (b.comparisons && b.comparisons.length) {
            html.push(sectionCard(
                cardTitle('Your data compared to other collections in the Hub') + cohortRows(b.comparisons),
                '460px'));
        }

        // --- Hour of day (with per-platform overlay for multi-platform donors)
        if (b.hour_of_day) {
            const habitTitle = b.platform_habits
                ? platformHabitsTitle(b.platform_habits)
                : `Your golden hour: you were most alive around <strong>${escapeHtml(friendlyHourLabel(b.hour_of_day))}</strong>`;
            html.push(sectionCard(
                cardTitle(habitTitle) +
                `<div id="${prefix}-chart-hours" style="height: 300px;"></div>`, '460px'));
        }

        // --- Weekday
        if (b.weekday) {
            html.push(sectionCard(
                cardTitle(`<strong>${cap(escapeHtml(b.weekday.top))}</strong> was your biggest scrolling day`) +
                `<div id="${prefix}-chart-weekday" style="height: 260px;"></div>`, '460px'));
        }

        // --- Weekly rhythm
        if (b.weekly && b.weekly.series && b.weekly.series.length > 1) {
            const tw = b.weekly.top_week;
            const [ty, tn] = String(tw.week).split('-');
            html.push(sectionCard(
                cardTitle(`You never watched more than in week ${escapeHtml(tn)} of ${escapeHtml(ty)}`) +
                `<div id="${prefix}-chart-weekly" style="height: 260px;"></div>`, '700px'));
        }

        // --- Calendar heatmap
        if (b.calendar && b.calendar.days && b.calendar.days.length > 6) {
            const streak = b.calendar.longest_streak;
            const streakLine = streak >= 3
                ? `<p class="text-sm" style="margin: 8px 0 0 0; color: var(--color-text-muted);">Your longest streak: <strong>${fmtInt(streak)} days in a row</strong>.</p>`
                : '';
            html.push(sectionCard(
                cardTitle('Your time here, one square per day') +
                `<div id="${prefix}-chart-calendar" style="height: 200px;"></div>` + streakLine, '700px'));
        }

        // --- Doomscroll profile
        if (b.doomscroll) {
            const under = b.stats && b.stats.under_3s != null ? b.stats.under_3s : b.doomscroll.buckets.under_3s;
            const over = b.stats && b.stats.over_60s != null ? b.stats.over_60s : b.doomscroll.buckets.over_60s;
            html.push(sectionCard(
                cardTitle('Your scroll finger vs. your attention span') +
                `<div id="${prefix}-chart-doomscroll" style="height: 260px;"></div>` +
                `<p class="text-sm" style="margin: 8px 0 0 0; color: var(--color-text-muted);">You scrolled past <strong>${fmtInt(under)}</strong> videos in under three seconds, but <strong>${fmtInt(over)}</strong> kept you hooked for over a minute.</p>`,
                '460px'));
        }

        // --- Rewatch champion
        if (b.rewatch) {
            const r = b.rewatch;
            const title = r.desc ? `&ldquo;${escapeHtml(r.desc.length > 90 ? r.desc.slice(0, 90) + '…' : r.desc)}&rdquo;` : `A certain ${escapeHtml(platformLabel(r.platform))} video`;
            const author = r.author_name ? ` by <strong>${escapeHtml(r.author_name)}</strong>` : '';
            const link = r.url ? `<p class="text-sm" style="margin: 8px 0 0 0;"><a href="${escapeHtml(r.url)}" target="_blank" rel="noopener noreferrer" style="color: var(--color-link);">Dare to watch it one more time &rarr;</a></p>` : '';
            html.push(sectionCard(
                cardTitle('Do you remember this one?') +
                `<p style="margin: 0;">${title}${author}</p>` +
                `<p class="text-sm" style="margin: 6px 0 0 0; color: var(--color-text-muted);">You came back to it <strong>${fmtInt(r.count)} times</strong>.</p>` + link,
                '460px'));
        }

        // --- Search terms
        if (b.searches && b.searches.top_terms && b.searches.top_terms.length) {
            const rows = b.searches.top_terms.map(t =>
                `<span style="border: 1px solid var(--color-border); border-radius: 16px; padding: 3px 10px; display: inline-block; margin: 0 6px 6px 0;" class="text-sm">${escapeHtml(t.term)} <span style="color: var(--color-text-faint);">&times;${fmtInt(t.count)}</span></span>`).join('');
            html.push(sectionCard(
                cardTitle('Things you went looking for') +
                `<div>${rows}</div>` +
                `<p class="text-xs" style="color: var(--color-text-faint); margin: 8px 0 0 0;">Your ${fmtInt(b.searches.n_searches)} searches, ranked. No judgement.</p>`,
                '460px'));
        }

        // (The signature-emoji card was removed 2026-08-24; the bundle still
        // carries b.emoji for potential future use.)

        // --- Stat strip
        if (b.stats) {
            html.push(sectionCard(cardTitle("Let's look deeper into your life on the feed") + statStrip(b.stats, singlePlat ? plat : null), '700px'));
        }

        html.push('</div>');
        el.innerHTML = html.join('');
        renderCharts(b, el, prefix);
    }

    function statStrip(s, platOrNull) {
        const lines = [];
        const watching = platOrNull ? `watching ${escapeHtml(platOrNull)} videos` : 'watching videos';
        if (s.total_watch_time_s != null && s.total_watch_time_s > 0) {
            const pct = s.watch_time_percentile != null && s.watch_time_percentile >= 75
                ? ` That puts you in the top ${Math.max(1, Math.round(100 - s.watch_time_percentile))}% of watchers on the Hub.` : '';
            lines.push(`You spent <strong>${humanizeDuration(s.total_watch_time_s)}</strong> ${watching} between ${escapeHtml(s.first_date || '?')} and ${escapeHtml(s.last_date || '?')}.${pct}`);
        } else if (s.first_date) {
            lines.push(`Your data covers ${escapeHtml(s.first_date)} to ${escapeHtml(s.last_date)}: <strong>${fmtInt(s.active_days)}</strong> active days.`);
        }
        if (s.n_videos) lines.push(`You watched <strong>${fmtInt(s.n_videos)}</strong> videos.`);
        if (s.n_sessions) {
            const longest = (s.longest_session_s || 0) >= 60
                ? ` The longest lasted <strong>${humanizeDuration(s.longest_session_s)}</strong>.` : '';
            lines.push(`You completed <strong>${fmtInt(s.n_sessions)}</strong> sessions.${longest}`);
        }
        if (s.n_likes) lines.push(`You handed out <strong>${fmtInt(s.n_likes)}</strong> likes.`);
        if (s.n_comments) lines.push(`You made <strong>${fmtInt(s.n_comments)}</strong> comments.`);
        if (s.n_posts) lines.push(`You posted <strong>${fmtInt(s.n_posts)}</strong> videos of your own.`);
        return lines.map(l => `<p class="text-sm" style="margin: 0 0 6px 0;">${l}</p>`).join('');
    }

    function friendlyHourLabel(h) {
        return h.peak_label || `${h.peak_hour}:00`;
    }

    // "way more than most" phrasing from your-value / cohort-median.
    function ratioPhrase(ratio) {
        if (ratio == null) return '';
        if (ratio >= 2) return 'way more than most';
        if (ratio >= 1.3) return 'more than most';
        if (ratio > 0.75) return 'right in the pack';
        if (ratio > 0.4) return 'less than most';
        return 'way less than most';
    }

    function cohortRows(comparisons) {
        return comparisons.map(c => {
            // Value-scaled track with the median pinned to the CENTER: the
            // scale runs 0 .. 2x median, so distances are proportional and a
            // value past double the median pins at the right edge.
            let track = '';
            if (c.cohort_median > 0) {
                const ownPos = Math.max(1, Math.min(99, c.own / (2 * c.cohort_median) * 100));
                track = `
                <div style="position: relative; height: 6px; border-radius: 3px; background: var(--color-border); margin: 6px 0 2px 0;">
                    <div style="position: absolute; left: 50%; top: -2px; width: 2px; height: 10px; background: var(--color-text-faint);" title="median"></div>
                    <div style="position: absolute; left: ${ownPos}%; top: -3px; width: 12px; height: 12px; margin-left: -6px; border-radius: 50%; background: var(--color-accent);" title="you"></div>
                </div>`;
            }
            const phrase = ratioPhrase(c.ratio);
            return `
                <div style="margin-bottom: 12px;">
                    <div class="text-sm"><strong>${escapeHtml(c.label)}</strong>${phrase ? ': ' + escapeHtml(phrase) : ''}</div>
                    <div class="text-xs" style="color: var(--color-text-muted);">you: <strong>${fmtInt(Math.round(c.own * 10) / 10)}</strong> &middot; median: ${fmtInt(Math.round(c.cohort_median * 10) / 10)}</div>
                    ${track}
                </div>`;
        }).join('');
    }

    function platformHabitsTitle(habits) {
        const segWords = { morning: 'in the morning', afternoon: 'in the afternoon',
                           evening: 'in the evening', night: 'in the middle of the night' };
        const parts = habits.map(h =>
            `<strong>${escapeHtml(platformLabel(h.platform))}</strong> ${escapeHtml(segWords[h.top_segment] || h.top_segment)}`);
        const distinct = new Set(habits.map(h => h.top_segment)).size > 1;
        if (distinct) return `Two apps, two lives: ${parts.join(', ')}`;
        return `All your feeds peak ${escapeHtml(segWords[habits[0].top_segment] || habits[0].top_segment)}`;
    }

    // ------------------------------------------------------------------
    // Self-serve donation upload
    // ------------------------------------------------------------------

    const UPLOAD_HINTS = {
        tiktok: 'Choose your extracted <strong>user_data_tiktok.json</strong>. If TikTok gave you a .zip, unzip it first and pick the .json inside. You can review and remove items before anything is uploaded.',
        instagram: 'Choose your Instagram export <strong>.zip</strong>. It is opened in your browser so you can review and remove items — only the activity you approve leaves your computer.',
        youtube: 'Choose your Google Takeout <strong>.zip</strong>. It is opened in your browser so you can review and remove items — only the activity you approve leaves your computer.',
    };

    // Per-platform "how do I get my data" guide modal. The content blocks
    // live in my_stuff.html; the video iframe is lazy-loaded from data-src on
    // open and unloaded on close so playback stops with the modal.
    window.mycOpenHowtoModal = function (platform) {
        const modal = document.getElementById('myc-howto-modal');
        if (!modal) return;
        let found = false;
        modal.querySelectorAll('.myc-howto-body').forEach(body => {
            const active = body.id === `myc-howto-${platform}`;
            body.style.display = active ? 'block' : 'none';
            if (active) {
                found = true;
                const frame = body.querySelector('iframe[data-src]');
                if (frame && !frame.getAttribute('src')) frame.src = frame.dataset.src;
            }
        });
        if (found) modal.style.display = 'block';
    };

    window.mycCloseHowtoModal = function (event) {
        if (event && event.target !== event.currentTarget) return;
        const modal = document.getElementById('myc-howto-modal');
        if (!modal) return;
        modal.style.display = 'none';
        modal.querySelectorAll('iframe[data-src]').forEach(f => f.removeAttribute('src'));
    };

    // The guide's Continue button: straight from "how do I get my data" into
    // that platform's upload modal.
    window.mycHowtoContinue = function (platform) {
        mycCloseHowtoModal();
        const idx = (mycSources || []).findIndex(s => s.source_platform === platform);
        if (idx !== -1) mycOpenUploadModal(idx);
    };

    window.mycOpenUploadModal = function (sourceIndex) {
        const source = mycSources && mycSources[sourceIndex];
        if (!source) return;
        mycUpload = { source, file: null, review: null };
        mycExitReviewMode();  // reset any leftover review state from a prior open
        const modal = document.getElementById('myc-upload-modal');
        document.getElementById('myc-upload-title').textContent = `Add your ${platformLabel(source.source_platform)} data`;
        document.getElementById('myc-upload-hint').innerHTML = UPLOAD_HINTS[source.source_platform]
            || `Upload your ${escapeHtml(platformLabel(source.source_platform))} export (${source.accepted_upload_suffixes.map(escapeHtml).join(' or ')}).`;
        const input = document.getElementById('myc-upload-file');
        input.value = '';
        input.accept = source.accepted_upload_suffixes.join(',');
        input.onchange = () => mycFileChosen(input.files && input.files[0]);
        document.getElementById('myc-upload-filebox').innerHTML =
            '<span class="text-sm" style="color: var(--color-text-muted);">Click to choose your file&hellip;</span>';
        document.getElementById('myc-upload-status').textContent = '';
        document.getElementById('myc-upload-submit').disabled = true;
        modal.style.display = 'block';
    };

    window.mycCloseUploadModal = function (event) {
        if (event && event.target !== event.currentTarget) return;
        document.getElementById('myc-upload-modal').style.display = 'none';
        mycExitReviewMode();
        mycUpload = null;  // also frees the parsed review model
    };

    // Switch the upload modal into review mode: the parsed export is shown as
    // per-section cards (DonationReview) and the submit button becomes
    // "Share N items", disabled while the pruned file would fail the server's
    // minimum-rows gate.
    function mycEnterReviewMode(model) {
        const content = document.querySelector('#myc-upload-modal .modal-content');
        content.classList.add('myc-review-mode');
        document.getElementById('myc-upload-filebox').style.display = 'none';
        document.getElementById('myc-upload-hint').style.display = 'none';
        document.getElementById('myc-review-back').style.display = '';
        const holder = document.getElementById('myc-review');
        holder.style.display = 'block';
        const submit = document.getElementById('myc-upload-submit');
        const consentSrc = document.getElementById('myc-consent-source');
        DonationReview.renderReview(holder, model, {
            consentHtml: consentSrc ? consentSrc.innerHTML : '',
            onChange: ({ totalKept, viabilityError, consentOk }) => {
                const warn = document.getElementById('myc-review-warning');
                warn.textContent = viabilityError || '';
                // Share requires both a viable donation and the ticked
                // read-and-understood consent box.
                submit.disabled = !!viabilityError || !consentOk;
                submit.title = (!viabilityError && !consentOk)
                    ? 'Tick the consent box above to enable sharing.' : '';
                submit.textContent = `Share ${totalKept.toLocaleString()} items`;
            },
        });
    }

    window.mycExitReviewMode = function () {
        const content = document.querySelector('#myc-upload-modal .modal-content');
        if (content) content.classList.remove('myc-review-mode');
        const holder = document.getElementById('myc-review');
        if (holder) { holder.innerHTML = ''; holder.style.display = 'none'; }
        const back = document.getElementById('myc-review-back');
        if (back) back.style.display = 'none';
        const warn = document.getElementById('myc-review-warning');
        if (warn) warn.textContent = '';
        const box = document.getElementById('myc-upload-filebox');
        if (box) {
            box.style.display = '';
            box.innerHTML = '<span class="text-sm" style="color: var(--color-text-muted);">Click to choose your file&hellip;</span>';
        }
        const hint = document.getElementById('myc-upload-hint');
        if (hint) hint.style.display = '';
        const input = document.getElementById('myc-upload-file');
        if (input) input.value = '';
        const submit = document.getElementById('myc-upload-submit');
        if (submit) { submit.disabled = true; submit.textContent = 'Upload'; submit.title = ''; }
        const status = document.getElementById('myc-upload-status');
        if (status) status.textContent = '';
        if (mycUpload) { mycUpload.review = null; mycUpload.file = null; }
    };

    async function mycFileChosen(file) {
        if (!file || !mycUpload) return;
        const status = document.getElementById('myc-upload-status');
        const submit = document.getElementById('myc-upload-submit');
        const box = document.getElementById('myc-upload-filebox');
        submit.disabled = true;
        box.innerHTML = `<span class="text-sm">${escapeHtml(file.name)}</span>`;

        const source = mycUpload.source;
        if (source.review && window.DonationReview) {
            // Review-before-upload: parse the export locally and open the
            // review step. On ANY failure the upload is blocked — an
            // unreviewed original file is never sent (privacy invariant).
            status.textContent = 'Reading your export in your browser…';
            let model;
            try {
                model = await DonationReview.buildReviewModel(file, source,
                    msg => { status.textContent = msg; });
            } catch (err) {
                if (!mycUpload) return;  // modal closed mid-parse
                console.warn('donation review failed:', err);
                mycUpload.file = null;
                mycUpload.review = null;
                status.textContent = (err && err.code === 'no_members')
                    ? `We couldn't find any ${platformLabel(source.source_platform)} activity in this file. Is it the right export?`
                    : "We couldn't open this file for review in your browser, so nothing was uploaded. Please reload the page and try again, or use a different browser.";
                return;
            }
            if (!mycUpload) return;  // modal closed mid-parse
            mycUpload.review = model;
            status.textContent = '';
            mycEnterReviewMode(model);
            return;
        }

        // Platforms without a review manifest (none today) fall back to the
        // plain upload path.
        mycUpload.file = file;
        status.textContent = 'Ready.';
        submit.disabled = false;
    }

    window.mycSubmitUpload = async function () {
        if (!mycUpload || (!mycUpload.file && !mycUpload.review)) return;
        const status = document.getElementById('myc-upload-status');
        const submit = document.getElementById('myc-upload-submit');
        submit.disabled = true;

        let uploadFile = mycUpload.file;
        const reviewed = !!mycUpload.review;
        if (reviewed) {
            status.textContent = 'Preparing your file…';
            try {
                uploadFile = await DonationReview.buildPrunedFile(mycUpload.review);
            } catch (err) {
                console.warn('pruned-file build failed:', err);
                if (!mycUpload) return;
                status.textContent = "We couldn't prepare your reviewed file, so nothing was uploaded. Please try again.";
                submit.disabled = false;
                return;
            }
            if (!mycUpload) return;  // modal closed mid-build
        }

        status.textContent = 'Uploading…';
        const fd = new FormData();
        fd.append('files', uploadFile, uploadFile.name);
        fd.append('raw_path', mycUpload.source.raw_path);
        if (reviewed) fd.append('client_review', '1');
        try {
            fd.append('tz', Intl.DateTimeFormat().resolvedOptions().timeZone || '');
        } catch (e) { /* no tz — the pipeline infers one */ }
        fetch('/api/my/collections/upload', { method: 'POST', body: fd })
            .then(r => r.json().then(data => ({ ok: r.ok, data })))
            .then(({ ok, data }) => {
                if (!ok) {
                    status.textContent = (data && data.error) || 'The upload failed. Please try again.';
                    submit.disabled = false;
                    return;
                }
                const first = data.collections && data.collections[0];
                mycCloseUploadModal();
                // Instant gratification + QA: open the preview right away.
                loadMyCollections({ force: true, open: first ? first.collection_id : undefined });
            })
            .catch(() => {
                status.textContent = 'The upload failed. Please try again.';
                submit.disabled = false;
            });
    };

    // Two-step in-place confirmation (no native confirm/alert dialogs): the
    // first click arms the button, the second within 5 seconds cancels the
    // pending donation and deletes the uploaded file.
    window.mycDeletePending = function (cid, btn) {
        const pend = mycPendingMap[cid];
        if (!pend || !btn) return;
        if (btn.dataset.armed !== '1') {
            btn.dataset.armed = '1';
            btn.textContent = 'Really cancel?';
            btn.style.background = 'var(--color-danger)';
            btn.style.color = '#fff';
            setTimeout(() => {
                if (btn.isConnected && btn.dataset.armed === '1') {
                    btn.dataset.armed = '';
                    btn.textContent = 'Cancel';
                    btn.style.background = 'none';
                    btn.style.color = 'var(--color-danger)';
                }
            }, 5000);
            return;
        }
        btn.disabled = true;
        btn.textContent = 'Removing…';
        fetch('/api/my/collections/pending/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ raw_path: pend.raw_path, filename: pend.filename }),
        })
            .then(r => r.json().then(data => ({ ok: r.ok, data })))
            .then(({ ok, data }) => {
                if (!ok) {
                    btn.disabled = false;
                    btn.textContent = 'Cancel';
                    btn.dataset.armed = '';
                    const p = personaBox(true);
                    if (p) p.innerHTML = `<p class="text-sm" style="color: var(--color-text-muted);">${escapeHtml((data && data.error) || 'Could not remove the donation. Try again.')}</p>`;
                    return;
                }
                if (mycActiveSelection === cid) {
                    mycActiveSelection = null;
                    const p = personaBox(false);
                    if (p) p.innerHTML = '';
                }
                loadMyCollections({ force: true });
            })
            .catch(() => {
                btn.disabled = false;
                btn.textContent = 'Cancel';
                btn.dataset.armed = '';
            });
    };

    // ------------------------------------------------------------------
    // Data withdrawal (delete a processed collection, 30-day restore window)
    // ------------------------------------------------------------------

    let mycWithdrawCid = null;

    window.mycOpenWithdrawModal = function (cid) {
        mycWithdrawCid = cid;
        document.getElementById('myc-withdraw-cid-label').textContent = cid;
        document.getElementById('myc-withdraw-cid-code').textContent = cid;
        const input = document.getElementById('myc-withdraw-confirm');
        input.value = '';
        document.getElementById('myc-withdraw-status').textContent = '';
        mycWithdrawInputChanged();
        document.getElementById('myc-withdraw-modal').style.display = 'block';
        input.focus();
    };

    window.mycCloseWithdrawModal = function (event) {
        if (event && event.target !== event.currentTarget) return;
        document.getElementById('myc-withdraw-modal').style.display = 'none';
        mycWithdrawCid = null;
    };

    window.mycWithdrawInputChanged = function () {
        const match = document.getElementById('myc-withdraw-confirm').value.trim() === mycWithdrawCid;
        const btn = document.getElementById('myc-withdraw-submit');
        btn.disabled = !match;
        btn.style.opacity = match ? '1' : '0.5';
    };

    window.mycSubmitWithdraw = function () {
        if (!mycWithdrawCid) return;
        const cid = mycWithdrawCid;
        const status = document.getElementById('myc-withdraw-status');
        const btn = document.getElementById('myc-withdraw-submit');
        btn.disabled = true;
        status.textContent = 'Removing your data from the dataset…';
        fetch(`/api/my/collections/${encodeURIComponent(cid)}/withdraw`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ confirm_id: document.getElementById('myc-withdraw-confirm').value.trim() }),
        })
            .then(r => r.json().then(data => ({ ok: r.ok, data })))
            .then(({ ok, data }) => {
                if (!ok) {
                    status.textContent = (data && data.error) || 'The deletion could not be started. Try again.';
                    mycWithdrawInputChanged();
                    return;
                }
                mycCloseWithdrawModal();
                if (mycActiveSelection === cid) {
                    mycActiveSelection = null;
                    const p = personaBox(false);
                    if (p) p.innerHTML = '';
                }
                loadMyCollections({ force: true });
            })
            .catch(() => {
                status.textContent = 'The deletion could not be started. Try again.';
                mycWithdrawInputChanged();
            });
    };

    window.mycRestore = function (cid, btn) {
        btn.disabled = true;
        btn.textContent = 'Restoring…';
        fetch(`/api/my/collections/${encodeURIComponent(cid)}/restore`, { method: 'POST' })
            .then(r => r.json().then(data => ({ ok: r.ok, data })))
            .then(({ ok, data }) => {
                if (!ok) {
                    btn.disabled = false;
                    btn.textContent = 'Restore';
                    const p = personaBox(true);
                    if (p) p.innerHTML = `<p class="text-sm" style="color: var(--color-text-muted);">${escapeHtml((data && data.error) || 'Could not restore. Try again.')}</p>`;
                    return;
                }
                // The donation is a pending upload again — open its preview.
                loadMyCollections({ force: true, open: cid });
            })
            .catch(() => {
                btn.disabled = false;
                btn.textContent = 'Restore';
            });
    };

    // ------------------------------------------------------------------
    // Process new collections
    // ------------------------------------------------------------------

    function renderProcessButton() {
        const el = document.getElementById('myc-process');
        if (!el) return;
        const nPending = Object.keys(mycPendingMap).length;
        if (!nPending) { el.innerHTML = ''; return; }
        el.innerHTML = `
            <button id="myc-process-btn" class="btn-primary btn-compact" onclick="mycStartProcess()">
                Process new collections (${nPending})
            </button>
            <span id="myc-process-status" class="text-sm" style="color: var(--color-text-muted); margin-left: 10px;"></span>`;
    }

    window.mycStartProcess = function () {
        const btn = document.getElementById('myc-process-btn');
        if (btn) btn.disabled = true;
        fetch('/api/my/collections/process', { method: 'POST' })
            .then(r => r.json().then(data => ({ ok: r.ok, data })))
            .then(({ ok }) => {
                // 409 = already running elsewhere: watch it all the same.
                mycWatchProcess();
            })
            .catch(() => { if (btn) btn.disabled = false; });
    };

    function mycWatchProcess() {
        if (mycProcessPoll) return;  // already watching
        const statusEl = () => document.getElementById('myc-process-status');
        let sawRunning = false;
        mycProcessPoll = setInterval(() => {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    const ir = data && data.ingest_refresh;
                    if (!ir) return;
                    const running = ir.state === 'running' || ir.state === 'stopping';
                    const el = statusEl();
                    if (running) {
                        sawRunning = true;
                        if (el) {
                            const pct = ir.progress && ir.progress.percent != null ? `${ir.progress.percent}% ` : '';
                            const msg = (ir.progress && ir.progress.message) || 'working';
                            el.textContent = `Processing… ${pct}${msg}`;
                        }
                        return;
                    }
                    if (!sawRunning) return;  // hasn't started yet
                    clearInterval(mycProcessPoll);
                    mycProcessPoll = null;
                    if (el) {
                        el.textContent = ir.last_run_outcome === 'Fail'
                            ? 'Processing hit a problem. The research team can see the details.'
                            : 'Done! Your collection is now part of the Hub.';
                    }
                    loadMyCollections({ force: true, open: mycActiveSelection || undefined });
                })
                .catch(() => { /* transient; keep polling */ });
        }, 3000);
    }

    // ------------------------------------------------------------------
    // Charts
    // ------------------------------------------------------------------

    function renderCharts(b, rootEl, prefix) {
        const accent = getCSSVar('--color-accent');
        const grid = getCSSVar('--chart-grid');
        const byId = id => rootEl.querySelector(`#${prefix}-${id}`);

        // Radar
        const radarEl = byId('chart-radar');
        if (radarEl && b.persona && b.persona.axes) {
            const axes = b.persona.axes;
            const present = Object.keys(AXIS_LABELS).filter(a => axes[a] && axes[a].score != null);
            const theta = present.map(a => AXIS_LABELS[a]);
            const r = present.map(a => axes[a].score);
            Plotly.newPlot(radarEl, [{
                type: 'scatterpolar',
                r: r.concat([r[0]]),
                theta: theta.concat([theta[0]]),
                fill: 'toself',
                fillcolor: hexToRgba(accent, 0.25),
                line: { color: accent },
                hovertemplate: '%{theta}: %{r:.0f}<extra></extra>',
            }], baseLayout({
                polar: {
                    bgcolor: 'rgba(0,0,0,0)',
                    radialaxis: { range: [0, 100], showticklabels: false, gridcolor: grid },
                    angularaxis: { gridcolor: grid, tickfont: chartFont() },
                },
                margin: { l: 60, r: 60, t: 30, b: 30 },
            }), PLOT_CONFIG);
        }

        // Hour-of-day polar (24 spokes) — single-platform bars, or one line
        // per platform (each a share of ITS OWN plays) for multi-platform donors.
        const hoursEl = byId('chart-hours');
        if (hoursEl && b.hour_of_day) {
            const counts = b.hour_of_day.counts;
            const labels = [];
            for (let h = 0; h < 24; h++) {
                labels.push(h === 0 ? 'midnight' : h === 12 ? 'noon' : h < 12 ? `${h} AM` : `${h - 12} PM`);
            }
            const byPlat = b.hour_of_day.by_platform;
            const traces = byPlat
                ? Object.entries(byPlat).map(([plat, shares]) => {
                    // Each curve is scaled to its own peak so a small donation's
                    // rhythm is as visible as a huge one's; hover keeps the true share.
                    const peak = Math.max(...shares, 0.0001);
                    const rel = shares.map(s => s / peak);
                    return {
                        type: 'scatterpolar',
                        r: rel.concat([rel[0]]),
                        theta: labels.map((_, h) => h * 15).concat([0]),
                        mode: 'lines',
                        name: platformLabel(plat),
                        line: { color: platformColor(plat), shape: 'spline' },
                        fill: 'toself', fillcolor: hexToRgba(platformColor(plat), 0.12),
                        customdata: labels.map((l, h) => [l, (shares[h] * 100).toFixed(1)]).concat([[labels[0], (shares[0] * 100).toFixed(1)]]),
                        hovertemplate: `${platformLabel(plat)} %{customdata[0]}: %{customdata[1]}% of its plays<extra></extra>`,
                    };
                })
                : [{
                    type: 'barpolar',
                    r: counts,
                    theta: labels.map((_, h) => h * 15),
                    width: 13,
                    marker: { color: accent },
                    hovertemplate: '%{customdata}: %{r} videos<extra></extra>',
                    customdata: labels,
                }];
            Plotly.newPlot(hoursEl, traces, baseLayout({
                showlegend: !!byPlat,
                legend: { orientation: 'h', y: -0.1, font: chartFont() },
                polar: {
                    bgcolor: 'rgba(0,0,0,0)',
                    radialaxis: { showticklabels: false, gridcolor: grid },
                    angularaxis: {
                        direction: 'clockwise', rotation: 90,
                        tickvals: [0, 90, 180, 270],
                        ticktext: ['midnight', '6 AM', 'noon', '6 PM'],
                        gridcolor: grid, tickfont: chartFont(),
                    },
                },
                margin: { l: 40, r: 40, t: 30, b: 30 },
            }), PLOT_CONFIG);
        }

        // Weekday bar — grouped per platform for multi-platform donors.
        const wdEl = byId('chart-weekday');
        if (wdEl && b.weekday) {
            const counts = b.weekday.counts || {};
            const wdByPlat = b.weekday.by_platform;
            const traces = wdByPlat
                ? Object.entries(wdByPlat).map(([plat, c]) => {
                    // Shares of each platform's own plays, so both rhythms read
                    // side by side regardless of donation size.
                    const total = Math.max(1, WEEKDAYS.reduce((s, d) => s + (c[d] || 0), 0));
                    return {
                        type: 'bar',
                        x: WEEKDAYS.map(cap),
                        y: WEEKDAYS.map(d => (c[d] || 0) / total * 100),
                        name: platformLabel(plat),
                        marker: { color: platformColor(plat) },
                        customdata: WEEKDAYS.map(d => c[d] || 0),
                        hovertemplate: `${platformLabel(plat)} %{x}: %{y:.1f}%% of its plays (%{customdata})<extra></extra>`,
                    };
                })
                : [{
                    type: 'bar',
                    x: WEEKDAYS.map(cap),
                    y: WEEKDAYS.map(d => counts[d] || 0),
                    marker: { color: WEEKDAYS.map(d => d === b.weekday.top ? accent : hexToRgba(accent, 0.45)) },
                    hovertemplate: '%{x}: %{y} videos<extra></extra>',
                }];
            Plotly.newPlot(wdEl, traces, baseLayout({
                barmode: 'group',
                showlegend: !!wdByPlat,
                legend: { orientation: 'h', y: -0.25, font: chartFont() },
                xaxis: { tickfont: chartFont() },
                yaxis: { gridcolor: grid, tickfont: chartFont(), title: { text: wdByPlat ? "% of that platform's videos" : 'videos viewed', font: chartFont() } },
            }), PLOT_CONFIG);
        }

        // Weekly rhythm line
        const wkEl = byId('chart-weekly');
        if (wkEl && b.weekly) {
            const s = b.weekly.series;
            Plotly.newPlot(wkEl, [{
                type: 'scatter', mode: 'lines',
                x: s.map(d => d.week),
                y: s.map(d => d.count),
                line: { color: accent, shape: 'spline', smoothing: 0.6 },
                fill: 'tozeroy', fillcolor: hexToRgba(accent, 0.15),
                hovertemplate: 'week %{x}: %{y} videos<extra></extra>',
            }], baseLayout({
                // 'YYYY-WW' strings must stay categorical — Plotly's date parser
                // mangles them into month positions otherwise.
                xaxis: { type: 'category', tickfont: chartFont(), nticks: 10 },
                yaxis: { gridcolor: grid, tickfont: chartFont(), title: { text: 'videos per week', font: chartFont() } },
            }), PLOT_CONFIG);
        }

        // Calendar heatmap: weekday rows x week columns
        const calEl = byId('chart-calendar');
        if (calEl && b.calendar) {
            const byDate = {};
            b.calendar.days.forEach(d => { byDate[d.date] = d.count; });
            const first = new Date(b.calendar.days[0].date);
            const last = new Date(b.calendar.days[b.calendar.days.length - 1].date);
            const weeks = [];   // x labels (start-of-week dates)
            const z = [[], [], [], [], [], [], []];  // 7 rows: Mon..Sun
            const cursor = new Date(first);
            cursor.setDate(cursor.getDate() - ((cursor.getDay() + 6) % 7));  // back to Monday
            while (cursor <= last) {
                weeks.push(cursor.toISOString().slice(0, 10));
                for (let dow = 0; dow < 7; dow++) {
                    const day = new Date(cursor);
                    day.setDate(day.getDate() + dow);
                    const key = day.toISOString().slice(0, 10);
                    z[dow].push(byDate[key] || 0);
                }
                cursor.setDate(cursor.getDate() + 7);
            }
            Plotly.newPlot(calEl, [{
                type: 'heatmap',
                z: z, x: weeks, y: WEEKDAYS.map(d => cap(d).slice(0, 3)),
                colorscale: [[0, getCSSVar('--chart-heatmap-mid') || 'rgba(128,128,128,0.15)'], [1, accent]],
                showscale: false, xgap: 2, ygap: 2,
                hovertemplate: 'week of %{x}, %{y}: %{z} videos<extra></extra>',
            }], baseLayout({
                xaxis: { tickfont: chartFont(), nticks: 8, showgrid: false },
                yaxis: { tickfont: chartFont(), autorange: 'reversed', showgrid: false },
                margin: { l: 40, r: 10, t: 5, b: 30 },
            }), PLOT_CONFIG);
        }

        // Doomscroll histogram
        const dsEl = byId('chart-doomscroll');
        if (dsEl && b.doomscroll) {
            const bk = b.doomscroll.buckets;
            const labels = ['< 3 s', '3–10 s', '10–30 s', '30–60 s', '> 60 s'];
            const vals = [bk.under_3s, bk['3_10s'], bk['10_30s'], bk['30_60s'], bk.over_60s];
            Plotly.newPlot(dsEl, [{
                type: 'bar', x: labels, y: vals,
                marker: { color: accent },
                hovertemplate: '%{x}: %{y} videos<extra></extra>',
            }], baseLayout({
                xaxis: { tickfont: chartFont(), title: { text: 'how long you stayed', font: chartFont() } },
                yaxis: { gridcolor: grid, tickfont: chartFont() },
            }), PLOT_CONFIG);
        }
    }

    function hexToRgba(hex, alpha) {
        const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex.trim());
        if (!m) return hex;  // token was already rgb()/named — use as-is
        return `rgba(${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}, ${alpha})`;
    }

    // Charts rendered while the tab was hidden come out at Plotly's fallback
    // size and overflow their cards. my_stuff_tab.js calls this whenever the
    // My Collections page becomes visible again.
    window.mycResizeCharts = function () {
        const resizeAll = () => {
            document.querySelectorAll('#my-stuff-page-my-collections .js-plotly-plot').forEach(el => {
                if (el.offsetParent !== null) {
                    try { Plotly.Plots.resize(el); } catch (e) { /* not a live plot */ }
                }
            });
        };
        resizeAll();  // immediately, in case rAF is throttled
        window.requestAnimationFrame(resizeAll);  // and again once layout settles
    };

    // Re-render every mounted personality view with the new token colors.
    window.addEventListener('theme-changed', () => {
        for (let i = mycMounted.length - 1; i >= 0; i--) {
            const m = mycMounted[i];
            if (!document.body.contains(m.el)) { mycMounted.splice(i, 1); continue; }
            renderBundle(m.el, m.bundle, m.prefix);
        }
    });
})();
