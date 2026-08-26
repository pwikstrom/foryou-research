// Guided tour of the Hub for new participants. No library: a step array, a
// highlight ring over the current target, and a positioned card with caret
// navigation in its top-right corner. Two variants share the machinery:
//   'example' — first visit (wizard step 2): walks the analysis tabs using
//               the admin-chosen demo collection (window.DEMO_COLLECTION,
//               Admin -> Site Settings, part of the default study).
//   'real'    — re-offered when the first annotated batch lands: same walk,
//               framed around the user's own data.
// Entry points: the #tour URL hash, the server-rendered HUB_TOUR_FLAGS
// (hub_tour_pending / hub_tour_real_data_pending user settings), and the
// "Take the guided tour" links in the help modal and getting-started panel.
// Every analysis step feature-detects: no permission, no demo collection, or
// a study without that tab's data means the step is skipped, never broken.
(function () {
    'use strict';

    let state = null;  // {variant, idx, steps, els:{backdrop, ring, card}}

    function navigate(tabId, pageId) {
        if (typeof _navigateToTabPage === 'function') {
            _navigateToTabPage(tabId, pageId || null);
        }
    }

    function can(perm) {
        return window.USER_IS_ADMIN
            || (Array.isArray(window.USER_PERMS) && window.USER_PERMS.includes(perm));
    }

    function tabUsable(tabId, perm) {
        if (!can(perm)) return false;
        if (!document.getElementById(tabId)) return false;
        if (typeof window.isTabDisabledForActiveStudy === 'function'
            && window.isTabDisabledForActiveStudy(tabId)) return false;
        return true;
    }

    function waitFor(pred, timeoutMs) {
        return new Promise((resolve) => {
            const t0 = Date.now();
            (function poll() {
                let ok = false;
                try { ok = !!pred(); } catch (e) { /* keep polling */ }
                if (ok) return resolve(true);
                if (Date.now() - t0 > (timeoutMs || 8000)) return resolve(false);
                setTimeout(poll, 200);
            })();
        });
    }

    // Make sure the site-wide default study is active and loaded: the demo
    // collection is defined inside it.
    async function ensureDefaultStudy() {
        if (!window.studyState) return;
        try {
            await window.studyState.ready;
            if (window.DEFAULT_STUDY && window.studyState.current !== window.DEFAULT_STUDY
                && typeof setActiveStudy === 'function') {
                await setActiveStudy(window.DEFAULT_STUDY);
            }
        } catch (e) { /* the tour still walks the tabs */ }
    }

    // Compact persona illustration for the "right after sharing" step. Just a
    // sketch of the real thing: radar, weekday bars, a stat line.
    const PERSONA_SKETCH = `
        <div class="hub-tour-sketch">
            <svg viewBox="0 0 300 130" aria-hidden="true">
                <g stroke="var(--color-border)" fill="none">
                    <polygon points="65,14 116,44 103,102 27,102 14,44"/>
                    <polygon points="65,36 98,55 90,92 40,92 32,55" opacity=".55"/>
                </g>
                <polygon points="65,22 106,49 88,96 38,88 26,51"
                    fill="var(--color-accent)" fill-opacity=".25"
                    stroke="var(--color-accent)" stroke-width="2"/>
                <g fill="var(--color-accent)">
                    <circle cx="65" cy="22" r="2.6"/><circle cx="106" cy="49" r="2.6"/>
                    <circle cx="88" cy="96" r="2.6"/><circle cx="38" cy="88" r="2.6"/>
                    <circle cx="26" cy="51" r="2.6"/>
                </g>
                <g fill="var(--color-accent)" fill-opacity=".7">
                    <rect x="150" y="78" width="14" height="32" rx="2"/>
                    <rect x="168" y="64" width="14" height="46" rx="2"/>
                    <rect x="186" y="88" width="14" height="22" rx="2"/>
                    <rect x="204" y="46" width="14" height="64" rx="2"/>
                    <rect x="222" y="30" width="14" height="80" rx="2"/>
                    <rect x="240" y="56" width="14" height="54" rx="2"/>
                    <rect x="258" y="70" width="14" height="40" rx="2"/>
                </g>
                <rect x="150" y="14" width="80" height="8" rx="4" fill="var(--color-border)"/>
                <rect x="150" y="28" width="122" height="6" rx="3" fill="var(--color-border)" opacity=".6"/>
            </svg>
        </div>`;

    function steps(variant) {
        const real = variant === 'real';
        const demo = window.DEMO_COLLECTION || '';
        const list = [];

        list.push({
            title: real ? 'Your first annotated videos are in!' : 'Welcome to the For You Data Hub',
            body: real
                ? 'The first batch of your videos has been analysed. This short tour shows what that unlocks across the analysis tabs. Two minutes, tops.'
                : 'This is the research workbench your data will feed into. This short tour shows you around the analysis tools and what your own data will look like. Two minutes, tops.',
        });

        list.push({
            target: '#main-tab-nav',
            title: 'Everything happens in these tabs',
            body: 'Analysis tools on the left, your personal pages under My stuff. You can’t break anything by looking around.',
        });

        list.push({
            title: real ? 'Your short-video persona' : 'The moment you share: your persona',
            body: real
                ? 'Your persona is computed from the data you shared: your archetype, viewing rhythms, doomscroll and rewatch habits. Find it under My stuff, on My Collections, where it grows richer as more of your data is analysed.'
                : 'The first thing you see after sharing your data is your short-video persona: your archetype, viewing rhythms, doomscroll and rewatch habits, and how you compare with the cohort. Something like this sketch, built from your real feed.',
            sketch: !real,
        });

        list.push({
            title: 'Raw versus annotated data',
            body: real
                ? 'Your activity history was usable immediately; the deeper analysis of what the videos are about arrives in annotation batches. The "Scraped / annotated" column on My Collections shows how far along your collection is.'
                : 'Your upload arrives as raw activity history: when and what you watched. The persona works right away. The deeper analysis of what the videos are about happens when we fetch and annotate them in batches. Your first batch is prioritised, and we email you the moment it is ready. The tabs that follow show what annotated data unlocks.',
        });

        const demoIntro = real ? 'your data' : `the demo collection`;

        if (demo && tabUsable('semantic_space', 'tab.semantic_space')) {
            list.push({
                target: '#semantic-space-plot',
                title: 'The Semantic Space: a map of the corpus',
                body: `Every annotated video, arranged so that similar content sits together. The highlighted trajectory shows where ${demoIntro} travels through this map: one real feed’s journey across the corpus.`,
                onEnter: async () => {
                    await ensureDefaultStudy();
                    navigate('semantic_space');
                    if (typeof initSemanticSpace === 'function') initSemanticSpace();
                    const sel = () => document.getElementById('ss-collection');
                    await waitFor(() => sel() && sel().options.length > 1, 12000);
                    const disclosure = document.getElementById('ss-traj-disclosure');
                    const controls = document.getElementById('ss-traj-controls');
                    if (disclosure && controls && controls.hidden) disclosure.click();
                    if (sel() && [...sel().options].some(o => o.value === demo)) {
                        sel().value = demo;
                        if (typeof _ssLoadTrajectory === 'function') await _ssLoadTrajectory();
                    }
                },
            });
        }

        if (demo && tabUsable('explore', 'tab.explore')) {
            list.push({
                target: '#explorer-v2-stats',
                title: 'Explore: one feed against everyone',
                body: `Explore compares slices of the data. Here slice 1 is ${demoIntro} and slice 2 is every collection in the study, variable by variable. Click any bar to open the matching videos in Video Analysis.`,
                onEnter: async () => {
                    await ensureDefaultStudy();
                    navigate('explore');
                    if (typeof window.ensureExploreLoaded === 'function') window.ensureExploreLoaded();
                    await waitFor(() => typeof explorerDataV2 === 'object'
                        && explorerDataV2.metadata && explorerDataV2.metadata.collection_id, 12000);
                    if (typeof setExplorerV2SliceMode === 'function') setExplorerV2SliceMode(true);
                    explorerDataV2.filters2 = {};
                    explorerDataV2.filters1 = { collection_id: { type: 'category', value: [demo] } };
                    if (typeof renderFiltersV2 === 'function') renderFiltersV2(explorerDataV2.metadata, 1);
                    if (typeof updateFilterSectionHighlights === 'function') updateFilterSectionHighlights(1);
                    if (typeof updateExplorerV2Stats === 'function') await updateExplorerV2Stats(null);
                },
            });
        }

        if (demo && tabUsable('video_analysis', 'tab.video_analysis')) {
            list.push({
                target: '#viewer-details-panel',
                title: 'Video Analysis: down to the single video',
                body: `Any selection can be opened video by video: watch it, read its AI annotations (story, themes, style), and add your own tags. The viewer is filtered to ${demoIntro} right now.`,
                onEnter: async () => {
                    await ensureDefaultStudy();
                    window._pendingDrillDown = {
                        filters: { collection_id: { type: 'category', value: [demo] } },
                        searchQuery: '',
                        timestamp: Date.now(),
                    };
                    navigate('video_analysis');
                },
            });
        }

        if (demo && tabUsable('timelines', 'tab.timelines')) {
            list.push({
                target: '#timelines-charts-container',
                title: 'Timelines: a feed evolving day by day',
                body: `Each chart follows one variable over time for a single collection. Watch categories rise, fall and spike across ${demoIntro}’s history.`,
                onEnter: async () => {
                    await ensureDefaultStudy();
                    navigate('timelines');
                    if (typeof window.ensureTimelinesLoaded === 'function') window.ensureTimelinesLoaded();
                    await waitFor(() => window.timelines
                        && Array.isArray(window.timelines.collectionList)
                        && window.timelines.collectionList.length, 12000);
                    const sel = document.getElementById('timelines-collection-select');
                    const opt = sel && [...sel.options].find(o => o.value === demo && !o.disabled);
                    if (opt) {
                        sel.value = demo;
                        await window.timelines.selectDonation(demo);
                    }
                },
            });
        }

        if (demo && tabUsable('sessions', 'tab.sessions')) {
            list.push({
                target: '#sess-detail',
                title: 'Sessions: inside a single scroll',
                body: 'One sitting on the feed, play by play. The coloured bands are binges: runs of videos whose content stays close together. Pick one and step through it in watch order.',
                onEnter: async () => {
                    await ensureDefaultStudy();
                    navigate('sessions');
                    await waitFor(() => typeof sessState === 'object'
                        && sessState.overview && Array.isArray(sessState.overview.sessions)
                        && sessState.overview.sessions.length, 12000);
                    const rows = sessState.overview.sessions;
                    const pick = rows.find(r => r.collection_id === demo && r.n_episodes > 0)
                        || rows.find(r => r.n_episodes > 0) || rows[0];
                    if (pick && typeof sessSelect === 'function') {
                        const rowEl = document.querySelector(
                            `#sess-list tr[data-sid="${pick.session_id}"]`);
                        await sessSelect(pick.collection_id, pick.session_id, rowEl || null);
                        const chip = document.querySelector('#sess-episode-chips .sess-seq-entry');
                        if (chip) chip.click();
                    }
                },
            });
        }

        if (can('tab.my_stuff.my_collections')) {
            list.push({
                target: '#myc-table',
                title: 'My Collections: your own data',
                body: real
                    ? 'The collections you shared and your persona are here, and the "Scraped / annotated" column tracks how much of each has been analysed so far.'
                    : 'Sharing starts on this page: each platform row guides you through requesting and sharing your file, and you review everything in your browser before anything is shared. Once shared, your collections and your persona appear right here.',
                onEnter: async () => {
                    try { if (typeof pauseSessionsVideos === 'function') pauseSessionsVideos(); } catch (e) { /* fine */ }
                    navigate('my_stuff', 'my-stuff-page-my-collections');
                },
            });
        }

        if (can('tab.my_stuff.profile')) {
            list.push({
                target: '#my-stuff-page-profile',
                title: 'Tell us about yourself',
                body: 'A few details (age range, country, occupation) are genuinely valuable to the research team: they are what turn a shared feed into research-grade data about real audiences. And ticking "consent to contact" lets us email you the moment your annotated data is ready.',
                onEnter: () => navigate('my_stuff', 'my-stuff-page-profile'),
            });
        }

        list.push({
            title: 'That’s the tour!',
            body: real
                ? 'Explore your persona and your collection, and check back as more batches are annotated.'
                : 'When your data export arrives, log in and My Collections takes it from there. See you soon!',
            last: true,
        });

        return list;
    }

    // ---------------- rendering ----------------

    function ensureEls() {
        if (state.els) return;
        const backdrop = document.createElement('div');
        backdrop.className = 'hub-tour-backdrop';
        const ring = document.createElement('div');
        ring.className = 'hub-tour-ring';
        const card = document.createElement('div');
        card.className = 'hub-tour-card';
        document.body.append(backdrop, ring, card);
        state.els = { backdrop, ring, card };
    }

    function positionRing(target) {
        const { ring } = state.els;
        if (!target || target.offsetParent === null) { ring.style.display = 'none'; return; }
        const r = target.getBoundingClientRect();
        ring.style.display = 'block';
        ring.style.top = `${r.top - 6}px`;
        ring.style.left = `${r.left - 6}px`;
        ring.style.width = `${r.width + 12}px`;
        ring.style.height = `${r.height + 12}px`;
    }

    function positionCard(target) {
        const { card } = state.els;
        if (!target || target.offsetParent === null) {
            card.style.top = '50%';
            card.style.left = '50%';
            card.style.transform = 'translate(-50%, -50%)';
            return;
        }
        card.style.transform = 'none';
        const r = target.getBoundingClientRect();
        const cw = card.offsetWidth, ch = card.offsetHeight;
        const margin = 14;
        let top = r.bottom + margin;
        if (top + ch > window.innerHeight - margin) top = Math.max(margin, r.top - ch - margin);
        let left = Math.min(Math.max(margin, r.left), window.innerWidth - cw - margin);
        card.style.top = `${top}px`;
        card.style.left = `${left}px`;
    }

    function currentTarget() {
        const step = state.steps[state.idx];
        return step && step.target ? document.querySelector(step.target) : null;
    }

    function reposition() {
        if (!state) return;
        const target = currentTarget();
        positionRing(target);
        positionCard(target);
    }

    function showStep(idx) {
        const step = state.steps[idx];
        state.idx = idx;
        const mySeq = ++state.seq;  // stale async setups must not reposition

        const { card } = state.els;
        const n = state.steps.length;
        card.innerHTML = `
            <div class="hub-tour-head">
                <span class="hub-tour-step-n">Step ${idx + 1} of ${n}</span>
                <span class="hub-tour-nav-btns">
                    <button class="hub-tour-nav-btn" data-tour="back" title="Back"
                        aria-label="Back" ${idx === 0 ? 'disabled' : ''}>&lsaquo;</button>
                    <button class="hub-tour-nav-btn" data-tour="next"
                        title="${step.last ? 'Finish' : 'Next'}"
                        aria-label="${step.last ? 'Finish' : 'Next'}">${step.last ? '&check;' : '&rsaquo;'}</button>
                    <button class="hub-tour-nav-btn hub-tour-close" data-tour="skip"
                        title="End the tour" aria-label="End the tour">&times;</button>
                </span>
            </div>
            <h4>${step.title}</h4>
            <p>${step.body}</p>
            ${step.sketch ? PERSONA_SKETCH : ''}`;
        card.querySelectorAll('[data-tour]').forEach((el) => {
            el.addEventListener('click', (e) => {
                e.preventDefault();
                const act = el.dataset.tour;
                if (act === 'back') { if (idx > 0) showStep(idx - 1); }
                else if (act === 'next') step.last ? finish(true) : showStep(idx + 1);
                else finish(false);
            });
        });

        reposition();
        const settle = () => {
            if (!state || state.seq !== mySeq) return;
            const target = currentTarget();
            if (target) target.scrollIntoView({ block: 'center', behavior: 'smooth' });
            setTimeout(() => { if (state && state.seq === mySeq) reposition(); }, 400);
            reposition();
        };
        if (step.onEnter) {
            Promise.resolve()
                .then(() => step.onEnter())
                .catch(() => { /* best effort: the copy still stands */ })
                .then(settle);
        } else {
            settle();
        }
    }

    function onKey(e) {
        if (e.key === 'Escape') finish(false);
    }

    function finish(completed) {
        if (!state) return;
        const { backdrop, ring, card } = state.els;
        backdrop.remove(); ring.remove(); card.remove();
        window.removeEventListener('keydown', onKey);
        window.removeEventListener('resize', reposition);
        const wasReal = state.variant === 'real';
        state = null;
        if (typeof saveUserSettings === 'function') {
            const settings = wasReal
                ? { hub_tour_real_data_pending: false }
                : { hub_tour_pending: false, hub_tour_done: !!completed };
            saveUserSettings(settings);
        }
        // Clear the #tour deep-link hash so a reload doesn't restart the tour.
        if (location.hash === '#tour') history.replaceState(null, '', location.pathname + location.search);
    }

    function start(variant) {
        if (state) return;
        state = { variant: variant || 'example', idx: 0, els: null, seq: 0 };
        state.steps = steps(state.variant);
        ensureEls();
        window.addEventListener('keydown', onKey);
        window.addEventListener('resize', reposition);
        // Warm up the default study in the background so the first analysis
        // step doesn't pay the whole wait.
        ensureDefaultStudy();
        showStep(0);
    }

    window.HubTour = { start };

    document.addEventListener('DOMContentLoaded', () => {
        const flags = window.HUB_TOUR_FLAGS || {};
        if (location.hash === '#tour' || flags.pending) {
            // Give the shell a beat to finish its own boot (default sub-pages,
            // settings load) before the tour takes over the viewport.
            setTimeout(() => start('example'), 600);
        } else if (flags.realDataPending) {
            setTimeout(() => start('real'), 600);
        }
    });
})();
