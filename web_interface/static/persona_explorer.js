// State for Persona Explorer
let pe_data = [];
let pe_selectedId = null;
let pe_percentileRanks = {};

// Metrics to display in histogram grid
const PE_METRICS = [
    'videos_per_day', 'avg_watch_time_s', 'avg_session_duration_s',
    'weekend_bias', 'activity_trend_slope', 'day_to_day_return_prob',
    'session_velocity_vpm', 'likes_per_video', 'consistency_top_2_hours'
];

// Metrics that benefit from log transform (right-skewed distributions)
const PE_LOG_METRICS = [
    'videos_per_day', 'avg_watch_time_s', 'avg_session_duration_s',
    'likes_per_video'
];

// Metric labels and tooltips with definitions
const PE_METRIC_INFO = {
    'videos_per_day': {
        label: 'Videos/Day (log)',
        tooltip: 'Average number of videos watched per day of activity. Log-transformed for better visualization.'
    },
    'avg_watch_time_s': {
        label: 'Avg Watch Time (log)',
        tooltip: 'Average duration in seconds spent watching each video. Log-transformed.'
    },
    'avg_session_duration_s': {
        label: 'Avg Session (log)',
        tooltip: 'Average length of a browsing session in seconds. Sessions are separated by 15+ min gaps.'
    },
    'weekend_bias': {
        label: 'Weekend Bias',
        tooltip: 'Ratio of weekend to weekday activity. >1 means more active on weekends.'
    },
    'activity_trend_slope': {
        label: 'Activity Trend',
        tooltip: 'Slope of daily activity over time. Positive = increasing usage, negative = decreasing.'
    },
    'day_to_day_return_prob': {
        label: 'Return Probability',
        tooltip: 'Probability of being active on day D+1 given activity on day D.'
    },
    'session_velocity_vpm': {
        label: 'Session Velocity',
        tooltip: 'Videos consumed per minute during sessions. Higher = faster consumption.'
    },
    'likes_per_video': {
        label: 'Likes/Video (log)',
        tooltip: 'Ratio of liked items to videos watched. Log-transformed.'
    },
    'consistency_top_2_hours': {
        label: 'Hour Consistency',
        tooltip: 'Share of activity concentrated in the two most active hours. Higher = more consistent timing.'
    }
};

// Radar chart metrics with tooltips
const PE_RADAR_METRICS = ['chattiness', 'enthusiasm', 'patience', 'binge_level', 'consistency'];
const PE_RADAR_INFO = {
    'chattiness': {
        label: 'Chattiness',
        tooltip: 'Comments per video watched. Higher = more engaged in commenting.'
    },
    'enthusiasm': {
        label: 'Enthusiasm',
        tooltip: 'Likes per video watched. Higher = more likely to like content.'
    },
    'patience': {
        label: 'Patience',
        tooltip: 'Proportion of videos watched for 30+ seconds.'
    },
    'binge_level': {
        label: 'Binge Level',
        tooltip: 'Proportion of sessions lasting 30+ minutes.'
    },
    'consistency': {
        label: 'Consistency',
        tooltip: 'How concentrated activity is in peak hours (top 2 hours share).'
    }
};

function pe_loadStats() {
    console.log(`Loading global stats...`);

    PE_METRICS.forEach(metric => {
        const el = document.getElementById(`pe-hist-${metric}`);
        if (el) el.innerHTML = '<p style="text-align:center; padding-top:50px; color:#999;">Loading...</p>';
    });

    fetch(`/api/persona_stats`, { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                alert(`Error: ${data.error}`);
                return;
            }

            console.log("Stats loaded:", data.length, "donations");
            pe_data = data;
            pe_calculatePercentileRanks();

            const select = document.getElementById('pe-donation-select');
            select.innerHTML = '';
            data.forEach(d => {
                const option = document.createElement('option');
                option.value = d.donation_id;
                option.text = d.donation_id;
                select.appendChild(option);
            });

            pe_renderAllHistograms();

            if (data.length > 0) {
                pe_selectDonation(data[0].donation_id);
            }
        })
        .catch(err => {
            console.error("Fetch error:", err);
            alert("Failed to load stats.");
        });
}

function pe_calculatePercentileRanks() {
    if (!pe_data || pe_data.length === 0) return;

    PE_RADAR_METRICS.forEach(metric => {
        const valuesWithIds = pe_data.map(d => ({
            id: d.donation_id,
            value: d[metric] !== null && d[metric] !== undefined ? d[metric] : 0
        }));

        const sortedValues = [...valuesWithIds].sort((a, b) => a.value - b.value);

        if (!pe_percentileRanks[metric]) pe_percentileRanks[metric] = {};

        sortedValues.forEach((item, idx) => {
            pe_percentileRanks[metric][item.id] = idx / Math.max(1, sortedValues.length - 1);
        });
    });

    console.log("Percentile ranks calculated");
}

function pe_renderAllHistograms() {
    if (!pe_data || pe_data.length === 0) return;
    PE_METRICS.forEach(metric => pe_renderHistogram(metric));
}

function pe_renderHistogram(metric) {
    const containerId = `pe-hist-${metric}`;
    const container = document.getElementById(containerId);
    if (!container) return;

    const useLog = PE_LOG_METRICS.includes(metric);
    const info = PE_METRIC_INFO[metric] || { label: metric, tooltip: '' };

    const values = pe_data.map(d => {
        let rawValue = d[metric] !== null && d[metric] !== undefined ? d[metric] : 0;
        let transformedValue = useLog ? Math.log1p(rawValue) : rawValue;
        return { id: d.donation_id, value: transformedValue };
    });

    const allValues = values.map(v => v.value);
    const minVal = Math.min(...allValues);
    const maxVal = Math.max(...allValues);

    const numBins = 10;
    const binWidth = (maxVal - minVal) / numBins || 0.1;
    const bins = Array.from({ length: numBins }, () => []);

    values.forEach(v => {
        let binIndex = Math.floor((v.value - minVal) / binWidth);
        if (binIndex >= numBins) binIndex = numBins - 1;
        if (binIndex < 0) binIndex = 0;
        bins[binIndex].push(v);
    });

    const traces = [];
    const maxStackHeight = Math.max(...bins.map(b => b.length));

    for (let layer = 0; layer < maxStackHeight; layer++) {
        const x = [], y = [], customdata = [], colors = [];

        bins.forEach((bin, binIdx) => {
            if (layer < bin.length) {
                const donation = bin[layer];
                const binCenter = minVal + (binIdx + 0.5) * binWidth;
                x.push(binCenter);
                y.push(1);
                customdata.push(donation.id);
                colors.push(donation.id === pe_selectedId ? '#e63946' : '#4a90d9');
            }
        });

        if (x.length > 0) {
            traces.push({
                x, y, customdata,
                type: 'bar',
                marker: { color: colors, line: { color: '#fff', width: 0.5 } },
                hovertemplate: '%{customdata}<extra></extra>',
                showlegend: false
            });
        }
    }

    const layout = {
        title: { text: info.label, font: { size: 14 } },
        barmode: 'stack',
        xaxis: { title: '', range: [minVal - binWidth * 0.5, maxVal + binWidth * 0.5] },
        yaxis: { title: 'Count', dtick: Math.ceil(maxStackHeight / 5) || 1 },
        margin: { t: 35, l: 40, r: 20, b: 30 },
        bargap: 0.05,
        annotations: [{
            text: info.tooltip,
            showarrow: false,
            x: 0.5, y: 1.15,
            xref: 'paper', yref: 'paper',
            font: { size: 10, color: '#888' },
            xanchor: 'center'
        }]
    };

    Plotly.newPlot(containerId, traces, layout, { responsive: true, displayModeBar: false });

    container.on('plotly_click', function (data) {
        if (data.points.length > 0 && data.points[0].customdata) {
            pe_selectDonation(data.points[0].customdata);
        }
    });
}

function pe_selectDonation(donationId) {
    if (!pe_data) return;

    const donation = pe_data.find(d => d.donation_id === donationId);
    if (!donation) return;

    pe_selectedId = donationId;

    document.getElementById('pe-donation-select').value = donationId;
    document.getElementById('pe-details-card').classList.remove('hidden');
    document.getElementById('pe-details-id').innerText = donationId;
    document.getElementById('pe-details-moniker').innerText = donation.moniker || "Unknown Persona";
    document.getElementById('pe-details-emoji').innerText = donation.most_freq_emoji || "❓";

    // Update stats list
    document.getElementById('pe-stat-active-days').innerText = donation.active_days || 0;
    document.getElementById('pe-stat-total-events').innerText = (donation.total_events || 0).toLocaleString();
    document.getElementById('pe-stat-peak-segment').innerText = donation.peak_day_segment || 'Unknown';

    // Format watch time as hours
    const watchHours = ((donation.total_watch_time_s || 0) / 3600).toFixed(1);
    document.getElementById('pe-stat-watch-time').innerText = `${watchHours} hrs`;

    // Format timestamps
    const fmtDate = (ts) => {
        if (!ts) return 'N/A';
        const d = new Date(ts);
        return d.toLocaleDateString('en-AU', { day: '2-digit', month: 'short', year: 'numeric' });
    };
    document.getElementById('pe-stat-first-event').innerText = fmtDate(donation.first_event_ts);
    document.getElementById('pe-stat-last-event').innerText = fmtDate(donation.last_event_ts);

    pe_renderRadar(donation);
    pe_renderAllHistograms();
}

function pe_onDonationSelect() {
    const id = document.getElementById('pe-donation-select').value;
    pe_selectDonation(id);
}

function pe_renderRadar(d) {
    const labels = PE_RADAR_METRICS.map(m => PE_RADAR_INFO[m]?.label || m);

    const percentileValues = PE_RADAR_METRICS.map(m =>
        pe_percentileRanks[m] ? (pe_percentileRanks[m][d.donation_id] || 0) : 0
    );

    const avgValues = PE_RADAR_METRICS.map(() => 0.5);

    // Build hover text with definitions
    const hoverTexts = PE_RADAR_METRICS.map(m => PE_RADAR_INFO[m]?.tooltip || '');

    const avgTrace = {
        type: 'scatterpolar',
        r: avgValues,
        theta: labels,
        fill: 'toself',
        fillcolor: 'rgba(150, 150, 150, 0.2)',
        line: { color: 'rgba(150, 150, 150, 0.5)', width: 1, dash: 'dot' },
        name: 'Median',
        hoverinfo: 'skip'
    };

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
        showlegend: true,
        legend: { orientation: 'h', y: -0.15 },
        margin: { t: 20, l: 50, r: 50, b: 50 }
    };

    Plotly.newPlot('pe-radar-plot', [avgTrace, donationTrace], layout, { responsive: true });
}
