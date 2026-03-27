
// Timelines Tab Logic

window.timelines = {
    currentStudy: null,
    currentDonationId: null,
    donationList: [],
    timelineData: null,
    timelineState: {
        categoricalSelections: {},
        analysisToggles: {},
        activeFilters: {},
        smoothing: 7
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
        // No study selection anymore, load all valid donations directly
        this.loadDonations();
    },

    loadStudies: function () {
        // Deprecated
    },

    loadDonations: async function () {
        const select = document.getElementById('timelines-collection-select');

        try {
            const res = await fetch('/api/timelines/donations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            });

            const data = await res.json();
            if (data.error) {
                select.innerHTML = `<option value="" disabled selected>Error: ${data.error}</option>`;
                return;
            }

            this.donationList = data.filter(d => !d.hidden);
            this.renderCollectionDropdown();

        } catch (e) {
            console.error("Error loading donations:", e);
            select.innerHTML = '<option value="" disabled selected>Failed to load</option>';
        }
    },

    renderCollectionDropdown: function () {
        const select = document.getElementById('timelines-collection-select');
        const countSpan = document.getElementById('timelines-collection-count');
        select.innerHTML = '';

        if (this.donationList.length === 0) {
            select.innerHTML = '<option value="" disabled selected>No collections found</option>';
            return;
        }

        this.donationList.forEach(d => {
            const opt = document.createElement('option');
            opt.value = d.D_donation_id;
            opt.textContent = d.display_donation_id && d.display_donation_id.trim() !== ''
                ? d.display_donation_id
                : d.D_donation_id;
            if (d.D_donation_id === this.currentDonationId) {
                opt.selected = true;
            }
            select.appendChild(opt);
        });

        if (countSpan) {
            countSpan.textContent = `${this.donationList.length} collections`;
        }

        // Auto-select first if none selected
        if (!this.currentDonationId && this.donationList.length > 0) {
            this.selectDonation(this.donationList[0].D_donation_id);
        }
    },

    onCollectionChange: function () {
        const select = document.getElementById('timelines-collection-select');
        if (select && select.value) {
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

    _movingAvg: function (arr, window) {
        const half = Math.floor(window / 2);
        return arr.map((_, i, a) => {
            const start = Math.max(0, i - half);
            const end = Math.min(a.length, i + half + 1);
            const slice = a.slice(start, end);
            return slice.reduce((s, v) => s + v, 0) / slice.length;
        });
    },

    selectDonation: async function (donationId) {
        this.currentDonationId = donationId;
        const header = document.getElementById('timelines-header');
        if (header) header.style.display = 'block';

        const donation = this.donationList.find(d => d.D_donation_id === donationId);
        let displayTitle = donationId;
        if (donation && donation.display_donation_id) {
            displayTitle = donation.display_donation_id;
        }

        const title = document.getElementById('timelines-title');
        if (title) {
            title.innerHTML = `Timeline: ${displayTitle}`;

            // Append Tags if available
            if (donation && donation.annotation_tags && Array.isArray(donation.annotation_tags) && donation.annotation_tags.length > 0) {
                const tagsContainer = document.createElement('span');
                tagsContainer.style.marginLeft = '15px';
                tagsContainer.style.fontSize = '0.6em'; // Smaller relative to h2
                tagsContainer.style.fontWeight = 'normal';

                donation.annotation_tags.forEach(tag => {
                    const chip = document.createElement('span');
                    chip.innerText = tag;
                    chip.style.display = 'inline-block';
                    chip.style.background = '#007acc';
                    chip.style.color = '#fff';
                    chip.style.padding = '2px 8px';
                    chip.style.borderRadius = '12px';
                    chip.style.marginRight = '5px';
                    chip.style.verticalAlign = 'middle';
                    tagsContainer.appendChild(chip);
                });

                title.appendChild(tagsContainer);
            }
        }

        // --- Collection Info Tooltip ---
        let pe_donation = null;
        if (window.pe_data && window.pe_data.length > 0) {
            pe_donation = window.pe_data.find(d => d.D_donation_id === donationId);
        }

        if (pe_donation) {

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
            updateInfoStat('timelines-stat-name', pe_donation.name);
            updateInfoStat('timelines-stat-email', pe_donation.email);
            updateInfoStat('timelines-stat-tiktok', pe_donation.tiktokHandle);
            updateInfoStat('timelines-stat-age', pe_donation.age);
            updateInfoStat('timelines-stat-country', pe_donation.country);
            updateInfoStat('timelines-stat-postcode', pe_donation.postCode);
            updateInfoStat('timelines-stat-display-id', pe_donation.display_donation_id);

            const pInfoSection = document.getElementById('timelines-participant-info-section');
            if (pInfoSection) {
                pInfoSection.style.display = visibleInfoCount > 0 ? 'block' : 'none';
            }

            const fmtDate = (ts) => {
                if (!ts) return 'not provided';
                const d = new Date(ts);
                return d.toLocaleDateString('en-AU', { day: '2-digit', month: 'short', year: 'numeric' });
            };
            document.getElementById('timelines-stat-donation-date').innerText = fmtDate(pe_donation.date);

            // Activity Stats
            const tz = pe_donation.inferred_tz_offset;
            const tzStr = tz !== null && tz !== undefined ? `UTC${tz >= 0 ? '+' : ''}${tz}` : 'Unknown';
            document.getElementById('timelines-stat-timezone').innerText = tzStr;
            document.getElementById('timelines-stat-active-days').innerText = pe_donation.active_days || 0;
            document.getElementById('timelines-stat-total-events').innerText = (pe_donation.total_events || 0).toLocaleString();
            document.getElementById('timelines-stat-peak-segment').innerText = pe_donation.peak_day_segment || 'Unknown';

            document.getElementById('timelines-stat-first-event').innerText = fmtDate(pe_donation.first_event_ts);
            document.getElementById('timelines-stat-last-event').innerText = fmtDate(pe_donation.last_event_ts);

            // Store first event date for the 'exclude before first activity' filter
            this.firstActivityDate = pe_donation.first_event_ts ? pe_donation.first_event_ts.substring(0, 10) : null;

            // Tags
            const currentTags = Array.isArray(pe_donation.annotation_tags) ? pe_donation.annotation_tags : [];
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
                    donation_id: donationId,
                    interval: 'day'
                })
            });
            const data = await res.json();

            if (data.error) {
                container.innerHTML = `<p style="color:red;">Error: ${data.error}</p>`;
                return;
            }

            //console.log("TIMELINE DEBUG: Received Data", data);
            this.timelineData = data;

            this.renderTimelineCharts();

        } catch (e) {
            console.error("Error fetching timeline data", e);
            container.innerHTML = `<p style="color:red;">Failed to load timeline data.</p>`;
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

        // Save scroll positions of category lists before clearing container
        const catScrollPositions = {};
        if (container) {
            container.querySelectorAll('[id^="controls-timeline-plot-"]').forEach(menu => {
                const scroller = menu.querySelector('div[style*="overflow-y:auto"]');
                if (scroller) {
                    catScrollPositions[menu.id] = scroller.scrollTop;
                }
            });
        }

        container.innerHTML = '';

        const excludeNoData = document.getElementById('timelines-exclude-nodata').checked;
        const excludeBeforeFirst = document.getElementById('timelines-exclude-before-first-activity').checked;
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

        // Ensure machine_state is always last
        if (varKeys.includes('machine_state')) {
            varKeys = varKeys.filter(k => k !== 'machine_state');
            varKeys.push('machine_state');
        }

        // Iterate over variables
        varKeys.forEach(varName => {
            const varData = data.variables[varName];
            const displayTitle = varData.display_name || (varName === 'machine_state' ? 'Scrape and Annotation States' : varName);

            const chartWrapper = document.createElement('div');
            chartWrapper.className = 'timeline-chart-wrapper';
            chartWrapper.style.marginBottom = '30px';
            chartWrapper.style.background = '#1e1e1e';
            chartWrapper.style.padding = '10px';
            chartWrapper.style.borderRadius = '5px';

            // Title Above Plot
            const titleDiv = document.createElement('div');
            titleDiv.style.display = 'flex';
            titleDiv.style.alignItems = 'center';
            titleDiv.style.gap = '10px';
            const subtitle = varData.type !== 'categorical' ? '<span style="font-size: 0.75em; color: #aaa; font-weight: normal;"> (mean values)</span>' : '';
            titleDiv.innerHTML = `<h3 style="margin-top: 0; margin-bottom: 10px; font-size: 1.25em;">${displayTitle}${subtitle}</h3>`;

            // Findings panel is rendered below each categorical chart with a show/hide toggle button.
            chartWrapper.appendChild(titleDiv);

            // Container for Controls and Plot — height matches the plot (400px)
            const innerFlexDiv = document.createElement('div');
            innerFlexDiv.style.display = 'flex';
            innerFlexDiv.style.flexDirection = 'row';
            innerFlexDiv.style.height = '400px';

            // Left Menu for Categorical (or placeholder for alignment)
            const controlsDiv = document.createElement('div');
            controlsDiv.style.width = '300px';
            controlsDiv.style.flexShrink = '0';
            controlsDiv.style.display = 'flex';
            controlsDiv.style.flexDirection = 'column';
            controlsDiv.style.overflow = 'hidden';

            if (varData.type === 'categorical') {
                controlsDiv.style.paddingRight = '10px';
                controlsDiv.style.borderRight = '1px solid #333';
            }

            const plotDiv = document.createElement('div');
            plotDiv.style.flex = 1;
            plotDiv.style.minWidth = '0'; // Critical for flex child resizing
            plotDiv.style.height = '400px'; // fixed height for plot to prevent resizing issues

            // Unique ID
            const plotId = `timeline-plot-${varName.replace(/\\s+/g, '_')}`;
            plotDiv.id = plotId;

            innerFlexDiv.appendChild(controlsDiv);
            innerFlexDiv.appendChild(plotDiv);
            chartWrapper.appendChild(innerFlexDiv);
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

                // Default selection: top 3 by interestingness (or all for machine_state)
                const defaultCats = varData.default_all
                    ? (varData.top_categories || [])
                    : (interestingnessOrder.length > 0 ? interestingnessOrder.slice(0, 3) : (varData.top_categories ? varData.top_categories.slice(0, 3) : []));
                const selectedCats = this.timelineState.categoricalSelections[varName] || defaultCats;

                // Store in state if not already
                if (!this.timelineState.categoricalSelections[varName]) {
                    this.timelineState.categoricalSelections[varName] = selectedCats;
                }

                // Setup Sidebar Category Selection
                // Calculate total frequency (use sliced counts)
                const slicedCounts = startIdx > 0 ? varData.counts.slice(startIdx) : varData.counts;
                const slicedVideoCounts = startIdx > 0 && varData.daily_video_counts ? varData.daily_video_counts.slice(startIdx) : varData.daily_video_counts;
                const slicedValidCounts = startIdx > 0 && varData.daily_valid_counts ? varData.daily_valid_counts.slice(startIdx) : varData.daily_valid_counts;
                const catTotals = {};
                slicedCounts.forEach(day => {
                    Object.keys(day).forEach(c => {
                        catTotals[c] = (catTotals[c] || 0) + day[c];
                    });
                });

                // Sort categories by interestingness score (fall back to frequency for unscored)
                const allCatsByFreq = Object.keys(catTotals).sort((a, b) => catTotals[b] - catTotals[a]);
                const allCategories = allCatsByFreq.slice().sort((a, b) => {
                    const scoreA = analysisMap[a] ? analysisMap[a].score : -1;
                    const scoreB = analysisMap[b] ? analysisMap[b].score : -1;
                    return scoreB - scoreA;
                });

                // --- Adaptive Category Filtering Heuristic ---
                // Balances two goals: keep the sidebar manageable, but don't drop too
                // many observations.  High-cardinality variables (hashtags, brands) get
                // more slots via the observation floor so they stay representative.
                const grandTotal = allCatsByFreq.reduce((sum, cat) => sum + catTotals[cat], 0);
                const keptCategories = [];
                let omittedCount = 0;
                let omittedObservations = 0;

                const minCategories = 5;
                const maxCategories = 25;
                const coverageTarget = 0.95;
                const minObservations = 10;
                const minCoverageFloor = 0.50;

                // First pass: apply standard filters (coverage target + max cap + min obs)
                let cumulativeSum = 0;
                const coveredSet = new Set();
                for (let i = 0; i < allCatsByFreq.length; i++) {
                    const cat = allCatsByFreq[i];
                    const catCount = catTotals[cat];
                    const isSelected = selectedCats.includes(cat);
                    const isWithinMin = i < minCategories;
                    const isMeaningful = catCount >= minObservations;
                    const isWithinMax = coveredSet.size < maxCategories;
                    const isWithinCoverage = cumulativeSum < (coverageTarget * grandTotal);

                    if (isSelected || isWithinMin || (isWithinCoverage && isMeaningful && isWithinMax)) {
                        coveredSet.add(cat);
                    }
                    cumulativeSum += catCount;
                }

                // Second pass: if we've dropped more than 50% of observations,
                // keep adding categories (by frequency) until we reach the floor
                let coveredObs = allCatsByFreq.filter(c => coveredSet.has(c)).reduce((s, c) => s + catTotals[c], 0);
                if (coveredObs < minCoverageFloor * grandTotal) {
                    for (const cat of allCatsByFreq) {
                        if (coveredSet.has(cat)) continue;
                        coveredSet.add(cat);
                        coveredObs += catTotals[cat];
                        if (coveredObs >= minCoverageFloor * grandTotal) break;
                    }
                }

                // Count omissions
                for (const cat of allCatsByFreq) {
                    if (!coveredSet.has(cat)) {
                        omittedCount++;
                        omittedObservations += catTotals[cat];
                    }
                }

                // Build kept list in interestingness order
                allCategories.forEach(cat => {
                    if (coveredSet.has(cat)) keptCategories.push(cat);
                });

                // Preserve scroll position if re-rendering an existing menu
                const catScrollPos = catScrollPositions[`controls-${plotId}`] || 0;

                const omittedPercent = grandTotal > 0 ? ((omittedObservations / grandTotal) * 100).toFixed(1) : 0;
                const activeFilter = this.timelineState.activeFilters[varName] || 'all';

                controlsDiv.id = `controls-${plotId}`;
                controlsDiv.innerHTML = `
                    <div style="font-size:0.85em; margin-bottom:5px; color:#aaa; display:flex; align-items:center; flex-wrap:wrap; gap:4px;">
                        <span style="margin-right:4px;">Select Categories:</span>
                        <div class="filter-chip" id="chip-${plotId}-rising" onclick="window.timelines.setFilter('${varName}', 'rising')" style="font-size:0.7em; padding:2px 6px; border-radius:10px; cursor:pointer; background:rgba(78, 201, 176, 0.15); color:#4ec9b0; border:1px solid rgba(78, 201, 176, 0.5); opacity:${activeFilter === 'rising' ? '1.0' : '0.4'};">↑ Rising</div>
                        <div class="filter-chip" id="chip-${plotId}-falling" onclick="window.timelines.setFilter('${varName}', 'falling')" style="font-size:0.7em; padding:2px 6px; border-radius:10px; cursor:pointer; background:rgba(244, 135, 113, 0.15); color:#f48771; border:1px solid rgba(244, 135, 113, 0.5); opacity:${activeFilter === 'falling' ? '1.0' : '0.4'};">↓ Falling</div>
                        <div class="filter-chip" id="chip-${plotId}-spikes" onclick="window.timelines.setFilter('${varName}', 'spikes')" style="font-size:0.7em; padding:2px 6px; border-radius:10px; cursor:pointer; background:rgba(206, 145, 120, 0.15); color:#ce9178; border:1px solid rgba(206, 145, 120, 0.5); opacity:${activeFilter === 'spikes' ? '1.0' : '0.4'};">◎ Spikes</div>
                        <div class="filter-chip" id="chip-${plotId}-breaks" onclick="window.timelines.setFilter('${varName}', 'breaks')" style="font-size:0.7em; padding:2px 6px; border-radius:10px; cursor:pointer; background:rgba(86, 156, 214, 0.15); color:#569cd6; border:1px solid rgba(86, 156, 214, 0.5); opacity:${activeFilter === 'breaks' ? '1.0' : '0.4'};">⋮ Breaks</div>
                        <div class="filter-chip" id="chip-${plotId}-volatile" onclick="window.timelines.setFilter('${varName}', 'volatile')" style="font-size:0.7em; padding:2px 6px; border-radius:10px; cursor:pointer; background:rgba(218, 112, 214, 0.15); color:#da70d6; border:1px solid rgba(218, 112, 214, 0.5); opacity:${activeFilter === 'volatile' ? '1.0' : '0.4'};">~ Volatile</div>
                    </div>

                    <div id="cat-list-${plotId}" style="flex:1; min-height:0; overflow-y:auto; border:1px solid #444; padding:5px;">
                        ${keptCategories.map(cat => {
                            const cd = analysisMap[cat] || {};
                            const isRising = (cd.trend && cd.trend.total_change > 4);
                            const isFalling = (cd.trend && cd.trend.total_change < -4);
                            const hasSpikes = (cd.anomalies && cd.anomalies.length > 0);
                            const hasBreak = (cd.break && Math.abs(cd.break.delta) > 4);
                            const isVolatile = (cd.volatility && cd.volatility.std > 2.5);
                            const isStable = (!isRising && !isFalling && !hasSpikes && !hasBreak && !isVolatile);

                            // Build inline badge string
                            let badges = '';
                            if (isRising)   badges += '<span style="color:#4ec9b0; font-size:0.8em;" title="Rising">↑</span> ';
                            if (isFalling)  badges += '<span style="color:#f48771; font-size:0.8em;" title="Falling">↓</span> ';
                            if (hasSpikes)  badges += '<span style="color:#ce9178; font-size:0.8em;" title="Spikes">◎</span> ';
                            if (hasBreak)   badges += '<span style="color:#569cd6; font-size:0.8em;" title="Step change">⋮</span> ';
                            if (isVolatile) badges += '<span style="color:#da70d6; font-size:0.8em;" title="Volatile">~</span> ';
                            if (isStable)   badges += '<span style="color:#aaa; font-size:0.8em;" title="Stable">—</span> ';

                            return `
                            <div class="category-item"
                                 data-cat="${cat}"
                                 data-rising="${isRising}"
                                 data-falling="${isFalling}"
                                 data-spikes="${hasSpikes}"
                                 data-breaks="${hasBreak}"
                                 data-volatile="${isVolatile}"
                                 data-stable="${isStable}"
                                 style="display:flex; align-items:flex-start; margin-bottom:3px;">
                                <input type="checkbox"
                                       value="${cat}"
                                       ${selectedCats.includes(cat) ? 'checked' : ''}
                                       onchange="window.timelines.toggleCategory('${varName}', '${cat}')"
                                       style="margin-right:5px; margin-top:2px;">
                                <span style="line-height:1.2;">
                                    ${cat}
                                    <span style="color:#aaa; font-size:0.85em; white-space:nowrap;">(${catTotals[cat].toLocaleString()})</span>
                                    ${badges ? '<span style="margin-left:3px;">' + badges + '</span>' : ''}
                                </span>
                            </div>
                            `;
                        }).join('')}
                    </div>
                    ${omittedCount > 0 ? `<div style="font-size:0.75em; color:#888; margin-top:5px; font-style:italic;">Dropped ${omittedCount} tiny cats → ${omittedPercent}% obs lost.</div>` : ''}
                `;

                // Restore scroll position
                if (catScrollPos > 0) {
                    // Small timeout ensures the DOM is painted first before we try to scroll
                    setTimeout(() => {
                        const newMenu = document.getElementById(`controls-${plotId}`);
                        if (newMenu) {
                            const scroller = newMenu.querySelector('div[style*="overflow-y:auto"]');
                            if (scroller) scroller.scrollTop = catScrollPos;
                        }
                    }, 0);
                }

                // Calculate share values and render as line/area charts

                let allYVals = []; // Track all y-values for axis range

                selectedCats.forEach((cat, idx) => {
                    const yVals = [];
                    const hoverTexts = [];
                    const slicedDateLabels = startIdx > 0 ? (data.date_labels || dates).slice(startIdx) : (data.date_labels || dates);

                    dates.forEach((d, i) => {
                        const dailyRecord = slicedCounts[i] || {};
                        const val = dailyRecord[cat] || 0;
                        const total = slicedValidCounts ? slicedValidCounts[i] : (slicedVideoCounts ? slicedVideoCounts[i] : 1);
                        const share = total > 0 ? (val / total) * 100 : 0;
                        yVals.push(share);
                        hoverTexts.push(
                            `<b>${cat}</b><br>` +
                            `Period: ${slicedDateLabels[i] || d}<br>` +
                            `Share: ${share.toFixed(1)}%<br>` +
                            `Count: ${val.toLocaleString()}`
                        );
                    });

                    // Apply smoothing if active
                    const smoothW = this.timelineState.smoothing || 1;
                    const displayY = smoothW > 1 ? this._movingAvg(yVals, smoothW) : yVals;

                    allYVals = allYVals.concat(displayY);
                    const catColor = colors[idx % colors.length];

                    // Reduce data line prominence when analysis overlays are visible
                    // Plotly's trace-level opacity doesn't fade line strokes, so we
                    // bake the alpha directly into the line color via rgba.
                    const overlaysActive = this.timelineState.findingsPanelOpen && this.timelineState.findingsPanelOpen[varName];
                    const lineAlpha = overlaysActive ? 0.25 : 1.0;
                    const lineWidth = overlaysActive ? 1.5 : 2;
                    const fillAlpha = overlaysActive ? '06' : '12';

                    // Convert hex (#RRGGBB) to rgba string
                    const hexToRgba = (hex, alpha) => {
                        const r = parseInt(hex.slice(1, 3), 16);
                        const g = parseInt(hex.slice(3, 5), 16);
                        const b = parseInt(hex.slice(5, 7), 16);
                        return `rgba(${r},${g},${b},${alpha})`;
                    };
                    const lineColor = hexToRgba(catColor, lineAlpha);

                    // Line trace
                    traces.push({
                        x: xVals,
                        y: displayY,
                        type: 'scatter',
                        mode: 'lines',
                        line: { width: lineWidth, shape: 'spline', color: lineColor },
                        fill: 'tozeroy',
                        fillcolor: catColor + fillAlpha,
                        name: cat,
                        text: hoverTexts,
                        hoverinfo: 'text',
                        hovertemplate: '%{text}<extra></extra>'
                    });
                });

                // Compute y-axis range from actual data
                if (allYVals.length > 0) {
                    const yMax = Math.max(...allYVals);
                    const yMin = Math.min(...allYVals);
                    const padding = Math.max((yMax - yMin) * 0.1, 2);
                    chartWrapper._catYRange = [Math.max(0, yMin - padding), Math.min(100, yMax + padding)];
                }

                yAxisTitle = 'Share (%)';

            } else {
                // Numeric
                // Remove [0,1] normalization assumption.
                // Log Support
                if (varData.log) {
                    yAxisTitle = 'Value (Log)';
                } else {
                    yAxisTitle = 'Value';
                }

                const slicedValues = startIdx > 0 ? varData.values.slice(startIdx) : varData.values;

                // Apply smoothing if active
                const smoothW = this.timelineState.smoothing || 1;
                const displayValues = smoothW > 1 ? this._movingAvg(slicedValues, smoothW) : slicedValues;

                traces.push({
                    x: xVals,
                    y: displayValues,
                    type: 'bar',
                    marker: { color: '#2196F3' },
                    name: varName
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
                margin: { t: 20, r: 20, b: (isCategorical ? 60 : 40), l: 40 },
                paper_bgcolor: 'rgba(0,0,0,0)',
                plot_bgcolor: 'rgba(0,0,0,0)',
                font: { color: '#999', size: 11 },
                xaxis: {
                    type: excludeNoData ? 'category' : 'date',
                    tickmode: 'array',
                    tickvals: tickVals,
                    ticktext: tickText,
                    tickangle: 0,
                    gridcolor: 'rgba(255,255,255,0.06)',
                    gridwidth: 1,
                    zeroline: false,
                    tickfont: { color: '#777' }
                },
                yaxis: {
                    title: { text: yAxisTitle, font: { color: '#888', size: 11 } },
                    range: isCategorical && chartWrapper._catYRange ? chartWrapper._catYRange : undefined,
                    gridcolor: 'rgba(255,255,255,0.06)',
                    gridwidth: 1,
                    zeroline: false,
                    tickfont: { color: '#777' }
                },
                barmode: isCategorical ? undefined : 'stack',
                showlegend: isCategorical
            };

            if (varData.type === 'categorical') {
                layout.legend = {
                    orientation: 'h',
                    y: -0.2, // Move legend below the plot
                    x: 0.5,
                    xanchor: 'center',
                    yanchor: 'top'
                };
            }

            // Log Axis specific
            if (varData.type !== 'categorical' && varData.log) {
                layout.yaxis.type = 'log';
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

                // --- Entropy / Evenness band ---
                // Compute Shannon evenness at each time point using a fixed N
                // (total distinct categories across the whole series) so the
                // denominator is stable and days with fewer categories present
                // correctly score lower.
                const entropyVals = [];
                const slicedCountsForEntropy = startIdx > 0 ? varData.counts.slice(startIdx) : varData.counts;

                // Fixed N: count all distinct categories across the sliced series
                const allCatsInSeries = new Set();
                for (const dayBucket of slicedCountsForEntropy) {
                    for (const k of Object.keys(dayBucket || {})) {
                        if ((dayBucket[k] || 0) > 0) allCatsInSeries.add(k);
                    }
                }
                const fixedN = allCatsInSeries.size;
                const fixedMaxH = fixedN > 1 ? Math.log(fixedN) : 1;

                for (let ti = 0; ti < slicedCountsForEntropy.length; ti++) {
                    const dayBucket = slicedCountsForEntropy[ti] || {};
                    const total = Object.values(dayBucket).reduce((s, v) => s + (v || 0), 0);
                    if (total === 0) {
                        entropyVals.push(null);
                        continue;
                    }
                    let H = 0;
                    for (const v of Object.values(dayBucket)) {
                        const p = (v || 0) / total;
                        if (p > 0) H -= p * Math.log(p);
                    }
                    entropyVals.push(H / fixedMaxH);
                }

                // Heavy independent centred smoothing (14-period) — entropy is
                // about the structural diversity trend, not daily fluctuations.
                // Null-aware: skip no-data days in the averaging window.
                const entropySmoothing = Math.max(14, this.timelineState.smoothing || 1);
                const entropyHalf = Math.floor(entropySmoothing / 2);
                const smoothedEntropy = entropyVals.map((_, i) => {
                    const start = Math.max(0, i - entropyHalf);
                    const end = Math.min(entropyVals.length, i + entropyHalf + 1);
                    let sum = 0, count = 0;
                    for (let j = start; j < end; j++) {
                        if (entropyVals[j] !== null) { sum += entropyVals[j]; count++; }
                    }
                    return count > 0 ? sum / count : null;
                });

                // Min-max normalised mapping so the evenness band uses the
                // full chart height, making variation clearly visible.
                const validEntropy = smoothedEntropy.filter(e => e !== null);
                const eMin = validEntropy.length > 0 ? Math.min(...validEntropy) : 0;
                const eMax = validEntropy.length > 0 ? Math.max(...validEntropy) : 1;
                const eSpan = eMax - eMin || 1;

                const yRange = chartWrapper._catYRange || [0, 100];
                const scaledEntropy = smoothedEntropy.map(e => {
                    if (e === null) return null;
                    return yRange[0] + ((e - eMin) / eSpan) * (yRange[1] - yRange[0]);
                });

                // Build hover text with actual evenness values
                const entropyHoverText = smoothedEntropy.map((e, i) => {
                    const dateLabel = (data.date_labels || dates)[startIdx + i] || xVals[i];
                    return `<b>Distribution Evenness</b><br>` +
                           `Period: ${dateLabel}<br>` +
                           `Evenness: ${(e * 100).toFixed(1)}%<br>` +
                           `<span style="font-size:0.85em;color:#aaa;">Range: ${(eMin * 100).toFixed(1)}% – ${(eMax * 100).toFixed(1)}%</span>`;
                });

                overlayTraces.push({
                    x: xVals,
                    y: scaledEntropy,
                    type: 'scatter',
                    mode: 'lines',
                    line: { width: 12, shape: 'spline', color: 'rgba(255,255,255,0.12)' },
                    name: 'Evenness',
                    showlegend: false,
                    text: entropyHoverText,
                    hoverinfo: 'text',
                    hovertemplate: '%{text}<extra></extra>'
                });
                const cats = analysisData.categories || [];
                const selectedSet = new Set(this.timelineState.categoricalSelections[varName] || []);

                // Only overlay for selected categories
                cats.forEach((catData, cIdx) => {
                    if (!selectedSet.has(catData.id)) return;
                    const catColor = colors[Array.from(selectedSet).indexOf(catData.id) % colors.length];

                    // Trend line (dashed line from regression)
                    // Note: trend was computed on the FULL series — adjust intercept for sliced view
                    if (catData.trend && Math.abs(catData.trend.total_change) > 4) {
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

                    // Structural break (vertical dashed line)
                    // Adjust index for sliced view
                    const adjBreakIdx = catData.break ? catData.break.index - startIdx : -1;
                    if (catData.break && Math.abs(catData.break.delta) > 4 && adjBreakIdx > 0 && adjBreakIdx < xVals.length) {
                        shapes.push({
                            type: 'line',
                            x0: xVals[adjBreakIdx],
                            x1: xVals[adjBreakIdx],
                            y0: 0,
                            y1: 1,
                            yref: 'paper',
                            line: { color: catColor, width: 1.5, dash: 'dot' },
                        });
                    }
                });

                // Add overlay traces, shapes, and evenness annotation
                if (overlayTraces.length > 0) {
                    Plotly.addTraces(plotId, overlayTraces);
                }
                const relayoutUpdates = {};
                if (shapes.length > 0) {
                    relayoutUpdates.shapes = shapes;
                }
                if (validEntropy.length > 0) {
                    relayoutUpdates.annotations = [{
                        text: `Evenness: ${(eMin * 100).toFixed(1)}% – ${(eMax * 100).toFixed(1)}%`,
                        xref: 'paper', yref: 'paper',
                        x: 0, y: 1.02,
                        xanchor: 'left', yanchor: 'bottom',
                        showarrow: false,
                        font: { size: 10, color: 'rgba(255,255,255,0.4)' }
                    }];
                }
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
                    toggleBtn.style.cssText = 'margin-top: 10px; padding: 6px 14px; background: #2a2d31; color: #ccc; border: 1px solid #444; border-radius: 6px; cursor: pointer; font-size: 0.85em;';
                    const isOpen = this.timelineState.findingsPanelOpen && this.timelineState.findingsPanelOpen[varName];
                    toggleBtn.textContent = isOpen ? 'Hide Findings' : 'Show Findings';
                    chartWrapper.appendChild(toggleBtn);

                    const findingsContainer = document.createElement('div');
                    findingsContainer.id = `findings-panel-${varName}`;
                    findingsContainer.style.cssText = 'margin-top: 10px; border-top: 1px solid #333; padding-top: 15px; display: ' + (isOpen ? 'flex' : 'none') + '; flex-direction: column; gap: 10px;';

                    toggleBtn.addEventListener('click', () => {
                        if (!this.timelineState.findingsPanelOpen) this.timelineState.findingsPanelOpen = {};
                        const visible = findingsContainer.style.display !== 'none';
                        this.timelineState.findingsPanelOpen[varName] = !visible;
                        this.renderTimelineCharts();
                    });

                    catsList.forEach((catData, index) => {
                        if (!selectedSet.has(catData.id)) return;

                        const globalRank = index + 1;
                        const catColor = colors[Array.from(selectedSet).indexOf(catData.id) % colors.length];

                        const card = document.createElement('div');
                        card.style.cssText = 'background: #2a2d31; padding: 15px; border-radius: 8px; border-left: 4px solid ' + catColor + ';';

                        // Card Header: Rank + Dot + Label + Badges
                        const headerRow = document.createElement('div');
                        headerRow.style.cssText = 'display: flex; align-items: center; margin-bottom: 10px; flex-wrap: wrap; gap: 10px;';

                        const titleWrap = document.createElement('div');
                        titleWrap.style.cssText = 'font-weight: 600; font-size: 1.1em; color: #e1e1e1; margin-right: 15px;';
                        titleWrap.innerHTML = `<span style="color:#888; font-size:0.9em; margin-right:5px;">#${globalRank}</span> <span style="display:inline-block; width:10px; height:10px; border-radius:50%; background:${catColor}; margin-right:6px;"></span> ${catData.label}`;
                        headerRow.appendChild(titleWrap);

                        const bullets = [];

                        // Helper to make badge
                        const makeBadge = (text, bg, fg, border) => {
                            return `<span style="background:${bg}; color:${fg}; padding:3px 8px; border-radius:12px; font-size:0.75em; border:1px solid ${border}; font-weight:500; white-space:nowrap;">${text}</span>`;
                        };

                        let isStable = true;

                        // 1. Trend
                        if (catData.trend && Math.abs(catData.trend.total_change) > 4) {
                            isStable = false;
                            const isRising = catData.trend.total_change > 0;
                            if (isRising) {
                                headerRow.innerHTML += makeBadge('↑ Rising', 'rgba(78, 201, 176, 0.15)', '#4ec9b0', 'rgba(78, 201, 176, 0.5)');
                                bullets.push(`Overall upward trend — total shift of +${catData.trend.total_change} pp over the period.`);
                            } else {
                                headerRow.innerHTML += makeBadge('↓ Falling', 'rgba(244, 135, 113, 0.15)', '#f48771', 'rgba(244, 135, 113, 0.5)');
                                bullets.push(`Overall downward trend — total shift of ${catData.trend.total_change} pp over the period.`);
                            }
                        }

                        // 2. Step Change
                        if (catData.break && Math.abs(catData.break.delta) > 4) {
                            isStable = false;
                            headerRow.innerHTML += makeBadge('⋮ Step change', 'rgba(86, 156, 214, 0.15)', '#569cd6', 'rgba(86, 156, 214, 0.5)');

                            let bDate = "the period";
                            const bIdx = catData.break.index;
                            if (data.date_labels && bIdx >= 0 && bIdx < data.date_labels.length) {
                                 bDate = data.date_labels[bIdx];
                            } else if (data.dates && bIdx >= 0 && bIdx < data.dates.length) {
                                 bDate = data.dates[bIdx];
                            }
                            const dir = catData.break.delta > 0 ? "jumped" : "dropped";
                            const sign = catData.break.delta > 0 ? "+" : "";
                            bullets.push(`Step change around ${bDate}: share ${dir} from ~${catData.break.mean_before}% to ~${catData.break.mean_after}% (${sign}${catData.break.delta} pp).`);
                        }

                        // 3. Anomalies
                        if (catData.anomalies && catData.anomalies.length > 0) {
                            isStable = false;
                            headerRow.innerHTML += makeBadge(`◎ ${catData.anomalies.length} spike(s)`, 'rgba(206, 145, 120, 0.15)', '#ce9178', 'rgba(206, 145, 120, 0.5)');

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

                        // 4. Volatility
                        if (catData.volatility && catData.volatility.std > 2.5) {
                            isStable = false;
                            headerRow.innerHTML += makeBadge('~ Volatile', 'rgba(218, 112, 214, 0.15)', '#da70d6', 'rgba(218, 112, 214, 0.5)');
                            bullets.push(`High variation — standard dev ${catData.volatility.std} pp around mean of ${catData.volatility.mean}%.`);
                        }

                        // 5. Stable
                        if (isStable) {
                            headerRow.innerHTML += makeBadge('— Stable', 'rgba(120, 120, 120, 0.2)', '#aaa', '#555');
                            let m = catData.volatility ? catData.volatility.mean : "?";
                            bullets.push(`No significant dynamics detected. Steady around ${m}% with low variation.`);
                        }

                        card.appendChild(headerRow);

                        // Render Bullets
                        const ul = document.createElement('ul');
                        ul.style.cssText = 'margin: 0; padding-left: 20px; color: #ccc; font-size: 0.9em; line-height: 1.5;';
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
            // Collect all matching categories (in interestingness order)
            const matching = [];
            if (varAnalysis && Array.isArray(varAnalysis.categories)) {
                varAnalysis.categories.forEach(cd => {
                    const isRising = (cd.trend && cd.trend.total_change > 4);
                    const isFalling = (cd.trend && cd.trend.total_change < -4);
                    const hasSpikes = (cd.anomalies && cd.anomalies.length > 0);
                    const hasBreak = (cd.break && Math.abs(cd.break.delta) > 4);
                    const isVolatile = (cd.volatility && cd.volatility.std > 2.5);
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
                    btn.style.backgroundColor = '#28a745'; // Green success
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

    showPeriodStats: function (clickedDate) {
        if (!this.timelineData || !this.timelineData.dates) return;

        const dateIndex = this.timelineData.dates.indexOf(clickedDate);
        if (dateIndex === -1) return;

        // Find formatted label
        const formattedLabel = this.timelineData.date_labels ? this.timelineData.date_labels[dateIndex] : clickedDate;

        const modal = document.getElementById('timeline-stats-modal');
        const titleEl = document.getElementById('timeline-stats-title');
        const contentEl = document.getElementById('timeline-stats-content');

        if (!modal || !titleEl || !contentEl) return;

        let periodStr = clickedDate;

        // Save state for voting button
        this.currentStatsPeriod = periodStr;

        titleEl.innerText = `Stats for: ${formattedLabel}`;

        // Reset vote button state from previous clicks
        const btn = document.getElementById('timeline-vote-btn');
        if (btn) {
            btn.innerText = 'Vote to machine annotate';
            btn.style.backgroundColor = '#007bff';
            btn.disabled = false;
        }

        let html = '<table style="width: 100%; border-collapse: collapse; margin-top: 10px; color: #eee;">';
        html += '<thead style="border-bottom: 2px solid #555;"><tr><th style="padding: 8px; text-align: left;">Variable</th><th style="padding: 8px; text-align: left;">Value</th></tr></thead>';
        html += '<tbody>';

        // Use the ordered keys to keep machine_state at top and preserve schema priorities
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

        if (varKeys.includes('machine_state')) {
            varKeys = varKeys.filter(k => k !== 'machine_state');
            varKeys.push('machine_state');
        }

        varKeys.forEach(varName => {
            const varData = this.timelineData.variables[varName];
            const displayTitle = varData.display_name || (varName === 'machine_state' ? 'Scrape/Annotation States' : varName);

            html += `<tr style="border-bottom: 1px solid #333;"><td style="padding: 8px; vertical-align: top; font-weight: bold;">${displayTitle}</td>`;
            html += `<td style="padding: 8px; vertical-align: top;">`;

            if (varData.type === 'categorical' && varData.counts) {
                const dayCounts = varData.counts[dateIndex] || {};

                // Sort categories by count descending for this specific day
                const sortedCats = Object.keys(dayCounts).sort((a, b) => dayCounts[b] - dayCounts[a]);

                if (sortedCats.length === 0) {
                    html += `<span style="color: #888;">No Data</span>`;
                } else {
                    html += `<ul style="margin: 0; padding-left: 20px;">`;
                    sortedCats.forEach(cat => {
                        html += `<li>${cat}: <b>${dayCounts[cat]}</b></li>`;
                    });
                    html += `</ul>`;
                }
            } else if (varData.type === 'numeric' && varData.values) {
                let val = varData.values[dateIndex];
                if (val === null || val === undefined) {
                    html += `<span style="color: #888;">No Data</span>`;
                } else {
                    // Format numeric
                    if (Number.isInteger(val)) {
                        html += `<b>${val}</b>`;
                    } else {
                        html += `<b>${val.toFixed(2)}</b>`;
                    }
                }
            } else {
                html += `<span style="color: #888;">Unknown</span>`;
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
        window.timelines.init();
    }
});
