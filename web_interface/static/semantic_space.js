// Semantic Space tab — global video embedding map.
// Loads recoded/video_map.parquet (via /api/semantic_space/map) once and
// renders a WebGL scatter of the 2D-projected videos. The map is corpus-wide
// (not study-scoped), so it loads independently of the active study.

let _ssData = null;
let _ssLoaded = false;
let _ssHandlersWired = false;
let _ssStatusTimer = null;
let _ssLoadedMapBuiltAt = null;   // mtime of the map currently rendered
let _ssLabelTimer = null;         // debounce for zoom-driven label refresh
let _ssHidden = new Set();        // categories toggled off via the legend swatches
let _ssLastColorMode = null;      // detect colour-variable switches to reset _ssHidden
let _ssLegendCats = null;         // distinct categories backing the current swatches
let _ssTrajectory = null;         // last-fetched collection trajectory payload
let _ssTrajOn = false;            // whether the trajectory overlay is shown
let _ssCollectionsStudy;          // study the collection list was loaded for (undefined = not loaded)
let _ssAnimPos = null;            // continuous playback position (float over points); null = static
let _ssAnimRAF = null;            // requestAnimationFrame handle while playing
let _ssAnimLastTime = null;       // last rAF timestamp, for dt-based advance
let _ssAnimStepMs = 800;          // ms to morph across one period (set per run)
let _ssAnimPlaying = false;       // whether playback is running
let _ssFocusNiche = null;         // focused niche id, or null for "all niches"
let _ssNicheSort = 'size';        // niche-picker ordering (see _SS_NICHE_SORTS)
let _ssNicheQuery = '';           // niche-picker search box contents
let _ssNicheActive = -1;          // keyboard-highlighted row in the picker list
let _ssTrajCollapsed = true;      // trajectory controls hidden behind the disclosure
let _ssFlashTimers = [];          // pending timers for the "flash nearest niches" pulse
let _ssFlashNiches = null;        // niche ids currently lit by the flash (null = none)
let _ssFlashXY = null;            // memoized {key, xs, ys} for the lit niches' points

// Categorical data palette (tab20-style). Niche colour = palette[niche % 20];
// category colours are assigned by index. Numeric overlays use _SS_NUMERIC_SCALE.
const _SS_PALETTE = [
    '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
    '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
    '#aec7e8', '#ffbb78', '#98df8a', '#ff9896', '#c5b0d5',
    '#c49c94', '#f7b6d2', '#c7c7c7', '#dbdb8d', '#9edae5'
];
const _SS_MAX_LABELS = 30;

// Numeric colour scale (low → high). Deliberately bounded to saturated,
// mid-luminance hues (blue → teal → green → orange → red) — it avoids the
// near-black and near-white ends of perceptual ramps like Viridis, so points
// stay visible on both the dark and light chart backgrounds. Used for the
// point colours (as a Plotly colorscale) and the HTML gradient legend, so the
// two always match.
const _SS_NUMERIC_SCALE = [
    '#2c6fd6', '#159ca8', '#1faa6d', '#5fb53b', '#e08a2b', '#cf3a3a'
];
const _SS_NUMERIC_COLORSCALE = _SS_NUMERIC_SCALE.map(
    (c, i, a) => [a.length === 1 ? 0 : i / (a.length - 1), c]
);

// Numeric overlays are coloured over a ROBUST range — the 2nd/98th percentile of
// the field, not its raw min/max — so a few outliers can't compress the bulk of
// the distribution into a single hue (e.g. "Face age estimate", whose spread is
// concentrated but has young/old outliers). Values past the range saturate at
// the ends, and the legend marks this with ≤/≥.
const _SS_ROBUST_PCT = 0.02;

// Trajectory overlay colours, deliberately chosen from hues the tab20 niche/
// category background palette never uses, so the overlay never reads as a
// background dot. The all-time centre of gravity is a neutral WHITE anchor;
// per-period centroids + dispersion clouds use a magenta recency ramp (early →
// late): deep magenta → bright pink (see _ssTimeColorRGB). Staying within the
// magenta family means the ramp never passes through a background hue.
const _SS_TRAJ_ACCENT = '#ffffff';
const _SS_TRAJ_T0 = [150, 45, 140];    // earliest period — deep magenta
const _SS_TRAJ_T1 = [255, 120, 225];   // latest period — bright pink
const _SS_TRAJ_MAX_ELLIPSES = 52;   // above this many periods, skip clouds (path only)

// Visual scale for the dispersion ellipses. Absolute size is arbitrary (the
// ellipse semi-axes are k·sigma of the period's 2D spread); this shrinks them
// uniformly so the RELATIVE sizes — how diversity changes over time and across
// collections — stay legible without the clouds swamping the map. Fixed at 0.30.
const _SS_ELLIPSE_SCALE = 0.30;

// Playback: older periods decay by _SS_ANIM_FADE per step back (comet trail);
// a period is dropped once its fade falls below _SS_ANIM_MIN_ALPHA. Slow decay
// + a higher floor → a long, clearly-visible trail.
const _SS_ANIM_FADE = 0.78;
const _SS_ANIM_MIN_ALPHA = 0.12;

// Focus-niche picker orderings. `key` returns the sort value (higher first for
// the numeric ones); `meta` is the secondary reading shown on each row, so the
// column the list is sorted by is also the column the reader can see.
const _SS_NICHE_SORTS = {
    size: {
        cmp: (a, b) => b.size - a.size,
        meta: c => c.size.toLocaleString()
    },
    name: {
        cmp: (a, b) => a.name.localeCompare(b.name),
        meta: c => c.size.toLocaleString()
    },
    typicality: {
        cmp: (a, b) => _ssPctDesc(a.typicality_pct, b.typicality_pct) || (b.size - a.size),
        meta: c => (c.typicality_pct == null ? c.size.toLocaleString()
            : `${c.size.toLocaleString()} · ${_ssQualitative(c.typicality_pct, _SS_TYPICALITY_BANDS)}`)
    },
    isolation: {
        cmp: (a, b) => _ssPctDesc(a.isolation_pct, b.isolation_pct) || (b.size - a.size),
        meta: c => (c.isolation_pct == null ? c.size.toLocaleString()
            : `${c.size.toLocaleString()} · ${_ssQualitative(c.isolation_pct, _SS_ISOLATION_BANDS)}`)
    }
};

// Qualitative bands for the two measured readings. A percentile is a precise
// number that most readers cannot act on; the word is what they can. The exact
// figure stays one hover away (see _ssReadingHtml), so nothing is lost.
// Boundaries are the percentile floors, highest first.
const _SS_TYPICALITY_BANDS = [
    [90, 'very typical'],
    [70, 'fairly typical'],
    [30, 'about average'],
    [10, 'fairly distinctive'],
    [0, 'very distinctive']
];
const _SS_ISOLATION_BANDS = [
    [90, 'very isolated'],
    [70, 'fairly isolated'],
    [30, 'about average'],
    [10, 'fairly crowded'],
    [0, 'very crowded']
];

// "Flash the closest niches": how many on/off pulses, and how long each half
// lasts. Three pulses is long enough to follow across a wide map without the
// control becoming an animation the reader has to wait out.
const _SS_FLASH_PULSES = 3;
const _SS_FLASH_ON_MS = 420;
const _SS_FLASH_OFF_MS = 260;


// Called by openTab() the first time the Semantic Space tab is shown.
function initSemanticSpace() {
    _ssStartStatusPoll();
    if (_ssLoaded) {
        // Container width may have changed while hidden — nudge a resize.
        const div = document.getElementById('semantic-space-plot');
        if (div && div.data) { Plotly.Plots.resize(div); }
        return;
    }
    _ssLoaded = true;
    loadSemanticSpace();
}


async function loadSemanticSpace() {
    const status = document.getElementById('ss-status');
    if (status) { status.innerText = 'Loading map…'; }
    try {
        const res = await fetch('/api/semantic_space/map');
        const data = await res.json();
        if (!res.ok || data.error) {
            if (status) { status.innerText = data.error || `Error ${res.status}`; }
            _ssLoaded = false;   // allow a retry on next open
            return;
        }
        _ssData = data;
        _ssLoadedMapBuiltAt = (data.map_built_at !== undefined) ? data.map_built_at : null;
        _ssComputeCentroids();
        // A rebuilt map can renumber or drop niches, so the focus survives only
        // if its id still names one. Either way the picker's label is refreshed
        // from the new map rather than left describing the old one.
        _ssSetFocusNiche(_ssCentroid(_ssFocusNiche) ? _ssFocusNiche : null, { render: false });
        _ssPopulateColorModes();
        if (status) {
            // innerHTML, not innerText: the accuracy figure carries a tooltip.
            status.innerHTML =
                `${data.total_mapped.toLocaleString()} videos shown · `
                + `${data.n_niches} niches · ${data.total_videos.toLocaleString()} embedded`
                + _ssPreservationHtml(data.neighbour_preservation);
        }
        _ssWireControls();
        // Load the study-scoped collection list once the active study is known.
        if (window.studyState && window.studyState.ready) {
            window.studyState.ready.then(() => _ssLoadCollections());
        } else {
            _ssLoadCollections();
        }
        renderSemanticSpace();
        _ssPollStatus();   // surface the freshness banner without a poll delay
    } catch (e) {
        console.error(e);
        if (status) { status.innerText = 'Failed to load map.'; }
        _ssLoaded = false;
    }
}


// Per-niche median position + size, used for centroid labels, the focus picker
// and the "flash the closest niches" rings. The measured readings ride along so
// the picker can sort by them without re-reaching into the niches lookup, and
// `r` is the niche's drawn spread (60th percentile of the distance from its
// median centre) — the radius that makes a flash ring cover roughly the niche
// rather than an arbitrary blob around its middle.
function _ssComputeCentroids() {
    const P = _ssData.points;
    const acc = {};   // niche -> {xs:[], ys:[]}
    for (let i = 0; i < P.x.length; i++) {
        const n = P.niche[i];
        (acc[n] = acc[n] || { xs: [], ys: [] });
        acc[n].xs.push(P.x[i]);
        acc[n].ys.push(P.y[i]);
    }
    const median = arr => {
        const s = arr.slice().sort((a, b) => a - b);
        return s[Math.floor(s.length / 2)];
    };
    _ssData._centroids = Object.keys(acc).map(n => {
        const meta = _ssData.niches[n] || {};
        const cx = median(acc[n].xs);
        const cy = median(acc[n].ys);
        const d = acc[n].xs
            .map((x, i) => Math.hypot(x - cx, acc[n].ys[i] - cy))
            .sort((a, b) => a - b);
        return {
            niche: +n,
            name: meta.name || `Niche ${n}`,
            size: meta.size || acc[n].xs.length,
            terms: meta.terms || [],
            typicality_pct: (meta.typicality_pct == null) ? null : meta.typicality_pct,
            isolation_pct: (meta.isolation_pct == null) ? null : meta.isolation_pct,
            x: cx,
            y: cy,
            r: d.length ? d[Math.floor(0.6 * (d.length - 1))] : 0,
            // Kept rather than discarded: the flash draws the niche's real
            // points, so it needs their coordinates. One extra copy of the
            // mapped coordinates, which is cheap at map scale.
            xs: acc[n].xs,
            ys: acc[n].ys
        };
    });
    _ssData._centroidById = {};
    _ssData._centroids.forEach(c => { _ssData._centroidById[c.niche] = c; });
}


function _ssCentroid(nicheId) {
    return ((_ssData && _ssData._centroidById) || {})[nicheId] || null;
}


// Descending sort on a percentile that may be absent; niches without the
// reading sink to the bottom rather than pretending to be zero.
function _ssPctDesc(a, b) {
    if (a == null && b == null) { return 0; }
    if (a == null) { return 1; }
    if (b == null) { return -1; }
    return b - a;
}


// The word for a percentile, from a highest-floor-first band table.
function _ssQualitative(pct, bands) {
    for (const [floor, word] of bands) {
        if (pct >= floor) { return word; }
    }
    return bands[bands.length - 1][1];
}


// ---------------------------------------------------------------------------
// Focus-niche picker — a searchable, sortable combobox over the niche list.
// The list runs to hundreds of entries and is the main instrument for finding
// a niche, which a plain <select> cannot serve: it can't be searched by term,
// can't be re-ordered by the readings the map publishes, and offers no way out
// of a selection except scrolling back to a placeholder.
// ---------------------------------------------------------------------------

// Centroids matching the current search, in the current sort order. A niche
// matches on its name OR one of its defining terms — the terms are often how a
// researcher remembers a niche whose generated name they never learned.
function _ssNicheMatches() {
    const q = _ssNicheQuery.trim().toLowerCase();
    let list = (_ssData && _ssData._centroids) || [];
    if (q) {
        list = list.filter(c => c.name.toLowerCase().includes(q)
            || (c.terms || []).some(t => String(t).toLowerCase().includes(q)));
    }
    const sort = _SS_NICHE_SORTS[_ssNicheSort] || _SS_NICHE_SORTS.size;
    return list.slice().sort(sort.cmp);
}


function _ssRenderNicheList() {
    const list = document.getElementById('ss-niche-list');
    if (!list || !_ssData) { return; }
    const matches = _ssNicheMatches();
    if (!matches.length) {
        list.innerHTML = '<div class="ss-niche-empty text-sm">No niche matches that search.</div>';
        _ssRenderNicheCount(0);
        return;
    }
    const sort = _SS_NICHE_SORTS[_ssNicheSort] || _SS_NICHE_SORTS.size;
    list.innerHTML = matches.map((c, i) => {
        const cls = 'ss-niche-option text-sm'
            + (c.niche === _ssFocusNiche ? ' is-selected' : '')
            + (i === _ssNicheActive ? ' is-active' : '');
        return `<div class="${cls}" role="option" data-niche="${c.niche}" data-idx="${i}"`
            + ` aria-selected="${c.niche === _ssFocusNiche}">`
            + `<span class="ss-niche-option-name">${escapeHtml(c.name)}</span>`
            + `<span class="ss-niche-option-meta text-xs">${escapeHtml(sort.meta(c))}</span>`
            + '</div>';
    }).join('');
    const active = list.querySelector('.ss-niche-option.is-active');
    if (active) { active.scrollIntoView({ block: 'nearest' }); }
    _ssRenderNicheCount(matches.length);
}


// The list holds every niche, but a hidden overlay scrollbar advertises none of
// that — so state the count outright, and say when there is more below.
function _ssRenderNicheCount(shown) {
    const el = document.getElementById('ss-niche-count');
    const list = document.getElementById('ss-niche-list');
    if (!el) { return; }
    const total = ((_ssData && _ssData._centroids) || []).length;
    const overflows = !!list && list.scrollHeight > list.clientHeight + 1;
    let text = _ssNicheQuery.trim()
        ? `${shown.toLocaleString()} of ${total.toLocaleString()} niches match`
        : `${total.toLocaleString()} niches`;
    if (overflows) { text += ' · scroll for more'; }
    el.textContent = text;
}


function _ssNichePanelOpen() {
    const panel = document.getElementById('ss-niche-panel');
    return !!(panel && !panel.hidden);
}


function _ssOpenNichePanel() {
    const panel = document.getElementById('ss-niche-panel');
    const trigger = document.getElementById('ss-niche-trigger');
    const search = document.getElementById('ss-niche-search');
    if (!panel) { return; }
    panel.hidden = false;
    if (trigger) { trigger.setAttribute('aria-expanded', 'true'); }
    _ssNicheActive = -1;
    _ssRenderNicheList();
    if (search) { search.focus(); search.select(); }
}


function _ssCloseNichePanel() {
    const panel = document.getElementById('ss-niche-panel');
    const trigger = document.getElementById('ss-niche-trigger');
    if (!panel) { return; }
    panel.hidden = true;
    if (trigger) { trigger.setAttribute('aria-expanded', 'false'); }
    _ssNicheActive = -1;
}


// The single entry point for changing the focus. Every caller (picker, clear
// button, the map's click dialog, map reload) goes through here, so the
// trigger label, the clear affordance, any running flash and the redraw stay
// in step no matter where the change came from.
function _ssSetFocusNiche(nicheId, opts) {
    const changed = _ssFocusNiche !== nicheId;
    _ssFocusNiche = nicheId;
    if (changed) { _ssStopFlash(); }

    const label = document.getElementById('ss-niche-trigger-label');
    if (label) {
        const c = nicheId === null ? null : _ssCentroid(nicheId);
        label.textContent = c ? `${c.name} (${c.size.toLocaleString()})` : '— all niches —';
    }
    const clear = document.getElementById('ss-niche-clear');
    if (clear) { clear.hidden = nicheId === null; }
    if (_ssNichePanelOpen()) { _ssRenderNicheList(); }
    if (!opts || opts.render !== false) { renderSemanticSpace(); }
}


// The projection's own accuracy score, shown beside the corpus counts. A 2D
// layout drops relationships silently, so without this figure the map's
// fidelity is unfalsifiable — publishing it is what lets a reader judge how
// much weight the picture will carry.
function _ssPreservationHtml(np) {
    if (!np || np.score == null) { return ''; }
    const pct = Math.round(100 * np.score);
    const chance = (100 * np.chance).toFixed(3);
    const tip = `Of a video's ${np.k} nearest neighbours in the full embedding space, `
        + `${pct}% are still drawn among its ${np.k} nearest here — against ${chance}% `
        + `for a random layout. The projection cannot honour every relationship at once, `
        + `so treat closeness on the map as evidence and check anything that matters `
        + `against the measured readings (typicality, closest niches).`;
    return ` · <span class="meta-tooltip tooltip-wide tooltip-below" `
        + `data-tooltip="${escapeHtml(tip)}">layout keeps ${pct}% of true neighbours</span>`;
}


// Tooltip copy for the typicality reading. The map cannot express this, so the
// tooltip says so — a niche near the middle of the picture is not the same
// thing as a niche near the middle of the corpus.
const _SS_TYPICALITY_TIP = 'How close this niche sits to the average of the whole corpus, '
    + 'measured in the full embedding space. It cannot be read off the map: the projection '
    + 'places each niche next to its nearest neighbours and is free to put that neighbourhood '
    + 'anywhere on the page, so the middle of the picture is not the middle of the corpus.';

// Tooltip copy for the defining terms — the words that separate this niche from
// the rest of the corpus, not merely the words most common inside it.
const _SS_TERMS_TIP = 'The words that most distinguish this niche\'s videos from the rest of '
    + 'the corpus. Videos are positioned by how they would be described in words like these, '
    + 'but the map can only show part of that structure — read closeness as a hint, not a '
    + 'measurement.';

// Tooltip copy for the isolation reading. Deliberately spells out that this is
// NOT typicality: the two rank the niches independently, and reading one as the
// other is the mistake the pair exists to prevent.
const _SS_ISOLATION_TIP = 'How far this niche sits from its nearest neighbouring niche, '
    + 'measured in the full embedding space. This asks a different question from typicality: '
    + 'a niche can sit far from the corpus average and still keep close company — several '
    + 'related niches beside it — or be thoroughly ordinary and yet have nothing near it. '
    + 'Unlike apparent separation on the map, this is measured. "Crowded" here means other '
    + 'niches sit close by, not that this niche holds many videos.';

// Tooltip copy for the measured nearest niches. This is the reading the picture
// cannot give, so the tooltip says plainly that the two can disagree.
const _SS_NEAREST_TIP = 'The niches most similar to this one, measured in the full embedding '
    + 'space rather than read off the map. Several are usually drawn far away: the projection '
    + 'can only place a niche in one spot and cannot honour every relationship at once. Where '
    + 'this list and the picture disagree, this list is the accurate one.';


// One measured reading, stated qualitatively with the exact percentile on the
// tooltip. The word is what a reader can act on; the number is what they check
// it against, so neither is dropped — they are just put in the right order.
function _ssReadingHtml(label, comparative, pct, bands, tip) {
    const word = _ssQualitative(pct, bands);
    const exact = `More ${comparative} than ${pct}% of other niches. ${tip}`;
    return `<span><span class="meta-tooltip tooltip-wide tooltip-below" `
        + `data-tooltip="${escapeHtml(exact)}">${label}</span>: ${escapeHtml(word)}</span>`;
}


// Render the niche detail bar under the controls: what actually defines the
// focused niche. This is the plain-language counterpart to the geometry — the
// map shows which niches are neighbours, the bar says why.
function _ssRenderNicheInfo(focusNiche) {
    const bar = document.getElementById('ss-niche-info');
    if (!bar) { return; }
    if (focusNiche === null) {
        bar.innerHTML = '<span>Focus a niche to see the terms that define it, how many '
            + 'videos it holds, and how typical it is of the corpus.</span>';
        return;
    }
    const meta = (_ssData.niches || {})[focusNiche] || {};
    const parts = [
        `<span class="font-semibold" style="color: var(--color-text-primary);">`
        + `${escapeHtml(meta.name || ('Niche ' + focusNiche))}</span>`,
        `<span>${(meta.size || 0).toLocaleString()} videos</span>`
    ];
    // Absent on maps built before typicality was added; the bar just omits it
    // rather than showing a blank reading.
    if (meta.typicality_pct != null) {
        parts.push(_ssReadingHtml('Typicality', 'typical', meta.typicality_pct,
            _SS_TYPICALITY_BANDS, _SS_TYPICALITY_TIP));
    }
    // Sits beside typicality as its counterpart: how far from the average vs
    // how far from the nearest company. The two rank niches independently.
    if (meta.isolation_pct != null) {
        parts.push(_ssReadingHtml('Isolation', 'isolated', meta.isolation_pct,
            _SS_ISOLATION_BANDS, _SS_ISOLATION_TIP));
    }
    // Placed directly after typicality: both are measured in the full space,
    // and together they are what the picture cannot be trusted to show.
    // The build stores five; three keeps the bar readable and still makes the
    // point that the measured neighbours are not the ones drawn alongside.
    // The pulse button answers the obvious next question — the list names the
    // neighbours, the map is where they are, and the two rarely look related.
    if ((meta.nearest || []).length) {
        const flashable = (meta.nearest_ids || []).some(n => _ssCentroid(n));
        const btn = flashable
            ? ` <button type="button" id="ss-flash-nearest" class="ss-flash-btn text-xxs meta-tooltip tooltip-below"`
                + ` data-tooltip="Pulse these niches on the map — they are usually drawn nowhere near this one."`
                + ` aria-label="Show the closest niches on the map">◎</button>`
            : '';
        // Each neighbour is a control, not just a name: the list answers "which
        // niches are like this one", and the obvious next question — "show me
        // that one" — is the same pair of moves a dot click offers.
        // nearest/nearest_ids are parallel (the backend builds the names from
        // the surviving ids), so the index carries across.
        const links = meta.nearest.slice(0, 3).map((nm, i) => {
            const id = (meta.nearest_ids || [])[i];
            return (id == null || !_ssCentroid(id))
                ? escapeHtml(nm)
                : `<button type="button" class="ss-niche-link" data-niche="${id}">${escapeHtml(nm)}</button>`;
        }).join(', ');
        parts.push(`<span><span class="meta-tooltip tooltip-wide tooltip-below" `
            + `data-tooltip="${escapeHtml(_SS_NEAREST_TIP)}">Closest niches</span>: `
            + `${links}${btn}</span>`);
    }
    if ((meta.terms || []).length) {
        parts.push(`<span><span class="meta-tooltip tooltip-wide tooltip-below" `
            + `data-tooltip="${escapeHtml(_SS_TERMS_TIP)}">Defining terms</span>: `
            + `${escapeHtml(meta.terms.join(', '))}</span>`);
    }
    // Entries carry a share of the niche; pct is null only on a map old enough
    // to predate the category column, where the bare names are all there is.
    if ((meta.top_categories || []).length) {
        const cats = meta.top_categories.map(c => escapeHtml(c.label)
            + (c.pct == null ? '' : ` ${c.pct}%`));
        parts.push(`<span>Top categories: ${cats.join(', ')}</span>`);
    }
    bar.innerHTML = parts.join('');
    const flashBtn = document.getElementById('ss-flash-nearest');
    if (flashBtn) { flashBtn.onclick = () => _ssFlashNearest(focusNiche); }
    bar.querySelectorAll('.ss-niche-link').forEach(link => {
        link.onclick = () => {
            const id = +link.dataset.niche;
            const c = _ssCentroid(id);
            if (!c) { return; }
            _ssShowNicheDialog({
                nicheId: id, nicheName: c.name, size: c.size,
                intro: 'Measured as one of the closest niches to the focused one:'
            });
        };
    });
}


function _ssWireControls() {
    if (_ssHandlersWired) { return; }
    _ssHandlersWired = true;
    ['ss-color-mode', 'ss-show-labels'].forEach(id => {
        const el = document.getElementById(id);
        if (el) { el.addEventListener('change', renderSemanticSpace); }
    });
    const legend = document.getElementById('ss-legend');
    if (legend) { legend.addEventListener('click', _ssOnLegendClick); }
    _ssWireNichePicker();
    _ssWireTrajectoryDisclosure();

    // Trajectory overlay controls. Selecting a collection (or changing the
    // interval) refetches; the toggle just flips overlay visibility on the
    // already-loaded payload, so it never hits the network.
    const coll = document.getElementById('ss-collection');
    if (coll) { coll.addEventListener('change', _ssLoadTrajectory); }
    const ivSel = document.getElementById('ss-traj-interval');
    if (ivSel) {
        ivSel.addEventListener('change', () => {
            if ((document.getElementById('ss-collection') || {}).value) { _ssLoadTrajectory(); }
        });
    }
    const tog = document.getElementById('ss-show-trajectory');
    if (tog) {
        tog.addEventListener('change', () => {
            _ssTrajOn = tog.checked;
            if (!_ssTrajOn) { _ssAnimReset(); } else { renderSemanticSpace(); }
        });
    }
    // Play/Stop: step through the periods, fading older clouds into a trail.
    const play = document.getElementById('ss-anim-play');
    if (play) { play.addEventListener('click', _ssAnimToggle); }
    // Scrub slider: jump straight to any period frame (no smooth tween needed).
    const scrub = document.getElementById('ss-scrub');
    if (scrub) { scrub.addEventListener('input', _ssOnScrub); }
    // Reload the (study-scoped) collection list when the active study changes,
    // dropping any trajectory whose collection isn't in the new study.
    document.addEventListener('study:changed', async () => {
        const coll = document.getElementById('ss-collection');
        const before = coll ? coll.value : '';
        await _ssLoadCollections();
        if (before && coll && coll.value !== before) { _ssLoadTrajectory(); }
    });
}


function _ssWireNichePicker() {
    const trigger = document.getElementById('ss-niche-trigger');
    const panel = document.getElementById('ss-niche-panel');
    const search = document.getElementById('ss-niche-search');
    const sort = document.getElementById('ss-niche-sort');
    const list = document.getElementById('ss-niche-list');
    const clear = document.getElementById('ss-niche-clear');

    if (trigger) {
        trigger.addEventListener('click', () => {
            if (_ssNichePanelOpen()) { _ssCloseNichePanel(); } else { _ssOpenNichePanel(); }
        });
    }
    if (clear) {
        clear.addEventListener('click', () => {
            _ssCloseNichePanel();
            _ssSetFocusNiche(null);
        });
    }
    if (search) {
        search.addEventListener('input', () => {
            _ssNicheQuery = search.value;
            _ssNicheActive = -1;
            _ssRenderNicheList();
        });
        search.addEventListener('keydown', _ssOnNicheKeydown);
    }
    if (sort) {
        sort.addEventListener('change', () => {
            _ssNicheSort = sort.value;
            _ssNicheActive = -1;
            _ssRenderNicheList();
        });
    }
    if (list) {
        list.addEventListener('click', ev => {
            const opt = ev.target.closest('.ss-niche-option');
            if (!opt) { return; }
            _ssCloseNichePanel();
            _ssSetFocusNiche(+opt.dataset.niche);
        });
    }
    // Click-away and Escape close the panel — it floats over the map, so it
    // must not become something the reader has to dismiss deliberately.
    document.addEventListener('click', ev => {
        if (!_ssNichePanelOpen()) { return; }
        if (panel && (panel.contains(ev.target)
            || (trigger && trigger.contains(ev.target)))) { return; }
        _ssCloseNichePanel();
    });
}


// Arrow keys move the highlight, Enter focuses the highlighted niche, Escape
// closes. Typing goes to the search box throughout, so the whole list is
// reachable without the mouse.
function _ssOnNicheKeydown(ev) {
    if (ev.key === 'Escape') {
        _ssCloseNichePanel();
        const trigger = document.getElementById('ss-niche-trigger');
        if (trigger) { trigger.focus(); }
        return;
    }
    const matches = _ssNicheMatches();
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        if (!matches.length) { return; }
        const step = ev.key === 'ArrowDown' ? 1 : -1;
        _ssNicheActive = (_ssNicheActive < 0)
            ? (step === 1 ? 0 : matches.length - 1)
            : (_ssNicheActive + step + matches.length) % matches.length;
        _ssRenderNicheList();
        return;
    }
    if (ev.key === 'Enter') {
        ev.preventDefault();
        // No explicit highlight yet: Enter takes the top match, which is what
        // a search-then-Enter gesture means.
        const pick = matches[_ssNicheActive >= 0 ? _ssNicheActive : 0];
        if (!pick) { return; }
        _ssCloseNichePanel();
        _ssSetFocusNiche(pick.niche);
    }
}


// The collection-trajectory overlay answers a narrower question than the map,
// so it lives behind a disclosure. Collapsing also HIDES the overlay (see
// _ssTrajVisible): a drawn overlay whose controls are off-screen is a change
// to the map the reader can neither explain nor undo. The selection itself is
// kept, so re-expanding restores exactly what was there.
function _ssWireTrajectoryDisclosure() {
    const btn = document.getElementById('ss-traj-disclosure');
    const panel = document.getElementById('ss-traj-controls');
    if (!btn || !panel) { return; }
    btn.addEventListener('click', () => {
        _ssTrajCollapsed = !_ssTrajCollapsed;
        panel.hidden = _ssTrajCollapsed;
        btn.setAttribute('aria-expanded', String(!_ssTrajCollapsed));
        if (_ssTrajCollapsed) { _ssAnimStop(); _ssAnimPos = null; }
        renderSemanticSpace();
        // The controls row changes the panel's height, and the scatter is
        // aspect-locked — without an explicit resize Plotly keeps the old plot
        // box and the map is drawn into the wrong space.
        const div = document.getElementById('semantic-space-plot');
        if (div && div.data) { Plotly.Plots.resize(div); }
    });
}


// Whether the trajectory overlay should be drawn: the user asked for it AND
// its controls are on screen.
function _ssTrajVisible() {
    return _ssTrajOn && !_ssTrajCollapsed;
}


// Build the "Colour by" dropdown: Niche (always) + every overlay the backend
// advertised for this map.
function _ssPopulateColorModes() {
    const sel = document.getElementById('ss-color-mode');
    if (!sel) { return; }
    const opts = ['<option value="niche">Niche</option>'];
    (_ssData.overlays || []).forEach(o => {
        opts.push(`<option value="${o.key}">${o.label}</option>`);
    });
    sel.innerHTML = opts.join('');
}


function _ssOverlay(key) {
    return (_ssData.overlays || []).find(o => o.key === key) || null;
}


function _ssDistinct(field) {
    return Array.from(new Set(_ssData.points[field] || [])).sort();
}


// Stable category → colour map for a categorical overlay field.
function _ssCatColorMap(field) {
    const map = {};
    _ssDistinct(field).forEach((v, i) => { map[v] = _SS_PALETTE[i % _SS_PALETTE.length]; });
    return map;
}


// Current zoom window in data coordinates, or null when the map is fully zoomed
// out (autoranged) — in which case every centroid stays eligible for a label.
function _ssCurrentRanges(div) {
    const fl = div && div._fullLayout;
    if (!fl || !fl.xaxis || !fl.yaxis) { return null; }
    if (fl.xaxis.autorange || fl.yaxis.autorange) { return null; }
    if (!fl.xaxis.range || !fl.yaxis.range) { return null; }
    return { x: fl.xaxis.range.slice(), y: fl.yaxis.range.slice() };
}


// Centroids whose median position falls inside the current view. With no zoom
// window every centroid qualifies; zooming in shrinks the set so smaller niches
// (previously crowded out) become eligible for a label.
function _ssVisibleCentroids(ranges) {
    const cents = _ssData._centroids || [];
    if (!ranges) { return cents; }
    const xlo = Math.min(ranges.x[0], ranges.x[1]);
    const xhi = Math.max(ranges.x[0], ranges.x[1]);
    const ylo = Math.min(ranges.y[0], ranges.y[1]);
    const yhi = Math.max(ranges.y[0], ranges.y[1]);
    return cents.filter(c => c.x >= xlo && c.x <= xhi && c.y >= ylo && c.y <= yhi);
}


function _ssLabelAnnotation(c) {
    return {
        x: c.x, y: c.y, text: c.name, showarrow: false,
        font: { family: getCSSVar('--font-sans'), size: 10, color: getCSSVar('--white') },
        bgcolor: getCSSVar('--chart-badge-bg'), borderpad: 2, opacity: 0.92
    };
}


// Centroid label annotations for the current view. Labels are niche markers, so
// they show whenever the checkbox is on regardless of which variable colours the
// points. While focusing, only the focused niche is labelled; otherwise the
// largest in-view niches are kept (capped at _SS_MAX_LABELS), so zooming in
// reveals more of them.
function _ssBuildLabels(focusNiche, showLabels, ranges) {
    if (!showLabels) { return []; }
    if (focusNiche !== null) {
        const c = (_ssData._centroids || []).find(cc => cc.niche === focusNiche);
        return c ? [_ssLabelAnnotation(c)] : [];
    }
    return _ssVisibleCentroids(ranges)
        .slice().sort((a, b) => b.size - a.size)
        .slice(0, _SS_MAX_LABELS)
        .map(_ssLabelAnnotation);
}


// Re-label as the user zooms/pans: recompute which centroids are in view and
// update just the annotation layer (no scatter redraw). Debounced so scroll-zoom
// bursts coalesce. Ignores the annotation-only relayouts this handler triggers.
function _ssOnZoomRelayout(ev) {
    const div = document.getElementById('semantic-space-plot');
    if (!div || !_ssData) { return; }
    const axisChange = Object.keys(ev).some(k => k.indexOf('xaxis') === 0 || k.indexOf('yaxis') === 0);
    if (!axisChange) { return; }
    clearTimeout(_ssLabelTimer);
    _ssLabelTimer = setTimeout(() => {
        const showLabels = (document.getElementById('ss-show-labels') || {}).checked;
        Plotly.relayout(div, {
            annotations: _ssBuildLabels(_ssFocusNiche, showLabels, _ssCurrentRanges(div))
                .concat(_ssFlashAnnotations())
        });
    }, 100);
}


function renderSemanticSpace() {
    if (!_ssData) { return; }
    const div = document.getElementById('semantic-space-plot');
    if (!div || typeof Plotly === 'undefined') { return; }

    const mode = (document.getElementById('ss-color-mode') || {}).value || 'niche';
    const showLabels = (document.getElementById('ss-show-labels') || {}).checked;
    const P = _ssData.points;
    const n = P.x.length;
    const focusNiche = _ssFocusNiche;

    // Switching the colour variable clears any per-category hide toggles (the
    // hidden set only makes sense for the categories currently on screen).
    if (mode !== _ssLastColorMode) { _ssHidden.clear(); _ssLastColorMode = mode; }

    // Per-point colour by the selected mode. Niche + categorical overlays use
    // the discrete palette; numeric overlays use the continuous _SS_NUMERIC_SCALE.
    const overlay = mode === 'niche' ? null : _ssOverlay(mode);
    let colorArr;
    let markerExtra = {};
    let catColorMap = null;
    if (overlay && overlay.kind === 'numeric') {
        // Clamp the colour range to the field's robust [p2, p98] so outliers
        // saturate at the ends instead of washing the bulk into one hue. Nulls
        // map to the low end.
        const [clo, chi] = _ssRobustRange(overlay.field);
        colorArr = (P[overlay.field] || []).map(v => (v == null ? clo : v));
        // Colour the points by the numeric scale, but DON'T draw Plotly's
        // in-figure colourbar — it resizes the plot box and (via the 1:1 aspect
        // lock) shifts the scatter when the scale changes. The scale is shown as
        // an HTML gradient legend below the plot instead (see _ssRenderLegend),
        // so the plot box never changes between colour modes.
        markerExtra = { colorscale: _SS_NUMERIC_COLORSCALE, showscale: false, cmin: clo, cmax: chi };
    } else if (overlay && overlay.kind === 'categorical') {
        catColorMap = _ssCatColorMap(overlay.field);
        colorArr = (P[overlay.field] || []).map(v => catColorMap[v] || '#888');
    } else {
        colorArr = P.niche.map(nn => _SS_PALETTE[nn % _SS_PALETTE.length]);
    }

    // Per-point size/opacity. Focus enlarges one niche and fades the rest;
    // legend toggles hide whole categories (size + opacity 0 so they vanish and
    // stop catching hovers). Hiding wins over focus. None of this changes the
    // plot box, so the scatter stays put.
    // While the trajectory overlay is on, fade the base dots so the clouds and
    // path (which now draw above them) clearly dominate; niche colour stays for
    // context.
    const baseOpacity = _ssTrajVisible() ? 0.18 : 0.75;
    let sizeArr = 4;
    let opacityArr = baseOpacity;
    const hideField = (overlay && overlay.kind === 'categorical' && _ssHidden.size) ? overlay.field : null;
    if (focusNiche !== null || hideField) {
        sizeArr = new Array(n);
        opacityArr = new Array(n);
        for (let i = 0; i < n; i++) {
            if (hideField && _ssHidden.has(P[hideField][i])) {
                sizeArr[i] = 0;
                opacityArr[i] = 0;
            } else if (focusNiche !== null) {
                const inFocus = P.niche[i] === focusNiche;
                sizeArr[i] = inFocus ? 7 : 3;
                opacityArr[i] = inFocus ? 0.9 : 0.08;
            } else {
                sizeArr[i] = 4;
                opacityArr[i] = baseOpacity;
            }
        }
    }

    const ovField = overlay ? overlay.field : null;
    const hover = new Array(n);
    for (let i = 0; i < n; i++) {
        const extra = ovField ? `<br>${_ssWrap(`${overlay.label}: ${P[ovField][i]}`)}` : '';
        hover[i] = `<b>${_ssWrap(P.niche_name[i])}</b>${extra}<br>${_ssWrap(P.story[i])}`;
    }

    const trace = {
        type: 'scattergl', mode: 'markers',
        x: P.x, y: P.y,
        customdata: P.item_id,
        text: hover, hoverinfo: 'text',
        // Plotly sizes the hover box to its longest line, so the wrapping above
        // is what keeps it narrow; left-align so the wrapped lines read as a
        // paragraph rather than a centred stack.
        hoverlabel: { align: 'left' },
        marker: Object.assign({ size: sizeArr, color: colorArr, opacity: opacityArr,
            line: { width: 0 } }, markerExtra)
    };

    // Centroid niche labels, scoped to the current zoom window so more (smaller)
    // niches get labelled as the user zooms in. Carry that window into the layout
    // too, so recolouring keeps the user's zoom instead of snapping to overview.
    const ranges = _ssCurrentRanges(div);
    const annotations = _ssBuildLabels(focusNiche, showLabels, ranges)
        .concat(_ssFlashAnnotations());

    const layout = {
        hovermode: 'closest', showlegend: false,
        // Symmetric margins: with no in-figure colourbar (the scale lives in the
        // HTML legend below the plot), the plot box is identical in every colour
        // mode, so the scatter never shifts when the colour variable changes.
        margin: { l: 10, r: 10, t: 10, b: 10 },
        xaxis: Object.assign({ visible: false, fixedrange: false },
            ranges ? { range: ranges.x, autorange: false } : {}),
        yaxis: Object.assign({ visible: false, scaleanchor: 'x', scaleratio: 1 },
            ranges ? { range: ranges.y, autorange: false } : {}),
        paper_bgcolor: getCSSVar('--chart-bg'),
        plot_bgcolor: getCSSVar('--chart-bg'),
        font: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text') },
        annotations: annotations,
        // Per-period dispersion ellipses (layer:'above' → on top of the dots).
        // Empty when the overlay is off, so toggling clears them. The flash
        // rings share the layer, and are likewise empty between pulses.
        shapes: _ssTrajectoryShapes()
    };

    // Base scatter stays trace 0; trajectory overlays (if any) are appended
    // after it, so the click/zoom/focus/legend logic above is untouched.
    Plotly.react(div, [trace].concat(_ssTrajectoryTraces(), _ssFlashTraces()), layout,
        { responsive: true, displayModeBar: true, scrollZoom: true });
    _ssRenderLegend(mode, overlay, catColorMap);
    _ssRenderNicheInfo(focusNiche);

    if (!div._ssClickWired) {
        div._ssClickWired = true;
        div.on('plotly_click', function (ev) {
            const pt = ev.points && ev.points[0];
            // Only the base scatter (curve 0) opens a video; overlay traces ignore clicks.
            if (pt && pt.curveNumber === 0 && pt.customdata) {
                _ssOnPointClick(pt.customdata, pt.pointNumber);
            }
        });
        div.on('plotly_relayout', _ssOnZoomRelayout);
    }
}


// Clicking a dot asks what the reader meant by it rather than assuming. The two
// readings of a click are genuinely different tasks — "show me videos like this
// one" (Video Analysis, filtered to the dot's NICHE, not the single video) and
// "show me this part of the map" (focus the niche here) — and a click that
// silently threw the reader onto another tab served only the first.
function _ssOnPointClick(itemId, i) {
    const P = _ssData && _ssData.points;
    if (!P || !itemId) { return; }
    // The trace is built from the payload arrays unfiltered, so Plotly's point
    // index addresses the parallel arrays directly. Guard anyway: a mismatch
    // would attach the wrong niche to the drill-down.
    const ok = (i != null && i < P.item_id.length && P.item_id[i] === itemId);
    if (!ok) { return; }
    const nicheId = P.niche[i];
    const nicheName = (_ssData.niches[nicheId] || {}).name || P.niche_name[i];
    const size = (_ssData.niches[nicheId] || {}).size || 0;

    _ssShowNicheDialog({
        nicheId: nicheId,
        nicheName: nicheName,
        size: size,
        story: P.story[i] || '',
        intro: 'You clicked a video in:'
    });
}


// Send Video Analysis to the clicked dot's niche. Deliberately NOT to the video
// itself: one dot out of a niche is a sample, and the reader who clicked it is
// asking about the neighbourhood it belongs to.
function _ssDrillToNiche(nicheName) {
    if (!nicheName) { return; }
    // Same contract Explore / Correlations / Timelines use (consumed by
    // checkPendingDrillDown, which enforces a 5s freshness window — hence the
    // synchronous tab click below).
    window._pendingDrillDown = {
        filters: { niche_name: { type: 'category', value: [nicheName] } },
        searchQuery: '',
        timestamp: Date.now()
    };
    const tabBtn = document.querySelector('.tab-button[onclick*="video_analysis"]');
    if (tabBtn) {
        tabBtn.click();
    } else {
        window._pendingDrillDown = null;
    }
}


// The niche dialog, shared by both ways of naming a niche: clicking a dot on
// the map, and clicking one of the measured neighbours in the detail bar. Both
// raise the same question, so both get the same two answers. Reuses the
// drill-down popup styling shared with Explore.
// "Open in Video Analysis" is unavailable without an active study (the tab is
// study-scoped) or without permission for that tab — in both cases the button
// is disabled and the reason is stated, rather than the click doing nothing.
function _ssShowNicheDialog(info) {
    const existing = document.getElementById('ss-point-dialog');
    if (existing) { existing.remove(); }

    const study = (window.studyState && window.studyState.current) || null;
    const hasTab = !!document.querySelector('.tab-button[onclick*="video_analysis"]');
    const canDrill = !!(study && hasTab);
    const blocked = !hasTab
        ? 'You do not have access to the Video Analysis tab.'
        : (!study ? 'Select a study first — Video Analysis is scoped to one study.' : '');

    const overlay = document.createElement('div');
    overlay.id = 'ss-point-dialog';
    overlay.className = 'drilldown-overlay';

    const card = document.createElement('div');
    card.className = 'drilldown-card ss-point-dialog-card';
    const story = info.story
        ? `<p class="text-sm" style="margin: 0 0 12px; color: var(--color-text-muted);">`
            + `“${escapeHtml(_ssTruncate(info.story, 150))}”</p>`
        : '';
    card.innerHTML = `
        <div class="drilldown-header">
            <span class="drilldown-icon">&#x1F50E;</span>
            <span class="text-h3 font-semibold">What would you like to do?</span>
        </div>
        <p class="text-body" style="margin: 12px 0 6px; color: var(--color-text-secondary);">
            ${escapeHtml(info.intro || 'Niche:')}
        </p>
        <div class="drilldown-filter-preview" style="margin-bottom: 10px;">
            <span class="font-semibold" style="color: var(--color-accent);">${escapeHtml(info.nicheName)}</span>
            <span style="color: var(--color-text-muted); margin-left: 8px;">${info.size.toLocaleString()} videos</span>
        </div>
        ${story}
        <p class="text-sm" style="margin: 0 0 16px; color: var(--color-text-muted);">
            ${canDrill
                ? (info.story
                    ? 'Video Analysis will be filtered to the whole niche, not this single video.'
                    : 'Video Analysis will be filtered to this niche.')
                : escapeHtml(blocked)}
        </p>
        <div class="drilldown-actions" style="flex-wrap: wrap;">
            <button class="btn btn-discreet ss-dlg-cancel">Cancel</button>
            <button class="btn btn-discreet ss-dlg-focus">Focus this niche</button>
            <button class="btn btn-primary ss-dlg-go"${canDrill ? '' : ' disabled'}>Open in Video Analysis</button>
        </div>
    `;
    overlay.appendChild(card);
    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add('visible'));

    const keyHandler = ev => {
        if (ev.key === 'Escape') { dismiss(); }
    };
    function dismiss() {
        document.removeEventListener('keydown', keyHandler);
        overlay.classList.remove('visible');
        setTimeout(() => overlay.remove(), 200);
    }
    document.addEventListener('keydown', keyHandler);

    card.querySelector('.ss-dlg-cancel').onclick = dismiss;
    card.querySelector('.ss-dlg-focus').onclick = () => {
        dismiss();
        _ssSetFocusNiche(info.nicheId);
    };
    const go = card.querySelector('.ss-dlg-go');
    if (canDrill) {
        go.onclick = () => { dismiss(); _ssDrillToNiche(info.nicheName); };
    }
    overlay.addEventListener('click', ev => { if (ev.target === overlay) { dismiss(); } });
}


function _ssTruncate(text, limit) {
    const s = String(text || '');
    return s.length <= limit ? s : s.slice(0, limit - 1).trimEnd() + '…';
}


// ---------------------------------------------------------------------------
// "Flash the closest niches" — pulse the measured nearest niches on the map.
// The detail bar names them; the map is where they are, and the two routinely
// disagree because the projection can only place a niche in one spot. Pulsing
// them is the cheapest way to show that disagreement rather than assert it.
// ---------------------------------------------------------------------------

function _ssStopFlash() {
    _ssFlashTimers.forEach(clearTimeout);
    _ssFlashTimers = [];
    _ssFlashNiches = null;
}


// The pulsed niches' OWN points, as one bright overlay trace (empty between
// pulses). A ring said "somewhere around here" and drew a circle over whatever
// else lives inside it; lighting up the actual dots shows the niche's real
// shape and extent, which on this map is rarely circular.
//
// Colour is the chart's text colour on the chart's background — the one pair
// guaranteed to contrast in both themes, and the one pair no niche in the
// tab20 palette can be confused with.
function _ssFlashTraces() {
    if (!_ssFlashNiches) { return []; }
    // The pulse re-enters here once per on-phase with the same niches, so the
    // concatenated coordinates are built once per flash, not once per pulse.
    const key = _ssFlashNiches.join(',');
    if (!_ssFlashXY || _ssFlashXY.key !== key) {
        const xs = [], ys = [];
        _ssFlashNiches.forEach(id => {
            const c = _ssCentroid(id);
            if (!c) { return; }
            for (let i = 0; i < c.xs.length; i++) { xs.push(c.xs[i]); ys.push(c.ys[i]); }
        });
        _ssFlashXY = { key: key, xs: xs, ys: ys };
    }
    const { xs, ys } = _ssFlashXY;
    if (!xs.length) { return []; }
    return [{
        type: 'scattergl', mode: 'markers', x: xs, y: ys,
        marker: {
            size: 9, color: getCSSVar('--chart-text'),
            line: { width: 1, color: getCSSVar('--chart-bg') }
        },
        hoverinfo: 'skip', showlegend: false
    }];
}


// Name labels for the pulsed niches — a ring alone says "over there", not
// "that is the one called X".
function _ssFlashAnnotations() {
    if (!_ssFlashNiches) { return []; }
    return _ssFlashNiches.map(id => {
        const c = _ssCentroid(id);
        if (!c) { return null; }
        return {
            x: c.x, y: c.y + Math.max(c.r, 0.6), text: c.name, showarrow: false,
            yanchor: 'bottom',
            font: { family: getCSSVar('--font-sans'), size: 11, color: getCSSVar('--white') },
            bgcolor: getCSSVar('--color-accent'), borderpad: 3, opacity: 0.95
        };
    }).filter(Boolean);
}


// Redraw one pulse frame. The flash is now a trace rather than a shape, so this
// is a react rather than a relayout — but trace 0 is passed by the SAME object
// reference, so Plotly diffs it as unchanged and never rebuilds the corpus-sized
// gl layer. Same technique the trajectory playback uses to hold ~60 fps.
function _ssFlashRedraw() {
    const div = document.getElementById('semantic-space-plot');
    if (!div || !div.data || !div.data.length) { return; }
    const showLabels = (document.getElementById('ss-show-labels') || {}).checked;
    const layout = Object.assign({}, div.layout, {
        shapes: _ssTrajectoryShapes(),
        annotations: _ssBuildLabels(_ssFocusNiche, showLabels, _ssCurrentRanges(div))
            .concat(_ssFlashAnnotations())
    });
    Plotly.react(div, [div.data[0]].concat(_ssTrajectoryTraces(), _ssFlashTraces()), layout,
        { responsive: true, displayModeBar: true, scrollZoom: true });
}


// Widen the view if any pulse target sits outside it. Without this the button
// silently does nothing whenever the reader has zoomed in — which is exactly
// when they are most likely to ask where the neighbours went. The requested box
// is squared first: the axes are aspect-locked, so an oblong request would be
// silently reinterpreted by Plotly.
function _ssEnsureVisible(div, targets) {
    const ranges = _ssCurrentRanges(div);
    if (!ranges) { return; }   // fully zoomed out: everything is already in view
    const xs = [], ys = [];
    targets.forEach(c => {
        const r = Math.max(c.r, 0.6);
        xs.push(c.x - r, c.x + r);
        ys.push(c.y - r, c.y + r);
    });
    if (!xs.length) { return; }
    const xlo = Math.min(ranges.x[0], ranges.x[1]), xhi = Math.max(ranges.x[0], ranges.x[1]);
    const ylo = Math.min(ranges.y[0], ranges.y[1]), yhi = Math.max(ranges.y[0], ranges.y[1]);
    const inside = Math.min(...xs) >= xlo && Math.max(...xs) <= xhi
        && Math.min(...ys) >= ylo && Math.max(...ys) <= yhi;
    if (inside) { return; }

    const nx0 = Math.min(xlo, ...xs), nx1 = Math.max(xhi, ...xs);
    const ny0 = Math.min(ylo, ...ys), ny1 = Math.max(yhi, ...ys);
    const cx = (nx0 + nx1) / 2, cy = (ny0 + ny1) / 2;
    const half = Math.max(nx1 - nx0, ny1 - ny0) / 2 * 1.08;   // square + a margin
    Plotly.relayout(div, {
        'xaxis.range': [cx - half, cx + half],
        'yaxis.range': [cy - half, cy + half]
    });
}


// Pulse the focused niche's measured nearest niches _SS_FLASH_PULSES times.
function _ssFlashNearest(focusNiche) {
    const div = document.getElementById('semantic-space-plot');
    if (!div || !_ssData || focusNiche === null) { return; }
    const meta = (_ssData.niches || {})[focusNiche] || {};
    const ids = (meta.nearest_ids || []).slice(0, 3).filter(id => _ssCentroid(id));
    if (!ids.length) { return; }

    _ssStopFlash();
    const focused = _ssCentroid(focusNiche);
    _ssEnsureVisible(div, ids.map(_ssCentroid).concat(focused ? [focused] : []));

    // Schedule the whole on/off sequence up front; every handle is tracked so a
    // focus change (or a second click) can cancel a pulse mid-flight.
    let at = 0;
    for (let p = 0; p < _SS_FLASH_PULSES; p++) {
        _ssFlashTimers.push(setTimeout(() => {
            _ssFlashNiches = ids;
            _ssFlashRedraw();
        }, at));
        at += _SS_FLASH_ON_MS;
        const last = p === _SS_FLASH_PULSES - 1;
        _ssFlashTimers.push(setTimeout(() => {
            _ssFlashNiches = null;
            _ssFlashRedraw();
            // The final off-pulse retires the whole sequence, so a finished
            // flash leaves no spent handles behind for the next one to cancel.
            if (last) { _ssFlashTimers = []; }
        }, at));
        at += _SS_FLASH_OFF_MS;
    }
}


// Robust [low, high] colour range for a numeric overlay: the field's
// _SS_ROBUST_PCT / (1 - _SS_ROBUST_PCT) percentiles over its non-null values
// (cached per field on _ssData). Used as Plotly's cmin/cmax and for the legend
// endpoints, so the same robust range drives both the dots and the scale.
function _ssRobustRange(field) {
    if (!_ssData) { return [0, 1]; }
    _ssData._robust = _ssData._robust || {};
    if (_ssData._robust[field]) { return _ssData._robust[field]; }
    const src = _ssData.points[field] || [];
    const arr = [];
    for (let i = 0; i < src.length; i++) {
        const v = src[i];
        if (v != null && isFinite(v)) { arr.push(v); }
    }
    let range;
    if (!arr.length) {
        range = [0, 1];
    } else {
        arr.sort((a, b) => a - b);
        const at = q => arr[Math.min(arr.length - 1, Math.max(0, Math.round(q * (arr.length - 1))))];
        let lo = at(_SS_ROBUST_PCT), hi = at(1 - _SS_ROBUST_PCT);
        if (!(hi > lo)) { hi = lo + (Math.abs(lo) || 1) * 1e-3 + 1e-6; }
        range = [lo, hi];
    }
    _ssData._robust[field] = range;
    return range;
}


function _ssFmtNum(v) {
    return formatMetricNumber(v);
}


// Break a hover line at word boundaries so Plotly's hover box stops widening to
// fit it. Video stories are a single unbroken 140-character run and categorical
// overlay values can be nearly as long, either of which produces a hover box
// wider than the plot. A word longer than the limit is left intact (it would
// only be hyphenated mid-token otherwise).
const _SS_HOVER_WRAP = 48;

function _ssWrap(text, width) {
    const s = (text === null || text === undefined) ? '' : String(text);
    const limit = width || _SS_HOVER_WRAP;
    if (s.length <= limit) { return s; }
    const lines = [];
    let line = '';
    for (const word of s.split(/\s+/)) {
        if (!word) { continue; }
        if (!line) {
            line = word;
        } else if (line.length + 1 + word.length <= limit) {
            line += ` ${word}`;
        } else {
            lines.push(line);
            line = word;
        }
    }
    if (line) { lines.push(line); }
    return lines.join('<br>');
}


// Click a categorical swatch to hide/show that category's points (delegated
// from #ss-legend; swatches are recreated on every render).
function _ssOnLegendClick(ev) {
    const sw = ev.target.closest('[data-cat-idx]');
    if (!sw || !_ssLegendCats) { return; }
    const cat = _ssLegendCats[+sw.dataset.catIdx];
    if (cat === undefined) { return; }
    if (_ssHidden.has(cat)) { _ssHidden.delete(cat); } else { _ssHidden.add(cat); }
    renderSemanticSpace();
}


// Colour legend, rendered as HTML below the plot (never inside the Plotly
// figure — see the template note). Category swatches (clickable to hide/show)
// or a numeric gradient bar.
function _ssRenderLegend(mode, overlay, catColorMap) {
    const legend = document.getElementById('ss-legend');
    if (!legend) { return; }
    const openHint = '<span style="margin-left:auto;white-space:nowrap;">click a point to focus or open its niche</span>';
    if (overlay && overlay.kind === 'categorical' && catColorMap) {
        _ssLegendCats = _ssDistinct(overlay.field);
        const swatches = _ssLegendCats.map((c, i) => {
            const off = _ssHidden.has(c);
            return `<span data-cat-idx="${i}" style="display:inline-flex;align-items:center;gap:4px;`
                + `white-space:nowrap;cursor:pointer;opacity:${off ? 0.45 : 1};`
                + `text-decoration:${off ? 'line-through' : 'none'};">`
                + `<span style="width:9px;height:9px;border-radius:2px;background:${catColorMap[c]};`
                + `display:inline-block;${off ? 'filter:grayscale(1);' : ''}"></span>${c}</span>`;
        }).join('');
        const hint = '<span style="margin-left:auto;white-space:nowrap;">click a swatch to show/hide · click a point to focus or open its niche</span>';
        legend.innerHTML = swatches + hint;
    } else if (overlay && overlay.kind === 'numeric') {
        _ssLegendCats = null;
        // Endpoints are the robust [p2, p98] range used to colour the dots; the
        // ≤/≥ marks that values beyond it saturate at the ends.
        const [lo, hi] = _ssRobustRange(overlay.field);
        const grad = `linear-gradient(to right, ${_SS_NUMERIC_SCALE.join(', ')})`;
        legend.innerHTML =
            `<span class="font-medium" style="white-space:nowrap;">${overlay.label}</span>`
            + `<span>≤${_ssFmtNum(lo)}</span>`
            + `<span style="width:140px;height:10px;border-radius:2px;background:${grad};display:inline-block;"></span>`
            + `<span>≥${_ssFmtNum(hi)}</span>` + openHint;
    } else {
        _ssLegendCats = null;
        legend.innerHTML = `<span>Coloured by niche</span>${openHint}`;
    }
}


// ---------------------------------------------------------------------------
// Collection trajectory overlay — projects one collection's play activity onto
// the map as a centre of gravity, a dispersion "entropy halo", and a daily
// path through semantic space. The geometry comes from the backend
// (/api/semantic_space/trajectory); see web_interface/semantic_trajectory.py.
// ---------------------------------------------------------------------------

// Populate the collection selector for the currently-selected study. Each
// option shows the collection's display id (falling back to the raw id) as its
// label, with the raw collection_id as the value. Reloads when the active study
// changes; cheap to re-call (no-ops if already loaded for this study).
async function _ssLoadCollections(attempt) {
    const sel = document.getElementById('ss-collection');
    if (!sel) { return; }
    const study = (window.studyState && window.studyState.current) || '';
    if (_ssCollectionsStudy === study && sel.options.length > 1) { return; }
    attempt = attempt || 0;
    try {
        const qs = study ? ('?study=' + encodeURIComponent(study)) : '';
        const res = await fetch('/api/semantic_space/collections' + qs);
        if (!res.ok) { throw new Error(`status ${res.status}`); }
        const data = await res.json();
        const cols = (data && data.collections) || [];
        const prev = sel.value;   // preserve the user's choice if still in-study
        sel.innerHTML = '';
        const ph = document.createElement('option');
        ph.value = ''; ph.textContent = '— select collection —';
        sel.appendChild(ph);
        cols.forEach(c => {
            const o = document.createElement('option');
            o.value = c.id; o.textContent = c.label || c.id;
            sel.appendChild(o);
        });
        if (prev && cols.some(c => c.id === prev)) { sel.value = prev; }
        _ssCollectionsStudy = study;
    } catch (e) {
        // Network / cold-start failure — retry a few times before giving up. An
        // OK-but-empty response is a valid "no collections in this study" answer,
        // not an error, so it is accepted (placeholder only).
        if (attempt < 5) {
            setTimeout(() => _ssLoadCollections(attempt + 1), 1000);
        } else {
            console.error('Failed to load collections', e);
        }
    }
}


// Fetch the selected collection's trajectory for the current interval, then
// re-render. Deselecting clears the overlay.
async function _ssLoadTrajectory() {
    const sel = document.getElementById('ss-collection');
    const cid = sel ? sel.value : '';
    const statusEl = document.getElementById('ss-traj-status');
    // A new collection / interval invalidates any running playback or scrub.
    _ssAnimStop();
    _ssAnimPos = null;
    if (!cid) {
        _ssTrajectory = null;
        _ssSetupScrub(0);
        if (statusEl) { statusEl.textContent = ''; }
        renderSemanticSpace();
        return;
    }
    const interval = (document.getElementById('ss-traj-interval') || {}).value || 'month';
    if (statusEl) { statusEl.textContent = 'Loading trajectory…'; }
    try {
        const qs = new URLSearchParams({ collection_id: cid, interval });
        const res = await fetch('/api/semantic_space/trajectory?' + qs.toString());
        const data = await res.json();
        if (!res.ok || data.error) {
            if (statusEl) { statusEl.textContent = data.error || `Error ${res.status}`; }
            return;
        }
        _ssTrajectory = data;
        _ssSetupScrub((data.points || []).length);
        // Selecting a collection turns the overlay on (the toggle stays the
        // master switch thereafter).
        const tog = document.getElementById('ss-show-trajectory');
        if (tog) { tog.checked = true; }
        _ssTrajOn = true;
        if (statusEl) { statusEl.textContent = _ssTrajSummary(data); }
        renderSemanticSpace();
    } catch (e) {
        console.error(e);
        if (statusEl) { statusEl.textContent = 'Failed to load trajectory.'; }
    }
}


// Configure the scrub slider for a trajectory of n periods (disabled when
// there's nothing to scrub through, e.g. "All-time only" or a single period).
function _ssSetupScrub(n) {
    const scrub = document.getElementById('ss-scrub');
    if (!scrub) { return; }
    scrub.max = Math.max(0, n - 1);
    scrub.value = 0;
    scrub.disabled = n < 2;
}


// Scrub handler: jump to the dragged period (frame mode). Stops any running
// playback; no smooth tween — each input just renders that frame.
function _ssOnScrub() {
    if (!_ssTrajVisible() || !_ssTrajectory) { return; }
    const n = (_ssTrajectory.points || []).length;
    if (n < 2) { return; }
    _ssAnimStop();
    const scrub = document.getElementById('ss-scrub');
    _ssAnimPos = Math.max(0, Math.min(n - 1, parseFloat(scrub.value) || 0));
    _ssAnimFrame();
    _ssAnimRefreshCaption();
}


// One-line summary for the status span (plays · days · all-time entropy).
// ↗/↘/→ for a {slope} trend object (from the payload's `trends`).
function _ssTrendArrow(t) {
    if (!t || t.slope === 0) { return '→'; }
    return t.slope > 0 ? '↗' : '↘';
}


function _ssTrajSummary(data) {
    const at = data.all_time;
    if (!at || at.x == null) { return 'No mapped plays for this collection in range.'; }
    const n = (data.points || []).length;
    const unit = data.interval === 'month' ? 'month' : (data.interval === 'week' ? 'week' : 'day');
    const wt = data.weight_mode === 'count' ? ' · unweighted (no watch time)' : '';
    // Headline = plays the metrics actually use (those with a niche). Unmapped
    // plays (video not in the corpus) become a coverage caveat, so the count
    // always matches the H / centroid / trend denominators.
    const total = data.n_plays_total || 0;
    const mapped = total - (data.n_unmapped || 0);
    const pct = total > 0 ? Math.round(100 * mapped / total) : 0;
    const head = data.n_unmapped
        ? `${pct}% of ${total.toLocaleString()} plays are mapped`
        : `${total.toLocaleString()} plays`;
    let summary = head
        + (n ? ` · ${n} ${unit}${n === 1 ? '' : 's'}` : '')
        + ` · H=${at.niche_entropy} (Ĥ=${at.niche_entropy_norm})${wt}`;
    // Per-series trend arrows + path directness (tortuosity), when computed.
    const tr = data.trends || {};
    const bits = [];
    if (tr.niche_entropy) { bits.push(`entropy ${_ssTrendArrow(tr.niche_entropy)}`); }
    if (tr.novelty) { bits.push(`novelty ${_ssTrendArrow(tr.novelty)}`); }
    if (tr.mean_political_score) { bits.push(`political ${_ssTrendArrow(tr.mean_political_score)}`); }
    if (tr.mean_sensitivity_score) { bits.push(`sensitivity ${_ssTrendArrow(tr.mean_sensitivity_score)}`); }
    if (bits.length) { summary += ` · trend: ${bits.join(' ')}`; }
    if (data.tortuosity != null) { summary += ` · directness ${data.tortuosity}`; }
    return summary;
}


// Time gradient (t in [0,1], early→late): cool blue → warm orange.
function _ssLerp(a, b, t) { return Math.round(a + (b - a) * t); }


function _ssTimeColorRGB(t) {
    return [
        _ssLerp(_SS_TRAJ_T0[0], _SS_TRAJ_T1[0], t),
        _ssLerp(_SS_TRAJ_T0[1], _SS_TRAJ_T1[1], t),
        _ssLerp(_SS_TRAJ_T0[2], _SS_TRAJ_T1[2], t)
    ];
}


function _ssTopNichesStr(top) {
    return (top || []).map(t => `${t.name} ${Math.round(t.share * 100)}%`).join(' · ');
}


// Per-period change metrics for the hover/caption: shift (distributional
// velocity vs the previous period) and novelty (% of attention on niches never
// watched before). Both are null on the first period / when unavailable.
function _ssChangeBits(p) {
    const bits = [];
    if (p.js_from_prev != null) { bits.push(`shift ${p.js_from_prev}`); }
    if (p.novelty != null) { bits.push(`${Math.round(p.novelty * 100)}% new`); }
    return bits.join(' · ');
}


function _ssTrajHover(p) {
    const lo = p.low_volume ? ' (low volume)' : '';
    const ch = _ssChangeBits(p);
    return `<b>${p.date}</b><br>${p.n_mapped} plays · H=${p.niche_entropy}${lo}`
        + (ch ? `<br>${ch}` : '')
        + `<br>${_ssTopNichesStr(p.top_niches)}`;
}


// A rotated dispersion ellipse as a Plotly LAYOUT SHAPE (type:path). Shapes with
// layer:'above' draw above the WebGL scatter, so the cloud sits ON TOP of the
// dots (an SVG scatter trace would be hidden beneath the gl canvas). t in [0,1]
// drives the time gradient; the fill is translucent so overlapping clouds read.
function _ssEllipseShape(ell, t, alphaMul, emphasize) {
    alphaMul = (alphaMul == null) ? 1 : alphaMul;
    const steps = 40;
    const th = ell.theta * Math.PI / 180;
    const ct = Math.cos(th), st = Math.sin(th);
    const rx = ell.rx * _SS_ELLIPSE_SCALE, ry = ell.ry * _SS_ELLIPSE_SCALE;
    let d = '';
    for (let i = 0; i <= steps; i++) {
        const a = (i / steps) * 2 * Math.PI;
        const ex = rx * Math.cos(a), ey = ry * Math.sin(a);
        const x = ell.cx + ex * ct - ey * st;
        const y = ell.cy + ex * st + ey * ct;
        d += (i === 0 ? 'M' : 'L') + x.toFixed(3) + ',' + y.toFixed(3) + ' ';
    }
    d += 'Z';
    const c = _ssTimeColorRGB(t);
    const fillA = (emphasize ? 0.36 : 0.16) * alphaMul;
    const lineA = (emphasize ? 1.0 : 0.9) * alphaMul;
    return {
        type: 'path', path: d, layer: 'above',
        fillcolor: `rgba(${c[0]},${c[1]},${c[2]},${fillA.toFixed(3)})`,
        line: { color: `rgba(${c[0]},${c[1]},${c[2]},${lineA.toFixed(3)})`, width: emphasize ? 2.5 : 1.5 }
    };
}


// Fade factor for the period at index i given the continuous playback position
// t (a float). Static (t == null) → 1 (every period at full strength). During
// playback, future periods (i > t) are hidden and older ones decay
// geometrically by their *fractional* age (t - i), so the trail fades smoothly
// as the head glides between periods.
function _ssAnimFactor(i, t) {
    if (t == null) { return 1; }
    if (i > t) { return 0; }
    return Math.pow(_SS_ANIM_FADE, t - i);
}


// Linearly interpolate two dispersion ellipses (centre/axes lerp; theta along
// the shortest angular path, mod 180° since the ellipse is symmetric). Returns
// the non-null one if only one exists, or null if neither does.
function _ssLerpEllipse(a, b, frac) {
    if (!a && !b) { return null; }
    if (!a) { return b; }
    if (!b) { return a; }
    const lerp = (u, v) => u + (v - u) * frac;
    let dth = (((b.theta - a.theta + 90) % 180) + 180) % 180 - 90;
    return {
        cx: lerp(a.cx, b.cx), cy: lerp(a.cy, b.cy),
        rx: lerp(a.rx, b.rx), ry: lerp(a.ry, b.ry), theta: a.theta + dth * frac
    };
}


// Per-period dispersion ellipses as layout shapes (drawn above the WebGL dots).
// Static: one time-graded cloud per period. Playback: a fading trail of ghost
// clouds (faded by fractional age) plus one bright "head" ellipse interpolated
// between the current and next period — so it glides rather than jumps. The
// >_SS_TRAJ_MAX_ELLIPSES skip applies only to the static view (playback shows
// just a short trailing window, so it stays cheap even at daily granularity).
function _ssTrajectoryShapes() {
    if (!_ssTrajVisible() || !_ssTrajectory) { return []; }
    const T = _ssTrajectory;
    const all = T.points || [];
    if (all.length) {
        if (_ssAnimPos == null && all.length > _SS_TRAJ_MAX_ELLIPSES) { return []; }
        const n = all.length, t = _ssAnimPos;
        const shapes = [];
        all.forEach((p, i) => {
            if (!p.ellipse) { return; }
            const f = _ssAnimFactor(i, t);
            if (f < _SS_ANIM_MIN_ALPHA) { return; }
            shapes.push(_ssEllipseShape(p.ellipse, n === 1 ? 1 : i / (n - 1), f, false));
        });
        // Gliding head ellipse (interpolated between current & next period;
        // clamped so scrubbing to the final period lands the head on it).
        if (t != null && n >= 2) {
            const k = Math.min(Math.floor(t), n - 2), frac = t - k;
            const eh = _ssLerpEllipse(all[k].ellipse, all[k + 1].ellipse, frac);
            if (eh) { shapes.push(_ssEllipseShape(eh, n === 1 ? 1 : t / (n - 1), 1, true)); }
        }
        return shapes;
    }
    if (T.all_time && T.all_time.ellipse) { return [_ssEllipseShape(T.all_time.ellipse, 1, 1, false)]; }
    return [];
}


// Trajectory point/line traces appended after the base scatter (scattergl, same
// WebGL layer as the base, drawn after it → on top of the dots). Static: a dot
// per period + the full path. Playback: ghost dots fade by fractional age, and
// a bright head dot is interpolated between the current and next period; the
// path connects the ghosts up to that gliding head. Ellipses live in
// layout.shapes (see _ssTrajectoryShapes).
function _ssTrajectoryTraces() {
    if (!_ssTrajVisible() || !_ssTrajectory) { return []; }
    const traces = [];
    const T = _ssTrajectory;
    const at = T.all_time;
    const all = T.points || [];
    const t = _ssAnimPos, n = all.length;

    const mx = [], my = [], mcolor = [], msize = [], mtext = [];
    const lx = [], ly = [];
    all.forEach((p, i) => {
        if (p.x == null || p.y == null) { return; }
        const f = _ssAnimFactor(i, t);
        if (f < _SS_ANIM_MIN_ALPHA) { return; }
        const c = _ssTimeColorRGB(n === 1 ? 1 : i / (n - 1));
        const alpha = (t == null) ? 1 : f;
        mx.push(p.x); my.push(p.y);
        mcolor.push(`rgba(${c[0]},${c[1]},${c[2]},${alpha.toFixed(3)})`);
        msize.push(12);
        mtext.push(_ssTrajHover(p));
        lx.push(p.x); ly.push(p.y);
    });

    // Gliding head dot (interpolated position) during playback/scrub.
    if (t != null && n >= 2) {
        const k = Math.min(Math.floor(t), n - 2), frac = t - k, a = all[k], b = all[k + 1];
        if (a && b && a.x != null && b.x != null) {
            const hx = a.x + (b.x - a.x) * frac, hy = a.y + (b.y - a.y) * frac;
            const c = _ssTimeColorRGB(n === 1 ? 1 : t / (n - 1));
            mx.push(hx); my.push(hy);
            mcolor.push(`rgb(${c[0]},${c[1]},${c[2]})`);
            msize.push(20);
            mtext.push('');
            lx.push(hx); ly.push(hy);
        }
    }

    if (lx.length > 1) {
        traces.push({
            type: 'scattergl', mode: 'lines', x: lx, y: ly,
            line: { color: getCSSVar('--chart-text'), width: 1.5 },
            opacity: 0.5, hoverinfo: 'skip', showlegend: false
        });
    }
    if (mx.length) {
        traces.push({
            type: 'scattergl', mode: 'markers', x: mx, y: my,
            marker: { size: msize, color: mcolor, line: { width: 1.5, color: getCSSVar('--chart-bg') } },
            text: mtext, hoverinfo: 'text', showlegend: false
        });
    }

    // All-time centre of gravity as a labelled diamond (always on top).
    if (at && at.x != null && at.y != null) {
        traces.push({
            type: 'scattergl', mode: 'markers',
            x: [at.x], y: [at.y],
            marker: {
                size: 18, symbol: 'diamond', color: _SS_TRAJ_ACCENT,
                line: { width: 2, color: getCSSVar('--chart-bg') }
            },
            text: [`<b>All-time centre</b><br>${at.n_mapped} mapped plays · `
                + `H=${at.niche_entropy} (Ĥ=${at.niche_entropy_norm})`
                + `<br>${_ssTopNichesStr(at.top_niches)}`],
            hoverinfo: 'text', showlegend: false
        });
    }
    return traces;
}


// ---- Trajectory playback: step through periods, fading the trail ------------

function _ssAnimCaption(p) {
    const top = _ssTopNichesStr(p.top_niches);
    const ch = _ssChangeBits(p);
    return `▶ ${p.date} · ${p.n_mapped} plays · H=${p.niche_entropy}`
        + (ch ? ` · ${ch}` : '')
        + (p.low_volume ? ' (low)' : '') + (top ? ` · ${top}` : '');
}


function _ssAnimSetButton(playing) {
    const btn = document.getElementById('ss-anim-play');
    if (btn) { btn.textContent = playing ? '■ Stop' : '▶ Play'; }
}


// Update the caption to the period nearest the gliding head.
function _ssAnimRefreshCaption() {
    const pts = (_ssTrajectory && _ssTrajectory.points) || [];
    if (!pts.length || _ssAnimPos == null) { return; }
    const idx = Math.max(0, Math.min(pts.length - 1, Math.round(_ssAnimPos)));
    const el = document.getElementById('ss-traj-status');
    if (el && pts[idx]) { el.textContent = _ssAnimCaption(pts[idx]); }
}


// One animation frame: redraw the overlay (interpolated head + fading trail) at
// the current position. Reuses the already-rendered base scatter (div.data[0])
// so the 30k-point gl layer is not rebuilt — only the lightweight overlay
// changes, keeping each frame ~12 ms (smooth at ~60 fps).
function _ssAnimFrame() {
    const div = document.getElementById('semantic-space-plot');
    if (!div || !div.data || !div.data.length) { renderSemanticSpace(); return; }
    const base = div.data[0];
    const layout = Object.assign({}, div.layout,
        { shapes: _ssTrajectoryShapes() });
    Plotly.react(div, [base].concat(_ssTrajectoryTraces(), _ssFlashTraces()), layout,
        { responsive: true, displayModeBar: true, scrollZoom: true });
}


// rAF tick: advance the continuous position by elapsed time, redraw, and pop
// back to the static all-periods view once the final period is reached.
function _ssAnimTick(now) {
    if (!_ssAnimPlaying) { return; }
    const N = ((_ssTrajectory && _ssTrajectory.points) || []).length;
    if (N < 2) { _ssAnimReset(); return; }
    if (_ssAnimLastTime == null) { _ssAnimLastTime = now; }
    _ssAnimPos += (now - _ssAnimLastTime) / _ssAnimStepMs;
    _ssAnimLastTime = now;
    if (_ssAnimPos >= N - 1) {
        _ssAnimPos = N - 1;
        _ssAnimFrame();
        _ssAnimRefreshCaption();
        _ssAnimReset();   // settle on the full static view
        return;
    }
    _ssAnimFrame();
    _ssAnimRefreshCaption();
    _ssAnimSyncScrub();
    _ssAnimRAF = requestAnimationFrame(_ssAnimTick);
}


// Keep the scrub slider thumb in step with the playback position.
function _ssAnimSyncScrub() {
    const scrub = document.getElementById('ss-scrub');
    if (scrub && !scrub.disabled && _ssAnimPos != null) { scrub.value = _ssAnimPos; }
}


function _ssAnimPlay() {
    if (!_ssTrajVisible() || !_ssTrajectory) { return; }
    const pts = _ssTrajectory.points || [];
    if (pts.length < 2) { return; }   // need at least two periods to morph between
    if (_ssAnimPos == null || _ssAnimPos >= pts.length - 1) { _ssAnimPos = 0; }   // (re)start
    _ssAnimPlaying = true;
    _ssAnimSetButton(true);
    // Per-period morph duration: slower with few periods, faster with many,
    // clamped to stay watchable.
    _ssAnimStepMs = Math.max(450, Math.min(1100, Math.round(9000 / pts.length)));
    _ssAnimLastTime = null;
    _ssAnimRAF = requestAnimationFrame(_ssAnimTick);
}


function _ssAnimStop() {
    if (_ssAnimRAF) { cancelAnimationFrame(_ssAnimRAF); _ssAnimRAF = null; }
    _ssAnimLastTime = null;
    _ssAnimPlaying = false;
    _ssAnimSetButton(false);
}


function _ssAnimToggle() {
    if (_ssAnimPlaying) { _ssAnimStop(); } else { _ssAnimPlay(); }
}


// Leave playback mode: stop, drop the frame index, redraw the static all-periods
// view, and restore the summary caption.
function _ssAnimReset() {
    _ssAnimStop();
    _ssAnimPos = null;
    const scrub = document.getElementById('ss-scrub');
    if (scrub) { scrub.value = 0; }
    renderSemanticSpace();
    const el = document.getElementById('ss-traj-status');
    if (el && _ssTrajectory) { el.textContent = _ssTrajSummary(_ssTrajectory); }
}


// ---------------------------------------------------------------------------
// Freshness banner — the map is global and rebuilt deliberately (not on every
// annotation), so it can lag the embedding store. We poll a light status
// endpoint while the tab is visible and surface one of four states without
// ever blocking the rendered map.
// ---------------------------------------------------------------------------

function _ssStartStatusPoll() {
    if (_ssStatusTimer) { return; }
    _ssPollStatus();
    _ssStatusTimer = setInterval(_ssPollStatus, 20000);
}


async function _ssPollStatus() {
    // Only poll while the tab is actually on screen (offsetParent is null when
    // the pane is display:none) and after the map has loaded.
    const pane = document.getElementById('semantic_space');
    if (!pane || pane.offsetParent === null || !_ssData) { return; }
    try {
        const res = await fetch('/api/semantic_space/status');
        if (!res.ok) { return; }
        _ssRenderBanner(await res.json());
    } catch (e) {
        // Transient (e.g. navigating away mid-fetch) — leave the banner as is.
    }
}


function _ssRenderBanner(s) {
    const banner = document.getElementById('ss-banner');
    const textEl = document.getElementById('ss-banner-text');
    const actionEl = document.getElementById('ss-banner-action');
    if (!banner || !textEl || !actionEl) { return; }

    const fresher = s.map_built_at != null && _ssLoadedMapBuiltAt != null
        && s.map_built_at > _ssLoadedMapBuiltAt;

    let text = '';
    let action = null;   // { label, fn }
    let warn = false;

    if (fresher && !s.map_rebuilding) {
        text = 'A new map has been calculated.';
        action = { label: 'Reload map', fn: _ssReloadMap };
    } else if (s.map_rebuilding) {
        text = '⟳ A new map is being calculated — showing the previous version…';
    } else if (s.model_mismatch) {
        const built = (s.map_meta && s.map_meta.embedding_model) || 'a different model';
        text = `This map was built with ${built}, but the active embedding backend is ` +
            `${s.active_embedding_model || 'different'} — run an embeddings refresh, then rebuild the map.`;
        warn = true;
        if (window.USER_IS_ADMIN) {
            action = { label: 'Rebuild map', fn: _ssRebuildMap };
        }
    } else if (s.map_stale) {
        const n = (s.behind || 0).toLocaleString();
        text = `This map is out of date — ${n} newer video${s.behind === 1 ? '' : 's'} embedded since it was built.`;
        warn = true;
        if (window.USER_IS_ADMIN) {
            action = { label: 'Rebuild map', fn: _ssRebuildMap };
        }
    } else if (s.embeddings_updating) {
        text = 'New videos are being added to the semantic data…';
    }

    if (!text) {
        banner.style.display = 'none';
        return;
    }
    textEl.textContent = text;
    textEl.style.color = warn ? 'var(--color-warning)' : 'var(--color-text-secondary)';
    if (action) {
        actionEl.style.display = '';
        actionEl.disabled = false;
        actionEl.textContent = action.label;
        actionEl.onclick = action.fn;
    } else {
        actionEl.style.display = 'none';
        actionEl.onclick = null;
    }
    banner.style.display = 'flex';
}


// Re-fetch the map after a rebuild, preserving the user's colour selection
// where the rebuilt map still offers it. The focus niche is preserved by
// loadSemanticSpace itself, which drops it when the rebuild no longer has that
// niche (ids can change across builds).
async function _ssReloadMap() {
    const prevMode = (document.getElementById('ss-color-mode') || {}).value;
    const banner = document.getElementById('ss-banner');
    if (banner) { banner.style.display = 'none'; }

    await loadSemanticSpace();

    const cm = document.getElementById('ss-color-mode');
    if (cm && prevMode && Array.from(cm.options).some(o => o.value === prevMode)) {
        cm.value = prevMode;
    }
    renderSemanticSpace();
}


// Admin-only: kick off a video_map_refresh. Embeddings are kept current by the
// consolidation cascade, so this rebuilds the 2D map/niches from the store.
// The backend defaults to auto_refresh, so a rebuild also re-recodes every
// study cache to propagate the new niche assignments into the analysis tabs.
async function _ssRebuildMap() {
    const actionEl = document.getElementById('ss-banner-action');
    if (actionEl) { actionEl.disabled = true; actionEl.textContent = 'Starting…'; }
    try {
        const meta = document.querySelector('meta[name="csrf-token"]');
        const res = await fetch('/api/start/video_map_refresh', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': meta ? meta.content : ''
            },
            body: '{}'
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || (data.status && data.status !== 'success')) {
            console.error('Rebuild map failed:', data.message || res.status);
        }
    } catch (e) {
        console.error('Rebuild map error:', e);
    } finally {
        if (actionEl) { actionEl.disabled = false; }
        _ssPollStatus();   // flip the banner to "being calculated…" promptly
    }
}
