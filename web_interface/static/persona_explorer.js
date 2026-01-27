// State for Persona Explorer (window.pe_data for main.js access)
window.pe_data = window.pe_data || [];
let pe_data = window.pe_data;
let pe_selectedId = null;
let pe_percentileRanks = {};

// Metrics to display as strip plots - now dynamically loaded
let PE_METRICS = [
    'videos_per_day', 'avg_watch_time_s', 'avg_session_duration_s',
    'weekend_bias', 'activity_trend_slope', 'day_to_day_return_prob',
    'session_velocity_vpm', 'likes_per_video', 'consistency_top_2_hours'
];

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
    'share_owl': { label: 'Night Owl Share', tooltip: 'Proportion of activity late night (0-4)' },
};

// Default Metrics (fallback)
const PE_DEFAULT_METRICS = [
    'videos_per_day', 'avg_watch_time_s', 'avg_session_duration_s',
    'weekend_bias', 'activity_trend_slope', 'day_to_day_return_prob',
    'session_velocity_vpm', 'likes_per_video', 'consistency_top_2_hours'
];

function pe_loadSettings() {
    const saved = localStorage.getItem('pe_selected_metrics');
    if (saved) {
        try {
            const parsed = JSON.parse(saved);
            if (Array.isArray(parsed) && parsed.length > 0) {
                // Filter to ensure they actully exist in our registry
                const valid = parsed.filter(m => PE_METRIC_INFO[m]);
                if (valid.length > 0) {
                    PE_METRICS = valid;
                    return;
                }
            }
        } catch (e) {
            console.warn('Error loading PE settings', e);
        }
    }
    // Fallback
    PE_METRICS = [...PE_DEFAULT_METRICS];
}

function pe_saveSettings() {
    localStorage.setItem('pe_selected_metrics', JSON.stringify(PE_METRICS));
}

// Open the configuration modal
function pe_openConfig() {
    const modal = document.getElementById('pe-config-modal');
    const container = document.getElementById('pe-config-checkboxes');
    if (!modal || !container) return;

    container.innerHTML = '';

    // Create checkboxes for all available metrics
    Object.keys(PE_METRIC_INFO).forEach(metric => {
        const info = PE_METRIC_INFO[metric];

        const label = document.createElement('label');
        label.className = 'config-checkbox';

        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = metric;
        cb.checked = PE_METRICS.includes(metric);

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
}

function pe_applyConfig() {
    const container = document.getElementById('pe-config-checkboxes');
    if (!container) return;

    const selected = [];
    container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
        if (cb.checked) selected.push(cb.value);
    });

    if (selected.length === 0) {
        alert("Please select at least one metric.");
        return;
    }

    PE_METRICS = selected;
    pe_saveSettings();
    pe_renderAllStrips();
    pe_closeConfig();
}


// Radar chart metrics
const PE_RADAR_METRICS = ['chattiness', 'enthusiasm', 'patience', 'binge_level', 'consistency'];
const PE_RADAR_INFO = {
    'chattiness': { label: 'Chattiness', tooltip: 'Comments per video watched' },
    'enthusiasm': { label: 'Enthusiasm', tooltip: 'Likes per video watched' },
    'patience': { label: 'Patience', tooltip: 'Proportion of videos watched 30+ seconds' },
    'binge_level': { label: 'Binge Level', tooltip: 'Proportion of sessions 30+ minutes' },
    'consistency': { label: 'Consistency', tooltip: 'How concentrated activity is in peak hours' }
};

// Initialize when tab is shown - check for cached stats
function pe_init() {
    pe_loadSettings();

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
    console.log('Loading cached stats...');

    const container = document.getElementById('pe-strips-container');
    if (container) container.innerHTML = '<p style="text-align:center; color:#999;">Loading cached stats...</p>';

    fetch('/api/persona_stats_cached')
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
                container.innerHTML = `<p style="text-align:center; color:#999;">${data.error}</p>`;
                return;
            }
            pe_handleStatsData(data);
        })
        .catch(err => {
            console.error('Fetch error:', err);
            container.innerHTML = '<p style="text-align:center; color:#e63946;">Failed to load cached stats.</p>';
        });
}

function pe_handleStatsData(data) {
    console.log('Stats loaded:', data.length, 'donations');
    pe_data = data;
    window.pe_data = data;  // Keep window reference in sync

    // Update count display
    const countEl = document.getElementById('pe-stats-count');
    if (countEl) {
        countEl.innerText = `(${data.length} donations)`;
    }

    pe_calculatePercentileRanks();

    // Populate donation select
    const select = document.getElementById('pe-donation-select');
    select.innerHTML = '';
    data.forEach(d => {
        const option = document.createElement('option');
        option.value = d.D_donation_id;

        // Format D_id as D00000 if available
        let label = d.D_donation_id;
        if (d.D_id !== undefined && d.D_id !== null) {
            const idStr = String(d.D_id).padStart(5, '0');
            label = `D${idStr}`;
        }

        option.text = label;
        select.appendChild(option);
    });

    // Render all strip plots
    pe_renderAllStrips();

    // Select first donation
    if (data.length > 0) {
        pe_selectDonation(data[0].D_donation_id);
    }
}


function pe_calculatePercentileRanks() {
    if (!pe_data || pe_data.length === 0) return;

    PE_RADAR_METRICS.forEach(metric => {
        const valuesWithIds = pe_data.map(d => ({
            id: d.D_donation_id,
            value: d[metric] !== null && d[metric] !== undefined ? d[metric] : 0
        }));

        const sortedValues = [...valuesWithIds].sort((a, b) => a.value - b.value);
        if (!pe_percentileRanks[metric]) pe_percentileRanks[metric] = {};

        sortedValues.forEach((item, idx) => {
            pe_percentileRanks[metric][item.id] = idx / Math.max(1, sortedValues.length - 1);
        });
    });
}

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

function pe_selectDonation(donationId) {
    if (!pe_data) return;

    const donation = pe_data.find(d => d.D_donation_id === donationId);
    if (!donation) return;

    pe_selectedId = donationId;

    document.getElementById('pe-donation-select').value = donationId;
    document.getElementById('pe-details-card').classList.remove('hidden');
    document.getElementById('pe-details-id').innerText = donationId;
    document.getElementById('pe-details-moniker').innerText = donation.moniker || 'Unknown Persona';

    // Helper for missing values
    const orNotProvided = (v) => (v !== null && v !== undefined && v !== '') ? v : 'not provided';

    // Participant info
    document.getElementById('pe-stat-name').innerText = orNotProvided(donation.name);
    document.getElementById('pe-stat-email').innerText = orNotProvided(donation.email);
    document.getElementById('pe-stat-tiktok').innerText = orNotProvided(donation.tiktokHandle);
    document.getElementById('pe-stat-age').innerText = orNotProvided(donation.age);
    document.getElementById('pe-stat-country').innerText = orNotProvided(donation.country);
    document.getElementById('pe-stat-postcode').innerText = orNotProvided(donation.postCode);

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

    // Update radar chart
    pe_renderRadar(donation);

    // Update strip selection highlighting
    pe_updateStripSelection();
}

function pe_onDonationSelect() {
    const id = document.getElementById('pe-donation-select').value;
    pe_selectDonation(id);
}

function pe_renderRadar(d) {
    const labels = PE_RADAR_METRICS.map(m => PE_RADAR_INFO[m]?.label || m);
    const percentileValues = PE_RADAR_METRICS.map(m =>
        pe_percentileRanks[m] ? (pe_percentileRanks[m][d.D_donation_id] || 0) : 0
    );
    const hoverTexts = PE_RADAR_METRICS.map(m => PE_RADAR_INFO[m]?.tooltip || '');

    const donationTrace = {
        type: 'scatterpolar',
        r: percentileValues,
        theta: labels,
        fill: 'toself',
        fillcolor: 'rgba(74, 144, 217, 0.4)',
        line: { color: '#4a90d9', width: 2 },
        name: 'Selected',
        text: hoverTexts,
        hovertemplate: '<b>%{theta}</b><br>Percentile: %{r:.0%}<br><i>%{text}</i><extra></extra>'
    };

    const layout = {
        polar: {
            radialaxis: {
                visible: true,
                range: [0, 1],
                tickvals: [0, 0.25, 0.5, 0.75, 1],
                ticktext: ['0%', '25%', '50%', '75%', '100%']
            }
        },
        showlegend: false,
        margin: { t: 20, l: 50, r: 50, b: 30 }
    };

    Plotly.newPlot('pe-radar-plot', [donationTrace], layout, {
        responsive: true,
        staticPlot: true,
        displayModeBar: false
    });
}

// Auto-init on load if present
document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('pe-details-card')) {
        pe_init();
    }
});
