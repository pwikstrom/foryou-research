// State for Persona Explorer (window.pe_data for main.js access)
window.pe_data = window.pe_data || [];
let pe_data = window.pe_data;
let pe_selectedId = null;
// Metrics to display as strip plots - now dynamically loaded
let PE_METRICS = []; // Will be populated from PE_METRIC_INFO

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

function pe_loadSettings() {
    PE_METRICS = Object.keys(PE_METRIC_INFO);
}

// Initialize when tab is shown - check for cached stats
// pe_onShow logic called by main.js
function pe_onShow() {
    // Ensure settings (PE_METRICS) are always loaded
    if (PE_METRICS.length === 0) {
        pe_loadSettings();
    }

    // If not initialized, init
    if (!window.pe_data || window.pe_data.length === 0) {
        if (typeof pe_init === 'function') pe_init();
        return;
    }

    // If strips are missing (e.g. after page reload), re-render them
    const container = document.getElementById('pe-strips-container');
    if (container && container.querySelectorAll('.strip-row').length === 0) {
        pe_renderAllStrips();
    }
}
// Expose pe_onShow
window.pe_onShow = pe_onShow;


function pe_init() {
    pe_loadSettings();

    // Fetch stats info to update timestamp display
    fetch('/api/collections/info')
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
    if (container) container.innerHTML = '<p style="text-align:center; color:var(--color-text-muted);">Loading cached stats...</p>';

    return fetch('/api/collections/cached')
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
                if (container) container.innerHTML = `<p style="text-align:center; color:var(--color-text-muted);">${data.error}</p>`;
                return;
            }
            pe_handleStatsData(data);
        })
        .catch(err => {
            console.error('Fetch error:', err);
            if (container) container.innerHTML = '<p style="text-align:center; color:var(--color-danger);">Failed to load cached stats.</p>';
        });
}
window.pe_loadCachedStats = pe_loadCachedStats;

function pe_handleStatsData(data) {
    // console.log('Stats loaded:', data.length, 'collections');
    pe_data = data;
    window.pe_data = data;  // Keep window reference in sync

    // Update count display
    const countEl = document.getElementById('pe-stats-count');
    if (countEl) {
        countEl.innerText = `(${data.length} collections)`;
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

    // Render all strip plots
    pe_renderAllStrips();

    // Select first collection
    if (data.length > 0) {
        pe_selectDonation(data[0].collection_id);
    }
}



function pe_renderAllStrips() {
    const container = document.getElementById('pe-strips-container');
    if (!container) return;

    container.innerHTML = '';

    PE_METRICS.forEach(metric => {
        // Skip metrics where all values are identical (no variation to show)
        const values = pe_data.map(d => parseFloat(d[metric])).filter(v => !isNaN(v));
        if (values.length === 0) return;
        const first = values[0];
        if (values.every(v => v === first)) return;

        const stripRow = pe_createStrip(metric);
        container.appendChild(stripRow);
    });
}


function pe_buildSwarmPoints(values, collectionIds, width, height, padding) {
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min;
    const plotW = width - padding.left - padding.right;
    const plotH = height - padding.top - padding.bottom;
    const midY = padding.top + plotH / 2;
    const radius = 3;
    const spacing = radius * 2 + 1;

    // Map each collection to an x position based on its value
    const points = values.map((v, i) => ({
        value: v,
        collectionId: collectionIds[i],
        x: range > 0 ? padding.left + ((v - min) / range) * plotW : padding.left + plotW / 2,
        y: midY
    }));

    // Sort by x so we can jitter efficiently
    points.sort((a, b) => a.x - b.x);

    // Bee swarm jitter: push overlapping points vertically
    for (let i = 0; i < points.length; i++) {
        for (let j = i - 1; j >= 0; j--) {
            const dx = points[i].x - points[j].x;
            if (dx > spacing) break;
            const dist = Math.sqrt(dx * dx + (points[i].y - points[j].y) ** 2);
            if (dist < spacing) {
                // Alternate pushing up and down
                const direction = (i % 2 === 0) ? -1 : 1;
                points[i].y = points[j].y + direction * spacing;
                // Clamp within bounds
                points[i].y = Math.max(padding.top + radius, Math.min(padding.top + plotH - radius, points[i].y));
            }
        }
    }

    return { points, min, max };
}


function pe_drawSwarmCanvas(canvas, swarmData, selectedId) {
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.scale(dpr, dpr);

    const { points } = swarmData;

    // Draw non-selected dots first
    points.forEach(p => {
        if (p.collectionId === selectedId) return;
        ctx.beginPath();
        ctx.arc(p.x, p.y, 3, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(74, 144, 217, 0.6)';
        ctx.fill();
    });

    // Draw selected dot on top (larger, red)
    const selected = points.find(p => p.collectionId === selectedId);
    if (selected) {
        ctx.beginPath();
        ctx.arc(selected.x, selected.y, 5.5, 0, Math.PI * 2);
        ctx.fillStyle = getCSSVar('--color-danger');
        ctx.fill();
        ctx.strokeStyle = getCSSVar('--color-bg-primary');
        ctx.lineWidth = 1.5;
        ctx.stroke();
    }
}


function pe_createStrip(metric) {
    const info = PE_METRIC_INFO[metric] || { label: metric, tooltip: '' };

    // Extract numeric values and collection IDs for this metric
    const values = [];
    const collectionIds = [];
    pe_data.forEach(d => {
        const v = parseFloat(d[metric]);
        if (!isNaN(v)) {
            values.push(v);
            collectionIds.push(d.collection_id);
        }
    });

    // Get selected value and percentile
    let selectedVal = null;
    let percentile = null;
    if (pe_selectedId) {
        const collection = pe_data.find(d => d.collection_id === pe_selectedId);
        if (collection) {
            selectedVal = parseFloat(collection[metric]);
            if (!isNaN(selectedVal)) {
                const below = values.filter(v => v < selectedVal).length;
                percentile = Math.round((below / values.length) * 100);
            }
        }
    }

    // Create row container
    const row = document.createElement('div');
    row.className = 'strip-row';
    row.dataset.metric = metric;

    // Header with label, percentile badge, and selected value
    const header = document.createElement('div');
    header.className = 'strip-header';

    const label = document.createElement('span');
    label.className = 'strip-label';
    label.innerText = info.label;
    label.title = info.tooltip;
    header.appendChild(label);

    const rightInfo = document.createElement('span');
    rightInfo.className = 'strip-right-info';

    const selectedValue = document.createElement('span');
    selectedValue.className = 'strip-selected-value';
    selectedValue.id = `pe-strip-value-${metric}`;
    if (selectedVal !== null && !isNaN(selectedVal)) {
        selectedValue.innerText = pe_formatValue(selectedVal);
    }
    rightInfo.appendChild(selectedValue);

    const pctBadge = document.createElement('span');
    pctBadge.className = 'strip-percentile-badge';
    pctBadge.id = `pe-strip-pct-${metric}`;
    if (percentile !== null) {
        pctBadge.innerText = `P${percentile}`;
        pctBadge.title = `${percentile}th percentile`;
    }
    rightInfo.appendChild(pctBadge);

    header.appendChild(rightInfo);
    row.appendChild(header);

    // Canvas for bee swarm
    const canvas = document.createElement('canvas');
    canvas.className = 'strip-swarm';
    canvas.dataset.metric = metric;
    row.appendChild(canvas);

    // Axis labels
    const minVal = values.length > 0 ? Math.min(...values) : 0;
    const maxVal = values.length > 0 ? Math.max(...values) : 0;
    const axis = document.createElement('div');
    axis.className = 'strip-axis';
    [minVal, (minVal + maxVal) / 2, maxVal].forEach(v => {
        const span = document.createElement('span');
        span.innerText = pe_formatValue(v);
        axis.appendChild(span);
    });
    row.appendChild(axis);

    // Draw after DOM insertion (requestAnimationFrame ensures canvas has layout)
    requestAnimationFrame(() => {
        const padding = { left: 6, right: 6, top: 6, bottom: 6 };
        const swarmData = pe_buildSwarmPoints(values, collectionIds, canvas.clientWidth, canvas.clientHeight, padding);
        canvas._swarmData = swarmData;
        pe_drawSwarmCanvas(canvas, swarmData, pe_selectedId);
    });

    // Click to select collection
    canvas.addEventListener('click', (e) => {
        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        const swarmData = canvas._swarmData;
        if (!swarmData) return;

        let closest = null;
        let closestDist = 8;
        swarmData.points.forEach(p => {
            const dist = Math.sqrt((p.x - x) ** 2 + (p.y - y) ** 2);
            if (dist < closestDist) {
                closestDist = dist;
                closest = p;
            }
        });
        if (closest) {
            pe_selectDonation(closest.collectionId);
        }
    });

    // Tooltip and cursor on hover
    canvas.addEventListener('mousemove', (e) => {
        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        const swarmData = canvas._swarmData;
        if (!swarmData) return;

        let closest = null;
        let closestDist = 8;
        swarmData.points.forEach(p => {
            const dist = Math.sqrt((p.x - x) ** 2 + (p.y - y) ** 2);
            if (dist < closestDist) {
                closestDist = dist;
                closest = p;
            }
        });
        canvas.style.cursor = closest ? 'pointer' : 'default';
        const displayName = closest ? (pe_data.find(d => d.collection_id === closest.collectionId)?.display_collection_id || closest.collectionId) : '';
        canvas.title = closest ? `${displayName}\nValue: ${pe_formatValue(closest.value)}` : '';
    });

    return row;
}

function pe_formatValue(v) {
    if (v === null || v === undefined) return 'N/A';
    v = parseFloat(v);
    if (isNaN(v)) return 'N/A';
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
    if (!pe_selectedId) return;

    const collection = pe_data.find(d => d.collection_id === pe_selectedId);
    if (!collection) return;

    PE_METRICS.forEach(metric => {
        const selectedVal = parseFloat(collection[metric]);

        // Update value display
        const valueEl = document.getElementById(`pe-strip-value-${metric}`);
        if (valueEl) {
            valueEl.innerText = !isNaN(selectedVal) ? pe_formatValue(selectedVal) : 'N/A';
        }

        // Update percentile badge
        const pctEl = document.getElementById(`pe-strip-pct-${metric}`);
        if (pctEl && !isNaN(selectedVal)) {
            const values = pe_data.map(d => parseFloat(d[metric])).filter(v => !isNaN(v));
            const below = values.filter(v => v < selectedVal).length;
            const percentile = Math.round((below / values.length) * 100);
            pctEl.innerText = `P${percentile}`;
            pctEl.title = `${percentile}th percentile`;
        }

        // Redraw swarm canvas with new selection
        const canvas = document.querySelector(`canvas.strip-swarm[data-metric="${metric}"]`);
        if (canvas && canvas._swarmData) {
            pe_drawSwarmCanvas(canvas, canvas._swarmData, pe_selectedId);
        }
    });
}


// --- Annotations Logic ---

// function pe_onDonationSelect() { ... } // Removed


function pe_selectDonation(collectionId) {
    pe_selectedId = collectionId;
    window.pe_selectedId = collectionId;

    // Update strip selection highlighting (works even without pe_data)
    if (pe_data && pe_data.length > 0) {
        pe_updateStripSelection();
    }
}

// Tag Management
function pe_renderTags() {
    if (!pe_selectedId || !pe_data) return;
    const collection = pe_data.find(d => d.collection_id === pe_selectedId);
    if (!collection) return;

    // Defensive: Ensure annotation_tags is an array
    const currentTags = Array.isArray(collection.annotation_tags) ? collection.annotation_tags : [];

    // Collect ALL tags from ALL collections
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
        const bg = isSelected ? 'var(--chip-selected-bg)' : 'var(--chip-bg)';
        const border = isSelected ? '1px solid var(--chip-selected-border)' : '1px solid var(--chip-border)';

        chip.style.cssText = `
            background: ${bg};
            color: var(--chip-text);
            border: ${border};
            padding: 4px 10px;
            border-radius: 12px;
            cursor: pointer;
            user-select: none;
            transition: all 0.1s;
        `;
        chip.classList.add('text-sm');

        chip.textContent = tag;

        chip.onclick = () => {
            pe_toggleTag(tag);
        };

        container.appendChild(chip);
    });
}

function pe_toggleTag(tag) {
    if (!pe_selectedId || !pe_data) return;
    const collection = pe_data.find(d => d.collection_id === pe_selectedId);
    if (!collection) return;

    // Ensure array exists
    if (!Array.isArray(collection.annotation_tags)) collection.annotation_tags = [];

    const idx = collection.annotation_tags.indexOf(tag);
    if (idx !== -1) {
        // Remove
        collection.annotation_tags.splice(idx, 1);
    } else {
        // Add
        collection.annotation_tags.push(tag);
    }
    // console.log(`[PE] Toggled ${tag}. New tags:`, collection.annotation_tags);
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
        const collection = pe_data.find(d => d.collection_id === pe_selectedId);
        if (!collection) return;

        if (!Array.isArray(collection.annotation_tags)) collection.annotation_tags = [];

        newTags.forEach(tag => {
            if (!collection.annotation_tags.includes(tag)) {
                collection.annotation_tags.push(tag);
            }
        });

        input.value = '';
        // console.log(`[PE] Added tags. New tags:`, collection.annotation_tags);
        pe_renderTags();
    }
}


function pe_saveAnnotation() {
    if (!pe_selectedId) return;

    const displayIdInput = document.getElementById('pe-annot-display-id');
    // Using collection.annotation_tags which is updated in real-time by UI
    const collection = pe_data.find(d => d.collection_id === pe_selectedId);
    if (!collection) return;

    if (!displayIdInput) return;

    const displayId = displayIdInput.value;
    const tags = collection.annotation_tags || [];

    const payload = {
        collection_id: pe_selectedId,
        display_collection_id: displayId,
        tags: tags
    };

    // Find the button to show feedback
    const btn = document.querySelector('button[onclick="pe_saveAnnotation()"]');
    if (btn) btn.disabled = true;

    fetch('/api/collection/annotate', {
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
                    const don = pe_data.find(d => d.collection_id === pe_selectedId);
                    if (don) {
                        don.display_collection_id = displayId;
                        don.annotation_tags = tags;
                    }
                }

                // Visual feedback
                if (btn) {
                    const originalHTML = '<i class="fas fa-save"></i> Save Annotations';
                    btn.innerHTML = '<i class="fas fa-check"></i> Saved!';
                    btn.classList.add('success');
                    // Assuming .success class exists or inline style
                    btn.style.backgroundColor = 'var(--color-success)';

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

window.addEventListener('theme-changed', () => {
    document.querySelectorAll('canvas.strip-swarm').forEach(canvas => {
        if (canvas._swarmData) {
            pe_drawSwarmCanvas(canvas, canvas._swarmData, pe_selectedId);
        }
    });
});
