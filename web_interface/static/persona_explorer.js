// State for Persona Explorer (window.pe_data for main.js access)
window.pe_data = window.pe_data || [];
let pe_data = window.pe_data;
let pe_selectedId = null;
let pe_percentileRanks = {};

// Metrics to display as strip plots - now dynamically loaded
let PE_METRICS = []; // Will be populated from PE_METRIC_INFO

// Radar chart metrics - configurable
let PE_RADAR_METRICS = ['chattiness', 'enthusiasm', 'patience', 'binge_level', 'consistency'];

// Available metrics and their info
const PE_METRIC_INFO = {
    // Volume
    'total_events': { label: 'Total Events', tooltip: 'Total number of events recorded' },
    'num_comments': { label: 'Comments (Total)', tooltip: 'Total number of comments made' },
    'num_likes': { label: 'Likes (Total)', tooltip: 'Total number of videos liked' },
    'num_posts': { label: 'Posts (Total)', tooltip: 'Total number of videos posted' },

    // Rates
    'videos_per_day': { label: 'Videos/Day', tooltip: 'Average number of videos watched per day' },
    'comments_per_day': { label: 'Comments/Day', tooltip: 'Average comments per day' },
    'likes_per_day': { label: 'Likes/Day', tooltip: 'Average likes per day' },
    'emoji_rate': { label: 'Emoji Rate', tooltip: 'Rate of emojis used in comments' },

    // Ratios
    'likes_per_video': { label: 'Likes/Video', tooltip: 'Ratio of liked items to videos watched' },
    'weekend_bias': { label: 'Weekend Bias', tooltip: 'Ratio of weekend to weekday activity (>1 = more weekend)' },

    // Time & Consumption
    'avg_watch_time_s': { label: 'Avg Watch Time (s)', tooltip: 'Average seconds spent watching each video' },
    'median_watch_time_s': { label: 'Median Watch Time (s)', tooltip: 'Median seconds spent watching each video' },
    'daily_watch_time_s': { label: 'Daily Watch Time (s)', tooltip: 'Average seconds spent watching per day' },
    'total_watch_time_s': { label: 'Total Watch Time (s)', tooltip: 'Total seconds spent watching' },
    'num_watches': { label: 'Video Watches', tooltip: 'Total number of videos watched' },

    // Sessions
    'num_sessions': { label: 'Total Sessions', tooltip: 'Total number of distinct sessions' },
    'sessions_per_day': { label: 'Sessions/Day', tooltip: 'Average sessions per day (lifespan)' },
    'avg_session_duration_s': { label: 'Avg Session (s)', tooltip: 'Average session length in seconds' },
    'longest_session_s': { label: 'Longest Session (s)', tooltip: 'Length of the longest single session' },
    'session_velocity_vpm': { label: 'Session Velocity', tooltip: 'Videos per minute during sessions' },
    'day_to_day_return_prob': { label: 'Return Probability', tooltip: 'Probability of returning the next day' },
    'binge_level': { label: 'Binge Level', tooltip: 'Proportion of sessions > 30 mins' },

    // Core Metrics (Radar)
    'chattiness': { label: 'Chattiness', tooltip: 'Comments per video' },
    'enthusiasm': { label: 'Enthusiasm', tooltip: 'Likes per video' },
    'patience': { label: 'Patience', tooltip: 'Proportion of watches > 30s' },

    // Advanced / Pattern
    'activity_trend_slope': { label: 'Activity Trend', tooltip: 'Slope of activity over time (+ve = growing)' },
    'consistency_top_2_hours': { label: 'Hour Consistency', tooltip: 'Share of activity in top 2 hours' },
    'peak_activity_hour_local': { label: 'Peak Hour', tooltip: 'Hour of day with most activity' },
    'activity_consistency_cv': { label: 'Consistency CV', tooltip: 'Variation in activity (Lower = More Consistent)' },
    'avg_comment_len_chars': { label: 'Avg Comment Len', tooltip: 'Average length of comments in characters' },
    'emoji_level_log': { label: 'Emoji Level (Log)', tooltip: 'Log-scaled emoji usage intensity' },

    // Lifespan
    'active_days': { label: 'Active Days', tooltip: 'Number of days with any activity' },
    'lifespan_days': { label: 'Lifespan (Days)', tooltip: 'Days between first and last event' },
    'events_per_active_day': { label: 'Events/Active Day', tooltip: 'Average events only on days active' },

    // Time of Day Shares
    'share_morning': { label: 'Morning Share', tooltip: 'Proportion of activity in Morning (5-11)' },
    'share_afternoon': { label: 'Afternoon Share', tooltip: 'Proportion of activity in Afternoon (12-17)' },
    'share_evening': { label: 'Evening Share', tooltip: 'Proportion of activity in Evening (18-23)' },
    'share_evening': { label: 'Evening Share', tooltip: 'Proportion of activity in Evening (18-23)' },
    'share_owl': { label: 'Night Owl Share', tooltip: 'Proportion of activity late night (0-4)' },

    // Special Radar Metrics
    'consistency': { label: 'Consistency', tooltip: 'How concentrated activity is in peak hours' }
};

// Default Radar Metrics
const PE_DEFAULT_RADAR_METRICS = ['chattiness', 'enthusiasm', 'patience', 'binge_level', 'consistency'];

function pe_loadSettings() {
    // Load Radar Settings
    const savedRadar = localStorage.getItem('pe_selected_radar_metrics');
    let loadedRadar = false;

    // Reset to defaults first to be safe
    PE_METRICS = Object.keys(PE_METRIC_INFO); // Strip metrics = All

    if (savedRadar) {
        try {
            const parsed = JSON.parse(savedRadar);
            // Validate array and length
            if (Array.isArray(parsed) && parsed.length >= 3 && parsed.length <= 8) {
                // Validate metrics exist
                const valid = parsed.filter(m => PE_METRIC_INFO[m]);
                if (valid.length >= 3) {
                    PE_RADAR_METRICS = valid;
                    loadedRadar = true;
                }
            }
        } catch (e) { console.warn('Error loading PE Radar settings', e); }
    }

    if (!loadedRadar) {
        // Fallback to defaults
        PE_RADAR_METRICS = [...PE_DEFAULT_RADAR_METRICS];
        // Save defaults so subsequent localized loads work immediately
        pe_saveSettings();
    }
}

function pe_saveSettings() {
    localStorage.setItem('pe_selected_radar_metrics', JSON.stringify(PE_RADAR_METRICS));
}

// Open the configuration modal
function pe_openConfig() {
    const modal = document.getElementById('pe-config-modal');
    const container = document.getElementById('pe-config-checkboxes');
    if (!modal || !container) return;

    container.innerHTML = '';

    container.innerHTML = '';

    // Create checkboxes for all available metrics
    Object.keys(PE_METRIC_INFO).sort().forEach(metric => {
        const info = PE_METRIC_INFO[metric];

        const label = document.createElement('label');
        label.className = 'config-checkbox';

        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = metric;
        cb.checked = PE_RADAR_METRICS.includes(metric);

        // Live Preview: Update chart immediately on toggle
        cb.onchange = pe_updateRadarPreview;

        const span = document.createElement('span');
        span.innerText = info.label;

        label.appendChild(cb);
        label.appendChild(span);
        container.appendChild(label);
    });

    modal.classList.remove('hidden');
}

function pe_closeConfig() {
    document.getElementById('pe-config-modal').classList.add('hidden');

    // Restore render to saved settings (cancels preview if not applied)
    if (pe_selectedId && pe_data) {
        const donation = pe_data.find(d => d.D_donation_id === pe_selectedId);
        if (donation) pe_renderRadar(donation);
    }
}

function pe_updateRadarPreview() {
    const container = document.getElementById('pe-config-checkboxes');
    if (!container) return;

    const selected = [];
    container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
        if (cb.checked) selected.push(cb.value);
    });

    if (selected.length < 3 || selected.length > 8) {
        // Optional: Visual warning? For now just don't render invalid state
        return;
    }

    // Calculate ranks for temporary metrics
    pe_calculatePercentileRanks(selected);

    // Render with temporary metrics
    if (pe_selectedId && pe_data) {
        const donation = pe_data.find(d => d.D_donation_id === pe_selectedId);
        if (donation) pe_renderRadar(donation, selected);
    }
}
window.pe_updateRadarPreview = pe_updateRadarPreview;

function pe_applyConfig() {
    const container = document.getElementById('pe-config-checkboxes');
    if (!container) return;

    const selected = [];
    container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
        if (cb.checked) selected.push(cb.value);
    });

    if (selected.length < 3 || selected.length > 8) {
        alert("Please select between 3 and 8 metrics for the radar chart.");
        return;
    }

    PE_RADAR_METRICS = selected;
    pe_saveSettings();
    pe_calculatePercentileRanks(); // Recalculate ranks for new metrics if needed

    // Re-render radar if a donation is selected
    if (pe_selectedId && pe_data) {
        const donation = pe_data.find(d => d.D_donation_id === pe_selectedId);
        if (donation) pe_renderRadar(donation);
    }

    // We don't need to call pe_closeConfig here if we want to follow standard flow, 
    // but the button says "Apply Changes", implying close. 
    // pe_closeConfig() will re-render, which is redundant but safe.
    // To avoid double render, we can just hide it manually or let it be.
    // Let's just call close, the double render is negligible.
    pe_closeConfig();
}

window.pe_setRadarMetrics = function (metrics) {
    PE_RADAR_METRICS = metrics;
};


// Radar chart metrics
// const PE_RADAR_METRICS = ... (Moved to top)
// const PE_RADAR_INFO = ... (Merged into PE_METRIC_INFO)

// Initialize when tab is shown - check for cached stats
// pe_onShow logic called by main.js
function pe_onShow() {
    // If not initialized, init
    if (!window.pe_data || window.pe_data.length === 0) {
        if (typeof pe_init === 'function') pe_init();
        return;
    }

    // Force resize/redraw of plots
    // Force redraw to ensure layout updates (margins) and data is fresh
    if (pe_selectedId && pe_data) {
        const donation = pe_data.find(d => d.D_donation_id === pe_selectedId);
        if (donation) pe_renderRadar(donation);
    } else {
        // Fallback if no selection yet
        const radarDiv = document.getElementById('pe-radar-plot');
        if (radarDiv) Plotly.Plots.resize(radarDiv);
    }
}
// Expose pe_onShow
window.pe_onShow = pe_onShow;


function pe_init() {
    pe_loadSettings();

    // Setup Search Listener
    const searchInput = document.getElementById('pe-donation-search');
    if (searchInput) {
        searchInput.addEventListener('input', pe_filterDonationList);
    } else {
        console.warn("pe_init: Search input not found");
    }

    // Fetch stats info to update timestamp display
    fetch('/api/persona_stats_info')
        .then(response => response.json())
        .then(info => {
            const timestampEl = document.getElementById('pe-stats-timestamp');
            if (info.exists && info.timestamp) {
                timestampEl.innerText = `Stats from: ${info.timestamp}`;
                // If cached stats exist, load them automatically
                pe_loadCachedStats();
            } else {
                timestampEl.innerText = 'No cached stats found.';
            }
        })
        .catch(err => console.error('Error fetching stats info:', err));
}

function pe_loadCachedStats() {
    // console.log('Loading cached stats...');

    const container = document.getElementById('pe-strips-container');
    if (container) container.innerHTML = '<p style="text-align:center; color:#999;">Loading cached stats...</p>';

    return fetch('/api/persona_stats_cached')
        .then(response => {
            // Extract MTime header
            const mtimeHeader = response.headers.get('X-Metadata-MTime');
            if (mtimeHeader) {
                const mtime = new Date(mtimeHeader);
                const timestampEl = document.getElementById('pe-stats-timestamp');
                if (timestampEl) {
                    timestampEl.innerText = `Stats from: ${mtime.toLocaleDateString('en-AU', { day: '2-digit', month: 'short', year: 'numeric' })} ${mtime.toLocaleTimeString('en-AU', { hour: '2-digit', minute: '2-digit' })}`;
                }
            } else {
                const timestampEl = document.getElementById('pe-stats-timestamp');
                if (timestampEl) timestampEl.innerText = '';
            }
            return response.json();
        })
        .then(data => {
            if (data.error) {
                if (container) container.innerHTML = `<p style="text-align:center; color:#999;">${data.error}</p>`;
                return;
            }
            pe_handleStatsData(data);
        })
        .catch(err => {
            console.error('Fetch error:', err);
            if (container) container.innerHTML = '<p style="text-align:center; color:#e63946;">Failed to load cached stats.</p>';
        });
}
window.pe_loadCachedStats = pe_loadCachedStats;

function pe_handleStatsData(data) {
    // console.log('Stats loaded:', data.length, 'donations');
    pe_data = data;
    window.pe_data = data;  // Keep window reference in sync

    // Update count display
    const countEl = document.getElementById('pe-stats-count');
    if (countEl) {
        countEl.innerText = `(${data.length} donations)`;
    }

    // Fix potential serialization issues with annotation_tags being strings
    pe_data.forEach(d => {
        if (typeof d.annotation_tags === 'string') {
            try {
                if (d.annotation_tags.startsWith('[')) {
                    d.annotation_tags = JSON.parse(d.annotation_tags.replace(/'/g, '"'));
                } else {
                    d.annotation_tags = [d.annotation_tags];
                }
            } catch (e) {
                console.warn('Failed to parse annotation_tags string:', d.annotation_tags);
                d.annotation_tags = [];
            }
        }
        if (!Array.isArray(d.annotation_tags)) d.annotation_tags = [];
    });

    pe_calculatePercentileRanks();

    // Populate Donation List (Timelines Style)
    pe_renderDonationList();

    // Render all strip plots
    pe_renderAllStrips();

    // Select first donation
    if (data.length > 0) {
        pe_selectDonation(data[0].D_donation_id);
    }
}


function pe_calculatePercentileRanks(metrics = null) {
    if (!pe_data || pe_data.length === 0) return;

    // Use provided metrics or global default
    const targetMetrics = metrics || PE_RADAR_METRICS;

    // Ensure metrics exist - Fallback to defaults if empty and global
    if (!metrics && (!targetMetrics || targetMetrics.length === 0)) {
        console.warn("PE_RADAR_METRICS empty, using default.");
        PE_RADAR_METRICS = [...PE_DEFAULT_RADAR_METRICS];
        return pe_calculatePercentileRanks(); // Retry
    }

    // Reset ranks for target metrics
    targetMetrics.forEach(metric => {
        pe_percentileRanks[metric] = {};
    });

    targetMetrics.forEach(metric => {
        const valuesWithIds = pe_data.map(d => {
            let val = d[metric];
            // Safe number parsing
            if (val === null || val === undefined || val === '') val = 0;
            val = parseFloat(val);
            if (isNaN(val)) val = 0;
            return {
                id: d.D_donation_id,
                value: val
            };
        });

        const sortedValues = [...valuesWithIds].sort((a, b) => a.value - b.value);
        const n = sortedValues.length;

        sortedValues.forEach((item, idx) => {
            // Percentile rank: 0 to 1
            pe_percentileRanks[metric][item.id] = n > 1 ? idx / (n - 1) : 0.5;
        });
    });
}
window.pe_calculatePercentileRanks = pe_calculatePercentileRanks;

function pe_renderRadar(donation, metrics = null, targetDivId = 'pe-radar-plot') {
    if (!donation) return;

    const targetMetrics = metrics || PE_RADAR_METRICS;

    // Ensure ranks are calculated if missing (backup safety)
    if (Object.keys(pe_percentileRanks).length === 0) {
        pe_calculatePercentileRanks(targetMetrics);
    }

    // Check if we need to calculate specifically for these metrics (if not in cache)
    // Simple check: take first metric
    if (targetMetrics.length > 0 && !pe_percentileRanks[targetMetrics[0]]) {
        pe_calculatePercentileRanks(targetMetrics);
    }

    // Calculate values (ranks)
    const rValues = targetMetrics.map(m => {
        const ranks = pe_percentileRanks[m];
        if (!ranks) return 0;
        return ranks[donation.D_donation_id] || 0;
    });

    // Close the loop
    const rPlot = [...rValues, rValues[0]];
    const thetaPlot = targetMetrics.map(m => (PE_METRIC_INFO[m] ? PE_METRIC_INFO[m].label : m));
    const theta = [...thetaPlot, thetaPlot[0]];

    const data = [{
        type: 'scatterpolar',
        r: rPlot,
        theta: theta,
        fill: 'toself',
        name: donation.moniker || 'Donation',
        line: { color: '#4ec9b0' }
    }];

    const layout = {
        polar: {
            radialaxis: {
                visible: true,
                range: [0, 1],
                tickfont: { size: 8, color: '#888' },
                // Fix grid colors
                gridcolor: '#444',
                linecolor: '#444'
            },
            angularaxis: {
                tickfont: { size: 10, color: '#d4d4d4' },
                gridcolor: '#444',
                linecolor: '#444'
            },
            bgcolor: '#252526',
        },
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        showlegend: false,
        margin: { t: 30, b: 30, l: 30, r: 30 },
        height: 300,
        font: { color: '#d4d4d4', size: 10 }
    };

    // Use Plotly.react for efficient updates (handles newPlot vs update automatically)
    Plotly.react(targetDivId, data, layout, { displayModeBar: false, responsive: true });
}
window.pe_renderRadar = pe_renderRadar;

function pe_renderAllStrips() {
    const container = document.getElementById('pe-strips-container');
    if (!container) return;

    container.innerHTML = '';

    PE_METRICS.forEach(metric => {
        const stripRow = pe_createStrip(metric);
        container.appendChild(stripRow);
    });
}

function pe_createStrip(metric) {
    const info = PE_METRIC_INFO[metric] || { label: metric, tooltip: '' };

    // Sort data by this metric
    const sortedData = [...pe_data].sort((a, b) => {
        const aVal = a[metric] !== null && a[metric] !== undefined ? a[metric] : 0;
        const bVal = b[metric] !== null && b[metric] !== undefined ? b[metric] : 0;
        return aVal - bVal;
    });

    // Get min/max for axis labels
    const minVal = sortedData.length > 0 ? sortedData[0][metric] || 0 : 0;
    const maxVal = sortedData.length > 0 ? sortedData[sortedData.length - 1][metric] || 0 : 0;

    // Create row container
    const row = document.createElement('div');
    row.className = 'strip-row';
    row.dataset.metric = metric;

    // Header with label and selected value
    const header = document.createElement('div');
    header.className = 'strip-header';

    const label = document.createElement('span');
    label.className = 'strip-label';
    label.innerText = info.label;
    label.title = info.tooltip;
    header.appendChild(label);

    const selectedValue = document.createElement('span');
    selectedValue.className = 'strip-selected-value';
    selectedValue.id = `pe-strip-value-${metric}`;
    header.appendChild(selectedValue);

    row.appendChild(header);

    // Boxes container
    const boxesContainer = document.createElement('div');
    boxesContainer.className = 'strip-boxes';

    sortedData.forEach(d => {
        const box = document.createElement('div');
        box.className = 'strip-box';
        box.dataset.donationId = d.D_donation_id;
        box.title = `${d.D_donation_id}\nValue: ${pe_formatValue(d[metric])}`;

        if (d.D_donation_id === pe_selectedId) {
            box.classList.add('selected');
        }

        box.addEventListener('click', () => {
            pe_selectDonation(d.D_donation_id);
        });

        boxesContainer.appendChild(box);
    });

    row.appendChild(boxesContainer);

    // Axis labels (5 labels: min, 25%, 50%, 75%, max)
    const axis = document.createElement('div');
    axis.className = 'strip-axis';

    const range = maxVal - minVal;
    const axisVals = [minVal, minVal + range * 0.25, minVal + range * 0.5, minVal + range * 0.75, maxVal];
    axisVals.forEach(v => {
        const span = document.createElement('span');
        span.innerText = pe_formatValue(v);
        axis.appendChild(span);
    });

    row.appendChild(axis);

    return row;
}

function pe_formatValue(v) {
    if (v === null || v === undefined) return 'N/A';
    if (Math.abs(v) >= 10000) return (v / 1000).toFixed(0) + 'k';
    if (Math.abs(v) >= 1000) return (v / 1000).toFixed(1) + 'k';
    if (Math.abs(v) >= 100) return v.toFixed(0);
    if (Math.abs(v) >= 10) return v.toFixed(1);
    if (Math.abs(v) >= 1) return v.toFixed(2);
    return v.toFixed(3);
}
window.pe_formatValue = pe_formatValue;
window.PE_METRIC_INFO = PE_METRIC_INFO;

function pe_updateStripSelection() {
    // Update selected class on all boxes
    document.querySelectorAll('.strip-box').forEach(box => {
        if (box.dataset.donationId === pe_selectedId) {
            box.classList.add('selected');
        } else {
            box.classList.remove('selected');
        }
    });

    // Update selected value display for each metric
    if (pe_selectedId) {
        const donation = pe_data.find(d => d.D_donation_id === pe_selectedId);
        if (donation) {
            PE_METRICS.forEach(metric => {
                const valueEl = document.getElementById(`pe-strip-value-${metric}`);
                if (valueEl) {
                    const val = donation[metric];
                    valueEl.innerText = `Selected: ${pe_formatValue(val)}`;
                }
            });
        }
    }
}


// --- Annotations Logic ---

// function pe_onDonationSelect() { ... } // Removed

function pe_filterDonationList() {
    const input = document.getElementById('pe-donation-search');
    if (!input) {
        console.error("PE Search: Input element not found");
        return;
    }
    const query = input.value.trim().toLowerCase();
    // console.log("PE Search Query:", query);
    pe_renderDonationList(query);
}
// Expose to window to ensure HTML onkeyup can find it
window.pe_filterDonationList = pe_filterDonationList;

function pe_renderDonationList(query = "") {
    const listContainer = document.getElementById('pe-donation-list');
    if (!listContainer) return;

    listContainer.innerHTML = '';

    if (!pe_data || pe_data.length === 0) {
        listContainer.innerHTML = '<div style="padding:10px; color:#777;">No donations found.</div>';
        return;
    }

    const filtered = pe_data.filter(d => {
        if (!query) return true;
        // Robust checking
        //const did = String(d.D_id || '').toLowerCase();
        const ddon = String(d.D_donation_id || '').toLowerCase();
        const display = String(d.display_donation_id || '').toLowerCase();

        return ddon.includes(query) || display.includes(query);
    });

    if (filtered.length === 0) {
        listContainer.innerHTML = '<div style="padding:10px; color:#777;">No matches.</div>';
        return;
    }

    filtered.forEach(d => {
        const item = document.createElement('div');
        item.style.padding = '8px 10px';
        item.style.cursor = 'pointer';
        item.style.borderBottom = '1px solid #333';
        item.style.color = '#ccc';

        if (d.D_donation_id === pe_selectedId) {
            item.style.background = '#007acc'; // Blue selection
            item.style.color = 'white';
        } else {
            // Hover effect handled by CSS or inline
            item.onmouseover = () => { if (d.D_donation_id !== pe_selectedId) item.style.background = '#333'; }
            item.onmouseout = () => { if (d.D_donation_id !== pe_selectedId) item.style.background = 'transparent'; }
        }

        let label = d.display_donation_id;
        if (!label || label.trim() === '') {
            label = d.D_donation_id;
        }
        item.innerText = label;

        item.onclick = () => {
            pe_selectDonation(d.D_donation_id);
            pe_renderDonationList(query); // Re-render to update selection highlight
        };

        listContainer.appendChild(item);
    });
}

function pe_selectDonation(donationId) {
    if (!pe_data) return;

    const donation = pe_data.find(d => d.D_donation_id === donationId);
    if (!donation) return;

    pe_selectedId = donationId;

    // Update Searchable List Selection (if not already handled by re-render)
    // Document.getElementById('pe-donation-select').value = donationId; // Removed
    document.getElementById('pe-details-card').classList.remove('hidden');
    document.getElementById('pe-details-id').innerText = donationId;
    document.getElementById('pe-details-moniker').innerText = donation.moniker || 'Unknown Persona';

    // Populate Annotations
    const annotDisplayId = document.getElementById('pe-annot-display-id');
    if (annotDisplayId) {
        // Default to D_donation_id if display ID is empty
        annotDisplayId.value = donation.display_donation_id || donation.D_donation_id || '';
    }

    // Initialize Tags
    pe_renderTags();

    // Helper for missing values
    const orNotProvided = (v) => (v !== null && v !== undefined && v !== '') ? v : 'not provided';

    // Participant info - Hide missing items
    const pInfoSection = document.getElementById('pe-participant-info-section');
    let visibleInfoCount = 0;

    const updateInfoStat = (elementId, value) => {
        const el = document.getElementById(elementId);
        if (!el) return;

        const li = el.closest('li');
        // Check if value is "meaningful" (not null, undefined, or empty string)
        if (value !== null && value !== undefined && value !== '') {
            el.innerText = value;
            if (li) li.style.display = 'flex';
            visibleInfoCount++;
        } else {
            if (li) li.style.display = 'none';
        }
    };

    updateInfoStat('pe-stat-name', donation.name);
    updateInfoStat('pe-stat-email', donation.email);
    updateInfoStat('pe-stat-tiktok', donation.tiktokHandle);
    updateInfoStat('pe-stat-age', donation.age);
    updateInfoStat('pe-stat-country', donation.country);
    updateInfoStat('pe-stat-postcode', donation.postCode);

    // Hide entire section if no info
    if (pInfoSection) {
        pInfoSection.style.display = visibleInfoCount > 0 ? 'block' : 'none';
    }

    // Donation date formatting
    const fmtDate = (ts) => {
        if (!ts) return 'not provided';
        const d = new Date(ts);
        return d.toLocaleDateString('en-AU', { day: '2-digit', month: 'short', year: 'numeric' });
    };
    document.getElementById('pe-stat-donation-date').innerText = fmtDate(donation.date);

    // Activity stats
    const tz = donation.inferred_tz_offset;
    const tzStr = tz !== null && tz !== undefined
        ? `UTC${tz >= 0 ? '+' : ''}${tz}`
        : 'Unknown';
    document.getElementById('pe-stat-timezone').innerText = tzStr;



    document.getElementById('pe-stat-active-days').innerText = donation.active_days || 0;
    document.getElementById('pe-stat-total-events').innerText = (donation.total_events || 0).toLocaleString();
    document.getElementById('pe-stat-peak-segment').innerText = donation.peak_day_segment || 'Unknown';

    const watchHours = ((donation.total_watch_time_s || 0) / 3600).toFixed(1);
    document.getElementById('pe-stat-watch-time').innerText = `${watchHours} hrs`;

    document.getElementById('pe-stat-first-event').innerText = fmtDate(donation.first_event_ts);
    document.getElementById('pe-stat-last-event').innerText = fmtDate(donation.last_event_ts);


    // Update Radar Title
    const radarTitle = document.getElementById('pe-radar-title');
    if (radarTitle) {
        radarTitle.innerText = donation.display_donation_id || donation.D_donation_id || 'Radar Chart';
    }

    // Render Radar Chart
    pe_renderRadar(donation);

    // Update strip selection highlighting
    pe_updateStripSelection();
}

// Tag Management
function pe_renderTags() {
    if (!pe_selectedId || !pe_data) return;
    const donation = pe_data.find(d => d.D_donation_id === pe_selectedId);
    if (!donation) return;

    // Defensive: Ensure annotation_tags is an array
    const currentTags = Array.isArray(donation.annotation_tags) ? donation.annotation_tags : [];

    // Collect ALL tags from ALL donations
    const allTagsSet = new Set();
    pe_data.forEach(d => {
        if (d.annotation_tags && Array.isArray(d.annotation_tags)) {
            d.annotation_tags.forEach(t => allTagsSet.add(t));
        }
    });

    // Add current tags to set just in case (e.g. newly added one)
    currentTags.forEach(t => allTagsSet.add(t));

    // 1. Text Display (For Activity Stats - All Users)
    const textDisplay = document.getElementById('pe-stat-tags');
    if (textDisplay) {
        if (currentTags.length > 0) {
            textDisplay.innerText = currentTags.join(', ');
            // Ensure parent li is visible (if hidden logic applies)
            const li = textDisplay.closest('li');
            if (li) li.style.display = 'flex';
        } else {
            textDisplay.innerText = ''; // or 'None'
            // Ensure parent li is HIDDEN if no tags (cleaner)
            const li = textDisplay.closest('li');
            if (li) li.style.display = 'none';
        }
    }

    // 2. Chip Display (For Admin Management - Only if container exists)
    const container = document.getElementById('pe-annot-tags-container');
    if (!container) return; // Not an admin or element missing

    container.innerHTML = '';
    const allTags = Array.from(allTagsSet).sort();

    allTags.forEach(tag => {
        const isSelected = currentTags.includes(tag);
        const chip = document.createElement('div');

        // Style based on state
        // Gray = Unselected (#444 bg, #555 border)
        // Blue/Green = Selected (#007acc bg, #009ce6 border)
        const bg = isSelected ? '#007acc' : '#444';
        const border = isSelected ? '1px solid #009ce6' : '1px solid #555';

        chip.style.cssText = `
            background: ${bg};
            color: white;
            border: ${border};
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 0.85em;
            cursor: pointer;
            user-select: none;
            transition: all 0.1s;
        `;

        chip.textContent = tag;

        chip.onclick = () => {
            pe_toggleTag(tag);
        };

        container.appendChild(chip);
    });
}

function pe_toggleTag(tag) {
    if (!pe_selectedId || !pe_data) return;
    const donation = pe_data.find(d => d.D_donation_id === pe_selectedId);
    if (!donation) return;

    // Ensure array exists
    if (!Array.isArray(donation.annotation_tags)) donation.annotation_tags = [];

    const idx = donation.annotation_tags.indexOf(tag);
    if (idx !== -1) {
        // Remove
        donation.annotation_tags.splice(idx, 1);
    } else {
        // Add
        donation.annotation_tags.push(tag);
    }
    // console.log(`[PE] Toggled ${tag}. New tags:`, donation.annotation_tags);
    pe_renderTags();
}

function pe_addNewTag() {
    const input = document.getElementById('pe-annot-new-tag');
    if (!input) return;

    const val = input.value.trim();
    if (!val) return;

    // Split by comma in case user pasted "tag1, tag2"
    const newTags = val.split(',').map(t => t.trim()).filter(t => t.length > 0);

    if (newTags.length > 0) {
        if (!pe_selectedId || !pe_data) return;
        const donation = pe_data.find(d => d.D_donation_id === pe_selectedId);
        if (!donation) return;

        if (!Array.isArray(donation.annotation_tags)) donation.annotation_tags = [];

        newTags.forEach(tag => {
            if (!donation.annotation_tags.includes(tag)) {
                donation.annotation_tags.push(tag);
            }
        });

        input.value = '';
        // console.log(`[PE] Added tags. New tags:`, donation.annotation_tags);
        pe_renderTags();
    }
}


function pe_saveAnnotation() {
    if (!pe_selectedId) return;

    const displayIdInput = document.getElementById('pe-annot-display-id');
    // Using donation.annotation_tags which is updated in real-time by UI
    const donation = pe_data.find(d => d.D_donation_id === pe_selectedId);
    if (!donation) return;

    if (!displayIdInput) return;

    const displayId = displayIdInput.value;
    const tags = donation.annotation_tags || [];

    const payload = {
        donation_id: pe_selectedId,
        display_donation_id: displayId,
        tags: tags
    };

    // Find the button to show feedback
    const btn = document.querySelector('button[onclick="pe_saveAnnotation()"]');
    if (btn) btn.disabled = true;

    fetch('/api/donation/annotate', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(payload)
    })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'success') {
                const btn = document.querySelector('button[onclick="pe_saveAnnotation()"]');

                // Update local state in pe_data (already updated, but good to confirm)
                if (pe_data) {
                    const don = pe_data.find(d => d.D_donation_id === pe_selectedId);
                    if (don) {
                        don.display_donation_id = displayId;
                        don.annotation_tags = tags;
                    }
                }

                // Visual feedback
                if (btn) {
                    const originalHTML = '<i class="fas fa-save"></i> Save Annotations';
                    btn.innerHTML = '<i class="fas fa-check"></i> Saved!';
                    btn.classList.add('success');
                    // Assuming .success class exists or inline style
                    btn.style.backgroundColor = '#2e7d32'; // Dark green

                    setTimeout(() => {
                        btn.innerHTML = originalHTML;
                        btn.style.backgroundColor = '';
                        btn.classList.remove('success');
                        btn.disabled = false;
                    }, 1500);
                }
            } else {
                const btn = document.querySelector('button[onclick="pe_saveAnnotation()"]');
                if (btn) btn.disabled = false;
                alert('Error: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(err => {
            console.error('Error saving annotations:', err);
            const btn = document.querySelector('button[onclick="pe_saveAnnotation()"]');
            if (btn) btn.disabled = false;
            alert('Error saving annotations. Check console.');
        });
}
