
// Timelines Tab Logic

window.timelines = {
    currentStudy: null,
    currentDonationId: null,
    donationList: [],
    timelineData: null,
    timelineState: {
        categoricalSelections: {},
        analysisToggles: {},
        smoothing: 1
    },

    init: async function () {
        console.log("Initializing Timelines Tab");
        // Ensure stats are loaded for the left panel Radar + Details
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
        // No study arg
        const listContainer = document.getElementById('timelines-donation-list');
        listContainer.innerHTML = '<div style="padding:10px;">Loading donations...</div>';

        try {
            // No body needed for POST if we just get all accepted
            const res = await fetch('/api/timelines/donations', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})
            });

            const data = await res.json();
            if (data.error) {
                listContainer.innerHTML = `<div style="padding:10px; color:red;">Error: ${data.error}</div>`;
                return;
            }

            this.donationList = data; // Array of objects
            this.renderDonationList();

        } catch (e) {
            console.error("Error loading donations:", e);
            listContainer.innerHTML = '<div style="padding:10px; color:red;">Failed to load donations.</div>';
        }
    },

    renderDonationList: function () {
        const queryInput = document.getElementById('timelines-donation-search');
        if (!queryInput) return;

        const query = queryInput.value.toLowerCase();
        const listContainer = document.getElementById('timelines-donation-list');
        listContainer.innerHTML = '';

        const simpleList = this.donationList.filter(d => {
            if (d.hidden) return false; // Hide collections flagged as hidden
            if (!query) return true;
            // Search by D_donation_id or display_donation_id
            const ddon = (d.D_donation_id || '').toLowerCase();
            const disp = (d.display_donation_id || '').toLowerCase();
            return ddon.includes(query) || disp.includes(query);
        });

        // Update the header with the count
        const headerTitle = document.getElementById('timelines-sidebar-header');
        if (headerTitle) {
            headerTitle.innerText = `Collection Timelines (${simpleList.length})`;
        }

        if (simpleList.length === 0) {
            listContainer.innerHTML = '<div style="padding:10px;">No matches found.</div>';
            return;
        }

        simpleList.forEach(d => {
            const item = document.createElement('div');
            item.className = 'donation-item';
            item.style.padding = '8px 10px';
            item.style.cursor = 'pointer';
            item.style.borderBottom = '1px solid #333';

            // Highlight selected
            if (this.currentDonationId && d.D_donation_id === this.currentDonationId) {
                item.style.background = '#007bff';
                item.style.color = 'white';
            } else {
                item.style.color = '#ccc';
                item.onmouseover = () => item.style.background = '#444';
                item.onmouseout = () => item.style.background = 'transparent';
            }

            // Display ID (custom) -> Filename (fallback)
            let label = d.display_donation_id;
            if (!label || label.trim() === '') {
                label = d.D_donation_id;
            }
            item.innerText = label;
            item.title = d.D_donation_id; // Add tooltip of original raw ID

            item.onclick = () => {
                this.selectDonation(d.D_donation_id);
                // Re-render to update highlight (lazy way)
                this.renderDonationList();
            };

            listContainer.appendChild(item);
        });

        // Auto-select first donation if none selected
        if (!this.currentDonationId && simpleList.length > 0) {
            this.selectDonation(simpleList[0].D_donation_id);
            // Re-render so the highlight applies to the newly auto-selected item
            this.renderDonationList();
        }
    },

    filterDonations: function () {
        this.renderDonationList();
    },

    onIntervalChange: function () {
        if (this.currentDonationId) {
            this.selectDonation(this.currentDonationId);
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
        return arr.map((_, i, a) => {
            const start = Math.max(0, i - window + 1);
            const slice = a.slice(start, i + 1);
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

        // --- Left Panel Details & Radar ---
        const detailsCard = document.getElementById('timelines-details-card');
        const radarCard = document.getElementById('timelines-radar-card');

        let pe_donation = null;
        if (window.pe_data && window.pe_data.length > 0) {
            pe_donation = window.pe_data.find(d => d.D_donation_id === donationId);
        }

        if (pe_donation) {
            if (detailsCard) detailsCard.classList.remove('hidden');
            if (radarCard) radarCard.style.display = 'block';

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

            // Radar
            const radarTitle = document.getElementById('timelines-radar-title');
            if (radarTitle) radarTitle.innerText = pe_donation.display_donation_id || pe_donation.D_donation_id || 'Radar Chart';
            if (typeof window.pe_renderRadar === 'function') {
                window.pe_renderRadar(pe_donation, null, 'timelines-radar-plot');
            }
        } else {
            // Hide if data not found
            if (detailsCard) detailsCard.classList.add('hidden');
            if (radarCard) radarCard.style.display = 'none';
        }

        const container = document.getElementById('timelines-charts-container');
        // Force resize of existing plots if any, just in case
        setTimeout(() => {
            window.dispatchEvent(new Event('resize'));
        }, 100);

        container.innerHTML = '<p>Loading timeline data...</p>';

        const intervalInput = document.querySelector('input[name="timeline-interval"]:checked');
        const interval = intervalInput ? intervalInput.value : 'day';

        try {
            const res = await fetch('/api/timelines/data', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    study: this.currentStudy,
                    donation_id: donationId,
                    interval: interval
                })
            });
            const data = await res.json();

            if (data.error) {
                container.innerHTML = `<p style="color:red;">Error: ${data.error}</p>`;
                return;
            }

            //console.log("TIMELINE DEBUG: Received Data", data);
            this.timelineData = data;

            // Update Aggregation Labels with Counts
            if (data.counts) {
                const dayLabel = document.getElementById('timelines-agg-label-day');
                const weekLabel = document.getElementById('timelines-agg-label-week');
                const monthLabel = document.getElementById('timelines-agg-label-month');
                if (dayLabel) dayLabel.innerText = `Day (${data.counts.day})`;
                if (weekLabel) weekLabel.innerText = `Week (${data.counts.week})`;
                if (monthLabel) monthLabel.innerText = `Month (${data.counts.month})`;
            }

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

            // Analysis toggle button for categorical charts
            if (varData.type === 'categorical' && data.analysis && data.analysis[varName]) {
                const toggleBtn = document.createElement('button');
                const isActive = this.timelineState.analysisToggles && this.timelineState.analysisToggles[varName];
                toggleBtn.innerHTML = '📊';
                toggleBtn.title = isActive ? 'Hide trend analysis' : 'Show trend analysis';
                toggleBtn.style.cssText = `background: ${isActive ? '#4ec9b0' : '#3c3c3c'}; border: 1px solid ${isActive ? '#4ec9b0' : '#555'}; color: ${isActive ? '#1e1e1e' : '#ccc'}; padding: 3px 8px; border-radius: 4px; cursor: pointer; font-size: 0.85em; margin-bottom: 10px; transition: all 0.2s;`;
                toggleBtn.onmouseenter = () => { if (!isActive) toggleBtn.style.background = '#505050'; };
                toggleBtn.onmouseleave = () => { if (!isActive) toggleBtn.style.background = '#3c3c3c'; };
                toggleBtn.onclick = () => {
                    if (!this.timelineState.analysisToggles) this.timelineState.analysisToggles = {};
                    this.timelineState.analysisToggles[varName] = !this.timelineState.analysisToggles[varName];
                    this.renderTimelineCharts();
                };
                titleDiv.appendChild(toggleBtn);

                // --- Summary Badge ---
                let badgeText = '— Stable';
                let badgeColor = 'rgba(120, 120, 120, 0.2)';
                let badgeTextColor = '#aaa';
                let badgeBorderColor = '#555';
                
                const anData = data.analysis[varName];
                let highestRise = {val: 0, cat: null};
                let lowestFall = {val: 0, cat: null};
                let hasAnomaly = false;
                let anomCount = 0;
                let highlightBreakCat = null;
                
                // Evaluate all categories for conditions
                for (let cat in anData) {
                    const cd = anData[cat];
                    if (!cd || cd.error) continue;
                    
                    if (cd.trend && cd.trend.total_change > highestRise.val) {
                        highestRise = {val: cd.trend.total_change, cat: cat};
                    }
                    if (cd.trend && cd.trend.total_change < lowestFall.val) {
                        lowestFall = {val: cd.trend.total_change, cat: cat};
                    }
                    if (cd.anomalies && cd.anomalies.length > 0) {
                        hasAnomaly = true;
                        anomCount += cd.anomalies.length;
                    }
                    if (cd.break && Math.abs(cd.break.delta) > 4 && !highlightBreakCat) {
                        highlightBreakCat = cat;
                    }
                }
                
                // Apply highest priority badge
                if (highestRise.val > 4) {
                    badgeText = `↑ Rising: ${highestRise.cat}`;
                    badgeColor = 'rgba(78, 201, 176, 0.15)';
                    badgeTextColor = '#4ec9b0';
                    badgeBorderColor = 'rgba(78, 201, 176, 0.5)';
                } else if (lowestFall.val < -4) {
                    badgeText = `↓ Falling: ${lowestFall.cat}`;
                    badgeColor = 'rgba(244, 135, 113, 0.15)';
                    badgeTextColor = '#f48771';
                    badgeBorderColor = 'rgba(244, 135, 113, 0.5)';
                } else if (hasAnomaly) {
                    badgeText = `◎ ${anomCount} spike(s)`;
                    badgeColor = 'rgba(206, 145, 120, 0.15)';
                    badgeTextColor = '#ce9178';
                    badgeBorderColor = 'rgba(206, 145, 120, 0.5)';
                } else if (highlightBreakCat) {
                    badgeText = `⋮ Step change: ${highlightBreakCat}`;
                    badgeColor = 'rgba(86, 156, 214, 0.15)';
                    badgeTextColor = '#569cd6';
                    badgeBorderColor = 'rgba(86, 156, 214, 0.5)';
                }
                
                const badge = document.createElement('span');
                badge.textContent = badgeText;
                badge.style.cssText = `background: ${badgeColor}; color: ${badgeTextColor}; padding: 3px 10px; border-radius: 12px; font-size: 0.75em; border: 1px solid ${badgeBorderColor}; font-weight: 500; margin-left: 10px; margin-bottom: 10px; white-space: nowrap;`;
                
                titleDiv.appendChild(badge);
            }
            chartWrapper.appendChild(titleDiv);

            // Container for Controls and Plot
            const innerFlexDiv = document.createElement('div');
            innerFlexDiv.style.display = 'flex';
            innerFlexDiv.style.flexDirection = 'row';

            // Left Menu for Categorical (or placeholder for alignment)
            const controlsDiv = document.createElement('div');
            controlsDiv.style.width = '200px';
            controlsDiv.style.flexShrink = '0';

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
                // Initialize or retrieve selected categories from state
                const defaultCats = varData.default_all ? (varData.top_categories || []) : (varData.top_categories ? varData.top_categories.slice(0, 3) : []);
                const selectedCats = this.timelineState.categoricalSelections[varName] || defaultCats;

                // Store in state if not already
                if (!this.timelineState.categoricalSelections[varName]) {
                    this.timelineState.categoricalSelections[varName] = selectedCats;
                }

                // Setup Sidebar Category Selection
                // Calculate total frequency for sorting (use sliced counts)
                const slicedCounts = startIdx > 0 ? varData.counts.slice(startIdx) : varData.counts;
                const slicedVideoCounts = startIdx > 0 && varData.daily_video_counts ? varData.daily_video_counts.slice(startIdx) : varData.daily_video_counts;
                const slicedValidCounts = startIdx > 0 && varData.daily_valid_counts ? varData.daily_valid_counts.slice(startIdx) : varData.daily_valid_counts;
                const catTotals = {};
                slicedCounts.forEach(day => {
                    Object.keys(day).forEach(c => {
                        catTotals[c] = (catTotals[c] || 0) + day[c];
                    });
                });

                const allCategories = Object.keys(catTotals).sort((a, b) => catTotals[b] - catTotals[a]);

                // Preserve scroll position if re-rendering an existing menu
                const catScrollPos = catScrollPositions[`controls-${plotId}`] || 0;

                // Keep UI container logic SAME
                // (This block recreates the checkboxes every time)
                // In production might want to only create once, but here we redraw all.
                controlsDiv.id = `controls-${plotId}`;
                controlsDiv.innerHTML = `
                    <div style="font-size:0.85em; margin-bottom:5px; color:#aaa;">Select Categories:</div>
                    <div style="max-height:300px; overflow-y:auto; border:1px solid #444; padding:5px;">
                        ${allCategories.map(cat => `
                            <div style="display:flex; align-items:flex-start; margin-bottom:3px;">
                                <input type="checkbox"
                                       value="${cat}"
                                       ${selectedCats.includes(cat) ? 'checked' : ''}
                                       onchange="window.timelines.toggleCategory('${varName}', '${cat}')"
                                       style="margin-right:5px; margin-top:2px;">
                                <span style="line-height:1.2;">
                                    ${cat} 
                                    <span style="color:#aaa; font-size:0.85em; white-space:nowrap;">(${catTotals[cat].toLocaleString()})</span>
                                </span>
                            </div>
                        `).join('')}
                    </div>
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

                    // Line trace
                    traces.push({
                        x: xVals,
                        y: displayY,
                        type: 'scatter',
                        mode: 'lines',
                        line: { width: 2, shape: 'spline', color: catColor },
                        fill: 'tozeroy',
                        fillcolor: catColor + '12',
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
                font: { color: '#ccc' },
                xaxis: {
                    type: excludeNoData ? 'category' : 'date',
                    tickmode: 'array',
                    tickvals: tickVals,
                    ticktext: tickText,
                    tickangle: 0
                },
                yaxis: {
                    title: yAxisTitle,
                    range: isCategorical && chartWrapper._catYRange ? chartWrapper._catYRange : undefined
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

            Plotly.newPlot(plotId, traces, layout, { displayModeBar: true, responsive: true });
            allPlotIds.push(plotId);

            // Render analysis overlays if toggled on
            if (isCategorical && data.analysis && data.analysis[varName]
                && this.timelineState.analysisToggles && this.timelineState.analysisToggles[varName]) {

                const analysisData = data.analysis[varName];
                const overlayTraces = [];
                const shapes = [];
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

                // Add overlay traces and shapes
                if (overlayTraces.length > 0) {
                    Plotly.addTraces(plotId, overlayTraces);
                }
                if (shapes.length > 0) {
                    Plotly.relayout(plotId, { shapes: shapes });
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

        // Determine formatted period string based on current active interval
        const intervalInput = document.querySelector('input[name="timeline-interval"]:checked');
        const interval = intervalInput ? intervalInput.value : 'day';

        let periodStr = clickedDate; // Day: 'YYYY-MM-DD'
        if (interval === 'month') {
            periodStr = clickedDate.substring(0, 7); // 'YYYY-MM'
        } else if (interval === 'week') {
            // For weeks, the backend date_label is 'YYYY-WW' (e.g., '2023-41')
            if (formattedLabel && /^\d{4}-\d{2}$/.test(formattedLabel)) {
                periodStr = formattedLabel.replace('-', '-W'); // '2023-W41'
            } else {
                periodStr = 'Week-of-' + clickedDate; // fallback
            }
        }

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

// --- Config Modal Logic for Timelines ---
window.timelines_openConfig = function () {
    const modal = document.getElementById('timelines-config-modal');
    const container = document.getElementById('timelines-config-checkboxes');
    if (!modal || !container) return;

    container.innerHTML = '';

    // Fallback if global is missing
    const PE_METRIC_INFO = window.PE_METRIC_INFO || {};
    // pe_radar_metrics in pe namespace
    const PE_RADAR_METRICS = JSON.parse(localStorage.getItem('pe_selected_radar_metrics') || '["chattiness", "enthusiasm", "patience", "binge_level", "consistency"]');

    Object.keys(PE_METRIC_INFO).sort().forEach(metric => {
        const info = PE_METRIC_INFO[metric];

        const label = document.createElement('label');
        label.className = 'config-checkbox';

        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = metric;
        cb.checked = PE_RADAR_METRICS.includes(metric);

        cb.onchange = window.timelines_updateRadarPreview;

        const span = document.createElement('span');
        span.innerText = info.label;

        label.appendChild(cb);
        label.appendChild(span);
        container.appendChild(label);
    });

    modal.classList.remove('hidden');
};

window.timelines_updateRadarPreview = function () {
    const container = document.getElementById('timelines-config-checkboxes');
    if (!container) return;

    const selected = [];
    container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
        if (cb.checked) selected.push(cb.value);
    });

    if (selected.length < 3 || selected.length > 8) return;

    if (typeof window.pe_calculatePercentileRanks === 'function') {
        window.pe_calculatePercentileRanks(selected);
    }

    if (window.timelines.currentDonationId && window.pe_data) {
        const donation = window.pe_data.find(d => d.D_donation_id === window.timelines.currentDonationId);
        if (donation && typeof window.pe_renderRadar === 'function') {
            window.pe_renderRadar(donation, selected, 'timelines-radar-plot');
        }
    }
};

window.timelines_closeConfig = function () {
    document.getElementById('timelines-config-modal').classList.add('hidden');

    if (window.timelines.currentDonationId && window.pe_data) {
        const donation = window.pe_data.find(d => d.D_donation_id === window.timelines.currentDonationId);
        if (donation && typeof window.pe_renderRadar === 'function') {
            // Re-render with null to use default/saved metrics
            window.pe_renderRadar(donation, null, 'timelines-radar-plot');
        }
    }
};

window.timelines_applyConfig = function () {
    const container = document.getElementById('timelines-config-checkboxes');
    if (!container) return;

    const selected = [];
    container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
        if (cb.checked) selected.push(cb.value);
    });

    if (selected.length < 3 || selected.length > 8) {
        alert("Please select between 3 and 8 metrics for the radar chart.");
        return;
    }

    localStorage.setItem('pe_selected_radar_metrics', JSON.stringify(selected));

    if (typeof window.pe_setRadarMetrics === 'function') {
        window.pe_setRadarMetrics(selected);
    }

    if (typeof window.pe_calculatePercentileRanks === 'function') {
        window.pe_calculatePercentileRanks(selected);
    }

    if (window.timelines.currentDonationId && window.pe_data) {
        const donation = window.pe_data.find(d => d.D_donation_id === window.timelines.currentDonationId);
        if (donation && typeof window.pe_renderRadar === 'function') {
            window.pe_renderRadar(donation, selected, 'timelines-radar-plot');
        }
    }

    window.timelines_closeConfig();

    // Also quietly update the PE Radar preview if it's currently rendered without switching tabs
    if (typeof window.pe_onShow === 'function') {
        // Just calling pe_onShow isn't reliable if we haven't clicked the tab, 
        // we can just let it handle itself on focus or force a redraw:
        const oldRadar = document.getElementById('pe-radar-plot');
        if (oldRadar && window.pe_selectedId === window.timelines.currentDonationId) {
            window.pe_renderRadar(donation, selected, 'pe-radar-plot');
        }
    }
};

// Initialize on load
document.addEventListener('DOMContentLoaded', () => {
    // If we are on a page with the timelines tab
    if (document.getElementById('timelines')) {
        window.timelines.init();
    }
});
