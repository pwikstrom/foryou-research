
// Timelines Tab Logic

window.timelines = {
    currentStudy: null,
    currentDonationId: null,
    collectionList: [],
    timelineData: null,
    timelineState: {
        categoricalSelections: {},
        analysisToggles: {},
        activeFilters: {},
        smoothing: 7,
        showRaw: false
    },

    init: async function () {
        console.log("Initializing Timelines Tab");
        // Ensure stats are loaded for the left panel Details
        if (!window.pe_data || window.pe_data.length === 0) {
            if (typeof window.pe_loadCachedStats === 'function') {
                try {
                    await window.pe_loadCachedStats();
                } catch (e) {
                    console.error('Failed to load pe stats in timelines:', e);
                }
            }
        }
        // No study selection anymore, load all valid collections directly
        this.loadDonations();
    },

    loadStudies: function () {
        // Deprecated
    },

    loadDonations: async function () {
        const select = document.getElementById('timelines-collection-select');

        try {
            const res = await fetch('/api/timelines/collections', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            });

            const data = await res.json();
            if (data.error) {
                select.innerHTML = `<option value="" disabled selected>Error: ${data.error}</option>`;
                return;
            }

            const activeStudy = (window.studyState && window.studyState.current) || null;

            let list = data.filter(d => !d.hidden);
            if (activeStudy) {
                list = list.filter(d => {
                    if (Array.isArray(d.studies)) return d.studies.includes(activeStudy);
                    return d.study === activeStudy;
                });
            }
            // Normalise `study` to the active study so downstream reads
            // (currentStudy, drill-down guards) reflect the user's context.
            if (activeStudy) {
                list.forEach(d => { d.study = activeStudy; });
            }

            list.sort((a, b) => {
                const nameA = (a.display_collection_id && a.display_collection_id.trim())
                    ? a.display_collection_id : a.collection_id;
                const nameB = (b.display_collection_id && b.display_collection_id.trim())
                    ? b.display_collection_id : b.collection_id;
                return String(nameA).toLowerCase().localeCompare(String(nameB).toLowerCase());
            });

            this.collectionList = list;

            // If the currently-selected collection is no longer in the filtered
            // list, clear it so renderCollectionDropdown auto-selects the first
            // eligible collection under the new study.
            if (this.currentDonationId &&
                !this.collectionList.some(d => d.collection_id === this.currentDonationId)) {
                this.currentDonationId = null;
                this.timelineData = null;
                const chartsContainer = document.getElementById('timelines-charts');
                if (chartsContainer) chartsContainer.innerHTML = '';
            }

            this.renderCollectionDropdown();

        } catch (e) {
            console.error("Error loading collections:", e);
            select.innerHTML = '<option value="" disabled selected>Failed to load</option>';
        }
    },

    // Collections with fewer than this many active days can't be analysed
    // meaningfully (7-day moving average + break/anomaly stats need breathing
    // room). Kept in sync with fyp.timeline_analysis.MIN_ACTIVE_DAYS_FOR_TIMELINE.
    MIN_ACTIVE_DAYS_FOR_TIMELINE: 14,

    renderCollectionDropdown: function () {
        const select = document.getElementById('timelines-collection-select');
        const countSpan = document.getElementById('timelines-collection-count');
        select.innerHTML = '';

        if (this.collectionList.length === 0) {
            select.innerHTML = '<option value="" disabled selected>No collections found</option>';
            return;
        }

        const minDays = this.MIN_ACTIVE_DAYS_FOR_TIMELINE;
        this.collectionList.forEach(d => {
            const opt = document.createElement('option');
            opt.value = d.collection_id;
            const base = d.display_collection_id && d.display_collection_id.trim() !== ''
                ? d.display_collection_id
                : d.collection_id;
            const ad = (d.active_days != null) ? d.active_days : null;
            const suffix = (ad != null) ? ` (${ad}d)` : '';
            opt.textContent = `${base}${suffix}`;
            if (ad != null && ad < minDays) {
                opt.disabled = true;
                opt.title = `Only ${ad} active day${ad === 1 ? '' : 's'} — need at least ${minDays} for timeline analysis.`;
            }
            if (d.collection_id === this.currentDonationId) {
                opt.selected = true;
            }
            select.appendChild(opt);
        });

        if (countSpan) {
            countSpan.textContent = `${this.collectionList.length} collections`;
        }

        // Auto-select first non-disabled option if none selected.
        if (!this.currentDonationId) {
            const firstEligible = this.collectionList.find(d =>
                d.active_days == null || d.active_days >= minDays);
            if (firstEligible) {
                this.selectDonation(firstEligible.collection_id);
            }
        }
    },

    onCollectionChange: function () {
        const select = document.getElementById('timelines-collection-select');
        if (select && select.value) {
            this.timelineState.categoricalSelections = {};
            this.timelineState.analysisToggles = {};
            this.timelineState.activeFilters = {};
            this.selectDonation(select.value);
        }
    },

    onSmoothingChange: function (value) {
        const w = parseInt(value, 10) || 1;
        this.timelineState.smoothing = w;
        const label = document.getElementById('timelines-smoothing-label');
        if (label) {
            label.textContent = w <= 1 ? 'Off' : `${w}-pt`;
        }
        if (this.timelineData) {
            this.renderTimelineCharts(this.timelineData);
        }
    },

    toggleShowRaw: function (checked) {
        this.timelineState.showRaw = !!checked;
        if (this.timelineData) {
            this.renderTimelineCharts(this.timelineData);
        }
    },

    _movingAvg: function (arr, window) {
        const half = Math.floor(window / 2);
        return arr.map((_, i, a) => {
            const start = Math.max(0, i - half);
            const end = Math.min(a.length, i + half + 1);
            const slice = a.slice(start, end);
            return slice.reduce((s, v) => s + v, 0) / slice.length;
        });
    },

    _formatDuration: function (secs) {
        if (!secs || secs <= 0) return '0s';
        const s = Math.round(secs);
        if (s < 60) return `${s}s`;
        const h = Math.floor(s / 3600);
        const m = Math.floor((s % 3600) / 60);
        if (h > 0) return `${h}h ${m}m`;
        return `${m}m ${s % 60}s`;
    },

    selectDonation: async function (collectionId) {
        this.currentDonationId = collectionId;
        const header = document.getElementById('timelines-header');
        if (header) header.style.display = '';

        const collection = this.collectionList.find(d => d.collection_id === collectionId);
        this.currentStudy = (collection && collection.study) || null;

        // Render tag chips next to the dropdown
        const tagChipsContainer = document.getElementById('timelines-tag-chips');
        if (tagChipsContainer) {
            tagChipsContainer.innerHTML = '';
            if (collection && collection.annotation_tags && Array.isArray(collection.annotation_tags) && collection.annotation_tags.length > 0) {
                collection.annotation_tags.forEach(tag => {
                    const chip = document.createElement('span');
                    chip.innerText = tag;
                    chip.style.display = 'inline-block';
                    chip.style.background = 'var(--btn-primary-bg)';
                    chip.style.color = 'var(--btn-primary-text)';
                    chip.style.padding = '2px 8px';
                    chip.style.borderRadius = '12px';
                    chip.style.verticalAlign = 'middle';
                    tagChipsContainer.appendChild(chip);
                });
            }
        }

        // --- Collection Info Tooltip ---
        let pe_collection = null;
        if (window.pe_data && window.pe_data.length > 0) {
            pe_collection = window.pe_data.find(d => d.collection_id === collectionId);
        }

        if (pe_collection) {

            // Helper to dynamically hide empty items just like PE
            let visibleInfoCount = 0;
            const updateInfoStat = (elementId, value) => {
                const el = document.getElementById(elementId);
                if (!el) return;

                const li = el.closest('li');
                if (value !== null && value !== undefined && value !== '') {
                    el.innerText = value;
                    if (li) li.style.display = 'flex';
                    visibleInfoCount++;
                } else {
                    if (li) li.style.display = 'none';
                }
            };

            // Participant Info
            updateInfoStat('timelines-stat-age', pe_collection.age);
            updateInfoStat('timelines-stat-country', pe_collection.country);
            updateInfoStat('timelines-stat-postcode', pe_collection.postCode);
            updateInfoStat('timelines-stat-display-id', pe_collection.display_collection_id);

            const pInfoSection = document.getElementById('timelines-participant-info-section');
            if (pInfoSection) {
                pInfoSection.style.display = visibleInfoCount > 0 ? 'block' : 'none';
            }

            const fmtDate = (ts) => {
                if (!ts) return 'not provided';
                const d = new Date(ts);
                return d.toLocaleDateString('en-AU', { day: '2-digit', month: 'short', year: 'numeric' });
            };
            document.getElementById('timelines-stat-collection-date').innerText = fmtDate(pe_collection.date);

            // Activity Stats
            const tz = pe_collection.inferred_tz_offset;
            const tzStr = tz !== null && tz !== undefined ? `UTC${tz >= 0 ? '+' : ''}${tz}` : 'Unknown';
            document.getElementById('timelines-stat-timezone').innerText = tzStr;
            document.getElementById('timelines-stat-active-days').innerText = pe_collection.active_days || 0;
            document.getElementById('timelines-stat-total-events').innerText = (pe_collection.total_events || 0).toLocaleString();
            document.getElementById('timelines-stat-peak-segment').innerText = pe_collection.peak_day_segment || 'Unknown';

            document.getElementById('timelines-stat-first-event').innerText = fmtDate(pe_collection.first_event_ts);
            document.getElementById('timelines-stat-last-event').innerText = fmtDate(pe_collection.last_event_ts);

            // Store first event date for the 'exclude before first activity' filter
            this.firstActivityDate = pe_collection.first_event_ts ? pe_collection.first_event_ts.substring(0, 10) : null;

            // Tags
            const currentTags = Array.isArray(pe_collection.annotation_tags) ? pe_collection.annotation_tags : [];
            const tagsDisplay = document.getElementById('timelines-stat-tags');
            if (tagsDisplay) {
                if (currentTags.length > 0) {
                    tagsDisplay.innerText = currentTags.join(', ');
                    const li = tagsDisplay.closest('li');
                    if (li) li.style.display = 'flex';
                } else {
                    tagsDisplay.innerText = '';
                    const li = tagsDisplay.closest('li');
                    if (li) li.style.display = 'none';
                }
            }

        }

        const container = document.getElementById('timelines-charts-container');
        // Force resize of existing plots if any, just in case
        setTimeout(() => {
            window.dispatchEvent(new Event('resize'));
        }, 100);

        container.innerHTML = '<p>Loading timeline data...</p>';

        try {
            const res = await fetch('/api/timelines/data', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    study: this.currentStudy,
                    collection_id: collectionId,
                    interval: 'day'
                })
            });
            const data = await res.json();

            if (data.error) {
                container.innerHTML = `<p style="color:var(--color-danger);">Error: ${data.error}</p>`;
                return;
            }

            //console.log("TIMELINE DEBUG: Received Data", data);
            this.timelineData = data;

            this.renderTimelineCharts();

        } catch (e) {
            console.error("Error fetching timeline data", e);
            container.innerHTML = `<p style="color:var(--color-danger);">Failed to load timeline data.</p>`;
        }
    },

    renderTimelineCharts: function () {
        //console.log("TIMELINE DEBUG: renderTimelineCharts called");
        if (!this.timelineData) {
            //console.log("TIMELINE DEBUG: No timelineData available");
            return;
        }

        const container = document.getElementById('timelines-charts-container');
        // Preserve scroll position of the main scrolling container
        const scrollContainer = document.querySelector('.timelines-main');
        const scrollPos = scrollContainer ? scrollContainer.scrollTop : 0;

        container.innerHTML = '';

        const settings = window.userSettings || {};
        const excludeNoData = !settings.timelines_include_empty_dates;
        const excludeBeforeFirst = !settings.timelines_include_pre_activity;
        const data = this.timelineData;
        let dates = data.dates;

        // Filter out dates before first main activity if checkbox checked
        let startIdx = 0;
        if (excludeBeforeFirst && this.firstActivityDate) {
            startIdx = dates.findIndex(d => d >= this.firstActivityDate);
            if (startIdx < 0) startIdx = 0;
        }

        // Slice dates from startIdx if needed
        if (startIdx > 0) {
            dates = dates.slice(startIdx);
        }

        //console.log("TIMELINE DEBUG: Dates length", dates.length);
        //console.log("TIMELINE DEBUG: Variables keys", Object.keys(data.variables));

        // Collect all plotIds for zoom sync
        const allPlotIds = [];

        // Sort variables according to variables_order if provided from backend (web_timeline_prio)
        let varKeys = Object.keys(data.variables);
        if (data.variables_order && Array.isArray(data.variables_order)) {
            varKeys.sort((a, b) => {
                const idxA = data.variables_order.indexOf(a);
                const idxB = data.variables_order.indexOf(b);
                // If not in array, push to bottom. Otherwise, sort by index
                if (idxA === -1 && idxB === -1) return 0;
                if (idxA === -1) return 1;
                if (idxB === -1) return -1;
                return idxA - idxB;
            });
        }

        // machine_state is always a flat "5: Scrape ok, MA ok" line because
        // the timeline universe filter drops all other states — hide the chart.
        varKeys = varKeys.filter(k => k !== 'machine_state');

        // Iterate over variables
        varKeys.forEach(varName => {
            const varData = data.variables[varName];
            const displayTitle = varData.display_name || (varName === 'machine_state' ? 'Scrape and Annotation States' : varName);

            const chartWrapper = document.createElement('div');
            chartWrapper.className = 'timeline-chart-wrapper';
            chartWrapper.style.marginBottom = '35px';
            chartWrapper.style.paddingBottom = '15px';
            chartWrapper.style.borderBottom = '1px solid var(--color-border-subtle)';
            chartWrapper.style.background = 'var(--chart-bg)';
            chartWrapper.style.padding = '10px';
            chartWrapper.style.borderRadius = '5px';

            // Title Above Plot — row 1: variable name, row 2: "Select top..." + filter chips
            const titleDiv = document.createElement('div');
            titleDiv.id = `title-${varName.replace(/\\s+/g, '_')}`;
            titleDiv.style.cssText = 'display:flex; flex-direction:column; align-items:center; gap:2px;';
            const subtitle = varData.type !== 'categorical' ? `<span class="text-xs font-normal" style="color: var(--color-text-tertiary);"> (mean values)</span>` : '';
            titleDiv.innerHTML = `<h3 class="text-h3" style="margin-top: 0; margin-bottom: 0;">${displayTitle}${subtitle}</h3>`;

            const plotDiv = document.createElement('div');
            plotDiv.style.width = '100%';
            plotDiv.style.height = '400px';

            // Unique ID
            const plotId = `timeline-plot-${varName.replace(/\\s+/g, '_')}`;
            plotDiv.id = plotId;

            chartWrapper.appendChild(titleDiv);
            chartWrapper.appendChild(plotDiv);
            container.appendChild(chartWrapper);

            const traces = [];
            let yAxisTitle = '';
            const xVals = dates; // Use dates for x-axis

            // Shared color palette for categorical charts and overlays
            const colors = [
                '#4CAF50', '#2196F3', '#FFC107', '#E91E63', '#9C27B0',
                '#00BCD4', '#CDDC39', '#FF5722', '#795548', '#607D8B',
                '#26A69A', '#AB47BC', '#EF5350', '#66BB6A'
            ];

            // Stable per-category color map — populated below for categorical
            // variables and reused by the ribbon, line traces, and findings panel
            // so each category keeps the same color everywhere.
            let catColorMap = {};

            // Logic per type
            if (varData.type === 'categorical') {
                // Build analysis lookup and interestingness-sorted category order
                const analysisMap = {};
                const varAnalysis = data.analysis && data.analysis[varName];
                const interestingnessOrder = [];
                if (varAnalysis && Array.isArray(varAnalysis.categories)) {
                    varAnalysis.categories.forEach(a => {
                        analysisMap[a.id] = a;
                        interestingnessOrder.push(a.id);
                    });
                }

                // Default selection: top 3 by interestingness, skipping the
                // "Other" bucket (a heterogeneous residual, not a signal).
                const OTHER_BUCKET = 'Other';
                const interestingnessOrderReal = interestingnessOrder.filter(c => c !== OTHER_BUCKET);
                const topCatsReal = (varData.top_categories || []).filter(c => c !== OTHER_BUCKET);
                const defaultCats = varData.default_all
                    ? (varData.top_categories || [])
                    : (interestingnessOrderReal.length > 0 ? interestingnessOrderReal.slice(0, 3) : topCatsReal.slice(0, 3));
                const selectedCats = this.timelineState.categoricalSelections[varName] || defaultCats;

                // Store in state if not already
                if (!this.timelineState.categoricalSelections[varName]) {
                    this.timelineState.categoricalSelections[varName] = selectedCats;
                }

                // Setup Sidebar Category Selection
                // Slice all per-period series in lockstep — counts (raw plays
                // for hover & sidebar heuristic), weighted_counts (Σw per cat),
                // share_series (pre-computed share pct from the backend), plus
                // the per-period denominators.
                //
                // IMPORTANT: sliceList always returns a NEW array, even when
                // startIdx is 0.  This prevents the frontend "Other"-fold
                // from mutating varData.counts / weighted_counts / share_series
                // on subsequent re-renders (which would accumulate folded
                // categories into Other and make selection-driven renders
                // diverge from the first render).
                const sliceList = (arr) => Array.isArray(arr) ? arr.slice(startIdx) : [];
                let slicedCounts = sliceList(varData.counts);
                let slicedWeightedCounts = sliceList(varData.weighted_counts);
                let slicedShareSeries = sliceList(varData.share_series);
                const slicedVideoCounts = sliceList(varData.daily_video_counts);
                const slicedValidCounts = sliceList(varData.daily_valid_counts);
                const slicedWeightedValid = sliceList(varData.daily_weighted_valid);
                const slicedWeightedVideoTotal = sliceList(varData.daily_weighted_video_total);
                const isMultiLabel = (varData.share_denominator === 'videos');
                // Sidebar heuristic still ranks by raw plays — that's the right
                // structural signal for "did this appear often enough to keep".
                const catTotals = {};
                slicedCounts.forEach(day => {
                    Object.keys(day).forEach(c => {
                        catTotals[c] = (catTotals[c] || 0) + day[c];
                    });
                });
                // Weighted totals across the window — these drive ribbon segment
                // sizing (so the ribbon reads as "share of attention" too).
                const catWeightedTotals = {};
                slicedWeightedCounts.forEach(day => {
                    Object.keys(day).forEach(c => {
                        catWeightedTotals[c] = (catWeightedTotals[c] || 0) + day[c];
                    });
                });
                // Window denominator used by the ribbon — matches per-day
                // chart denominator semantics:
                //   single-label → Σ weighted_valid     (videos with this annotation)
                //   multi-label  → Σ weighted_video_total (all watched videos)
                const windowDenom = isMultiLabel
                    ? slicedWeightedVideoTotal.reduce((s, v) => s + (v || 0), 0)
                    : slicedWeightedValid.reduce((s, v) => s + (v || 0), 0);

                // Two sort orders for different purposes:
                //  - allCatsByAttention: drives ribbon ORDER and fold priority
                //    so the ribbon reads left-to-right from widest to narrowest
                //    segment.  Matches how the ribbon actually looks.
                //  - allCategories (by interestingness): drives default selection
                //    and findings-panel ordering, so the "most interesting" cats
                //    surface first in the chart.
                const allCatsByAttention = Object.keys(catWeightedTotals).sort((a, b) => {
                    if (a === OTHER_BUCKET) return 1;
                    if (b === OTHER_BUCKET) return -1;
                    return (catWeightedTotals[b] || 0) - (catWeightedTotals[a] || 0);
                });
                const allCategories = allCatsByAttention.slice().sort((a, b) => {
                    if (a === OTHER_BUCKET) return 1;
                    if (b === OTHER_BUCKET) return -1;
                    const scoreA = analysisMap[a] && analysisMap[a].score != null ? analysisMap[a].score : -1;
                    const scoreB = analysisMap[b] && analysisMap[b].score != null ? analysisMap[b].score : -1;
                    return scoreB - scoreA;
                });

                // --- Category Filtering Heuristic ---
                // Hard-cap the ribbon at maxCategories.  For high-cardinality
                // variables (hashtags, symbols) the long tail folds into the
                // "Other" bucket, which the user can toggle on via the footer
                // checkbox.  The previous "ensure 50 % coverage" second pass
                // was removed — for long-tail variables it made the ribbon
                // unreadable (hundreds of sliver-width segments).
                //
                // Note: selected cats — whether set manually or via the filter
                // chips (setFilter) — bypass the cap via `isSelected`, so
                // clicking "↑ Rising" can surface a rising cat that didn't
                // make the top-N by attention.  Intended behaviour.
                const grandWeighted = allCatsByAttention.reduce((s, c) => s + (catWeightedTotals[c] || 0), 0);
                const keptCategories = [];

                const minCategories = 5;
                const maxCategories = 30;
                const coverageTarget = 0.95;
                const minObservations = 10;

                let cumulativeWeighted = 0;
                const coveredSet = new Set();
                for (let i = 0; i < allCatsByAttention.length; i++) {
                    const cat = allCatsByAttention[i];
                    if (cat === OTHER_BUCKET) { coveredSet.add(cat); continue; }
                    const catAttention = catWeightedTotals[cat] || 0;
                    const catCount = catTotals[cat] || 0;
                    const isSelected = selectedCats.includes(cat);
                    const isWithinMin = i < minCategories;
                    const isMeaningful = catCount >= minObservations;
                    const isWithinMax = coveredSet.size < maxCategories;
                    const isWithinCoverage = cumulativeWeighted < (coverageTarget * grandWeighted);

                    if (isSelected || isWithinMin || (isWithinCoverage && isMeaningful && isWithinMax)) {
                        coveredSet.add(cat);
                    }
                    cumulativeWeighted += catAttention;
                }

                // Cats that didn't make the cut fold into "Other" — visible
                // only via the footer toggle.
                const frontendFolded = allCatsByAttention.filter(c => !coveredSet.has(c) && c !== OTHER_BUCKET);

                // If anything was folded, mirror the fold across all three
                // per-day series (raw counts, weighted counts, pre-computed
                // shares) so the chart, ribbon, and sidebar agree.  Clone
                // first to avoid polluting varData.* (shared by reference).
                if (frontendFolded.length > 0) {
                    const foldSeries = (series, roundTo) => {
                        const cloned = series.map(day => Object.assign({}, day || {}));
                        for (const cat of frontendFolded) {
                            for (const day of cloned) {
                                if (day[cat]) {
                                    let merged = (day[OTHER_BUCKET] || 0) + day[cat];
                                    if (roundTo != null) merged = +merged.toFixed(roundTo);
                                    day[OTHER_BUCKET] = merged;
                                    delete day[cat];
                                }
                            }
                        }
                        return cloned;
                    };

                    const clonedCounts = foldSeries(slicedCounts, null);
                    const clonedWeighted = foldSeries(slicedWeightedCounts, 2);
                    const clonedShares = foldSeries(slicedShareSeries, 2);

                    let foldedRawGrand = 0;
                    let foldedWeightedGrand = 0;
                    for (const cat of frontendFolded) {
                        foldedRawGrand += catTotals[cat] || 0;
                        foldedWeightedGrand += catWeightedTotals[cat] || 0;
                        delete catTotals[cat];
                        delete catWeightedTotals[cat];
                    }
                    catTotals[OTHER_BUCKET] = (catTotals[OTHER_BUCKET] || 0) + foldedRawGrand;
                    catWeightedTotals[OTHER_BUCKET] = (catWeightedTotals[OTHER_BUCKET] || 0) + foldedWeightedGrand;
                    coveredSet.add(OTHER_BUCKET);

                    // Reassign (don't mutate) — slicedCounts is already a
                    // local array produced by sliceList, but reassigning is
                    // cleaner and leaves the per-day dicts inside varData
                    // untouched regardless.
                    slicedCounts = clonedCounts;
                    slicedWeightedCounts = clonedWeighted;
                    slicedShareSeries = clonedShares;
                }

                // Build kept list in weighted-attention order so ribbon
                // segments read left-to-right from widest to narrowest.
                // "Other" is intentionally excluded from the ribbon — it's
                // opt-in only, controlled by the toggle next to the "Other"
                // footer under the ribbon.
                allCatsByAttention.forEach(cat => {
                    if (coveredSet.has(cat) && cat !== OTHER_BUCKET) keptCategories.push(cat);
                });
                const otherInData = coveredSet.has(OTHER_BUCKET) && (catTotals[OTHER_BUCKET] || 0) > 0;

                // Populate the shared per-category color map.  "Other" still
                // gets a muted gray so if the user toggles it on in the chart
                // it reads as a residual bucket.
                const otherColor = getCSSVar('--color-text-tertiary') || '#888888';
                keptCategories.forEach((cat, i) => {
                    catColorMap[cat] = colors[i % colors.length];
                });
                catColorMap[OTHER_BUCKET] = otherColor;

                const activeFilter = this.timelineState.activeFilters[varName] || 'all';

                // Compute per-chip availability: is there any category in
                // the analysis whose signal matches this filter?  Uses the
                // same normalised thresholds as setFilter and the badges
                // so the three agree.
                const chipAvailable = { rising: false, falling: false, spikes: false, breaks: false, volatile: false };
                if (varAnalysis && Array.isArray(varAnalysis.categories)) {
                    for (const cd of varAnalysis.categories) {
                        if (cd.is_other) continue;
                        const meanShare = (cd.volatility && cd.volatility.mean) || (cd.trend && cd.trend.mean) || 0;
                        const trendThresh = 0.5 * Math.max(meanShare, 1.0);
                        const breakThresh = 0.5 * Math.max(meanShare, 1.0);
                        const volThresh = 0.3 * Math.max(meanShare, 1.0);
                        if (cd.trend && cd.trend.total_change > trendThresh) chipAvailable.rising = true;
                        if (cd.trend && cd.trend.total_change < -trendThresh) chipAvailable.falling = true;
                        if (cd.anomalies && cd.anomalies.length > 0) chipAvailable.spikes = true;
                        if (cd.break && Math.abs(cd.break.delta) > breakThresh) chipAvailable.breaks = true;
                        if (cd.volatility && cd.volatility.std > volThresh) chipAvailable.volatile = true;
                    }
                }

                // Filter chips — styled so the default state reads as the
                // "active look" (bright, full opacity).  Selected state is
                // conveyed by an outline ring + bold weight, not by turning
                // unselected chips faint.  Chips with no matching cats for
                // this variable are the only ones shown as faded/disabled.
                const chipRow = document.createElement('div');
                chipRow.className = 'text-sm';
                chipRow.style.cssText = 'display:flex; align-items:center; gap:6px; margin-bottom:8px;';

                const chipDefs = [
                    { key: 'rising',   label: '↑ Rising',      bg: 'var(--trend-rising-bg)',   color: 'var(--color-accent)',      border: 'var(--trend-rising-border)' },
                    { key: 'falling',  label: '↓ Falling',     bg: 'var(--trend-falling-bg)',  color: 'var(--color-danger-soft)', border: 'var(--trend-falling-border)' },
                    { key: 'spikes',   label: '◎ Spikes',      bg: 'var(--trend-spikes-bg)',   color: 'var(--color-save)',        border: 'var(--trend-spikes-border)' },
                    // Hidden: users find these chips confusing — restore when needed
                    // { key: 'breaks',   label: '⋮ Breaks',      bg: 'var(--trend-breaks-bg)',   color: 'var(--color-info)',        border: 'var(--trend-breaks-border)' },
                    // { key: 'volatile', label: '~ Volatile',    bg: 'var(--trend-volatile-bg)', color: 'var(--color-purple)',      border: 'var(--trend-volatile-border)' },
                ];

                const selectTopSpan = document.createElement('span');
                selectTopSpan.style.cssText = 'color: var(--color-text-tertiary); white-space: nowrap;';
                selectTopSpan.textContent = 'Select top...';
                chipRow.appendChild(selectTopSpan);

                chipDefs.forEach(def => {
                    const chip = document.createElement('div');
                    chip.className = 'filter-chip';
                    chip.id = `chip-${plotId}-${def.key}`;
                    const isActive = (activeFilter === def.key);
                    const isAvail = !!chipAvailable[def.key];
                    const base = `padding: 2px 6px; border-radius: 10px; background: ${def.bg}; color: ${def.color}; border: 1px solid ${def.border};`;
                    if (isAvail) {
                        // Active: outline ring (offset by 2 px) + bold weight.
                        const ring = isActive
                            ? ` box-shadow: 0 0 0 2px ${def.color}; font-weight: 600;`
                            : '';
                        chip.style.cssText = base + ' cursor: pointer; opacity: 1;' + ring;
                        chip.addEventListener('click', () => this.setFilter(varName, def.key));
                    } else {
                        chip.style.cssText = base + ' cursor: not-allowed; opacity: 0.35;';
                        chip.setAttribute('aria-disabled', 'true');
                        chip.setAttribute('title', 'No matching labels for this variable');
                    }
                    chip.textContent = def.label;
                    chipRow.appendChild(chip);
                });
                titleDiv.appendChild(chipRow);

                // Selection indicator — absolutely-positioned markers above
                // the ribbon, one per selected category.  Using absolute
                // positioning avoids flex sibling-shrink drift (which was
                // pulling markers up to ~8 px left of their ribbon segment
                // when multiple tiny selected cats forced min-width growth).
                //
                // Layout:
                //   indicatorWrapper (2 px horizontal padding = Plotly ribbon margin)
                //     indicatorRow (100 % of wrapper content box = plot area)
                //       marker for each selected cat (position: absolute)
                //
                // left/width in %:
                //   left = (Σ catWeight before this cat) / totalKeptWeight * 100
                //   width = max(this cat's segPct%, 6 px) via min-width
                const _indicatorRibbonDenom = Math.max(1, keptCategories.reduce((s, c) => s + (catWeightedTotals[c] || 0), 0));
                const indicatorWrapper = document.createElement('div');
                indicatorWrapper.style.cssText = 'padding: 0 2px; margin-top: 3px; margin-bottom: 2px; box-sizing: border-box;';
                const indicatorRow = document.createElement('div');
                indicatorRow.style.cssText = 'position: relative; width: 100%; height: 3px;';
                indicatorWrapper.appendChild(indicatorRow);
                let cumWeighted = 0;
                keptCategories.forEach(cat => {
                    const wTotal = catWeightedTotals[cat] || 0;
                    const leftPct = (cumWeighted / _indicatorRibbonDenom) * 100;
                    const segPct = (wTotal / _indicatorRibbonDenom) * 100;
                    const isSelected = selectedCats.includes(cat);
                    if (isSelected) {
                        const marker = document.createElement('div');
                        marker.style.cssText = `position: absolute; left: ${leftPct.toFixed(4)}%; width: ${segPct.toFixed(4)}%; min-width: 6px; height: 100%; background: ${catColorMap[cat]}; border-radius: 1.5px;`;
                        indicatorRow.appendChild(marker);
                    }
                    cumWeighted += wTotal;
                });
                chartWrapper.appendChild(indicatorWrapper);

                // Ribbon: horizontal Plotly stacked bar shown under the plot.
                // Clicking a segment toggles the category's line in the chart.
                // Selection cue: unselected segments render at 40% opacity
                // plus no indicator bar above them; selected segments render
                // at full opacity with a colored indicator bar above.
                const ribbonId = `ribbon-${plotId}`;
                const ribbonDiv = document.createElement('div');
                ribbonDiv.id = ribbonId;
                ribbonDiv.style.width = '100%';
                ribbonDiv.style.height = '50px';
                ribbonDiv.style.cursor = 'pointer';
                chartWrapper.appendChild(ribbonDiv);

                // Ribbon segment denominator: use Σ(kept-cat weighted) for
                // BOTH single- and multi-label vars.  Segments then sum to
                // 100 % and fill the bar end-to-end.  For single-label the
                // hover still reads "X % of watch time" (using the true
                // watch-time denominator, which equals Σ weighted_valid —
                // includes folded/Other cats).  For multi-label we show
                // both "% of tag mentions" (segment width) and "% of watch
                // time" (unbiased truth) since they diverge.
                const ribbonSegmentDenom = keptCategories.reduce((s, c) => s + (catWeightedTotals[c] || 0), 0);
                const safeRibbonDenom = Math.max(1, ribbonSegmentDenom);
                const safeWatchDenom = Math.max(1, windowDenom);

                const UNTAGGED_BUCKET = 'No label';

                const ribbonTraces = keptCategories.map(cat => {
                    const wTotal = catWeightedTotals[cat] || 0;
                    const segPct = (wTotal / safeRibbonDenom) * 100;
                    const watchPct = (wTotal / safeWatchDenom) * 100;
                    const isSelected = selectedCats.includes(cat);
                    const cd = analysisMap[cat] || {};
                    // Findings thresholds normalised by category mean share so
                    // multi-label vars (smaller absolute shares) still surface
                    // their genuine signals.
                    const meanShare = (cd.volatility && cd.volatility.mean) || (cd.trend && cd.trend.mean) || 0;
                    const trendChange = cd.trend ? cd.trend.total_change : 0;
                    const trendThresh = 0.5 * Math.max(meanShare, 1.0);
                    const breakDelta = cd.break ? cd.break.delta : 0;
                    const breakThresh = 0.5 * Math.max(meanShare, 1.0);
                    const volStd = cd.volatility ? cd.volatility.std : 0;
                    const volThresh = 0.3 * Math.max(meanShare, 1.0);
                    const badgeBits = [];
                    if (trendChange > trendThresh) badgeBits.push('↑ Rising');
                    if (trendChange < -trendThresh) badgeBits.push('↓ Falling');
                    if (cd.anomalies && cd.anomalies.length > 0) badgeBits.push('◎ Spikes');
                    if (cd.break && Math.abs(breakDelta) > breakThresh) badgeBits.push('⋮ Step change');
                    if (cd.volatility && volStd > volThresh) badgeBits.push('~ Volatile');
                    const trendLine = badgeBits.length ? `<br>${badgeBits.join(' · ')}` : '';
                    const watchSec = Math.round(wTotal);
                    const playCount = (catTotals[cat] || 0).toLocaleString();
                    const primaryLine = isMultiLabel
                        ? `${segPct.toFixed(1)}% of label mentions · ${watchPct.toFixed(1)}% of time spent`
                        : `${watchPct.toFixed(1)}% of time spent`;
                    return {
                        x: [segPct],
                        y: ['cats'],
                        name: cat,
                        type: 'bar',
                        orientation: 'h',
                        marker: {
                            color: catColorMap[cat],
                            opacity: isSelected ? 1.0 : 0.4,
                            line: { width: 0 }
                        },
                        text: [cat],
                        textposition: 'inside',
                        insidetextanchor: 'middle',
                        customdata: [[cat, primaryLine, watchSec.toLocaleString(), playCount, trendLine]],
                        hovertemplate: '<b>%{customdata[0]}</b><br>%{customdata[1]}%{customdata[4]}<extra></extra>'
                    };
                });

                const ribbonLayout = {
                    barmode: 'stack',
                    margin: { t: 2, b: 2, l: 2, r: 2 },
                    paper_bgcolor: 'rgba(0,0,0,0)',
                    plot_bgcolor: 'rgba(0,0,0,0)',
                    showlegend: false,
                    font: { family: getCSSVar('--font-sans'), color: getCSSVar('--color-text-primary') },
                    xaxis: { range: [0, 100], showgrid: false, showticklabels: false, fixedrange: true, zeroline: false },
                    yaxis: { showticklabels: false, fixedrange: true, zeroline: false },
                    height: 50
                };

                Plotly.newPlot(ribbonDiv, ribbonTraces, ribbonLayout, { displayModeBar: false, responsive: true });

                ribbonDiv.on('plotly_click', function (eventData) {
                    if (!eventData || !eventData.points || !eventData.points.length) return;
                    const cat = eventData.points[0].data.name;
                    window.timelines.toggleCategory(varName, cat);
                });

                // Unified residual message: combines backend-folded and
                // frontend-folded low-occurrence categories into a single
                // "Other" bucket description. Members list is the union of
                // both sources, shown as a tooltip for transparency.
                const backendOtherMembers = (varAnalysis && Array.isArray(varAnalysis.other_members))
                    ? varAnalysis.other_members : [];
                const combinedOtherMembers = Array.from(new Set([
                    ...backendOtherMembers,
                    ...frontendFolded
                ])).sort();
                // Window-level "untagged" share — share of attention paid
                // to plays where the variable was empty (no tag at all).
                // Computed directly from per-day denominators (NOT from
                // 100 - sum_of_shares, which is unreliable for multi-label
                // because per-cat shares can overlap).
                const totalUntaggedW = isMultiLabel
                    ? slicedWeightedVideoTotal.reduce((s, v, i) => s + Math.max(0, (v || 0) - (slicedWeightedValid[i] || 0)), 0)
                    : 0;
                const untaggedWindowPct = windowDenom > 0 ? (totalUntaggedW / windowDenom) * 100 : 0;

                // Toggle state defaults: both Other and Untagged off.  Users
                // opt in explicitly via the footer checkbox.  "No category"
                // is a useful residual but it can dominate the axis range
                // on sparse multi-label vars, so default-off keeps the
                // primary label traces visible.
                if (!this.timelineState.showOther) this.timelineState.showOther = {};
                if (!this.timelineState.showUntagged) this.timelineState.showUntagged = {};
                const showOther = !!this.timelineState.showOther[varName];
                const showUntagged = !!this.timelineState.showUntagged[varName];

                // Helper to build a footer row: `[☐] <text>`.  Clicking the
                // checkbox (or its wrapping label which includes the text)
                // toggles the trace.  Uses addEventListener so varName with
                // unusual chars is safe.
                const makeFooterRow = (textContent, tooltip, toggleFn, toggleState) => {
                    const label = document.createElement('label');
                    label.className = 'text-xs italic' + (tooltip ? ' meta-tooltip' : '');
                    label.style.cssText = 'display: flex; align-items: baseline; gap: 6px; margin-top: 4px; color: var(--color-text-muted); cursor: pointer; user-select: none;';
                    if (tooltip) label.setAttribute('data-tooltip', tooltip);

                    const cb = document.createElement('input');
                    cb.type = 'checkbox';
                    cb.checked = !!toggleState;
                    cb.style.cssText = 'margin: 0; flex-shrink: 0; cursor: pointer;';
                    cb.addEventListener('change', () => toggleFn(varName));
                    label.appendChild(cb);

                    const textEl = document.createElement('span');
                    textEl.textContent = textContent;
                    label.appendChild(textEl);
                    return label;
                };

                if (combinedOtherMembers.length > 0) {
                    const preview = combinedOtherMembers.slice(0, 8).join(', ');
                    const suffix = combinedOtherMembers.length > 8 ? `, +${combinedOtherMembers.length - 8} more` : '';
                    const tooltip = `${preview}${suffix}`;
                    const noun = combinedOtherMembers.length === 1 ? 'label' : 'labels';
                    const otherWeighted = catWeightedTotals[OTHER_BUCKET] || 0;
                    let otherPct;
                    if (isMultiLabel) {
                        // For multi-label, summing per-cat weighted totals
                        // overcounts when plays carry multiple folded tags.
                        // Cap at the tagged-share ceiling so the stated %
                        // stays in [0, tagged%] — an honest upper bound.
                        const taggedCeiling = slicedWeightedValid.reduce((s, v) => s + (v || 0), 0);
                        const capped = Math.min(otherWeighted, taggedCeiling);
                        otherPct = windowDenom > 0 ? ((capped / windowDenom) * 100).toFixed(1) : '0.0';
                    } else {
                        otherPct = windowDenom > 0 ? ((otherWeighted / windowDenom) * 100).toFixed(1) : '0.0';
                    }
                    const text = `"Other" bundles ${combinedOtherMembers.length} low-occurrence ${noun} (${otherPct}% of time spent).`;
                    chartWrapper.appendChild(makeFooterRow(
                        text, tooltip, this.toggleOther.bind(this), showOther
                    ));
                }

                if (isMultiLabel) {
                    const text = `"No label" — ${untaggedWindowPct.toFixed(1)}% of time spent on items with no label for this variable.`;
                    chartWrapper.appendChild(makeFooterRow(
                        text, null, this.toggleUntagged.bind(this), showUntagged
                    ));
                }

                // Per-category traces — read pre-computed share values directly
                // from the backend (one source of truth across chart, ribbon,
                // and analysis overlays).  The `val` (raw plays) and `wval`
                // (watch-seconds) feed the hover only.
                //
                // OTHER_BUCKET is filtered out here — its visibility is
                // controlled exclusively by the "Show in chart" toggle next
                // to the "Other" footer below the ribbon.
                let allYVals = [];
                const slicedDateLabels = startIdx > 0 ? (data.date_labels || dates).slice(startIdx) : (data.date_labels || dates);
                const hexToRgba = (hex, alpha) => {
                    const r = parseInt(hex.slice(1, 3), 16);
                    const g = parseInt(hex.slice(3, 5), 16);
                    const b = parseInt(hex.slice(5, 7), 16);
                    return `rgba(${r},${g},${b},${alpha})`;
                };
                const overlaysActive = this.timelineState.findingsPanelOpen && this.timelineState.findingsPanelOpen[varName];
                const lineAlpha = overlaysActive ? 0.25 : 1.0;
                const lineWidth = overlaysActive ? 1.5 : 2;
                const fillAlpha = overlaysActive ? '06' : '12';
                const smoothW = this.timelineState.smoothing || 1;
                const showRaw = !!this.timelineState.showRaw && smoothW > 1;
                const halfW = Math.floor(smoothW / 2);
                const denomSeries = isMultiLabel ? slicedWeightedVideoTotal : slicedWeightedValid;

                // Hover on the smoothed line should describe the window it
                // represents, not the single day under the cursor.  Pre-compute
                // per-index window sums (O(n·W) but n is small).
                const windowLabel = (i) => {
                    const wstart = Math.max(0, i - halfW);
                    const wend = Math.min(dates.length - 1, i + halfW);
                    if (wstart === wend) return slicedDateLabels[i] || dates[i];
                    const startLbl = slicedDateLabels[wstart] || String(dates[wstart]).slice(0, 10);
                    const endLbl = slicedDateLabels[wend] || String(dates[wend]).slice(0, 10);
                    return `${startLbl} – ${endLbl}`;
                };
                const windowSums = dates.map((d, i) => {
                    const wstart = Math.max(0, i - halfW);
                    const wend = Math.min(dates.length - 1, i + halfW);
                    let plays = 0, time = 0, denom = 0, validSum = 0;
                    for (let j = wstart; j <= wend; j++) {
                        plays += slicedVideoCounts[j] || 0;
                        time += slicedWeightedVideoTotal[j] || 0;
                        denom += denomSeries[j] || 0;
                        validSum += slicedWeightedValid[j] || 0;
                    }
                    return { plays, time, denom, validSum };
                });

                selectedCats.forEach((cat, idx) => {
                    if (cat === OTHER_BUCKET) return;
                    const yVals = [];
                    const rawHoverTexts = [];
                    const windowHoverTexts = [];

                    dates.forEach((d, i) => {
                        const shareDay = slicedShareSeries[i] || {};
                        const share = shareDay[cat] || 0;
                        yVals.push(share);

                        // Per-day hover (used by the raw-values bar trace)
                        const dayPlays = slicedVideoCounts[i] || 0;
                        const dayTime = slicedWeightedVideoTotal[i] || 0;
                        rawHoverTexts.push(
                            `<b>${cat}</b><br>` +
                            `Day: ${slicedDateLabels[i] || d}<br>` +
                            `Share: ${share.toFixed(1)}%<br>` +
                            `Plays: ${dayPlays}<br>` +
                            `Time on platform: ${this._formatDuration(dayTime)}`
                        );

                        // Window-aggregated hover (used by the smoothed line)
                        const wstart = Math.max(0, i - halfW);
                        const wend = Math.min(dates.length - 1, i + halfW);
                        let winWCat = 0;
                        for (let j = wstart; j <= wend; j++) {
                            winWCat += (slicedWeightedCounts[j] || {})[cat] || 0;
                        }
                        const winDenom = windowSums[i].denom;
                        const winShare = winDenom > 0 ? (winWCat / winDenom) * 100 : 0;
                        const periodLbl = smoothW > 1 ? 'Window' : 'Day';
                        windowHoverTexts.push(
                            `<b>${cat}</b><br>` +
                            `${periodLbl}: ${windowLabel(i)}<br>` +
                            `Share: ${winShare.toFixed(1)}%<br>` +
                            `Plays: ${windowSums[i].plays}<br>` +
                            `Time on platform: ${this._formatDuration(windowSums[i].time)}`
                        );
                    });

                    const displayY = smoothW > 1 ? this._movingAvg(yVals, smoothW) : yVals;
                    allYVals = allYVals.concat(displayY);
                    const catColor = catColorMap[cat] || colors[idx % colors.length];
                    const lineColor = hexToRgba(catColor, lineAlpha);

                    // Raw daily bars behind the smoothed line: push first so
                    // Plotly renders them below.  Per-day hover here so the
                    // user can inspect individual days when bars are visible.
                    if (showRaw) {
                        traces.push({
                            x: xVals,
                            y: yVals,
                            type: 'bar',
                            marker: { color: hexToRgba(catColor, 0.18) },
                            name: cat,
                            showlegend: false,
                            text: rawHoverTexts,
                            textposition: 'none',
                            hoverinfo: 'text',
                            hovertemplate: '%{text}<extra></extra>'
                        });
                    }

                    traces.push({
                        x: xVals,
                        y: displayY,
                        type: 'scatter',
                        mode: 'lines',
                        line: { width: lineWidth, shape: 'spline', color: lineColor },
                        fill: showRaw ? 'none' : 'tozeroy',
                        fillcolor: showRaw ? undefined : catColor + fillAlpha,
                        name: cat,
                        text: windowHoverTexts,
                        hoverinfo: 'text',
                        hovertemplate: '%{text}<extra></extra>'
                    });
                });

                // "Other" residual trace — toggled on via the footer checkbox.
                // For multi-label, cap at each day's tagged-share ceiling
                // (weighted_valid / weighted_video_total) because summing
                // per-cat shares can overcount when plays carry multiple
                // folded tags.
                if (showOther && otherInData) {
                    const yVals = [];
                    const hoverTexts = [];
                    dates.forEach((d, i) => {
                        const shareDay = slicedShareSeries[i] || {};
                        let share = shareDay[OTHER_BUCKET] || 0;
                        if (isMultiLabel) {
                            const denom = slicedWeightedVideoTotal[i] || 0;
                            const valid = slicedWeightedValid[i] || 0;
                            const ceiling = denom > 0 ? (valid / denom) * 100 : 100;
                            if (share > ceiling) share = ceiling;
                        }
                        yVals.push(share);

                        // Window-aggregated hover: mirror the smoothed line.
                        const wstart = Math.max(0, i - halfW);
                        const wend = Math.min(dates.length - 1, i + halfW);
                        let winWOther = 0;
                        for (let j = wstart; j <= wend; j++) {
                            winWOther += (slicedWeightedCounts[j] || {})[OTHER_BUCKET] || 0;
                        }
                        const winDenom = windowSums[i].denom;
                        let winShare = winDenom > 0 ? (winWOther / winDenom) * 100 : 0;
                        if (isMultiLabel) {
                            const winCeiling = winDenom > 0 ? (windowSums[i].validSum / winDenom) * 100 : 100;
                            if (winShare > winCeiling) winShare = winCeiling;
                        }
                        const extraNote = isMultiLabel
                            ? '<br><i>(aggregate of rare labels; may under-report days with many overlapping labels)</i>'
                            : '';
                        const periodLbl = smoothW > 1 ? 'Window' : 'Day';
                        hoverTexts.push(
                            `<b>Other</b><br>` +
                            `${periodLbl}: ${windowLabel(i)}<br>` +
                            `Share: ${winShare.toFixed(1)}%<br>` +
                            `Plays: ${windowSums[i].plays}<br>` +
                            `Time on platform: ${this._formatDuration(windowSums[i].time)}${extraNote}`
                        );
                    });
                    const displayY = smoothW > 1 ? this._movingAvg(yVals, smoothW) : yVals;
                    allYVals = allYVals.concat(displayY);
                    const lineColor = hexToRgba(otherColor, lineAlpha);
                    traces.push({
                        x: xVals,
                        y: displayY,
                        type: 'scatter',
                        mode: 'lines',
                        line: { width: lineWidth, shape: 'spline', color: lineColor },
                        fill: showRaw ? 'none' : 'tozeroy',
                        fillcolor: showRaw ? undefined : otherColor + fillAlpha,
                        name: OTHER_BUCKET,
                        text: hoverTexts,
                        hoverinfo: 'text',
                        hovertemplate: '%{text}<extra></extra>'
                    });
                }

                // Untagged residual trace (multi-label only) — toggled on via
                // the footer checkbox.  Per day, untagged share is the
                // proportion of attention paid to plays with NO tag for this
                // variable: (video_total − valid) / video_total.  That's
                // non-additive and robust to overlapping tags.
                if (isMultiLabel && showUntagged) {
                    const untaggedY = [];
                    const untaggedHover = [];
                    dates.forEach((d, i) => {
                        const dayDenom = slicedWeightedVideoTotal[i] || 0;
                        const dayValid = slicedWeightedValid[i] || 0;
                        const untaggedW = Math.max(0, dayDenom - dayValid);
                        const untagged = dayDenom > 0 ? (untaggedW / dayDenom) * 100 : 0;
                        untaggedY.push(untagged);

                        const winDenom = windowSums[i].time;
                        const winValid = windowSums[i].validSum;
                        const winUntaggedW = Math.max(0, winDenom - winValid);
                        const winUntagged = winDenom > 0 ? (winUntaggedW / winDenom) * 100 : 0;
                        const periodLbl = smoothW > 1 ? 'Window' : 'Day';
                        untaggedHover.push(
                            `<b>${UNTAGGED_BUCKET}</b><br>` +
                            `${periodLbl}: ${windowLabel(i)}<br>` +
                            `Share: ${winUntagged.toFixed(1)}%<br>` +
                            `Plays: ${windowSums[i].plays}<br>` +
                            `Time on platform: ${this._formatDuration(windowSums[i].time)}`
                        );
                    });
                    const displayUntagged = smoothW > 1 ? this._movingAvg(untaggedY, smoothW) : untaggedY;
                    allYVals = allYVals.concat(displayUntagged);
                    const untaggedColor = getCSSVar('--color-text-faint') || '#bbbbbb';
                    traces.push({
                        x: xVals,
                        y: displayUntagged,
                        type: 'scatter',
                        mode: 'lines',
                        line: { width: 1.5, shape: 'spline', color: untaggedColor, dash: 'dot' },
                        fill: 'none',
                        name: UNTAGGED_BUCKET,
                        text: untaggedHover,
                        hoverinfo: 'text',
                        hovertemplate: '%{text}<extra></extra>'
                    });
                }

                if (allYVals.length > 0) {
                    const yMax = Math.max(...allYVals);
                    const yMin = Math.min(...allYVals);
                    const padding = Math.max((yMax - yMin) * 0.1, 2);
                    chartWrapper._catYRange = [Math.max(0, yMin - padding), Math.min(100, yMax + padding)];
                }

                // Y-axis label: single consistent phrasing for all categorical
                // charts.  Multi-label shares are naturally smaller (items
                // often carry more than one label) but the meaning is the
                // same in both cases — share of time spent on items carrying
                // the specific label.
                yAxisTitle = '% of time spent on items with label';

            } else {
                // Numeric — values are watch-time-weighted means
                // (Σ(value · w) / Σ(w)).  Each play's contribution is
                // proportional to attention paid, so a 5-second swipe-past
                // sways the mean a fifth as much as a 25-second dwell.
                if (varData.log) {
                    yAxisTitle = 'Play-time-weighted mean (log)';
                } else {
                    yAxisTitle = 'Play-time-weighted mean';
                }

                const slicedValues = startIdx > 0 ? varData.values.slice(startIdx) : varData.values;
                const slicedDateLabelsN = startIdx > 0 ? (data.date_labels || dates).slice(startIdx) : (data.date_labels || dates);
                const slicedVideoCountsN = startIdx > 0 ? (varData.daily_video_counts || []).slice(startIdx) : (varData.daily_video_counts || []);
                const slicedWeightedTotalN = startIdx > 0 ? (varData.daily_weighted_video_total || []).slice(startIdx) : (varData.daily_weighted_video_total || []);
                const slicedWeightedValidN = startIdx > 0 ? (varData.daily_weighted_valid || []).slice(startIdx) : (varData.daily_weighted_valid || []);

                // Apply smoothing if active
                const smoothW = this.timelineState.smoothing || 1;
                const displayValues = smoothW > 1 ? this._movingAvg(slicedValues, smoothW) : slicedValues;
                const showRawNumeric = !!this.timelineState.showRaw && smoothW > 1;
                const infoColor = getCSSVar('--color-info');
                const halfWN = Math.floor(smoothW / 2);

                // Window-aggregated hover for the smoothed line: weighted mean
                // (Σ v_i · w_i / Σ w_i) mirrors the modal.  Plays and time are
                // summed over the window.
                const fmtNum = (v) => (v === null || v === undefined)
                    ? '—'
                    : (Number.isInteger(v) ? `${v}` : v.toFixed(3));
                const windowLabelN = (i) => {
                    const wstart = Math.max(0, i - halfWN);
                    const wend = Math.min(dates.length - 1, i + halfWN);
                    if (wstart === wend) return slicedDateLabelsN[i] || dates[i];
                    const startLbl = slicedDateLabelsN[wstart] || String(dates[wstart]).slice(0, 10);
                    const endLbl = slicedDateLabelsN[wend] || String(dates[wend]).slice(0, 10);
                    return `${startLbl} – ${endLbl}`;
                };
                const numericWindowHover = dates.map((d, i) => {
                    const wstart = Math.max(0, i - halfWN);
                    const wend = Math.min(dates.length - 1, i + halfWN);
                    let num = 0, den = 0, plays = 0, time = 0;
                    for (let j = wstart; j <= wend; j++) {
                        const v = slicedValues[j];
                        if (v !== null && v !== undefined) {
                            const w = slicedWeightedValidN[j] || 0;
                            if (w > 0) { num += v * w; den += w; }
                        }
                        plays += slicedVideoCountsN[j] || 0;
                        time += slicedWeightedTotalN[j] || 0;
                    }
                    const winMean = den > 0 ? num / den : null;
                    const periodLbl = smoothW > 1 ? 'Window' : 'Day';
                    return `<b>${varName}</b><br>` +
                        `${periodLbl}: ${windowLabelN(i)}<br>` +
                        `Mean: ${fmtNum(winMean)}<br>` +
                        `Plays: ${plays}<br>` +
                        `Time on platform: ${this._formatDuration(time)}`;
                });

                const numericRawHover = dates.map((d, i) => {
                    const raw = slicedValues[i];
                    const dayPlays = slicedVideoCountsN[i] || 0;
                    const dayTime = slicedWeightedTotalN[i] || 0;
                    return `<b>${varName}</b><br>` +
                        `Day: ${slicedDateLabelsN[i] || d}<br>` +
                        `Mean: ${fmtNum(raw)}<br>` +
                        `Plays: ${dayPlays}<br>` +
                        `Time on platform: ${this._formatDuration(dayTime)}`;
                });

                const hexToRgbaNum = (hex, alpha) => {
                    const h = hex.trim().replace('#', '');
                    if (h.length !== 6) return hex;
                    const r = parseInt(h.slice(0, 2), 16);
                    const g = parseInt(h.slice(2, 4), 16);
                    const b = parseInt(h.slice(4, 6), 16);
                    return `rgba(${r},${g},${b},${alpha})`;
                };

                if (showRawNumeric) {
                    // Raw daily bars behind the smoothed line, with per-day
                    // hover so the user can inspect individual days.
                    traces.push({
                        x: xVals,
                        y: slicedValues,
                        type: 'bar',
                        marker: { color: hexToRgbaNum(infoColor, 0.25) },
                        name: varName + ' (daily)',
                        showlegend: false,
                        text: numericRawHover,
                        textposition: 'none',
                        hoverinfo: 'text',
                        hovertemplate: '%{text}<extra></extra>'
                    });
                }

                traces.push({
                    x: xVals,
                    y: displayValues,
                    type: 'scatter',
                    mode: 'lines',
                    line: { width: 2, shape: 'spline', color: infoColor },
                    fill: showRawNumeric ? 'none' : 'tozeroy',
                    fillcolor: showRawNumeric ? undefined : hexToRgbaNum(infoColor, 0.08),
                    name: varName,
                    text: numericWindowHover,
                    hoverinfo: 'text',
                    hovertemplate: '%{text}<extra></extra>'
                });

                // Close Numeric Logic
            }

            // Create Plot
            // Formatted Axis Labels Logic
            // We use 'date_labels' from backend.
            const allLabels = data.date_labels || dates; // fallback
            const slicedLabels = startIdx > 0 ? allLabels.slice(startIdx) : allLabels;

            // Limit number of labels to avoid overcrowding
            const maxLabels = 15;
            const skip = Math.ceil(dates.length / maxLabels);

            const tickVals = [];
            const tickText = [];

            dates.forEach((d, i) => {
                if (i % skip === 0) {
                    tickVals.push(d);
                    tickText.push(slicedLabels[i]);
                }
            });

            const isCategorical = (varData.type === 'categorical');

            const layout = {
                margin: { t: 20, r: 20, b: (isCategorical ? 45 : 30), l: 40 },
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0)',
                font: { family: getCSSVar('--font-sans'), color: getCSSVar('--color-text-muted'), size: 11 },
                xaxis: {
                    type: excludeNoData ? 'category' : 'date',
                    tickmode: 'array',
                    tickvals: tickVals,
                    ticktext: tickText,
                    tickangle: 0,
                    gridcolor: getCSSVar('--chart-grid-line'),
                    gridwidth: 1,
                    zeroline: false,
                    tickfont: { family: getCSSVar('--font-sans'), color: getCSSVar('--color-text-faint') }
                },
                yaxis: {
                    title: { text: yAxisTitle, font: { family: getCSSVar('--font-sans'), color: getCSSVar('--color-text-muted'), size: 11 } },
                    range: isCategorical && chartWrapper._catYRange ? chartWrapper._catYRange : undefined,
                    gridcolor: getCSSVar('--chart-grid-line'),
                    gridwidth: 1,
                    zeroline: false,
                    tickfont: { family: getCSSVar('--font-sans'), color: getCSSVar('--color-text-faint') }
                },
                barmode: 'overlay',
                showlegend: isCategorical
            };

            if (varData.type === 'categorical') {
                layout.legend = {
                    orientation: 'h',
                    y: -0.08,
                    x: 0.5,
                    xanchor: 'center',
                    yanchor: 'top'
                };
            }

            // Log Axis specific
            if (varData.type !== 'categorical' && varData.log) {
                layout.yaxis.type = 'log';
            }

            // Extra-data engagement spikes (vertical lines from baseline, height = count)
            if (data.extra_data_counts) {
                const SPIKE_MAX_FRAC = 0.15;
                const edXVals = [];
                const edCounts = [];
                const edTexts = [];
                const slicedEdCounts = startIdx > 0 ? data.extra_data_counts.slice(startIdx) : data.extra_data_counts;
                slicedEdCounts.forEach((cnt, i) => {
                    if (cnt > 0) {
                        edXVals.push(xVals[i]);
                        edCounts.push(cnt);
                        edTexts.push(`${cnt} engagement activit${cnt === 1 ? 'y' : 'ies'}`);
                    }
                });
                if (edXVals.length > 0) {
                    const maxCount = Math.max(...edCounts);

                    layout.yaxis2 = {
                        overlaying: 'y',
                        side: 'right',
                        range: [0, maxCount / SPIKE_MAX_FRAC],
                        showticklabels: false,
                        showgrid: false,
                        zeroline: false,
                        fixedrange: true,
                        showline: false
                    };

                    const spikeX = [];
                    const spikeY = [];
                    edXVals.forEach((x, i) => {
                        spikeX.push(x, x, null);
                        spikeY.push(0, edCounts[i], null);
                    });
                    traces.push({
                        x: spikeX,
                        y: spikeY,
                        type: 'scatter',
                        mode: 'lines',
                        line: { width: 2, color: getCSSVar('--color-warning') },
                        opacity: 0.7,
                        showlegend: false,
                        hoverinfo: 'skip',
                        yaxis: 'y2'
                    });

                    traces.push({
                        x: edXVals,
                        y: edCounts,
                        type: 'scatter',
                        mode: 'markers',
                        marker: { symbol: 'diamond', size: 5, color: getCSSVar('--color-warning'), opacity: 0.9 },
                        name: 'Engagement',
                        text: edTexts,
                        hoverinfo: 'text',
                        hovertemplate: '%{text}<extra></extra>',
                        showlegend: false,
                        yaxis: 'y2'
                    });
                }
            }

            Plotly.newPlot(plotId, traces, layout, {
                displayModeBar: true,
                responsive: true,
                modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d', 'toggleSpikelines'],
                displaylogo: false
            });
            allPlotIds.push(plotId);

            // Render analysis overlays when findings panel is open
            if (isCategorical && data.analysis && data.analysis[varName]
                && this.timelineState.findingsPanelOpen && this.timelineState.findingsPanelOpen[varName]) {

                const analysisData = data.analysis[varName];
                const overlayTraces = [];
                const shapes = [];

                // Hidden: Evenness curve — users find it confusing; restore when needed
                // // --- Entropy / Evenness band ---
                // // Compute Shannon evenness at each time point using a fixed N
                // // (total distinct categories across the whole series) so the
                // // denominator is stable and days with fewer categories present
                // // correctly score lower.
                // const entropyVals = [];
                // const slicedCountsForEntropy = startIdx > 0 ? varData.counts.slice(startIdx) : varData.counts;
                //
                // // Fixed N: count all distinct categories across the sliced series
                // const allCatsInSeries = new Set();
                // for (const dayBucket of slicedCountsForEntropy) {
                //     for (const k of Object.keys(dayBucket || {})) {
                //         if ((dayBucket[k] || 0) > 0) allCatsInSeries.add(k);
                //     }
                // }
                // const fixedN = allCatsInSeries.size;
                // const fixedMaxH = fixedN > 1 ? Math.log(fixedN) : 1;
                //
                // for (let ti = 0; ti < slicedCountsForEntropy.length; ti++) {
                //     const dayBucket = slicedCountsForEntropy[ti] || {};
                //     const total = Object.values(dayBucket).reduce((s, v) => s + (v || 0), 0);
                //     if (total === 0) {
                //         entropyVals.push(null);
                //         continue;
                //     }
                //     let H = 0;
                //     for (const v of Object.values(dayBucket)) {
                //         const p = (v || 0) / total;
                //         if (p > 0) H -= p * Math.log(p);
                //     }
                //     entropyVals.push(H / fixedMaxH);
                // }
                //
                // // Heavy independent centred smoothing (14-period) — entropy is
                // // about the structural diversity trend, not daily fluctuations.
                // // Null-aware: skip no-data days in the averaging window.
                // const entropySmoothing = Math.max(14, this.timelineState.smoothing || 1);
                // const entropyHalf = Math.floor(entropySmoothing / 2);
                // const smoothedEntropy = entropyVals.map((_, i) => {
                //     const start = Math.max(0, i - entropyHalf);
                //     const end = Math.min(entropyVals.length, i + entropyHalf + 1);
                //     let sum = 0, count = 0;
                //     for (let j = start; j < end; j++) {
                //         if (entropyVals[j] !== null) { sum += entropyVals[j]; count++; }
                //     }
                //     return count > 0 ? sum / count : null;
                // });
                //
                // // Min-max normalised mapping so the evenness band uses the
                // // full chart height, making variation clearly visible.
                // const validEntropy = smoothedEntropy.filter(e => e !== null);
                // const eMin = validEntropy.length > 0 ? Math.min(...validEntropy) : 0;
                // const eMax = validEntropy.length > 0 ? Math.max(...validEntropy) : 1;
                // const eSpan = eMax - eMin || 1;
                //
                // const yRange = chartWrapper._catYRange || [0, 100];
                // const scaledEntropy = smoothedEntropy.map(e => {
                //     if (e === null) return null;
                //     return yRange[0] + ((e - eMin) / eSpan) * (yRange[1] - yRange[0]);
                // });
                //
                // // Build hover text with actual evenness values
                // const entropyHoverText = smoothedEntropy.map((e, i) => {
                //     const dateLabel = (data.date_labels || dates)[startIdx + i] || xVals[i];
                //     return `<b>Distribution Evenness</b><br>` +
                //            `Period: ${dateLabel}<br>` +
                //            `Evenness: ${(e * 100).toFixed(1)}%<br>` +
                //            `<span class="text-sm" style="color:var(--color-text-tertiary);">Range: ${(eMin * 100).toFixed(1)}% – ${(eMax * 100).toFixed(1)}%</span>`;
                // });
                //
                // overlayTraces.push({
                //     x: xVals,
                //     y: scaledEntropy,
                //     type: 'scatter',
                //     mode: 'lines',
                //     line: { width: 12, shape: 'spline', color: getCSSVar('--chart-overlay-line') },
                //     name: 'Evenness',
                //     showlegend: false,
                //     text: entropyHoverText,
                //     hoverinfo: 'text',
                //     hovertemplate: '%{text}<extra></extra>'
                // });
                const cats = analysisData.categories || [];
                const selectedSet = new Set(this.timelineState.categoricalSelections[varName] || []);

                // Only overlay for selected categories
                cats.forEach((catData, cIdx) => {
                    if (!selectedSet.has(catData.id)) return;
                    const catColor = catColorMap[catData.id] || colors[Array.from(selectedSet).indexOf(catData.id) % colors.length];

                    // Trend line (dashed line from regression) — threshold mirrors
                    // the Rising/Falling badge at line 1491 so every flagged
                    // category actually gets a trend line on the chart.
                    const meanShareForTrend = (catData.volatility && catData.volatility.mean) || (catData.trend && catData.trend.mean) || 0;
                    const trendThreshForOverlay = 0.5 * Math.max(meanShareForTrend, 1.0);
                    if (catData.trend && Math.abs(catData.trend.total_change) > trendThreshForOverlay) {
                        const n = xVals.length;
                        const adjIntercept = catData.trend.intercept + catData.trend.slope * startIdx;
                        const trendY0 = adjIntercept;
                        const trendY1 = adjIntercept + catData.trend.slope * (n - 1);
                        overlayTraces.push({
                            x: [xVals[0], xVals[n - 1]],
                            y: [trendY0, trendY1],
                            type: 'scatter',
                            mode: 'lines',
                            line: { color: catColor, width: 2, dash: 'dash' },
                            name: `${catData.label} trend`,
                            showlegend: false,
                            hoverinfo: 'text',
                            text: [`Trend: ${catData.trend.total_change > 0 ? '+' : ''}${catData.trend.total_change}pp over entire series`],
                            hovertemplate: '%{text}<extra></extra>'
                        });
                    }

                    // Anomaly markers (circles)
                    // Adjust indices for sliced view
                    if (catData.anomalies && catData.anomalies.length > 0) {
                        const anomX = [];
                        const anomY = [];
                        const anomText = [];
                        catData.anomalies.forEach(a => {
                            const adjIdx = a.index - startIdx;
                            if (adjIdx >= 0 && adjIdx < xVals.length) {
                                anomX.push(xVals[adjIdx]);
                                anomY.push(a.value);
                                anomText.push(
                                    `<b>Anomaly: ${catData.label}</b><br>` +
                                    `Value: ${a.value}%<br>` +
                                    `Z-score: ${a.z}<br>` +
                                    `Mean: ${a.mean}%`
                                );
                            }
                        });
                        if (anomX.length > 0) {
                            overlayTraces.push({
                                x: anomX,
                                y: anomY,
                                type: 'scatter',
                                mode: 'markers',
                                marker: { color: 'rgba(0,0,0,0)', size: 14, line: { color: catColor, width: 2.5 } },
                                name: `${catData.label} anomalies`,
                                showlegend: false,
                                text: anomText,
                                hoverinfo: 'text',
                                hovertemplate: '%{text}<extra></extra>'
                            });
                        }
                    }

                    // Hidden: structural break vertical line — restore with "breaks" chip when needed
                    // const adjBreakIdx = catData.break ? catData.break.index - startIdx : -1;
                    // if (catData.break && Math.abs(catData.break.delta) > 4 && adjBreakIdx > 0 && adjBreakIdx < xVals.length) {
                    //     shapes.push({
                    //         type: 'line',
                    //         x0: xVals[adjBreakIdx],
                    //         x1: xVals[adjBreakIdx],
                    //         y0: 0,
                    //         y1: 1,
                    //         yref: 'paper',
                    //         line: { color: catColor, width: 1.5, dash: 'dot' },
                    //     });
                    // }
                });

                // Add overlay traces, shapes, and evenness annotation
                if (overlayTraces.length > 0) {
                    Plotly.addTraces(plotId, overlayTraces);
                }
                const relayoutUpdates = {};
                if (shapes.length > 0) {
                    relayoutUpdates.shapes = shapes;
                }
                // Hidden: evenness annotation — restore with evenness curve when needed
                // if (validEntropy.length > 0) {
                //     relayoutUpdates.annotations = [{
                //         text: `Evenness: ${(eMin * 100).toFixed(1)}% – ${(eMax * 100).toFixed(1)}%`,
                //         xref: 'paper', yref: 'paper',
                //         x: 0, y: 1.02,
                //         xanchor: 'left', yanchor: 'bottom',
                //         showarrow: false,
                //         font: { family: getCSSVar('--font-sans'), size: 10, color: getCSSVar('--chart-annotation-text') }
                //     }];
                // }
                if (Object.keys(relayoutUpdates).length > 0) {
                    Plotly.relayout(plotId, relayoutUpdates);
                }

            }

            // --- FINDINGS PANEL (outside analysis toggle, always available for categorical) ---
            if (isCategorical) {
                const selectedCatsForPanel = this.timelineState.categoricalSelections[varName] || [];
                if (data.analysis && data.analysis[varName] && selectedCatsForPanel.length > 0) {
                    const analysisData = data.analysis[varName];
                    const catsList = analysisData.categories || [];
                    const selectedSet = new Set(selectedCatsForPanel);

                    // Toggle button
                    const findingsToggleId = `findings-toggle-${varName}`;
                    const toggleBtn = document.createElement('button');
                    toggleBtn.id = findingsToggleId;
                    toggleBtn.classList.add('text-sm');
                    toggleBtn.style.cssText = `margin-top: 10px; padding: 6px 14px; background: var(--color-bg-elevated); color: var(--chart-text); border: 1px solid var(--chart-grid); border-radius: 6px; cursor: pointer;`;
                    const isOpen = this.timelineState.findingsPanelOpen && this.timelineState.findingsPanelOpen[varName];
                    toggleBtn.textContent = isOpen ? 'Hide Findings' : 'Show Findings';

                    // Centre the button below the ribbon.
                    const btnWrapper = document.createElement('div');
                    btnWrapper.style.cssText = 'display:flex; justify-content:center; margin-top:4px;';
                    btnWrapper.appendChild(toggleBtn);
                    chartWrapper.appendChild(btnWrapper);

                    const findingsContainer = document.createElement('div');
                    findingsContainer.id = `findings-panel-${varName}`;
                    findingsContainer.style.cssText = `margin-top: 10px; border-top: 1px solid var(--color-border-subtle); padding-top: 15px; display: ${isOpen ? 'flex' : 'none'}; flex-direction: column; gap: 10px;`;

                    toggleBtn.addEventListener('click', () => {
                        if (!this.timelineState.findingsPanelOpen) this.timelineState.findingsPanelOpen = {};
                        const visible = findingsContainer.style.display !== 'none';
                        this.timelineState.findingsPanelOpen[varName] = !visible;
                        this.renderTimelineCharts();
                    });

                    catsList.forEach((catData, index) => {
                        if (!selectedSet.has(catData.id)) return;

                        const globalRank = index + 1;
                        const catColor = catColorMap[catData.id] || colors[Array.from(selectedSet).indexOf(catData.id) % colors.length];

                        const card = document.createElement('div');
                        card.style.cssText = `background: var(--color-bg-elevated); padding: 15px; border-radius: 8px; border-left: 4px solid ${catColor};`;

                        // Card Header: Rank + Dot + Label + Badges
                        const headerRow = document.createElement('div');
                        headerRow.style.cssText = 'display: flex; align-items: center; margin-bottom: 10px; flex-wrap: wrap; gap: 10px;';

                        const titleWrap = document.createElement('div');
                        titleWrap.classList.add('font-semibold', 'text-body');
                        titleWrap.style.cssText = `color: var(--color-text-primary); margin-right: 15px;`;
                        titleWrap.innerHTML = `<span class="text-sm" style="color:var(--color-text-muted); margin-right:5px;">#${globalRank}</span> <span style="display:inline-block; width:10px; height:10px; border-radius:50%; background:${catColor}; margin-right:6px;"></span> ${catData.label}`;
                        headerRow.appendChild(titleWrap);

                        const bullets = [];

                        // Helper to make badge
                        const makeBadge = (text, bg, fg, border) => {
                            return `<span class="text-xs font-medium" style="background:${bg}; color:${fg}; padding:3px 8px; border-radius:12px; border:1px solid ${border}; white-space:nowrap;">${text}</span>`;
                        };

                        let isStable = true;

                        // Findings thresholds normalised by category mean
                        // share — keeps badges meaningful for multi-label
                        // vars where absolute shares are naturally smaller
                        // (e.g. 0.5%-2% per category).  1 pp floor avoids
                        // over-firing on near-zero categories.
                        const meanShareCat = (catData.volatility && catData.volatility.mean) || (catData.trend && catData.trend.mean) || 0;
                        const trendThreshCat = 0.5 * Math.max(meanShareCat, 1.0);
                        const breakThreshCat = 0.5 * Math.max(meanShareCat, 1.0);
                        const volThreshCat = 0.3 * Math.max(meanShareCat, 1.0);

                        // 1. Trend
                        if (catData.trend && Math.abs(catData.trend.total_change) > trendThreshCat) {
                            isStable = false;
                            const isRising = catData.trend.total_change > 0;
                            if (isRising) {
                                headerRow.innerHTML += makeBadge('↑ Rising', 'var(--trend-rising-bg)', 'var(--color-accent)', 'var(--trend-rising-border)');
                                bullets.push(`Overall upward trend — total shift of +${catData.trend.total_change} pp over the period.`);
                            } else {
                                headerRow.innerHTML += makeBadge('↓ Falling', 'var(--trend-falling-bg)', 'var(--color-danger-soft)', 'var(--trend-falling-border)');
                                bullets.push(`Overall downward trend — total shift of ${catData.trend.total_change} pp over the period.`);
                            }
                        }

                        // Hidden: "Step change" badge — restore with "breaks" chip when needed
                        // if (catData.break && Math.abs(catData.break.delta) > breakThreshCat) {
                        //     isStable = false;
                        //     headerRow.innerHTML += makeBadge('⋮ Step change', 'var(--trend-breaks-bg)', 'var(--color-info)', 'var(--trend-breaks-border)');
                        //     let bDate = "the period";
                        //     const bIdx = catData.break.index;
                        //     if (data.date_labels && bIdx >= 0 && bIdx < data.date_labels.length) {
                        //          bDate = data.date_labels[bIdx];
                        //     } else if (data.dates && bIdx >= 0 && bIdx < data.dates.length) {
                        //          bDate = data.dates[bIdx];
                        //     }
                        //     const dir = catData.break.delta > 0 ? "jumped" : "dropped";
                        //     const sign = catData.break.delta > 0 ? "+" : "";
                        //     bullets.push(`Step change around ${bDate}: share ${dir} from ~${catData.break.mean_before}% to ~${catData.break.mean_after}% (${sign}${catData.break.delta} pp).`);
                        // }

                        // 3. Anomalies
                        if (catData.anomalies && catData.anomalies.length > 0) {
                            isStable = false;
                            headerRow.innerHTML += makeBadge(`◎ ${catData.anomalies.length} spike(s)`, 'var(--trend-spikes-bg)', 'var(--color-save)', 'var(--trend-spikes-border)');

                            catData.anomalies.slice(0, 2).forEach(a => {
                                let aDate = "Unknown Date";
                                if (data.date_labels && a.index >= 0 && a.index < data.date_labels.length) {
                                    aDate = data.date_labels[a.index];
                                } else if (data.dates && a.index >= 0 && a.index < data.dates.length) {
                                    aDate = data.dates[a.index];
                                }
                                const dir = a.value > a.mean ? "Peak" : "Trough";
                                const sign = a.z > 0 ? "+" : "";
                                bullets.push(`${dir} at ${aDate}: ${a.value}% vs. mean ${a.mean}% (${sign}${a.z} σ).`);
                            });
                        }

                        // Hidden: "Volatile" badge — restore with "volatile" chip when needed
                        // if (catData.volatility && catData.volatility.std > volThreshCat) {
                        //     isStable = false;
                        //     headerRow.innerHTML += makeBadge('~ Volatile', 'var(--trend-volatile-bg)', 'var(--color-purple)', 'var(--trend-volatile-border)');
                        //     bullets.push(`High variation — standard dev ${catData.volatility.std} pp around mean of ${catData.volatility.mean}%.`);
                        // }

                        // 5. Stable
                        if (isStable) {
                            headerRow.innerHTML += makeBadge('— Stable', 'var(--trend-stable-bg)', 'var(--color-text-tertiary)', 'var(--color-border-strong)');
                            let m = catData.volatility ? catData.volatility.mean : "?";
                            bullets.push(`No significant dynamics detected. Steady around ${m}% with low variation.`);
                        }

                        card.appendChild(headerRow);

                        // Render Bullets
                        const ul = document.createElement('ul');
                        ul.classList.add('text-sm');
                        ul.style.cssText = `margin: 0; padding-left: 20px; color: var(--chart-text);`;
                        bullets.forEach(b => {
                            const li = document.createElement('li');
                            li.innerText = b;
                            li.style.marginBottom = '4px';
                            ul.appendChild(li);
                        });

                        card.appendChild(ul);
                        findingsContainer.appendChild(card);
                    });

                    chartWrapper.appendChild(findingsContainer);
                }
            }

            // Bind click event to show stats modal
            document.getElementById(plotId).on('plotly_click', (eventData) => {
                if (eventData && eventData.points && eventData.points.length > 0) {
                    window.timelines.showPeriodStats(eventData.points[0].x);
                }
            });

        });

        // Synced zoom across all charts
        let _syncingZoom = false;
        allPlotIds.forEach(srcId => {
            const srcDiv = document.getElementById(srcId);
            if (!srcDiv) return;
            srcDiv.on('plotly_relayout', (relayoutData) => {
                if (_syncingZoom) return;

                const update = {};
                if (relayoutData['xaxis.range[0]'] !== undefined && relayoutData['xaxis.range[1]'] !== undefined) {
                    update['xaxis.range[0]'] = relayoutData['xaxis.range[0]'];
                    update['xaxis.range[1]'] = relayoutData['xaxis.range[1]'];
                } else if (relayoutData['xaxis.autorange']) {
                    update['xaxis.autorange'] = true;
                } else {
                    return;
                }

                _syncingZoom = true;
                allPlotIds.forEach(tgtId => {
                    if (tgtId === srcId) return;
                    Plotly.relayout(tgtId, update);
                });
                // Reset after async events have propagated
                setTimeout(() => { _syncingZoom = false; }, 200);
            });
        });

        // Restore scroll position
        if (scrollContainer) {
            scrollContainer.scrollTop = scrollPos;
        }

    },

    setFilter: function (varName, filterType) {
        if (!this.timelineState.activeFilters) this.timelineState.activeFilters = {};

        // Toggle off if already active, else set
        if (this.timelineState.activeFilters[varName] === filterType) {
            this.timelineState.activeFilters[varName] = 'all';
        } else {
            this.timelineState.activeFilters[varName] = filterType;
        }

        const activeFilter = this.timelineState.activeFilters[varName];
        const data = this.timelineData;
        const varAnalysis = data && data.analysis && data.analysis[varName];

        // Auto-select categories matching the filter.
        // Categories are already sorted by interestingness score in the
        // analysis data, so slicing gives us the most interesting matches.
        const maxAutoSelect = 5;

        if (activeFilter === 'all') {
            // Reset to top 3 by interestingness
            if (varAnalysis && Array.isArray(varAnalysis.categories)) {
                this.timelineState.categoricalSelections[varName] = varAnalysis.categories.slice(0, 3).map(c => c.id);
            }
        } else {
            // Collect all matching categories (in interestingness order).
            // Thresholds here mirror the badge thresholds in the ribbon and
            // findings panel (0.5×/0.3× of category mean share, 1 pp floor)
            // so the chip, the badge, and the stats cards agree.
            const matching = [];
            if (varAnalysis && Array.isArray(varAnalysis.categories)) {
                varAnalysis.categories.forEach(cd => {
                    if (cd.is_other) return; // "Other" is a residual bucket
                    const meanShare = (cd.volatility && cd.volatility.mean) || (cd.trend && cd.trend.mean) || 0;
                    const trendThresh = 0.5 * Math.max(meanShare, 1.0);
                    const breakThresh = 0.5 * Math.max(meanShare, 1.0);
                    const volThresh = 0.3 * Math.max(meanShare, 1.0);
                    const isRising = (cd.trend && cd.trend.total_change > trendThresh);
                    const isFalling = (cd.trend && cd.trend.total_change < -trendThresh);
                    const hasSpikes = (cd.anomalies && cd.anomalies.length > 0);
                    const hasBreak = (cd.break && Math.abs(cd.break.delta) > breakThresh);
                    const isVolatile = (cd.volatility && cd.volatility.std > volThresh);
                    const isStable = (!isRising && !isFalling && !hasSpikes && !hasBreak && !isVolatile);

                    if ((activeFilter === 'rising' && isRising) ||
                        (activeFilter === 'falling' && isFalling) ||
                        (activeFilter === 'spikes' && hasSpikes) ||
                        (activeFilter === 'breaks' && hasBreak) ||
                        (activeFilter === 'volatile' && isVolatile) ||
                        (activeFilter === 'stable' && isStable)) {
                        matching.push(cd.id);
                    }
                });
            }
            // Select the top N most interesting matches
            this.timelineState.categoricalSelections[varName] = matching.slice(0, maxAutoSelect);
        }

        this.renderTimelineCharts();
    },

    toggleCategory: function (varName, cat) {
        if (!this.timelineState.categoricalSelections[varName]) {
            this.timelineState.categoricalSelections[varName] = [];
        }

        const list = this.timelineState.categoricalSelections[varName];
        const idx = list.indexOf(cat);
        if (idx > -1) {
            list.splice(idx, 1);
        } else {
            list.push(cat);
        }

        this.renderTimelineCharts();
    },

    // Toggle the "Other" residual trace in the chart for `varName`.
    // Default state is off (opt-in); Other is intentionally excluded from
    // the ribbon so it can't visually dominate unless the user asks for it.
    toggleOther: function (varName) {
        if (!this.timelineState.showOther) this.timelineState.showOther = {};
        this.timelineState.showOther[varName] = !this.timelineState.showOther[varName];
        this.renderTimelineCharts();
    },

    // Toggle the "No label" dashed residual trace in the chart for
    // `varName`.  Default state is off; users opt-in via the footer
    // checkbox.  Only rendered for multi-label vars.
    toggleUntagged: function (varName) {
        if (!this.timelineState.showUntagged) this.timelineState.showUntagged = {};
        this.timelineState.showUntagged[varName] = !this.timelineState.showUntagged[varName];
        this.renderTimelineCharts();
    },

    voteMachineAnnotation: function () {
        if (!this.currentStatsPeriod || !this.currentDonationId) {
            alert('Selection data missing.');
            return;
        }

        // Grab current context
        const periodStr = this.currentStatsPeriod;
        const collectionId = this.currentDonationId;

        const btn = document.getElementById('timeline-vote-btn');
        btn.disabled = true;
        const originalText = btn.innerText;
        btn.innerText = 'Voting...';

        fetch('/api/timelines/vote_annotation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                collection_id: collectionId,
                period: periodStr
            })
        })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    btn.innerText = 'Voted \u2713'; // Unicode checkmark
                    btn.style.backgroundColor = 'var(--color-success)';
                    // Disable button completely or leave it success state (allow re-click but backend handles dupes)
                } else {
                    alert('Failed to vote: ' + data.error);
                    btn.innerText = originalText;
                    btn.disabled = false;
                }
            })
            .catch(err => {
                console.error(err);
                alert('Network error submitting vote.');
                btn.innerText = originalText;
                btn.disabled = false;
            });
    },

    viewInVideoAnalysis: function () {
        if (!this.currentStatsPeriod || !this.currentDonationId) return;

        const collection = this.collectionList.find(d => d.collection_id === this.currentDonationId);
        const study = collection && collection.study;
        if (!study) {
            alert('No study found for this collection.');
            return;
        }

        const dateValues = (this.currentStatsPeriodDates && this.currentStatsPeriodDates.length > 0)
            ? this.currentStatsPeriodDates
            : [String(this.currentStatsPeriod).slice(0, 10)];

        window._pendingDrillDown = {
            filters: {
                'collection_id': { type: 'category', value: [this.currentDonationId] },
                'local_date': { type: 'category', value: dateValues }
            },
            searchQuery: '',
            timestamp: Date.now()
        };

        document.getElementById('timeline-stats-modal').style.display = 'none';
        const tabBtn = document.querySelector('.tab-button[onclick*="video_analysis"]');
        if (tabBtn) tabBtn.click();
    },

    showPeriodStats: function (clickedDate) {
        if (!this.timelineData || !this.timelineData.dates) return;

        const esc = (s) => {
            const el = document.createElement('span');
            el.textContent = s;
            return el.innerHTML;
        };

        // Normalise date string for lookup (Plotly date-axis may append time)
        const dateStr = String(clickedDate).slice(0, 10);
        const dateIndex = this.timelineData.dates.findIndex(d => String(d).slice(0, 10) === dateStr);
        if (dateIndex === -1) return;

        const formattedLabel = this.timelineData.date_labels ? this.timelineData.date_labels[dateIndex] : dateStr;

        const modal = document.getElementById('timeline-stats-modal');
        const titleEl = document.getElementById('timeline-stats-title');
        const contentEl = document.getElementById('timeline-stats-content');
        if (!modal || !titleEl || !contentEl) return;

        // Aggregate across the same centred window the chart uses for smoothing.
        // This makes the modal answer "what's in the bump I clicked on" instead
        // of showing one noisy day that may not match the averaged line.
        const smoothW = Math.max(1, this.timelineState.smoothing || 1);
        const half = Math.floor(smoothW / 2);
        const startIdx = Math.max(0, dateIndex - half);
        const endIdx = Math.min(this.timelineData.dates.length - 1, dateIndex + half);
        const windowDates = this.timelineData.dates.slice(startIdx, endIdx + 1);

        this.currentStatsPeriod = this.timelineData.dates[dateIndex];
        this.currentStatsPeriodDates = windowDates.map(d => String(d).slice(0, 10));

        const startIso = String(windowDates[0]).slice(0, 10);
        const endIso = String(windowDates[windowDates.length - 1]).slice(0, 10);
        titleEl.textContent = startIso === endIso
            ? `Stats for ${startIso}`
            : `Stats for ${startIso} – ${endIso}`;

        // Reset vote button
        const voteBtn = document.getElementById('timeline-vote-btn');
        if (voteBtn) {
            voteBtn.textContent = 'Vote to annotate';
            voteBtn.style.backgroundColor = '';
            voteBtn.disabled = false;
        }

        // Hide "View videos" if no study mapping
        const viewBtn = document.getElementById('timeline-view-videos-btn');
        if (viewBtn) {
            const collection = this.collectionList.find(d => d.collection_id === this.currentDonationId);
            viewBtn.style.display = (collection && collection.study) ? '' : 'none';
        }

        let html = '<table class="stats-table" style="margin-top: 4px;">';
        html += '<thead><tr><th style="padding: 4px; text-align: left; border-bottom: 2px solid var(--color-border-strong);">Variable</th>';
        html += '<th style="padding: 4px; text-align: right; border-bottom: 2px solid var(--color-border-strong); font-weight: var(--weight-semibold);">% of attention</th></tr></thead>';
        html += '<tbody>';

        let varKeys = Object.keys(this.timelineData.variables);
        if (this.timelineData.variables_order && Array.isArray(this.timelineData.variables_order)) {
            varKeys.sort((a, b) => {
                const idxA = this.timelineData.variables_order.indexOf(a);
                const idxB = this.timelineData.variables_order.indexOf(b);
                if (idxA === -1 && idxB === -1) return 0;
                if (idxA === -1) return 1;
                if (idxB === -1) return -1;
                return idxA - idxB;
            });
        }
        // machine_state is a flat "5: Scrape ok, MA ok" post-filter — drop it.
        varKeys = varKeys.filter(k => k !== 'machine_state');

        // Numerics live under the "% of attention" header but aren't percentages
        // (they're watch-time-weighted means of a score).  Push them to the
        // bottom and render them under a sub-header that sets the right
        // expectation.
        const numericKeys = varKeys.filter(k => this.timelineData.variables[k].type === 'numeric');
        const categoricalKeys = varKeys.filter(k => this.timelineData.variables[k].type !== 'numeric');
        varKeys = categoricalKeys.concat(numericKeys);

        let subHeaderInserted = false;
        varKeys.forEach(varName => {
            const varData = this.timelineData.variables[varName];
            const displayTitle = varData.display_name || varName;

            if (varData.type === 'numeric' && !subHeaderInserted) {
                html += '<tr><td colspan="2" style="padding: 14px 4px 4px; border-top: 1px solid var(--color-border); font-size: var(--text-xs); color: var(--color-text-muted); font-style: italic;">'
                    + 'Play-time-weighted mean'
                    + '</td></tr>';
                subHeaderInserted = true;
            }

            html += `<tr><td style="padding: 4px; vertical-align: top; font-weight: var(--weight-semibold);">${esc(displayTitle)}</td>`;
            html += `<td style="padding: 4px; vertical-align: top; text-align: left; font-weight: var(--weight-normal);">`;

            if (varData.type === 'categorical') {
                // Aggregate over [startIdx, endIdx]: sum weighted_counts per
                // category (numerator) and sum the matching denominator (either
                // weighted video totals or weighted valid counts, depending on
                // share_denominator) so shares come out correctly weighted by
                // the true per-day totals rather than averaging percentages.
                const denomSeries = varData.share_denominator === 'videos'
                    ? (varData.daily_weighted_video_total || [])
                    : (varData.daily_weighted_valid || []);
                const wCountsSeries = varData.weighted_counts || [];
                const rawCountsSeries = varData.counts || [];

                const weightedTotals = {};
                const rawTotals = {};
                let denomSum = 0;
                for (let i = startIdx; i <= endIdx; i++) {
                    const wc = wCountsSeries[i] || {};
                    for (const k in wc) {
                        weightedTotals[k] = (weightedTotals[k] || 0) + (wc[k] || 0);
                    }
                    const rc = rawCountsSeries[i] || {};
                    for (const k in rc) {
                        rawTotals[k] = (rawTotals[k] || 0) + (rc[k] || 0);
                    }
                    denomSum += (denomSeries[i] || 0);
                }

                const aggShares = {};
                if (denomSum > 0) {
                    for (const k in weightedTotals) {
                        aggShares[k] = (weightedTotals[k] / denomSum) * 100;
                    }
                }

                // Fallback to single-day share_series if weighted_counts isn't
                // present (older cached data): equivalent to prior behaviour.
                const useFallback = Object.keys(weightedTotals).length === 0
                    && Object.keys(rawTotals).length > 0
                    && varData.share_series;
                let sharesForDisplay = aggShares;
                if (useFallback) {
                    sharesForDisplay = (varData.share_series && varData.share_series[dateIndex]) || {};
                }

                const cats = Object.keys({...sharesForDisplay, ...rawTotals});
                cats.sort((a, b) => (sharesForDisplay[b] || 0) - (sharesForDisplay[a] || 0) || (rawTotals[b] || 0) - (rawTotals[a] || 0));

                if (cats.length === 0) {
                    html += `<span style="color: var(--color-text-muted);">No data</span>`;
                } else {
                    const MAX_SHOW = 10;
                    const visible = cats.slice(0, MAX_SHOW);
                    visible.forEach(cat => {
                        const share = sharesForDisplay[cat];
                        const shareStr = share != null ? `${share.toFixed(1)}%` : '—';
                        html += `<div style="display: flex; justify-content: space-between; gap: 12px; line-height: var(--leading-normal);">`
                            + `<span>${esc(cat)}</span>`
                            + `<span style="white-space: nowrap;">${shareStr}</span>`
                            + `</div>`;
                    });
                    if (cats.length > MAX_SHOW) {
                        html += `<div class="text-xs" style="color: var(--color-text-muted); margin-top: 2px;">and ${cats.length - MAX_SHOW} more\u2026</div>`;
                    }
                }
            } else if (varData.type === 'numeric' && varData.values) {
                // Weighted mean across the window: Σ(value_i · weight_i) / Σweight_i
                // where weight is daily_weighted_valid (matches how the
                // per-period value is already a watch-time-weighted mean).
                const weights = varData.daily_weighted_valid || [];
                let num = 0, den = 0;
                for (let i = startIdx; i <= endIdx; i++) {
                    const v = varData.values[i];
                    if (v === null || v === undefined) continue;
                    const w = weights[i] || 0;
                    if (w > 0) {
                        num += v * w;
                        den += w;
                    }
                }
                let val;
                if (den > 0) {
                    val = num / den;
                } else {
                    // Fallback: simple mean of non-null values if no weights available.
                    let sum = 0, n = 0;
                    for (let i = startIdx; i <= endIdx; i++) {
                        const v = varData.values[i];
                        if (v !== null && v !== undefined) { sum += v; n += 1; }
                    }
                    val = n > 0 ? (sum / n) : null;
                }
                if (val === null || val === undefined) {
                    html += `<span style="color: var(--color-text-muted);">No data</span>`;
                } else {
                    const valStr = Number.isInteger(val) ? `${val}` : val.toFixed(2);
                    html += `<div style="text-align: right;">${valStr}</div>`;
                }
            } else {
                html += `<span style="color: var(--color-text-muted);">\u2014</span>`;
            }

            html += `</td></tr>`;
        });

        html += '</tbody></table>';
        contentEl.innerHTML = html;
        modal.style.display = 'block';
    }
};

// Initialize on load
document.addEventListener('DOMContentLoaded', () => {
    // If we are on a page with the timelines tab
    if (document.getElementById('timelines')) {
        if (window.studyState && window.studyState.ready) {
            window.studyState.ready.then(() => {
                window.timelines.init();
            });
        } else {
            window.timelines.init();
        }

        document.addEventListener('study:changed', () => {
            // Reload collections for the new active study.
            if (window.timelines) {
                window.timelines.currentDonationId = null;
                window.timelines.timelineData = null;
                window.timelines.loadDonations();
            }
        });
    }
});

window.addEventListener('theme-changed', () => {
    if (window.timelines && window.timelines.timelineData) {
        window.timelines.renderTimelineCharts();
    }
});
