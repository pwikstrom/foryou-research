// Data Pipeline: Automatic enrichment (Edit Collection modal): target, spread cap and earliest
// date controls, run progress, the queue-aware confirmation, the enrichment
// history and the armed-plan notes on the queue pages.
// One of the js/data_management/*.js files; they share one global scope and
// load in a fixed order (templates/index.html).

// --- Automatic enrichment (Edit Collection modal, single-collection only) ---
// The A+B loop's knobs and status. Settings live in the server-side
// collection_enrichment.json ledger and are saved by their own endpoint —
// the modal's main Save button never touches them.

let dmEnrichCollectionId = null;   // collection the panel is showing
let dmEnrichArmed = false;         // plan exists (any state)
let dmEnrichState = null;          // running | paused | done | blocked | null

function dmEnrichHide() {
    const group = document.getElementById('edit-collection-enrichment-group');
    if (group) group.style.display = 'none';
    dmEnrichCollectionId = null;
    if (dmEnrichRefreshTimer) {
        clearTimeout(dmEnrichRefreshTimer);
        dmEnrichRefreshTimer = null;
    }
}

// tone: 'error' | 'ok' | anything else = neutral progress text. Confirmations
// get the success colour — the span sits after the buttons, and in its neutral
// grey a "Settings saved." is easily missed.
function dmEnrichMsg(text, tone = '') {
    const el = document.getElementById('dm-enrich-msg');
    if (!el) return;
    el.textContent = text || '';
    el.style.color = (tone === true || tone === 'error')
        ? 'var(--color-danger, #c0392b)'
        : (tone === 'ok' ? 'var(--color-success, #2e7d32)' : 'var(--color-text-tertiary)');
    el.style.fontWeight = tone === 'ok' ? 'var(--weight-bold)' : '';
}

// Live figures the target widgets need between renders: the latest progress
// payload, the per-1000-items annotation cost estimate (null when the active
// backend has no pricing), and the target itself — the box that shows it is
// read-only (it renders "16,000", which no number input can hold), so this
// variable, driven by the slider, is the value that gets saved.
let dmEnrichProgressCache = {};
let dmEnrichCostPer1000 = null;
let dmEnrichTargetValue = 0;
// Two more values with no input element of their own: the earliest date is
// set by a handle on the chart, the spread's per-day cap by a log slider.
// These variables are what the form reads and the autosave writes.
let dmEnrichEarliest = '';
let dmEnrichDayCapValue = 50;
// What the last target-based estimate placed, per process, in days — the
// line under the cap slider reads it.
let dmEnrichEstimateStats = null;
// How long a cycle's steps take, from the GET payload: measured from this
// collection's recent runs, or the Hub's typical figures until it has some.
const DM_ENRICH_DEFAULT_TIMING = {
    scrape_per_min: 70, annotate_fixed_min: 8, annotate_per_video_min: 0.01,
    consolidate_min: 2, measured: { scrape: false, annotate: false, consolidate: false },
};
let dmEnrichTiming = null;
// The dirty check's baseline: the form as last filled from the saved plan
// (serialized via dmEnrichReadSettings). null until the first render.
let dmEnrichSavedSettings = null;
// The status strip's self-refresh timer (armed only while a worker runs).
let dmEnrichRefreshTimer = null;
// True from a tick's dispatch until it reports back — keeps the input-driven
// button refresh from re-enabling the tick button mid-poll.
let dmEnrichTickInFlight = false;
// The plan settings save themselves, debounced: a slider drag is one write,
// not one per pixel. These two say a write is pending or in flight, which is
// also what stops an incoming render from overwriting the form under the
// operator's hands.
let dmEnrichAutoSaveTimer = null;
let dmEnrichAutoSaveInFlight = false;
// The site-wide switch, remembered from the last GET: an autosave POST does
// not report it, and re-fetching the whole panel just to learn it would
// re-render the form mid-edit.
let dmEnrichEnabledSiteWide = true;

// The status strip's words for what the machinery is doing right now.
const DM_ENRICH_ACTIVITY_LABELS = {
    scraping: 'scraping now',
    annotating: 'annotating now',
    consolidating: 'consolidating results now',
    refreshing: 'the analyses are being refreshed — enrichment continues when that finishes',
};

// Mirrors the supervisor's auto-cycle cap (MAX_CONCURRENT_JOBS x the
// annotator's job slice): one cycle = at most one full set of concurrent
// Gemini jobs, so its annotation is ~one job turnaround.
const DM_ENRICH_AUTO_CYCLE_CAP = 2000;

function dmEnrichEffectiveCycleItems() {
    // Always automatic (there is no manual cycle-size setting): the supervisor
    // sizes each cycle as min(target headroom, one annotation job), and the
    // readout's cycle count uses the same rule on the panel's figures.
    const target = dmEnrichTargetValue || 0;
    const annotated = dmEnrichProgressCache.target_floor ?? 0;
    const remaining = Math.max(1, target - annotated);
    return Math.min(remaining, DM_ENRICH_AUTO_CYCLE_CAP);
}

function dmEnrichFillSettings(settings, progress = {}) {
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
    set('dm-enrich-sample-share', Math.round((settings.sample_share ?? 0.5) * 100));
    dmEnrichEarliest = settings.earliest_date || '';
    dmEnrichDayCapValue = Number(settings.a_day_cap) || 50;
    // The target: the stored goal, or — for a plan that has never had one — a
    // suggested starter (current annotated + 2,000, inside the reachable
    // window). A suggestion only prefills the display; nothing is saved until
    // the operator presses Save or Arm.
    const floor = progress.target_floor ?? 0;
    const ceiling = progress.target_ceiling ?? 0;
    let target = Number(settings.annotation_target) || 0;
    if (!target && ceiling > 0) {
        target = Math.min(ceiling, floor + 2000);
    }
    dmEnrichTargetValue = target;
    dmEnrichTargetBounds(progress);
    dmEnrichTargetSync(target);
    // The cap slider's window is the collection's day sizes, which the
    // progress payload carries; the chart (and so the handle) comes later.
    dmEnrichCapBounds();
    dmEnrichCapSync(dmEnrichDayCapValue);
    dmEnrichEarliestSync();
}

// ---- Annotation target: number field + log slider + bar marker ------------ #
// The slider is log-scaled: the analytically useful targets sit in the first
// few percent of a big collection, so a linear scale would put every sensible
// value in its first pixels. The number field stays authoritative; both drive
// the same value and the marker on the (linear) bar.

function _dmEnrichSliderWindow() {
    const floor = dmEnrichProgressCache.target_floor ?? 0;
    const ceiling = dmEnrichProgressCache.target_ceiling ?? 0;
    const lo = Math.max(50, floor || 1);
    return (ceiling > lo) ? { lo, hi: ceiling } : null;
}

function dmEnrichTargetBounds(progress = {}) {
    dmEnrichProgressCache = progress || {};
    const slider = document.getElementById('dm-enrich-target-slider');
    if (slider) {
        const win = _dmEnrichSliderWindow();
        slider.disabled = !win;
        slider.style.opacity = win ? '' : '0.4';
    }
}

// value → slider position and back, on the log scale.
function _dmEnrichTargetToSlider(target) {
    const win = _dmEnrichSliderWindow();
    if (!win || target <= win.lo) return 0;
    if (target >= win.hi) return 1000;
    return Math.round(1000 * Math.log(target / win.lo) / Math.log(win.hi / win.lo));
}

function _dmEnrichSliderToTarget(pos) {
    const win = _dmEnrichSliderWindow();
    if (!win) return 0;
    const raw = win.lo * Math.pow(win.hi / win.lo, pos / 1000);
    // Snap to tens so dragging reads as clean numbers, but keep the endpoints
    // exact — the top of the slider must be the ceiling itself.
    if (pos >= 1000) return win.hi;
    return Math.min(win.hi, Math.max(win.lo, Math.round(raw / 10) * 10));
}

// ---- Spread cap: log slider from the analysis floor to the busiest day --- #
// The cap says what one sampled day is worth; its useful values run from the
// ten-video floor to the collection's busiest day, and like the target most
// of the sensible ones sit low, so the scale is logarithmic too.
function _dmEnrichCapWindow() {
    const daily = dmEnrichDailyCache || dmEnrichProgressCache.daily || null;
    const totals = (daily && daily.total) || [];
    let busiest = 0;
    for (const v of totals) if (v > busiest) busiest = v;
    const lo = dmEnrichProgressCache.min_day_items || 10;
    const hi = Math.min(1000, busiest);
    return hi > lo ? { lo, hi } : null;
}

function dmEnrichCapBounds() {
    const slider = document.getElementById('dm-enrich-day-cap-slider');
    if (!slider) return;
    const win = _dmEnrichCapWindow();
    slider.disabled = !win;
    slider.style.opacity = win ? '' : '0.4';
}

function _dmEnrichCapToSlider(cap) {
    const win = _dmEnrichCapWindow();
    if (!win || cap <= win.lo) return 0;
    if (cap >= win.hi) return 1000;
    return Math.round(1000 * Math.log(cap / win.lo) / Math.log(win.hi / win.lo));
}

function _dmEnrichSliderToCap(pos) {
    const win = _dmEnrichCapWindow();
    if (!win) return dmEnrichDayCapValue;
    if (pos >= 1000) return win.hi;
    const raw = win.lo * Math.pow(win.hi / win.lo, pos / 1000);
    return Math.min(win.hi, Math.max(win.lo, Math.round(raw)));
}

function dmEnrichCapSync(cap) {
    dmEnrichDayCapValue = cap;
    const el = document.getElementById('dm-enrich-day-cap-value');
    if (el) el.textContent = cap ? `${Number(cap).toLocaleString()} items` : '';
    const slider = document.getElementById('dm-enrich-day-cap-slider');
    if (slider && !slider.disabled) slider.value = String(_dmEnrichCapToSlider(cap));
}

function dmEnrichCapSliderInput() {
    const slider = document.getElementById('dm-enrich-day-cap-slider');
    if (!slider) return;
    dmEnrichCapSync(_dmEnrichSliderToCap(Number(slider.value)));
    dmEnrichChartRefresh();
}

// ---- Earliest date: a handle on the chart, day steppers, Full history --- #
// The same control as the Define Study modal's window start, minus the end:
// a plan always reaches the newest day. Unset means the whole history, and
// the handle then rests on the first day.
function _dmEnrichDaySpan() {
    const dates = (dmEnrichDailyCache && dmEnrichDailyCache.dates) || [];
    return dates.length ? { lo: dates[0], hi: dates[dates.length - 1] } : null;
}

// Set the earliest date (clamped to the history; the first day means unset).
// fire=false redraws the readout and the handle only — a drag calls that per
// frame and fires once on release, so the estimate runs once per drag.
function dmEnrichEarliestSet(iso, opts = {}) {
    const span = _dmEnrichDaySpan();
    let value = iso || '';
    if (value && span) value = value <= span.lo ? '' : _clampIso(value, span.lo, span.hi);
    const changed = value !== dmEnrichEarliest;
    dmEnrichEarliest = value;
    dmEnrichEarliestSync();
    if (opts.fire !== false && changed) {
        dmEnrichChartRefresh();
        dmEnrichPanelChanged();
    }
    return changed;
}

function dmEnrichEarliestStep(delta) {
    const span = _dmEnrichDaySpan();
    if (!span) return;
    dmEnrichEarliestSet(_shiftIsoDate(dmEnrichEarliest || span.lo, delta));
}

function dmEnrichEarliestReset() {
    dmEnrichEarliestSet('');
}

function dmEnrichEarliestSync() {
    const span = _dmEnrichDaySpan();
    const row = document.getElementById('dm-enrich-earliest-row');
    if (row) row.style.display = span ? '' : 'none';
    const valueEl = document.getElementById('dm-enrich-earliest-value');
    if (valueEl) {
        valueEl.textContent = dmEnrichEarliest || (span ? 'full history' : '');
        valueEl.title = dmEnrichEarliest
            ? 'Nothing before this date is processed'
            : (span ? `The plan may reach back to ${span.lo}, the first day with activity` : '');
    }
    const reset = document.getElementById('dm-enrich-earliest-reset');
    if (reset) reset.disabled = !dmEnrichEarliest;
    const steps = document.querySelectorAll('.dm-enrich-earliest-step');
    if (steps.length === 2 && span) {
        steps[0].disabled = !dmEnrichEarliest;                       // already at the first day
        steps[1].disabled = (dmEnrichEarliest || span.lo) >= span.hi;
    }
    _dmEnrichPositionEarliestHandle();
}

function _dmEnrichEnsureEarliestHandle() {
    const wrap = document.getElementById('dm-enrich-chart-wrap');
    if (!wrap) return null;
    let handle = wrap.querySelector('.dm-enrich-earliest-handle');
    if (handle) return handle;
    handle = document.createElement('div');
    handle.className = 'dm-enrich-earliest-handle';
    handle.style.display = 'none';
    handle.title = 'Drag to set the earliest date the plan reaches back to';
    handle.innerHTML = '<span class="dm-enrich-earliest-handle__rule"></span>'
                     + '<span class="dm-enrich-earliest-handle__grip"></span>';
    handle.addEventListener('mousedown', _dmEnrichBeginEarliestDrag);
    wrap.appendChild(handle);
    return handle;
}

// Put the handle on the day it selects. Read from the drawn axis, so it is
// right after a responsive resize too (Plotly's afterplot calls this).
function _dmEnrichPositionEarliestHandle() {
    const handle = _dmEnrichEnsureEarliestHandle();
    if (!handle) return;
    const chartDiv = document.getElementById('dm-enrich-daily-chart');
    const fullLayout = chartDiv && chartDiv._fullLayout;
    const span = _dmEnrichDaySpan();
    if (!span || !fullLayout || !fullLayout.xaxis || !chartDiv._plotlyInited
            || chartDiv.style.display === 'none') {
        handle.style.display = 'none';
        return;
    }
    const xa = fullLayout.xaxis;
    const ya = fullLayout.yaxis;
    // Bars are anchored at noon, so the handle sits on the centre of its day.
    const px = xa._offset + xa.d2p(_isoDayMs(dmEnrichEarliest || span.lo) + 43200000);
    if (!isFinite(px)) { handle.style.display = 'none'; return; }
    const clamped = Math.max(xa._offset, Math.min(xa._offset + xa._length, px));
    handle.style.display = '';
    handle.style.top = `${chartDiv.offsetTop + (ya._offset || 0)}px`;
    handle.style.height = `${ya._length || chartDiv.clientHeight}px`;
    handle.style.left = `${chartDiv.offsetLeft + clamped}px`;
}

function _dmEnrichBeginEarliestDrag(ev) {
    if (ev.button !== 0) return;
    const chartDiv = document.getElementById('dm-enrich-daily-chart');
    const fullLayout = chartDiv && chartDiv._fullLayout;
    if (!fullLayout || !fullLayout.xaxis) return;
    ev.preventDefault();
    ev.stopPropagation();
    const xa = fullLayout.xaxis;
    const rect = chartDiv.getBoundingClientRect();
    const wrap = document.getElementById('dm-enrich-chart-wrap');
    const span = _dmEnrichDaySpan();
    wrap?.classList.add('dragging-range');
    document.body.classList.add('study-range-dragging');

    let pending = null, frame = null, moved = false;
    const apply = () => {
        frame = null;
        if (pending == null) return;
        const iso = pending;
        pending = null;
        if (dmEnrichEarliestSet(iso, { fire: false })) moved = true;
    };
    const onMove = (e) => {
        const inAxis = Math.max(xa._offset, Math.min(xa._offset + xa._length,
                                                     e.clientX - rect.left)) - xa._offset;
        const iso = _toIsoDate(xa.p2d(inAxis));
        if (!iso) return;
        pending = span ? _clampIso(iso, span.lo, span.hi) : iso;
        if (frame == null) frame = requestAnimationFrame(apply);
    };
    const onUp = () => {
        window.removeEventListener('mousemove', onMove, true);
        window.removeEventListener('mouseup', onUp, true);
        if (frame != null) { cancelAnimationFrame(frame); apply(); }
        wrap?.classList.remove('dragging-range');
        document.body.classList.remove('study-range-dragging');
        if (moved) {
            dmEnrichChartRefresh();
            dmEnrichPanelChanged();
        }
    };
    window.addEventListener('mousemove', onMove, true);
    window.addEventListener('mouseup', onUp, true);
}

function dmEnrichDrawMarker(target) {
    const marker = document.getElementById('dm-enrich-bar-marker');
    const total = dmEnrichProgressCache.unique_items || 0;
    if (!marker) return;
    if (!total || !target) { marker.style.display = 'none'; return; }
    const frac = Math.max(0, Math.min(1, target / total));
    marker.style.display = '';
    marker.style.left = `${(frac * 100).toFixed(2)}%`;
}

// The quieter second marker: where the green zone stood when this run was
// armed, so the bar shows what the run itself has added.
function dmEnrichDrawStartMarker(progress) {
    const el = document.getElementById('dm-enrich-bar-start');
    if (!el) return;
    const total = progress.unique_items || 0;
    const start = progress.run_start_annotated;
    if (!dmEnrichArmed || !total || start === null || start === undefined) {
        el.style.display = 'none';
        return;
    }
    el.style.display = '';
    el.style.left = `${(100 * Math.max(0, Math.min(1, start / total))).toFixed(2)}%`;
    el.title = `${Number(start).toLocaleString()} videos were annotated when this run was armed`;
}

// How long the plan will take to annotate `more` videos, from the measured
// step timings. A cycle is: scrape a slice, consolidate, then annotate that
// slice WHILE the next slice scrapes, consolidate — so from the second
// slice on a cycle costs the longer of its annotation and the next scrape,
// plus the two consolidations. Already-scraped videos (the backlog sweep)
// go straight to annotation alongside the first scrape.
function dmEnrichTimeEstimate(more) {
    const t = dmEnrichTiming || DM_ENRICH_DEFAULT_TIMING;
    const cap = DM_ENRICH_AUTO_CYCLE_CAP;
    const c = t.consolidate_min || 2;
    const scrape = n => (n > 0 ? n / (t.scrape_per_min || 70) : 0);
    const annotate = n => (n > 0 ? (t.annotate_fixed_min || 8) + n * (t.annotate_per_video_min || 0.01) : 0);
    const yieldRate = Math.min(1, Math.max(0.5, Number(dmEnrichProgressCache.last_yield) || 0.85));

    // The backlog the handoff annotates first: scraped, not annotated, and
    // not burnt (a burnt annotation is failed for good, never re-queued).
    const scrapedAwaiting = dmEnrichProgressCache.unique_awaiting !== undefined
        ? Math.max(0, dmEnrichProgressCache.unique_awaiting || 0)
        : Math.max(0, (dmEnrichProgressCache.unique_scraped || 0)
                      - (dmEnrichProgressCache.unique_annotated || 0));
    const backlog = Math.min(more, scrapedAwaiting);
    const rest = more - backlog;
    const slices = rest > 0 ? Math.max(1, Math.ceil(rest / cap)) : 0;
    const perSlice = slices ? rest / slices : 0;
    const perScrape = perSlice / yieldRate;      // cut more than needed: some fail

    let minutes = 0;
    if (backlog) {
        // The sweep annotates while the first slice (if any) scrapes.
        minutes += Math.max(annotate(backlog), scrape(perScrape)) + c * (slices ? 2 : 1);
    } else if (slices) {
        minutes += scrape(perScrape) + c;
    }
    for (let i = 1; i <= slices; i++) {
        const next = i < slices ? scrape(perScrape) : 0;
        minutes += Math.max(annotate(perSlice), next) + c * (next ? 2 : 1);
    }
    return { minutes, cycles: slices + (backlog ? 1 : 0), measured: t.measured || {} };
}

// "about 40 min" / "about 2 h 10 min" / "about 1.5 days".
function dmEnrichMinutesLabel(minutes) {
    if (minutes < 60) return `about ${Math.max(5, Math.round(minutes / 5) * 5)} min`;
    if (minutes < 24 * 60) {
        const m = Math.round(minutes / 10) * 10;
        const h = Math.floor(m / 60), r = m % 60;
        return r ? `about ${h} h ${r} min` : `about ${h} h`;
    }
    const d = (minutes / 1440).toFixed(1).replace(/\.0$/, '');
    return `about ${d} day${d === '1' ? '' : 's'}`;
}

function dmEnrichTargetReadout(target) {
    dmEnrichTargetWarning(target);
    const el = document.getElementById('dm-enrich-target-readout');
    const pctEl = document.getElementById('dm-enrich-target-pct');
    const total = dmEnrichProgressCache.unique_items || 0;
    if (pctEl) {
        pctEl.textContent = (target && total)
            ? `(${Math.round(100 * target / total)}%)` : '';
    }
    if (!el) return;
    const annotated = dmEnrichProgressCache.target_floor ?? 0;
    if (!target || !total) { el.textContent = ''; return; }
    const more = Math.max(0, target - annotated);
    if (!more) { el.textContent = 'already met'; return; }
    let text = `These settings will annotate ${more.toLocaleString()} items.`;
    if (dmEnrichCostPer1000 && dmEnrichCostPer1000.est_cost_usd) {
        const usd = more * dmEnrichCostPer1000.est_cost_usd / 1000;
        const model = dmEnrichCostPer1000.model || dmEnrichCostPer1000.backend;
        text += ` Estimated cost${model ? ` using ${model}` : ''}: `
              + `$${usd < 10 ? usd.toFixed(2) : Math.round(usd).toLocaleString()}.`;
    }
    const est = dmEnrichTimeEstimate(more);
    const m = est.measured || {};
    const basis = (m.scrape || m.annotate || m.consolidate)
        ? 'from this collection\u2019s recent runs'
        : 'typical figures, nothing measured for this collection yet';
    text += ` Estimated time to reach the target: ${dmEnrichMinutesLabel(est.minutes)}`
          + ` (${est.cycles.toLocaleString()} cycle${est.cycles === 1 ? '' : 's'}; ${basis}).`;
    el.textContent = text;
}

// The readout depends on Items per cycle too; its input calls this.
function dmEnrichReadoutRefresh() {
    dmEnrichTargetReadout(dmEnrichTargetValue);
    dmEnrichDaysReadout();
}

// One value, four widgets: keep the display box, the slider, the marker and
// the readout agreed on dmEnrichTargetValue.
function dmEnrichTargetSync(target) {
    dmEnrichTargetValue = target;
    const el = document.getElementById('dm-enrich-target');
    if (el) el.textContent = target ? `${Number(target).toLocaleString()} items` : '';
    const slider = document.getElementById('dm-enrich-target-slider');
    if (slider && !slider.disabled) slider.value = String(_dmEnrichTargetToSlider(target));
    dmEnrichDrawMarker(target);
    dmEnrichTargetReadout(target);
}

function dmEnrichSliderInput() {
    const slider = document.getElementById('dm-enrich-target-slider');
    if (!slider) return;
    const target = _dmEnrichSliderToTarget(Number(slider.value));
    dmEnrichTargetValue = target;
    const el = document.getElementById('dm-enrich-target');
    if (el) el.textContent = target ? `${Number(target).toLocaleString()} items` : '';
    dmEnrichDrawMarker(target);
    dmEnrichTargetReadout(target);
    dmEnrichChartRefresh();
}

// Shared by the Data Management disclosures (History, Collection persona, the
// annotation-queue block): flip the panel and rotate the caret.
function dmToggleAdvanced(panelId, btnId) {
    const panel = document.getElementById(panelId);
    const btn = document.getElementById(btnId);
    if (!panel || !btn) return;
    const open = panel.style.display === 'none';
    panel.style.display = open ? '' : 'none';
    btn.classList.toggle('open', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function dmEnrichReadSettings() {
    const num = (id) => Number(document.getElementById(id)?.value);
    return {
        annotation_target: dmEnrichTargetValue,
        sample_share: num('dm-enrich-sample-share') / 100,
        a_day_cap: dmEnrichDayCapValue,
        earliest_date: dmEnrichEarliest || null,
    };
}

// Plain-language state names. The ledger's own words ("done", "blocked") are
// for the code; these are what the operator reads in the panel and the table.
function dmEnrichStateLabel(state) {
    return _ENRICHMENT_STATE_LABELS[state] || state;
}


// What "Run a cycle now" would do from here, and — when it is disabled —
// why. The dynamic text lives on the button's WRAPPER span, not the button:
// a disabled button eats its own hover tooltip in most browsers, and the
// no-plan / target-met states disable the button precisely because a cycle
// would do nothing.
function dmEnrichTickTooltip(armed, state, progress) {
    const base = 'Runs one step of automatic enrichment right now instead of '
               + 'waiting for the next automatic cycle. A manual nudge and '
               + 'diagnostic — it does not arm anything, and one click advances '
               + 'the plan by exactly one step: queue the next batch of videos to '
               + 'scrape, start the scraper or the annotator, or consolidate '
               + 'finished results. If videos queued elsewhere are waiting in the '
               + 'shared queues, a dialog shows them first — they are processed '
               + 'before this collection\'s own.';
    if (!armed) {
        return 'Disabled: this collection has no plan yet, so a cycle has '
             + 'nothing to run. Press Arm first \u2014 that is what creates the '
             + 'plan, with the settings shown here.';
    }
    const target = progress.annotation_target ?? 0;
    const annotated = progress.target_floor ?? 0;
    if (!target) {
        return 'No annotation target is set, so a cycle will find nothing to '
             + 'do. Pick a target below first. ' + base;
    }
    if (annotated >= target) {
        return `Disabled: the annotation target (${target.toLocaleString()}) `
             + `is already met — ${annotated.toLocaleString()} videos are `
             + `annotated. Raise the target and arm the plan to continue.`;
    }
    if (state === 'blocked') {
        return 'This plan stopped itself because the work it queued was not '
             + 'getting done. A cycle will run, but fix the cause first or it '
             + 'will stop again. ' + base;
    }
    if (state === 'paused') {
        return 'This plan is paused, so nothing happens on its own — but this '
             + 'button still runs one cycle by hand. ' + base;
    }
    return base;
}


// The panel's per-day activity chart: one stacked bar per active day, split
// by enrichment state in the coverage bar's colours, in the style of the
// study modal's daily chart. Completely non-interactive (staticPlot). Two
// dashed lines track settings live: the spread's per-day cap and the
// min_day_items analysis floor.
function dmEnrichChartShapes() {
    // Just the analysis floor. No day-cap line: it would be noise, since the
    // red estimate line already shows the cap's effect where it matters.
    const floor = dmEnrichProgressCache.min_day_items || 10;
    return [{
        type: 'line', xref: 'paper', x0: 0, x1: 1, yref: 'y', y0: floor, y1: floor,
        line: { color: getCSSVar('--color-text-tertiary'), width: 1, dash: 'dot' },
    }];
}

// The red estimate line: where each day's annotated count would land if the
// plan ran to the current target with the current settings. A deliberately
// rough client-side mirror of the planner — backlog first, then whole recent
// days for the deep dive, then capped days per month — as many as the
// target needs — walking backwards for the spread (which in reality draws
// its days at random; here the newest eligible ones stand in). The shape of
// the outcome, not the exact days.
function _dmEnrichPlanEstimate(daily, remainingOverride = null) {
    const n = daily.dates.length;
    const planned = new Array(n).fill(0);
    const annotatedTotal = dmEnrichProgressCache.target_floor ?? 0;
    let remaining = remainingOverride !== null
        ? remainingOverride
        : Math.max(0, (dmEnrichTargetValue || 0) - annotatedTotal);
    if (!remaining) {
        if (remainingOverride === null) dmEnrichEstimateStats = null;
        return planned;
    }

    const share = (Number(document.getElementById('dm-enrich-sample-share')?.value) || 0) / 100;
    const dayCap = dmEnrichDayCapValue || 50;
    const minDay = dmEnrichProgressCache.min_day_items || 10;
    const earliest = dmEnrichEarliest || '';

    const awaiting = (i) => daily.awaiting[i] || 0;
    const unscraped = (i) => Math.max(0, (daily.total[i] || 0) - (daily.annotated[i] || 0)
        - (daily.failed[i] || 0) - awaiting(i));
    // The viewing sessions still to complete, by start day — what a cut
    // day takes whole before single items (null on a payload without them).
    const sessByDay = _dmEnrichSessionsByDay();

    // 1. The backlog sweep: already-scraped videos are annotated first.
    //    Remembered per day: they are already in the day's scraped count,
    //    so the cap room below must not charge them twice.
    const swept = new Array(n).fill(0);
    for (let i = n - 1; i >= 0 && remaining > 0; i--) {
        const take = Math.min(awaiting(i), remaining);
        planned[i] += take; swept[i] += take; remaining -= take;
    }
    // 2. What is left splits between the processes by the spread share.
    let dd = Math.round(remaining * (1 - share));
    let sp = remaining - dd;
    const ddDays = new Set();
    // The deep dive takes every day, quiet ones included — only the spread
    // honours the analysis floor (a one-video day is still a session).
    // Returns what it could not place (history exhausted).
    const deep = new Array(n).fill(0);   // what the deep dive placed per day
    const deepDive = (budget) => {
        for (let i = n - 1; i >= 0 && budget > 0; i--) {
            if (earliest && daily.dates[i] < earliest) break;
            const room = unscraped(i) - deep[i];
            if (room <= 0) continue;
            const take = Math.min(room, budget);      // last day drawn partial
            planned[i] += take; deep[i] += take; budget -= take; ddDays.add(i);
        }
        return budget;
    };
    deepDive(dd);
    // 3. Spread: the fewest capped days per month, uniform across the months,
    //    that give the spread its share — mirrors the planner's derivation
    //    (spread_days_per_month), on the same per-day quotas this estimate
    //    then places. Newest month first.
    // The day's room under the cap, as the planner counts it: everything
    // already scraped on the day — annotated, awaiting, failed for good —
    // is charged against the cap (the sweep's take is already in the
    // awaiting count, hence `swept`).
    const capRoom = (i) => dayCap - ((daily.annotated[i] || 0) + awaiting(i)
        + (daily.failed[i] || 0) + planned[i] - swept[i]);
    const months = _dmEnrichSampleMonths(daily, { ddDays, minDay, earliest, unscraped });
    const daysPerMonth = _dmEnrichSpreadDays(months, sp, { capRoom, unscraped });
    let spreadDays = 0;
    // Months newest first; within a month the first `daysPerMonth` days in
    // the planner's own draw order. A drawn day already at the cap uses
    // its slot and adds nothing, as it does in the planner.
    outer: for (const idxs of months) {
        let taken = 0;
        for (const i of idxs) {
            if (sp <= 0) break outer;
            if (taken >= daysPerMonth) break;
            taken += 1;
            const room = Math.max(0, capRoom(i));
            if (room <= 0) continue;
            const list = sessByDay && sessByDay[i];
            // A day with sessions to complete takes them whole, the one that
            // crosses the cap included — so the day (and the sample's share)
            // may overshoot by one session, as the planner's does. A day
            // without takes single items, bounded by the share, as before.
            const take = list && list.length
                ? _dmEnrichDayTake(list, room, unscraped(i))
                : Math.min(unscraped(i), room, sp);
            if (take <= 0) continue;
            planned[i] += take; sp -= take; spreadDays += 1;
        }
    }
    // 4. Whatever the spread could not place goes back to the deep dive, as
    //    the planner's reallocation does — so with any deep-dive share at all
    //    the estimate walks the whole history.
    if (share < 1 && sp > 0) deepDive(sp);
    if (remainingOverride === null) {
        // What the three sliders add up to, in days — for the line under
        // the cap slider. Only the target-bound run counts; the reachability
        // probe (override) is not a plan.
        // Analysis-ready = at least minDay annotated items on the day. A
        // deep-dive day = every item played that day annotated or failed
        // for good (whole-day coverage, what Sessions needs). Both counted
        // now and after the plan, days already there included, so the line
        // can say "from X1 to X2".
        let readyNow = 0, readyAfter = 0, deepNow = 0, deepAfter = 0;
        // Analysis-ready sessions: every item of a session with at least
        // the Sessions tab's minimum plays annotated or failed for good.
        // What the plan places on a day is handed to that day's sessions
        // in the server's order, whole sessions first; a day the plan
        // covers to the top completes every session that started on it.
        let sessionsDone = 0;
        for (let i = 0; i < n; i++) {
            const total = daily.total[i] || 0;
            if (!total) continue;
            const ann = daily.annotated[i] || 0;
            const fail = daily.failed[i] || 0;
            if (ann >= minDay) readyNow += 1;
            if (ann + planned[i] >= minDay) readyAfter += 1;
            if (ann + fail >= total) deepNow += 1;
            const covered = ann + fail + planned[i] >= total;
            if (covered) deepAfter += 1;
            const list = sessByDay && sessByDay[i];
            if (!list) continue;
            if (covered) { sessionsDone += list.length; continue; }
            // Sittings with nothing left to scrape are finished by the
            // backlog sweep whatever the order; the rest go in the
            // server's order until one no longer fits.
            let left = planned[i];
            const rest = [];
            for (const s of list) {
                if (s.n > 0) { rest.push(s); continue; }
                if (s.w <= left) { left -= s.w; sessionsDone += 1; }
            }
            for (const s of rest) {
                if (s.n + s.w > left) break;
                left -= s.n + s.w; sessionsDone += 1;
            }
        }
        const sessionsNow = sessByDay
            ? ((dmEnrichProgressCache.sessions || {}).ready || 0) : null;
        dmEnrichEstimateStats = {
            deepDays: ddDays.size,
            spreadDays,
            daysPerMonth,
            readyNow,
            readyAfter,
            deepNow,
            deepAfter,
            sessionsNow,
            sessionsAfter: sessByDay ? sessionsNow + sessionsDone : null,
            share,
        };
    }
    return planned;
}

// The collection's viewing sessions not yet analysis-ready, grouped by the
// index of the day they started on, in the server's newest-first order —
// from the GET payload (collection_enrichment._session_figures): per
// session, the items still to scrape (n) and those awaiting annotation
// (w). null when the payload carries none (an older server), and the
// estimate then places single items alone, as it always did.
function _dmEnrichSessionsByDay() {
    const per = (dmEnrichProgressCache.sessions || {}).per;
    if (!per || !Array.isArray(per.d)) return null;
    const byDay = {};
    for (let k = 0; k < per.d.length; k++) {
        const d = per.d[k];
        if (d === undefined || d === null || d < 0) continue;
        (byDay[d] = byDay[d] || []).push({ n: per.n[k] || 0, w: per.w[k] || 0 });
    }
    return byDay;
}

// What a cut day takes: whole viewing sessions, in order, until the room
// is met — the session that crosses the line included — then single items
// up to the room. The planner's _pick_in_day, on the counts the estimate
// has; never more than the day still has to scrape.
function _dmEnrichDayTake(sessions, room, unscraped) {
    if (room <= 0) return 0;
    let take = 0;
    for (const s of sessions) {
        if (take >= room) break;
        take += s.n;
    }
    return Math.min(Math.max(take, room), unscraped);
}

// The spread's density for the estimate: per month, the qualifying days' cap
// room in the order the estimate walks them, then the smallest uniform
// days-per-month whose summed room covers what the spread must place.
function _dmEnrichSpreadDays(months, want, ctx) {
    if (want <= 0 || !months.length) return 0;
    const { capRoom, unscraped } = ctx;
    // Per month, what each drawn day can still take under the cap, in draw
    // order — zero for a day already at the cap, which still uses a slot.
    const quotas = months.map(idxs => idxs.map(i => Math.min(unscraped(i), Math.max(0, capRoom(i)))));
    const densest = Math.min(31, Math.max(...quotas.map(q => q.length)));
    for (let d = 1; d <= densest; d++) {
        const capacity = quotas.reduce((a, q) => a + q.slice(0, d).reduce((x, y) => x + y, 0), 0);
        if (capacity >= want) return d;
    }
    return densest;
}

// The days the random daily sample can draw from, per month newest first,
// each month's days in the order the planner draws them: the salted ranking
// the server ships as `daily.draw` (the same ranking the planner samples
// by, so the estimate lands on the planner's own days), else newest first
// on a payload without it. A day qualifies as the planner's does — at least
// the analysis floor of items, something left to scrape, not a deep-dive
// day — whatever its room under the cap.
function _dmEnrichSampleMonths(daily, ctx) {
    const { ddDays, minDay, earliest, unscraped } = ctx;
    const n = daily.dates.length;
    const draw = Array.isArray(daily.draw) && daily.draw.length === n ? daily.draw : null;
    const byMonth = new Map();
    for (let i = n - 1; i >= 0; i--) {
        if (earliest && daily.dates[i] < earliest) break;
        if (ddDays.has(i) || (daily.total[i] || 0) < minDay || unscraped(i) <= 0) continue;
        const m = daily.dates[i].slice(0, 7);
        if (!byMonth.has(m)) byMonth.set(m, []);
        byMonth.get(m).push(i);
    }
    const months = [...byMonth.values()];
    if (draw) for (const idxs of months) idxs.sort((a, b) => draw[a] - draw[b]);
    return months;
}

// The line under the cap slider: what the three sliders add up to, in days
// — the unit every analysis works in. Both counts are what the plan would
// add on top of what is already annotated.
function dmEnrichDaysReadout() {
    const el = document.getElementById('dm-enrich-days-readout');
    if (!el) return;
    const st = dmEnrichEstimateStats;
    if (!dmEnrichDailyCache) { el.textContent = ''; return; }
    if (!st) {
        // Nothing to place: the target is met (or unset), so no days are added.
        el.textContent = dmEnrichTargetValue ? 'nothing more to process \u2014 the target is already met' : '';
        return;
    }
    // Both counts now and after the plan, so the line reads as a change.
    const span = (what, now, after) => `${what} from ${now.toLocaleString()} `
        + `to \u2248 ${after.toLocaleString()}`;
    const days = span('analysis-ready days', st.readyNow, st.readyAfter);
    const deep = span('deep-dive days', st.deepNow, st.deepAfter);
    // The sessions clause only where the server reports sessions.
    el.textContent = st.sessionsAfter === null || st.sessionsAfter === undefined
        ? `These settings will take ${days} and ${deep}.`
        : `These settings will take ${days}, ${deep}, and `
          + `${span('analysis-ready sessions', st.sessionsNow, st.sessionsAfter)}.`;
}

// What the CURRENT settings can ever reach: the estimate run with no target
// bound. Only two things make this fall short of the reachable ceiling — a
// 100%-spread balance (the per-day cap and the analysis floor cap the
// total) and an earliest-date floor; any deep-dive share walks the whole
// history eventually, quiet days included. Same code path as the red line,
// so it moves with it.
function dmEnrichSettingsReachable() {
    if (!dmEnrichDailyCache) return null;
    const annotated = dmEnrichProgressCache.target_floor ?? 0;
    const ceiling = dmEnrichProgressCache.target_ceiling
        ?? (dmEnrichProgressCache.unique_items || 0);
    const planned = _dmEnrichPlanEstimate(dmEnrichDailyCache,
                                          Math.max(0, ceiling - annotated));
    return annotated + planned.reduce((a, b) => a + b, 0);
}

// The amber warning under the target row: shown when the chosen target sits
// above what the current settings can reach, naming the number and the
// binding setting. A signal, not a block — the estimate is approximate, the
// settings can be changed later, and an unreachable target wastes nothing
// (the plan simply goes idle early).
function dmEnrichTargetWarning(target) {
    const el = document.getElementById('dm-enrich-target-warning');
    if (!el) return;
    const reachable = dmEnrichSettingsReachable();
    // Slack absorbs the estimate's roughness so the warning doesn't flicker
    // at the boundary.
    const slack = reachable === null ? 0 : Math.max(25, Math.round(reachable * 0.02));
    if (reachable === null || !target || target <= reachable + slack) {
        el.style.display = 'none';
        el.textContent = '';
        return;
    }
    const share = (Number(document.getElementById('dm-enrich-sample-share')?.value) || 0) / 100;
    const earliest = dmEnrichEarliest || '';
    const causes = [];
    const fixes = [];
    if (share >= 1) {
        causes.push('the balance set to only random daily sample');
        fixes.push('move the balance toward the deep dive or raise the items per day');
    }
    if (earliest) {
        causes.push('the earliest date');
        fixes.push('move or clear the earliest date');
    }
    const cause = causes.length ? `With ${causes.join(' and ')}, ` : '';
    let fix = fixes.join(', or ');
    fix = fix ? fix.charAt(0).toUpperCase() + fix.slice(1) + ' to reach more of the collection.' : '';
    el.textContent = `\u26a0 ${cause}these settings can annotate at most about `
        + `${reachable.toLocaleString()} videos, so the plan will stop `
        + `short of this target. ${fix}`;
    el.style.display = '';
}

// Full re-render from the cached daily series — fired by the target slider
// and every plan setting the estimate line depends on. The readout and
// the reachability warning depend on the same inputs, so they ride along.
function dmEnrichChartRefresh() {
    if (dmEnrichDailyCache) dmEnrichRenderChart(dmEnrichDailyCache);
    dmEnrichReadoutRefresh();
}

let dmEnrichDailyCache = null;

function dmEnrichRenderChart(daily) {
    const div = document.getElementById('dm-enrich-daily-chart');
    if (!div) return;
    dmEnrichDailyCache = daily || null;
    const dates = (daily && daily.dates) || [];
    if (!dates.length || !window.Plotly) {
        div.style.display = 'none';
        if (div._plotlyInited && window.Plotly) {
            window.Plotly.purge(div);
            div._plotlyInited = false;
            div._dmEnrichAfterplotHooked = false;
        }
        dmEnrichEarliestSync();
        return;
    }
    div.style.display = '';
    // Noon-anchored like the study modal's chart, so each bar sits cleanly
    // inside its own date label.
    const xs = dates.map(d => d + 'T12:00:00Z');
    const rest = dates.map((_, i) => Math.max(0,
        (daily.total[i] || 0) - (daily.annotated[i] || 0)
        - (daily.awaiting[i] || 0) - (daily.failed[i] || 0)));
    const trace = (ys, color, opacity) => ({
        type: 'bar', x: xs, y: ys, marker: { color, opacity }, hoverinfo: 'skip',
    });
    const traces = [
        trace(daily.annotated, getCSSVar('--color-success') || '#6A9B7E', 1),
        trace(daily.awaiting, getCSSVar('--color-info') || '#5B7E98', 1),
        trace(rest, getCSSVar('--color-text-faint') || '#888', 0.3),
        trace(daily.failed, getCSSVar('--color-border-strong') || '#666', 0.8),
    ];
    const planned = _dmEnrichPlanEstimate(daily);
    if (planned.some(v => v > 0)) {
        // The line stops at the earliest date: nothing before it is ever
        // enriched, so the estimate has nothing to say there (null = gap).
        const earliest = dmEnrichEarliest || '';
        traces.push({
            type: 'scatter', mode: 'lines', x: xs, connectgaps: false,
            y: dates.map((d, i) => (earliest && d < earliest)
                ? null : (daily.annotated[i] || 0) + planned[i]),
            line: { color: getCSSVar('--color-danger') || '#c0392b',
                    width: 1.5, shape: 'hvh' },
            hoverinfo: 'skip',
        });
    }
    const layout = {
        barmode: 'stack',
        bargap: 0,
        margin: { l: 32, r: 8, t: 6, b: 26 },
        paper_bgcolor: getCSSVar('--chart-bg'),
        plot_bgcolor: getCSSVar('--chart-bg'),
        font: { family: getCSSVar('--font-sans'), color: getCSSVar('--chart-text'), size: 10 },
        xaxis: { type: 'date', gridcolor: getCSSVar('--chart-grid'),
                 tickfont: { size: 9 }, tickformat: '%Y-%m-%d', fixedrange: true },
        // Log y: day sizes span orders of magnitude, and on a linear axis the
        // small days (the very ones near the analysis floor) were invisible.
        // Segment boundaries stay truthful at their values; only the visual
        // proportions within a bar distort, which the coverage bar below
        // reports linearly anyway.
        yaxis: { gridcolor: getCSSVar('--chart-grid'), tickfont: { size: 9 },
                 fixedrange: true, type: 'log' },
        showlegend: false,
        shapes: dmEnrichChartShapes(),
    };
    window.Plotly.react(div, traces, layout,
                        { staticPlot: true, displayModeBar: false, responsive: true });
    div._plotlyInited = true;
    // The earliest-date handle rides on the drawn axis: place it now, and
    // again whenever Plotly redraws (responsive resizes move the axis).
    if (!div._dmEnrichAfterplotHooked && typeof div.on === 'function') {
        div._dmEnrichAfterplotHooked = true;
        div.on('plotly_afterplot', _dmEnrichPositionEarliestHandle);
    }
    dmEnrichEarliestSync();
}

// Draw the coverage bar: four zones that sum to the collection's unique
// videos. Deliberately linear — stacked segments only mean anything when
// lengths add up, which a log axis destroys; the log scale lives in the
// slider, where it belongs.
function dmEnrichDrawBar(progress) {
    const total = progress.unique_items || 0;
    const seg = (id, n) => {
        const el = document.getElementById(id);
        if (el) el.style.width = total ? `${(100 * (n || 0) / total).toFixed(2)}%` : '0%';
        return n || 0;
    };
    const bar = document.getElementById('dm-enrich-bar');
    const slider = document.getElementById('dm-enrich-target-slider');
    const legend = document.getElementById('dm-enrich-legend');
    const show = total > 0;
    if (bar) bar.style.display = show ? '' : 'none';
    if (slider) slider.style.display = show ? '' : 'none';
    if (legend) legend.style.display = show ? '' : 'none';
    dmEnrichDrawStartMarker(progress);
    if (!show) return;

    const annotated = seg('dm-enrich-bar-annotated', progress.unique_annotated);
    const awaiting = seg('dm-enrich-bar-scraped',
                         Math.max(0, (progress.unique_scraped || 0) - annotated));
    const failed = seg('dm-enrich-bar-failed', progress.unique_failed);
    const rest = Math.max(0, total - annotated - awaiting - failed);

    const put = (id, label, n) => {
        const el = document.getElementById(id);
        if (el) el.textContent = `${label} ${n.toLocaleString()} (${Math.round(100 * n / total)}%)`;
    };
    put('dm-enrich-leg-annotated', 'annotated', annotated);
    put('dm-enrich-leg-scraped', 'awaiting annotation', awaiting);
    put('dm-enrich-leg-rest', 'not yet scraped', rest);
    put('dm-enrich-leg-failed', 'failed for good', failed);
}

function dmEnrichRender(data) {
    // One delegated listener carries every input in the panel — sliders and
    // number fields alike bubble 'input' here — into the autosave.
    const panel = document.getElementById('dm-enrich-panel');
    if (panel && !panel._dmEnrichDirtyHooked) {
        panel._dmEnrichDirtyHooked = true;
        panel.addEventListener('input', dmEnrichPanelChanged);
        // Checkboxes reliably fire 'change'; the handler is idempotent.
        panel.addEventListener('change', dmEnrichPanelChanged);
    }
    dmEnrichArmed = !!data.armed;
    const loadingEl = document.getElementById('dm-enrich-loading');
    if (loadingEl) loadingEl.style.display = 'none';
    const progress = data.progress || {};
    dmEnrichState = progress.state || null;
    dmEnrichSyncTableRow(dmEnrichCollectionId, dmEnrichArmed ? dmEnrichState : null);

    if (data.cost_per_1000 !== undefined) dmEnrichCostPer1000 = data.cost_per_1000;
    if (data.timing) dmEnrichTiming = data.timing;

    // The status strip (bottom of the panel, above the buttons): the plan's
    // state, then the live activity. Everything else is visible on the bar
    // or lives in the tooltips.
    const statusEl = document.getElementById('dm-enrich-status-line');
    if (statusEl) {
        const act = data.activity || {};
        let line = `Status: ${dmEnrichArmed ? dmEnrichStateLabel(dmEnrichState) : 'Not armed'}`;
        if (dmEnrichState === 'blocked' && progress.last_error) {
            line += ` \u2014 ${progress.last_error}`;
        }
        // What is happening right now, server-derived from the worker
        // statuses (never guessed client-side) \u2014 or, when armed and between
        // steps, what the next tick will do.
        const actLabel = DM_ENRICH_ACTIVITY_LABELS[act.kind];
        if (actLabel) {
            line += ` \u00b7 ${actLabel}`;
            const msg = (act.message || '').trim();
            if (msg) line += ` \u2014 ${msg.length > 90 ? msg.slice(0, 87) + '\u2026' : msg}`;
        } else if (dmEnrichArmed && dmEnrichState === 'running' && progress.finishing) {
            // Nothing more to scrape; the plan closes once its last queued
            // videos are annotated and consolidated. Said here rather than
            // "waiting for the next cycle", which promises work that is not
            // coming.
            const n = Number((progress.finishing || {}).pending || 0);
            line += ' \u00b7 finishing \u2014 the plan closes once the'
                  + (n ? ` ${n.toLocaleString()}` : '')
                  + ' queued videos are annotated and consolidated';
        } else if (dmEnrichArmed && dmEnrichState === 'running') {
            const next = dmEnrichNextLabel(progress);
            line += ' \u00b7 waiting for the next cycle'
                  + (next ? ` \u2014 next: ${next}` : '');
        }
        if (dmEnrichArmed && (data.deferred_refresh || {}).pending) {
            line += ' \u00b7 the analyses are refreshed when the plan finishes';
        }
        if (!data.enabled_site_wide) {
            line += ' \u00b7 automatic enrichment is switched off for the whole site (Admin \u2192 Site Settings)';
        }
        statusEl.textContent = line;
    }

    // Above the chart: the collection's size, and how much of it is already
    // analysable. Both are live figures, so both sit on the line rather than
    // inside the (i), where a number that moves every cycle is exactly the
    // wrong thing to keep. The tooltip explains; the line reports.
    const progEl = document.getElementById('dm-enrich-progress');
    const videos = progress.unique_items || 0;
    if (progEl) {
        progEl.textContent = videos
            ? `${videos.toLocaleString()} items played over ${progress.total_days} days`
            : '';
    }
    const readyEl = document.getElementById('dm-enrich-ready-days');
    if (readyEl) {
        const ready = progress.qualifying_days || 0;
        const need = progress.milestone_days || 0;
        // Past the milestone the "of the ~14 needed" half is not just
        // redundant, it reads as a shortfall ("46 of the ~14").
        readyEl.textContent = !videos ? ''
            : (ready >= need
                ? `${ready.toLocaleString()} analysis-ready days`
                : `${ready.toLocaleString()} of the ~${need} analysis-ready days needed`);
    }
    // Viewing sessions the researcher can analyse now — whole sittings, every
    // item annotated or failed for good — beside the ready days. Blank on a
    // payload without the figure.
    const sessEl = document.getElementById('dm-enrich-ready-sessions');
    if (sessEl) {
        const sessions = progress.sessions;
        const ready = sessions ? (sessions.ready || 0) : 0;
        sessEl.textContent = (!videos || !sessions) ? ''
            : `${ready.toLocaleString()} analysis-ready session${ready === 1 ? '' : 's'}`;
    }

    dmEnrichDrawBar(progress);
    // Settings first: the chart's estimate line reads the plan inputs.
    // A render that lands while a write is pending or in flight must NOT
    // refill the form — that is the operator still typing, and the values
    // coming back are the ones they have already moved past. The figures the
    // chart and the readout need are refreshed either way.
    if (dmEnrichFormBusy()) {
        dmEnrichTargetBounds(progress);
        dmEnrichTargetSync(dmEnrichTargetValue);
    } else {
        dmEnrichFillSettings(data.settings || {}, progress);
    }
    dmEnrichRenderChart(progress.daily);
    dmEnrichRenderRun(progress);
    // The reachability warning needs the daily cache the chart just set.
    dmEnrichReadoutRefresh();

    // The Arm button is set by dmEnrichButtonsRefresh (called at the end of
    // this render) rather than here: its wording depends on the target the
    // form is showing, which the operator can move without a render.
    const tickWrap = document.getElementById('dm-enrich-tick-wrap');
    if (tickWrap) {
        tickWrap.dataset.tooltip = dmEnrichTickTooltip(dmEnrichArmed, dmEnrichState, progress);
    }
    // The baseline the autosave's dirty check compares against: the form as
    // just filled from the plan, via the same reader the autosave writes with.
    // Skipped mid-write, or a pending edit would be marked already saved.
    if (!dmEnrichFormBusy()) {
        dmEnrichSavedSettings = JSON.stringify(dmEnrichReadSettings());
    }
    dmEnrichButtonsRefresh();
    dmEnrichScheduleRefresh(data);
}


// --- Run progress ---------------------------------------------------------
// Where this run started, where it stands now, and where it stops. The chart
// and the coverage bar describe the collection; only this describes the run,
// which is what "how far has it come" actually asks. The start point is the
// annotated count recorded when the plan was armed (the ledger's
// run_start_annotated), so it survives edits, pauses and hand-queued work.
function dmEnrichRenderRun(progress) {
    const box = document.getElementById('dm-enrich-run');
    if (!box) return;
    const total = progress.unique_items || 0;
    // A finished run is history: its meter reads the target it ended with
    // and the count it ended at, not the target the operator is now moving
    // to prepare the next run (so it does not slide with the slider).
    const finished = dmEnrichState === 'done'
        && progress.run_end_target != null && progress.run_end_annotated != null;
    const target = finished ? progress.run_end_target : (progress.annotation_target || 0);
    const now = finished ? progress.run_end_annotated : (progress.target_floor ?? 0);
    const start = progress.run_start_annotated;
    if (!dmEnrichArmed || !total || !target || start === null || start === undefined) {
        box.style.display = 'none';
        return;
    }
    box.style.display = '';

    const span = Math.max(1, target - start);
    const done = Math.max(0, Math.min(span, now - start));
    const pct = Math.round(100 * done / span);
    const pctOf = (n) => `${(100 * n / total).toFixed(total > 2000 ? 1 : 0)}%`;

    const title = document.getElementById('dm-enrich-run-title');
    if (title) {
        const when = fypFmtDate(progress.run_started_at, '');
        const label = dmEnrichState === 'running' ? 'This run'
            : (dmEnrichState === 'paused' ? 'This run (paused)' : 'Last run');
        const ended = finished ? fypFmtDate(progress.run_finished_at, '') : '';
        title.textContent = (when ? `${label}, armed ${when}` : label)
                          + (ended ? `, finished ${ended}` : '');
    }
    const pctEl = document.getElementById('dm-enrich-run-pct');
    if (pctEl) pctEl.textContent = `${pct}% of the way`;
    const fill = document.getElementById('dm-enrich-run-fill');
    if (fill) fill.style.width = `${pct}%`;

    const legend = document.getElementById('dm-enrich-run-legend');
    if (legend) {
        legend.innerHTML = '';
        const item = (label, value) => {
            const span_ = document.createElement('span');
            const b = document.createElement('b');
            b.textContent = value;
            span_.append(`${label} `, b);
            legend.appendChild(span_);
        };
        item('started at', `${start.toLocaleString()} (${pctOf(start)})`);
        item(finished ? 'finished at' : 'now', `${now.toLocaleString()} (${pctOf(now)})`);
        item('target', `${target.toLocaleString()} (${pctOf(target)})`);
        if (finished) {
            item('', now >= target ? 'target reached'
                     : `stopped ${(target - now).toLocaleString()} short of the target`);
        } else {
            item('still to annotate', Math.max(0, target - now).toLocaleString());
        }
    }

    // How far back through the person's history each half of the cycle has
    // walked — the other half of "how far has it come", shown on the panel
    // rather than only in the chart's tooltip.
    const cursors = document.getElementById('dm-enrich-run-cursors');
    if (cursors) {
        // Only the halves that have moved, so a plan with no deep-dive share
        // never reads "Deep dive has worked back to —".
        const walked = [];
        if (progress.b_cursor) walked.push(`Deep dive has worked back to ${progress.b_cursor}`);
        if (progress.a_cursor) {
            walked.push(walked.length
                ? `random daily sample to ${progress.a_cursor}`
                : `Random daily sample has worked back to ${progress.a_cursor}`);
        }
        cursors.textContent = walked.join(' \u00b7 ');
    }
}


// The panel's two live buttons: what Arm says and whether either can be
// pressed. Both go dead when the annotation target is met — there is nothing
// left for a cycle to do — and the tooltip that explains each disabled state
// sits on the button's WRAPPER span, which still hovers when the button
// inside it is disabled. Called on every render AND on every panel input,
// because the target the operator is typing decides both.
function dmEnrichButtonsRefresh() {
    // The target Arm would actually apply: the form's live value, because Arm
    // posts the settings along with the state. Raising the target is how a met
    // plan becomes armable again, and the button has to say so as the slider
    // moves — not a save and a render later. The tick button starts no save,
    // so it stays on the saved plan's target.
    const armTarget = dmEnrichTargetValue || dmEnrichProgressCache.annotation_target || 0;
    const floor = dmEnrichProgressCache.target_floor ?? 0;

    const armBtn = document.getElementById('dm-enrich-arm-btn');
    if (armBtn) {
        const running = dmEnrichArmed && dmEnrichState === 'running';
        // Idle with the target met is the one state where arming does
        // nothing: the supervisor closes the plan again on its next cycle,
        // after the re-arm has reset both cursors and moved the run's
        // starting line for no work. Name the actual next step instead —
        // the word "again" only warns, it does not tell the operator what
        // to do, and it does not stop the click.
        const stuck = dmEnrichState === 'done' && armTarget > 0 && floor >= armTarget;
        armBtn.disabled = stuck;
        armBtn.textContent = running ? 'Pause'
            : (dmEnrichState === 'paused' ? 'Resume'
            : (stuck ? 'Raise the target to arm'
            // Idle with headroom left is a genuinely fresh start — arming an
            // idle plan resets its cursors and walks again from the newest
            // day — so it is an Arm. Only Needs attention keeps the warning
            // word: there the fault is still there unless it has been fixed.
            : (dmEnrichState === 'blocked' ? 'Arm again' : 'Arm')));
        armBtn.classList.toggle('dm-enrich-armed-pulse', running);
        dmEnrichArmTooltip(stuck ? armTarget : 0, floor);
    }

    const tickBtn = document.getElementById('dm-enrich-tick-btn');
    if (tickBtn) {
        const target = dmEnrichProgressCache.annotation_target ?? 0;
        const met = target > 0 && (dmEnrichProgressCache.target_floor ?? 0) >= target;
        tickBtn.disabled = dmEnrichTickInFlight || !dmEnrichArmed || met;
    }
}


// Why the Arm button is disabled, ahead of the panel's standing explanation
// of what Arm does. On the WRAPPER span, like the tick button's: a disabled
// button eats its own hover tooltip in most browsers. Pass target 0 to put
// the standing text back.
let _dmEnrichArmTooltipBase = null;
function dmEnrichArmTooltip(target, floor) {
    const wrap = document.getElementById('dm-enrich-arm-wrap');
    if (!wrap) return;
    if (_dmEnrichArmTooltipBase === null) _dmEnrichArmTooltipBase = wrap.dataset.tooltip || '';
    wrap.dataset.tooltip = target
        ? `Nothing to arm: the annotation target of ${target.toLocaleString()} `
          + `videos is already met (${floor.toLocaleString()} annotated), so a `
          + `plan armed now would stop again on its first cycle. Raise the `
          + `target above ${floor.toLocaleString()} and this button turns back `
          + `into Arm.\n\n${_dmEnrichArmTooltipBase}`
        : _dmEnrichArmTooltipBase;
}


// Any panel input: keep the two buttons honest, then schedule the write.
function dmEnrichPanelChanged() {
    dmEnrichButtonsRefresh();
    dmEnrichAutoSaveSoon();
}


// True while a settings write is pending or in flight — the window in which
// an incoming render must leave the form alone.
function dmEnrichFormBusy() {
    return dmEnrichAutoSaveTimer !== null || dmEnrichAutoSaveInFlight;
}


// The plan's settings have no Save button: a change writes itself, debounced
// so that dragging a slider is one write rather than one per pixel. There is
// one thing autosave cannot do — CREATE a plan. Merely opening the modal
// prefills a suggested target, and writing that would arm-by-accident every
// collection an operator so much as looked at; so before a plan exists, the
// settings are held and Arm saves them (which is what Arm has always done).
function dmEnrichAutoSaveSoon() {
    if (!dmEnrichCollectionId || dmEnrichSavedSettings === null) return;
    if (JSON.stringify(dmEnrichReadSettings()) === dmEnrichSavedSettings) return;
    if (!dmEnrichArmed) {
        dmEnrichMsg('Not armed yet \u2014 press Arm to start with these settings.');
        return;
    }
    if (dmEnrichAutoSaveTimer) clearTimeout(dmEnrichAutoSaveTimer);
    dmEnrichMsg('Saving\u2026');
    dmEnrichAutoSaveTimer = setTimeout(dmEnrichAutoSaveNow, 900);
}


// Write a pending settings change NOW instead of at the end of its debounce —
// for anything that is about to overtake it (closing the modal, Arm, a tick).
function dmEnrichAutoSaveFlush() {
    if (!dmEnrichAutoSaveTimer) return Promise.resolve();
    clearTimeout(dmEnrichAutoSaveTimer);
    dmEnrichAutoSaveTimer = null;
    return dmEnrichAutoSaveNow();
}


function dmEnrichAutoSaveNow() {
    dmEnrichAutoSaveTimer = null;
    if (!dmEnrichCollectionId || !dmEnrichArmed) return Promise.resolve();
    const settings = dmEnrichReadSettings();
    const body = JSON.stringify(settings);
    if (body === dmEnrichSavedSettings) return Promise.resolve();
    dmEnrichAutoSaveInFlight = true;
    // No re-fetch after the write: the POST answers with the plan and its
    // progress, and a second GET would re-render the form the operator is
    // still holding.
    return dmEnrichPost({ settings }, 'Saving\u2026', { reload: false })
        .then(saved => {
            dmEnrichAutoSaveInFlight = false;
            if (!saved) return;
            // The baseline is what was SENT, not what the form says now:
            // anything changed since stays dirty and schedules its own write.
            dmEnrichSavedSettings = body;
            dmEnrichMsg(dmEnrichTargetValue > 0
                ? `Saved \u2014 target ${dmEnrichTargetValue.toLocaleString()}.`
                : 'Saved.', 'ok');
        });
}


// What the next tick will do for this plan, in the operator's words — the
// same backlog-first rule the supervisor's handoff applies. Shown in the
// status strip while the plan is armed and nothing is in flight.
function dmEnrichNextLabel(progress) {
    const target = progress.annotation_target ?? 0;
    const annotated = progress.target_floor ?? 0;
    if (!target || annotated >= target) return '';
    const backlog = Math.min(
        Math.max(0, (progress.unique_scraped || 0) - (progress.unique_annotated || 0)),
        target - annotated);
    if (backlog) return `annotate the ${backlog.toLocaleString()} videos that are already scraped`;
    const cycleItems = dmEnrichEffectiveCycleItems();
    return cycleItems
        ? `queue ~${cycleItems.toLocaleString()} videos to scrape`
        : 'queue the next videos to scrape';
}


// While a worker is in flight, re-fetch the panel every 20s so the status
// strip's activity line (and the bar, when a batch lands) stays current.
// Stops itself when the modal closes or moves to another collection, and
// idles completely between steps — the strip then changes only on a tick,
// which the tick button already reports.
function dmEnrichScheduleRefresh(data) {
    if (dmEnrichRefreshTimer) {
        clearTimeout(dmEnrichRefreshTimer);
        dmEnrichRefreshTimer = null;
    }
    const kind = (data.activity || {}).kind;
    if (!kind || kind === 'waiting') return;
    const cid = dmEnrichCollectionId;
    dmEnrichRefreshTimer = setTimeout(() => {
        dmEnrichRefreshTimer = null;
        const group = document.getElementById('edit-collection-enrichment-group');
        if (dmEnrichCollectionId !== cid || !group || group.offsetParent === null) return;
        dmEnrichLoad(cid);
    }, 20000);
}


// Blank every data-bearing element before a load: the panel is shared by all
// collections, so without this the modal opens showing the PREVIOUS
// collection's charts for the seconds the fetch takes.
function dmEnrichResetPanel() {
    const statusEl = document.getElementById('dm-enrich-status-line');
    if (statusEl) statusEl.textContent = '';
    const loadingEl = document.getElementById('dm-enrich-loading');
    if (loadingEl) loadingEl.style.display = '';
    const historyEl = document.getElementById('dm-enrich-history-list');
    if (historyEl) historyEl.innerHTML = '';
    const progEl = document.getElementById('dm-enrich-progress');
    if (progEl) progEl.textContent = '';
    for (const id of ['dm-enrich-target', 'dm-enrich-target-pct',
                      'dm-enrich-target-readout', 'dm-enrich-target-warning',
                      'dm-enrich-day-cap-value', 'dm-enrich-days-readout',
                      'dm-enrich-earliest-value', 'dm-enrich-ready-sessions']) {
        const el = document.getElementById(id);
        if (el) el.textContent = '';
    }
    dmEnrichEarliest = '';
    dmEnrichDayCapValue = 50;
    dmEnrichEstimateStats = null;
    dmEnrichTiming = null;
    const earliestRow = document.getElementById('dm-enrich-earliest-row');
    if (earliestRow) earliestRow.style.display = 'none';
    const handle = document.querySelector('#dm-enrich-chart-wrap .dm-enrich-earliest-handle');
    if (handle) handle.style.display = 'none';
    const capSlider = document.getElementById('dm-enrich-day-cap-slider');
    if (capSlider) capSlider.disabled = true;
    const warn = document.getElementById('dm-enrich-target-warning');
    if (warn) warn.style.display = 'none';
    const runBox = document.getElementById('dm-enrich-run');
    if (runBox) runBox.style.display = 'none';
    dmEnrichProgressCache = {};
    dmEnrichDailyCache = null;
    dmEnrichTargetValue = 0;
    dmEnrichSavedSettings = null;
    if (dmEnrichAutoSaveTimer) {
        clearTimeout(dmEnrichAutoSaveTimer);
        dmEnrichAutoSaveTimer = null;
    }
    dmEnrichAutoSaveInFlight = false;
    if (dmEnrichRefreshTimer) {
        clearTimeout(dmEnrichRefreshTimer);
        dmEnrichRefreshTimer = null;
    }
    const chart = document.getElementById('dm-enrich-daily-chart');
    if (chart) {
        chart.style.display = 'none';
        if (chart._plotlyInited && window.Plotly) {
            window.Plotly.purge(chart);
            chart._plotlyInited = false;
            chart._dmEnrichAfterplotHooked = false;
        }
    }
    for (const id of ['dm-enrich-bar', 'dm-enrich-legend']) {
        const el = document.getElementById(id);
        if (el) el.style.display = 'none';
    }
    const slider = document.getElementById('dm-enrich-target-slider');
    if (slider) slider.disabled = true;
    const armBtn = document.getElementById('dm-enrich-arm-btn');
    if (armBtn) armBtn.classList.toggle('dm-enrich-armed-pulse', false);
}

function dmEnrichLoad(collectionId) {
    const group = document.getElementById('edit-collection-enrichment-group');
    if (!group) return;
    if (!_dmCan('tab.data_management.edit_collections')) { dmEnrichHide(); return; }
    group.style.display = '';
    // Only wipe when the panel moves to a DIFFERENT collection — same-id
    // reloads (after a save or a tick) keep the current render until fresh
    // data lands, avoiding a flicker on every save.
    if (dmEnrichCollectionId !== collectionId) dmEnrichResetPanel();
    dmEnrichCollectionId = collectionId;
    dmEnrichMsg('');
    fetch(`/api/manage/collections/${encodeURIComponent(collectionId)}/enrichment`)
        .then(r => r.json())
        .then(data => {
            if (dmEnrichCollectionId !== collectionId) return; // modal moved on
            if (data.error) { dmEnrichMsg(data.error, true); return; }
            dmEnrichEnabledSiteWide = !!data.enabled_site_wide;
            dmEnrichRender(data);
            if (dmEnrichHistoryOpen) dmEnrichHistoryLoad();
        })
        .catch(err => dmEnrichMsg(`Could not load the enrichment plan: ${err}`, true));
}

// opts.reload: re-fetch the whole panel afterwards. Arm/Pause do (the state
// change is worth the authoritative read); an autosave does not — the second
// render would land on a form the operator is still using.
function dmEnrichPost(payload, busyMsg, opts = {}) {
    const cid = dmEnrichCollectionId;
    if (!cid) return Promise.resolve(null);
    const reload = opts.reload !== false;
    dmEnrichMsg(busyMsg || 'Saving...');
    return fetch(`/api/manage/collections/${encodeURIComponent(cid)}/enrichment`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    })
        .then(r => r.json())
        .then(data => {
            if (dmEnrichCollectionId !== cid) return null;
            if (data.error) { dmEnrichMsg(data.error, true); return null; }
            // The POST does not report the site-wide switch; the last GET did.
            dmEnrichRender({ ...data, enabled_site_wide: dmEnrichEnabledSiteWide });
            if (reload) dmEnrichLoad(cid);
            return data;
        })
        .catch(err => { dmEnrichMsg(`Save failed: ${err}`, true); return null; });
}

async function dmEnrichToggleArmed() {
    // This POST sends the settings itself; a debounce still ticking would
    // repeat the write a second later for nothing.
    if (dmEnrichAutoSaveTimer) {
        clearTimeout(dmEnrichAutoSaveTimer);
        dmEnrichAutoSaveTimer = null;
    }
    const next = (dmEnrichArmed && dmEnrichState === 'running') ? 'paused' : 'running';
    const extra = {};
    if (next === 'running') {
        // Arming hands the loop whatever is already in the shared queues —
        // ask first when any of it is not this collection's own work.
        const verb = dmEnrichState === 'paused' ? 'Resume' : 'Arm';
        const gate = await dmEnrichQueueGate(verb);
        if (!gate || gate.choice === null) { dmEnrichMsg('Cancelled — nothing changed.'); return; }
        const foreign = _dmEnrichForeignCount(gate.preview);
        if (gate.choice === 'empty') await _dmEnrichEmptyTicked(gate.preview);
        extra.queue_choice = gate.choice;
        extra.foreign_queued = foreign;
    }
    dmEnrichPost({ state: next, settings: dmEnrichReadSettings(), ...extra },
                 next === 'running' ? 'Arming...' : 'Pausing...')
        .then(d => { if (d) dmEnrichMsg(next === 'running' ? 'Armed.' : 'Paused.', 'ok'); });
}

// What one supervisor tick decided, in the operator's words. Both tick paths
// (inline locally, dispatched on Cloud Run) report through this map — a tick
// that does nothing has to say so, or the panel looks dead.
const DM_ENRICH_TICK_LABELS = {
    scrape: 'started the scraper',
    annotate: 'started the annotator',
    consolidate: 'started a consolidation',
    handoff: 'queued the scraped videos for annotation',
    annotate_held: 'holding the queued videos until there are enough for an annotation batch',
    plan: 'queued the next batch of videos to scrape',
    busy: 'a scrape, annotation or refresh is still running — try again when it finishes',
    idle: 'no collection is armed',
    nothing_to_do: 'nothing to do right now',
    disabled: 'automatic enrichment is switched off for the whole site',
    scrape_stalled: 'the scrape queue is not getting any shorter, so the plan was stopped (Needs attention)',
    annotate_stalled: 'the annotation queue is not getting any shorter, so the plan was stopped (Needs attention)',
    waiting_consolidate: 'waiting for a scrape or annotation to finish before consolidating',
    finalize: 'started the analysis refresh',
};

function dmEnrichTickLabel(action) {
    return DM_ENRICH_TICK_LABELS[action] || action || 'done';
}

// On Cloud Run the tick is a Cloud Task, so the POST proves only that it was
// dispatched. Poll the plan endpoint until the supervisor's status file shows a
// run NEWER than the one that was there before the dispatch (compared by value,
// so clock skew between the two services cannot fool it), then report what it
// decided. Without this, "found nothing to do" and "started work" look the same.
function dmEnrichPollTick(cid, prevStart, attempt = 0) {
    const MAX_ATTEMPTS = 20;   // ~40s; a tick is a couple of parquet reads
    const stop = () => { dmEnrichTickInFlight = false; dmEnrichButtonsRefresh(); };
    if (dmEnrichCollectionId !== cid) { stop(); return; }
    fetch(`/api/manage/collections/${encodeURIComponent(cid)}/enrichment`)
        .then(r => r.json())
        .then(data => {
            if (dmEnrichCollectionId !== cid) { stop(); return; }
            if (data.error) { stop(); dmEnrichMsg(data.error, true); return; }
            const tick = data.last_tick || {};
            const settled = tick.start_time && tick.start_time !== prevStart
                            && tick.state !== 'running';
            if (settled || attempt >= MAX_ATTEMPTS) {
                stop();
                dmEnrichRender(data);
                if (!settled) {
                    dmEnrichMsg('Cycle dispatched, but it has not reported back yet — '
                              + 'watch the workers on the Scrape/Annotation pages.');
                } else if (tick.error || tick.state === 'failed') {
                    dmEnrichMsg(`Tick failed: ${tick.error || 'see the worker log'}.`, true);
                } else {
                    dmEnrichMsg(`Tick: ${dmEnrichTickLabel(tick.action)}.`);
                }
                return;
            }
            setTimeout(() => dmEnrichPollTick(cid, prevStart, attempt + 1), 2000);
        })
        .catch(() => {
            if (attempt >= MAX_ATTEMPTS) { stop(); return; }
            setTimeout(() => dmEnrichPollTick(cid, prevStart, attempt + 1), 2000);
        });
}

// ---- Queue-aware confirmation before a cycle starts ----------------------
// The scrape queue is one file per platform and the annotation queue one file
// for everyone; the loop drains whatever is in them before it can do its own
// work. A backlog another study queued (a few thousand videos) can take a
// plan's whole first hour, with nothing saying so. Before Arm / Resume / Run
// a cycle, ask the server what the queues hold and, when any of it is not
// this collection's, show it and let the operator choose: drain it first
// (the default) or empty it now.
let _dmEnrichQueueResolver = null;

function _dmEnrichForeignCount(preview) {
    if (!preview) return 0;
    return ((preview.scrape || {}).foreign || 0) + ((preview.annotate || {}).foreign || 0);
}

// The platforms' own spellings ("TikTok", not "Tiktok"); unknown ones capitalised.
const _DM_PLATFORM_LABELS = { tiktok: 'TikTok', instagram: 'Instagram', youtube: 'YouTube' };
function _dmPlatformLabel(platform) {
    const key = String(platform || '').toLowerCase();
    return _DM_PLATFORM_LABELS[key] || (key ? key.charAt(0).toUpperCase() + key.slice(1) : '');
}

// Resolves {choice: 'drain' | 'empty' | null, preview}. A preview that cannot
// be fetched resolves 'drain': the gate is information, never a lock on Arm.
function dmEnrichQueueGate(verb) {
    const cid = dmEnrichCollectionId;
    if (!cid) return Promise.resolve({ choice: 'drain', preview: null });
    dmEnrichMsg('Checking the queues...');
    return fetch(`/api/manage/collections/${encodeURIComponent(cid)}/enrichment/queue_preview`)
        .then(r => r.json())
        .then(preview => {
            dmEnrichMsg('');
            if (!preview || preview.error || !_dmEnrichForeignCount(preview)) {
                return { choice: 'drain', preview: preview && !preview.error ? preview : null };
            }
            return _dmEnrichQueueDialog(verb, preview).then(choice => ({ choice, preview }));
        })
        .catch(() => { dmEnrichMsg(''); return { choice: 'drain', preview: null }; });
}

function _dmEnrichQueueRowHtml(key, q, label, platform, canEmpty) {
    const foreign = q.foreign || 0;
    let who;
    if (q.breakdown === false) {
        who = 'Too many videos to work out which collections they belong to.';
    } else {
        const parts = [`This collection: ${(q.this_collection || 0).toLocaleString()}`];
        const others = (q.others || []).map(o =>
            `${escapeHtml(o.display_id || o.collection_id)} (${(o.n || 0).toLocaleString()})`);
        if (others.length) {
            parts.push(`Other collections: ${others.join(', ')}`
                     + (q.more ? ` and ${q.more.toLocaleString()} more` : ''));
        }
        if (q.unattributed) parts.push(`Not in any collection: ${q.unattributed.toLocaleString()}`);
        who = parts.join(' · ');
    }
    let cost = '';
    if (key === 'annotate' && q.cost_estimate && q.cost_estimate.est_cost_usd) {
        cost = ` Annotating them would cost about $${q.cost_estimate.est_cost_usd.toLocaleString()}`
             + ` (${escapeHtml(q.cost_estimate.model || q.cost_estimate.backend || '')}).`;
    }
    const tick = canEmpty
        ? `<label><input type="checkbox" class="dm-enrich-queue-empty" data-queue="${key}"`
          + ` data-platform="${escapeHtml(platform)}"${foreign ? ' checked' : ''}>`
          + ` Remove all ${q.total.toLocaleString()} from this queue first</label>`
        : '';
    return `<div class="dm-enrich-queue-row">
        <div class="text-sm"><b>${escapeHtml(label)}:</b> ${q.total.toLocaleString()} video(s) waiting,
            <b>${foreign.toLocaleString()}</b> of them from other collections.${cost}</div>
        <div class="text-xs who">${who}</div>${tick}</div>`;
}

function _dmEnrichQueueDialog(verb, preview) {
    const overlay = document.getElementById('dm-enrich-queue-overlay');
    const rows = document.getElementById('dm-enrich-queue-rows');
    if (!overlay || !rows) return Promise.resolve('drain');
    const platform = preview.platform || 'tiktok';
    const platformLabel = _dmPlatformLabel(platform);
    const canEmpty = !!preview.can_empty;
    let html = '';
    const scrape = { ...(preview.scrape || {}), breakdown: preview.breakdown };
    const annotate = { ...(preview.annotate || {}), breakdown: preview.breakdown };
    if (scrape.total) html += _dmEnrichQueueRowHtml('scrape', scrape, `${platformLabel} scrape queue`, platform, canEmpty);
    if (annotate.total) html += _dmEnrichQueueRowHtml('annotate', annotate, 'Annotation queue', platform, canEmpty);
    rows.innerHTML = html;

    const note = document.getElementById('dm-enrich-queue-note');
    if (note) {
        const armed = (preview.armed_elsewhere || []).map(p => p.display_id || p.collection_id);
        let text = canEmpty
            ? 'Removed videos are not scraped or annotated unless someone queues them again from their study.'
            : 'You cannot remove queued videos from here — that needs access to the Scrape or Annotation page.';
        if (armed.length) {
            text += ` Automatic enrichment is also running for ${armed.join(', ')} on ${platformLabel}; the plans take turns.`;
        }
        note.textContent = text;
    }

    const emptyBtn = document.getElementById('dm-enrich-queue-empty');
    const drainBtn = document.getElementById('dm-enrich-queue-drain');
    const refreshButtons = () => {
        const ticked = rows.querySelectorAll('.dm-enrich-queue-empty:checked').length;
        if (emptyBtn) {
            emptyBtn.style.display = canEmpty && ticked ? '' : 'none';
            emptyBtn.textContent = `Remove them, then ${verb}`;
        }
        if (drainBtn) drainBtn.textContent = `${verb} — process them first`;
    };
    rows.querySelectorAll('.dm-enrich-queue-empty').forEach(cb => cb.addEventListener('change', refreshButtons));
    refreshButtons();

    overlay.classList.add('visible');
    setTimeout(() => { try { if (drainBtn) drainBtn.focus(); } catch (_) { } }, 50);
    return new Promise(resolve => { _dmEnrichQueueResolver = resolve; });
}

function _dmEnrichQueueResolve(value) {
    const overlay = document.getElementById('dm-enrich-queue-overlay');
    if (overlay) overlay.classList.remove('visible');
    if (_dmEnrichQueueResolver) {
        const r = _dmEnrichQueueResolver;
        _dmEnrichQueueResolver = null;
        r(value);
    }
}

// Empty the queues ticked in the dialog (the existing Empty Queue endpoint,
// one call per queue). Resolves when every call has answered.
async function _dmEnrichEmptyTicked(preview) {
    const rows = document.getElementById('dm-enrich-queue-rows');
    if (!rows) return;
    const ticked = [...rows.querySelectorAll('.dm-enrich-queue-empty:checked')];
    if (!ticked.length) return;
    dmEnrichMsg('Removing the queued videos...');
    const reason = `before arming ${dmEnrichCollectionId}`;
    for (const cb of ticked) {
        const type = cb.dataset.queue;
        const body = type === 'scrape' ? { platform: cb.dataset.platform, reason } : { reason };
        try {
            const res = await fetch(`/api/manage/enrichment/empty_queue/${type}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            }).then(r => r.json());
            if (res && res.error) dmEnrichMsg(`Could not empty the ${type} queue: ${res.error}`, true);
        } catch (err) {
            dmEnrichMsg(`Could not empty the ${type} queue: ${err}`, true);
        }
    }
    if (typeof fetchEnrichmentStats === 'function') fetchEnrichmentStats();
}


// ---- Enrichment history ----------------------------------------------------
// One durable, high-level record of what the machinery did — read from the
// journal the supervisor, the workers and the queue endpoints write. The panel
// shows one collection's slice; Dataset Assembly shows everything.
let dmEnrichHistoryOpen = false;

function _dmHistoryRowHtml(e) {
    const when = (typeof fypFmtDateTimeShort === 'function' && e.ts) ? fypFmtDateTimeShort(e.ts) : (e.ts || '');
    const coll = e.display_id
        ? `<span class="dm-history-coll">${escapeHtml(e.display_id)}</span>` : '';
    const tagged = e.display_ids || [];
    const more = (e.collection_ids || []).length - tagged.length;
    const tip = tagged.length
        ? ` title="${escapeHtml(tagged.join(', '))}${more > 0 ? `, and ${more} more` : ''}"` : '';
    return `<tr class="dm-history-row dm-history--${escapeHtml(e.family || 'plan')}">
        <td class="dm-history-when">${escapeHtml(when)}</td>
        <td class="dm-history-kind"><span class="dm-history-chip">${escapeHtml(e.label || e.kind || '')}</span></td>
        <td class="dm-history-msg"${tip}>${coll}${escapeHtml(e.message || '')}</td></tr>`;
}

function _dmHistoryRender(container, events, emptyText) {
    if (!container) return;
    if (!events || !events.length) {
        container.innerHTML = `<div class="text-xs" style="padding: 8px 10px; color: var(--color-text-tertiary);">${escapeHtml(emptyText)}</div>`;
        return;
    }
    container.innerHTML = `<table class="dm-history"><tbody>${events.map(_dmHistoryRowHtml).join('')}</tbody></table>`;
}

function dmEnrichHistoryToggle() {
    dmToggleAdvanced('dm-enrich-history', 'dm-enrich-history-toggle');
    const panel = document.getElementById('dm-enrich-history');
    dmEnrichHistoryOpen = !!panel && panel.style.display !== 'none';
    if (dmEnrichHistoryOpen) dmEnrichHistoryLoad();
}

function dmEnrichHistoryLoad() {
    const cid = dmEnrichCollectionId;
    const box = document.getElementById('dm-enrich-history-list');
    if (!cid || !box) return;
    if (!box.innerHTML) box.innerHTML = '<div class="text-xs" style="padding: 8px 10px; color: var(--color-text-tertiary);">Loading…</div>';
    fetch(`/api/manage/enrichment/history?collection_id=${encodeURIComponent(cid)}&limit=40`)
        .then(r => r.json())
        .then(data => {
            if (dmEnrichCollectionId !== cid) return;
            _dmHistoryRender(box, data.events,
                'Nothing recorded for this collection yet — the history starts with the next plan action or worker run.');
        })
        .catch(() => {});
}

// The Dataset Assembly card: everything, or one collection via the filter.
function dmHistoryLoad() {
    const box = document.getElementById('dm-history-list');
    if (!box) return;
    const sel = document.getElementById('dm-history-filter');
    const cid = sel && sel.value ? sel.value : '';
    const url = `/api/manage/enrichment/history?limit=100${cid ? `&collection_id=${encodeURIComponent(cid)}` : ''}`;
    fetch(url)
        .then(r => r.json())
        .then(data => {
            _dmHistoryRender(box, data.events,
                'Nothing recorded yet — the history starts with the next plan action, queue build or worker run.');
            if (sel && Array.isArray(data.collections)) {
                const current = sel.value;
                const known = new Set([...sel.options].map(o => o.value));
                for (const c of data.collections) {
                    if (known.has(c.collection_id)) continue;
                    const opt = document.createElement('option');
                    opt.value = c.collection_id;
                    opt.textContent = c.display_id || c.collection_id;
                    sel.appendChild(opt);
                }
                sel.value = current;
            }
            const stamp = document.getElementById('dm-history-stamp');
            if (stamp) {
                const n = (data.events || []).length;
                // An epoch instant: the shared helper renders it in the viewer's zone.
                stamp.textContent = `${n} event(s)${cid ? ' for this collection' : ''} · as of `
                    + fypFmtTime(Date.now());
            }
        })
        .catch(() => {});
}


// ---- Armed plans on the queue pages ----------------------------------------
// The Scrape/Annotation pages show which armed plan will drain a queue on its
// next tick, and how much of the queue is that plan's own slice — before an
// operator builds or empties a queue under it.
let _lastEnrichmentStats = null;

function _armedPlanLabel(plans) {
    return plans.map(p => p.display_id || p.collection_id).join(', ');
}

function renderArmedPlanNotes(data) {
    const armed = data.armed_plans_by_platform || {};
    const own = data.queue_plan_items_by_platform || {};
    for (const el of document.querySelectorAll('[id^="enrich_scrape_plan_note_"]')) {
        const platform = el.id.slice('enrich_scrape_plan_note_'.length);
        const plans = armed[platform] || [];
        if (!plans.length) { el.style.display = 'none'; el.textContent = ''; continue; }
        const queued = (data.scrape_queues || {})[platform] || 0;
        const mine = own[platform] || 0;
        const other = Math.max(0, queued - mine);
        let text = `Automatic enrichment is running for ${_armedPlanLabel(plans)}. `
                 + 'Whatever is in this queue is scraped in its next cycle';
        if (!queued) {
            text += '.';
        } else if (!mine) {
            text += ` — none of these ${queued.toLocaleString()} videos were queued by the plan, `
                  + 'so they are scraped first, before the plan\'s own videos.';
        } else if (!other) {
            text += ` — all ${queued.toLocaleString()} of these videos were queued by the plan.`;
        } else {
            text += ` — ${mine.toLocaleString()} of these ${queued.toLocaleString()} videos were queued by the plan; `
                  + `the other ${other.toLocaleString()} were queued here and are scraped along with them.`;
        }
        el.textContent = text;
        el.style.display = '';
    }
    const annNote = document.getElementById('enrich_annotate_plan_note');
    if (annNote) {
        const all = Object.values(armed).flat();
        if (!all.length) {
            annNote.style.display = 'none';
            annNote.textContent = '';
        } else {
            annNote.textContent = `Automatic enrichment is running for ${_armedPlanLabel(all)}. `
                + 'Anything in this queue is annotated in its next cycle, and annotation costs money — '
                + 'queue only videos you mean to annotate.';
            annNote.style.display = '';
        }
    }
}


function dmEnrichTick() {
    const cid = dmEnrichCollectionId;
    if (!cid) return;
    _dmEnrichTickAfterGate(cid);
}

async function _dmEnrichTickAfterGate(cid) {
    // Settings first: a cycle run by hand must use what the panel shows, not
    // what the plan held before the operator's last edit finished its debounce.
    await dmEnrichAutoSaveFlush();
    // A manual cycle drains the shared queues exactly like an automatic one,
    // so it gets the same queue check.
    const gate = await dmEnrichQueueGate('Run a cycle');
    if (!gate || gate.choice === null) { dmEnrichMsg('Cancelled — nothing started.'); return; }
    if (gate.choice === 'empty') await _dmEnrichEmptyTicked(gate.preview);
    if (dmEnrichCollectionId !== cid) return;
    const btn = document.getElementById('dm-enrich-tick-btn');
    dmEnrichTickInFlight = true;
    if (btn) btn.disabled = true;
    dmEnrichMsg('Starting a cycle...');
    fetch(`/api/manage/collections/${encodeURIComponent(cid)}/enrichment/tick`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
    })
        .then(r => r.json())
        .then(data => {
            const stop = () => { dmEnrichTickInFlight = false; dmEnrichButtonsRefresh(); };
            if (data.error || data.status === 'error') {
                stop();
                dmEnrichMsg(data.error || data.message || 'Could not start.', true);
                return;
            }
            if (data.status === 'completed') {
                // Local mode runs the tick inline and reports what it did.
                stop();
                dmEnrichMsg(`Tick: ${dmEnrichTickLabel(data.action)}.`);
                dmEnrichLoad(cid);
                return;
            }
            dmEnrichMsg('Cycle dispatched — waiting for it to report back...');
            dmEnrichPollTick(cid, data.prev_start_time || null);
        })
        .catch(err => {
            dmEnrichTickInFlight = false;
            dmEnrichButtonsRefresh();
            dmEnrichMsg(`Failed: ${err}`, true);
        });
}


// as N tasks would reload and rewrite the multi-GB activity parquet N times.
