// Video Viewer Logic

// Detail-panel keys whose value is a date or a date-time, and so goes through
// the shared formatter (datetime_format.js) instead of being printed raw.
// Matches utc_timestamp, local_timestamp, scrape_ts, inference_ts, local_date, …
const _TIMESTAMP_KEY_RE = /(^|_)(timestamp|ts|date)$|^create_time$/;

function _isTimestampKey(key) {
    return _TIMESTAMP_KEY_RE.test(String(key));
}

let viewerData = {
    metadata: null,
    filters: {},
    activeStudy: null,
    filteredIds: [],
    itemCount: 0,
    searchQuery: "",
    currentIndex: -1,
    userTags: {},
    userVotes: [],
    activeModal: { item_id: null, variable: null, currentTags: [] },
    displayIds: {}, // Map of raw_id -> display_id
    collapsedDetailSections: (() => {
        // Track sections the user has explicitly collapsed. Persisted so the
        // choice survives reloads. Everything not in this set is expanded by
        // default — otherwise users don't see Collection ID, Activity timestamp,
        // Popularity, etc. without clicking to expand.
        try { return new Set(JSON.parse(localStorage.getItem('viewer_collapsed_detail_sections') || '[]')); }
        catch (e) { return new Set(); }
    })(),
    extraDataIndices: new Set(), // Global 0-based indices of items with extra_data (engagement activity)
    leftPanelVisible: true,
    rightPanelVisible: true,
    // Prefetch / caching infrastructure
    _metadataCache: new Map(),       // itemId -> {data, timestamp}
    _metadataCacheMax: 50,           // LRU eviction threshold
    _inflightItem: null,             // { itemId, promise, abort } for the in-flight metadata prefetch
    _itemFetchAbort: null,           // AbortController for in-flight item-metadata fetch
    _preloadedVideoIndex: null       // Index whose video is preloaded in the hidden element
};

// Shared player-overlay loader markup (viewer-video-msg).
const VIEWER_LOADER_HTML = '<div class="fun-loader-container"><div class="fun-loader"><div></div><div></div><div></div><div></div><div></div></div><div class="loading-text">Loading video...</div></div>';

// The global [misc] max_duration_for_download default (seconds). Used ONLY to
// choose the wording of the no-media notice (too-long vs failed download) —
// the authoritative cap lives server-side in the scraper.
const MAX_DOWNLOAD_SECONDS_HINT = 300;


function formatVideoDuration(seconds) {
    // "45s" / "14m 32s" / "1h 02m"
    const s = Math.round(seconds);
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
    return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
}


function videoNoticeText(item) {
    // Returns the explanatory no-media notice for an item, or null when the
    // item should have playable media. Strict === false: an absent/unknown
    // flag falls through to the reactive player-error handler instead.
    if (!item || item.video_downloaded !== false) return null;
    const dur = Number(item.duration);
    if (Number.isFinite(dur) && dur >= MAX_DOWNLOAD_SECONDS_HINT) {
        return `This video is ${formatVideoDuration(dur)} long — it exceeds the maximum download duration, so only its metadata was collected. No media is available.`;
    }
    return "No media was downloaded for this item (the video may have been removed or the download failed). Its metadata is shown on the right.";
}


function showVideoNotice(text) {
    // Replace the player with an explanatory message and stop the stream.
    const videoEl = document.getElementById('viewer-video');
    const msgEl = document.getElementById('viewer-video-msg');
    if (!videoEl || !msgEl) return;
    msgEl.dataset.notice = "1";
    msgEl.innerHTML = '';
    const box = document.createElement('div');
    box.className = 'viewer-video-notice';
    box.textContent = text;
    msgEl.appendChild(box);
    msgEl.style.display = "block";
    videoEl.pause();
    videoEl.removeAttribute('src');
    videoEl.load();
}


function maybeShowNoMediaNotice(item) {
    // Proactive layer: when the item's metadata says there is no media, swap
    // the player for the notice (unless the stream already delivered frames —
    // then the flag is stale and playback wins).
    const videoEl = document.getElementById('viewer-video');
    if (videoEl && videoEl.readyState >= 2) return;
    const text = videoNoticeText(item);
    if (text) showVideoNotice(text);
}


window.vaToggleLeft = function () {
    viewerData.leftPanelVisible = !viewerData.leftPanelVisible;
    const panel = document.getElementById('va-left-panel');
    if (panel) panel.style.display = viewerData.leftPanelVisible ? 'flex' : 'none';
};


window.vaToggleRight = function () {
    viewerData.rightPanelVisible = !viewerData.rightPanelVisible;
    const panel = document.getElementById('viewer-details-panel');
    if (panel) panel.style.display = viewerData.rightPanelVisible ? '' : 'none';
};

// Drill-down from Explore tab: consume pending filter state
let _drillDownInProgress = false;

async function checkPendingDrillDown() {
    if (_drillDownInProgress) return false;
    const pending = window._pendingDrillDown;
    if (!pending) return false;
    if (Date.now() - pending.timestamp > 5000) {
        window._pendingDrillDown = null;
        return false;
    }

    window._pendingDrillDown = null;
    _drillDownInProgress = true;

    try {
        const globalStudy = (window.studyState && window.studyState.current) || null;
        const studyChanged = (viewerData.activeStudy !== globalStudy);

        // Set state — study is sourced from the global selector.
        viewerData.activeStudy = globalStudy;
        viewerData.filters = pending.filters;
        viewerData.searchQuery = pending.searchQuery || "";
        viewerData.filteredIds = [];
        viewerData.currentIndex = -1;
        clearMetadataCache();

        const searchInput = document.getElementById('viewer-search-input');
        if (searchInput) searchInput.value = viewerData.searchQuery;

        // Clear player
        document.getElementById('viewer-video').src = "";
        document.getElementById('viewer-metadata').querySelector('tbody').innerHTML = "";
        const msgEl = document.getElementById('viewer-video-msg');
        delete msgEl.dataset.notice;
        msgEl.innerHTML = VIEWER_LOADER_HTML;
        msgEl.style.display = "block";
        updateNavUI();

        if (studyChanged || !viewerData.metadata) {
            await loadViewerMetadata();
        } else {
            renderViewerFilters(viewerData.metadata);
        }

        const landed = await applyViewerFilters(pending.itemId || null);

        // The Semantic Space map spans the whole corpus while this tab is scoped
        // to one study, so a named video is often simply not here. Say so instead
        // of silently showing row 0 of the niche.
        if (pending.itemId && !landed) {
            _showViewerNotice(pending.missNotice
                || `That video isn't in "${globalStudy}". Showing the rest of the filter instead.`,
                pending.platformUrl);
        }
        return true;
    } finally {
        _drillDownInProgress = false;
    }
}


// One-off banner above the player, cleared by the next item load. Used for
// drill-downs that could not land on the video they named.
function _showViewerNotice(message, platformUrl) {
    const bar = document.getElementById('viewer-drilldown-notice');
    if (!bar) return;
    const link = platformUrl
        ? ` <a href="${platformUrl}" target="_blank" rel="noopener">Open it on the platform ↗</a>`
        : '';
    bar.innerHTML = `${escapeHtml(message)}${link}`;
    bar.style.display = '';
}

function _clearViewerNotice() {
    const bar = document.getElementById('viewer-drilldown-notice');
    if (bar) { bar.style.display = 'none'; bar.innerHTML = ''; }
}


// Initialization
document.addEventListener('DOMContentLoaded', function () {
    // Only init if tab exists
    if (document.getElementById('video_analysis')) {
        loadUserTags();
        loadUserVotes();

        // Initial hydration: adopt the current global study once it's ready.
        if (window.studyState && window.studyState.ready) {
            window.studyState.ready.then(() => {
                if (window.studyState.current) {
                    applyViewerActiveStudy(window.studyState.current, { reload: true });
                }
            });
        }

        // Respond to global study changes.
        document.addEventListener('study:changed', (e) => {
            const next = e.detail && e.detail.study;
            applyViewerActiveStudy(next, { reload: true });
        });

        // Tagging Input Listener
        const tagInput = document.getElementById('tagging-input');
        if (tagInput) {
            tagInput.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') {
                    const val = tagInput.value.trim();
                    if (!val) return;

                    // Split by semicolon
                    const tags = val.split(';').map(t => t.trim()).filter(t => t.length > 0);

                    let added = false;
                    tags.forEach(tag => {
                        if (!viewerData.activeModal.currentTags.includes(tag)) {
                            viewerData.activeModal.currentTags.push(tag);
                            added = true;
                        }
                    });

                    if (added) {
                        renderModalChips();
                        tagInput.value = "";
                    }
                }
            });
        }

        // Search Input Listener
        const searchInput = document.getElementById('viewer-search-input');
        if (searchInput) {
            // Note: Unlike explorer, we might not auto-apply on input change if it's too heavy?
            // "applyViewerFilters" is manual. So we just update state.
            // But user might expect search to be live?
            // Explorer is live (debounced). Viewer requires "Apply". 
            // Let's stick to "Apply" paradigm for Viewer to match existing UI flow, OR make it live?
            // User did not specify. Explorer was live (auto updateStats).
            // Viewer has explicit "Apply Filters" button (line 27 html).
            // So we should just update the state, and let user click Apply.
            // OR we can make it auto-apply.
            // Given "Add it to the video viewer filter as well", and Viewer has explicit Apply, simply updating state is safest.

            searchInput.addEventListener('input', (e) => {
                viewerData.searchQuery = e.target.value;
                markApplyButtonDirty(true);
            });
        }

        // Slider Listener
        const slider = document.getElementById('viewer-slider');
        if (slider) {
            // Live update of index text
            slider.addEventListener('input', (e) => {
                const val = parseInt(e.target.value) - 1;
                // Only update text, don't load yet
                const count = viewerData.itemCount;
                document.getElementById('viewer-index').innerText = `${val + 1} / ${count}`;
            });

            // Load on release (change)
            slider.addEventListener('change', (e) => {
                const val = parseInt(e.target.value) - 1;
                viewerData.currentIndex = val;
                loadViewerItem(val);
            });
        }
    }
});

async function loadUserTags() {
    try {
        const res = await fetch('/api/video_analysis/tags');
        if (res.ok) {
            viewerData.userTags = await res.json();
        }
    } catch (e) { console.error("Failed to load tags", e); }
}

async function loadUserVotes() {
    try {
        const res = await fetch('/api/video_analysis/votes');
        if (res.ok) {
            viewerData.userVotes = await res.json();
        }
    } catch (e) { console.error("Failed to load votes", e); }
}

async function applyViewerActiveStudy(studyName, options = {}) {
    const { reload = true } = options;
    if (studyName === viewerData.activeStudy && !reload) return;

    viewerData.activeStudy = studyName || null;
    viewerData.filters = {};
    viewerData.filteredIds = [];
    viewerData.currentIndex = -1;
    viewerData.searchQuery = "";
    clearMetadataCache();

    const searchInput = document.getElementById('viewer-search-input');
    if (searchInput) searchInput.value = "";

    const videoEl = document.getElementById('viewer-video');
    if (videoEl) videoEl.src = "";
    const metaTbody = document.getElementById('viewer-metadata');
    if (metaTbody && metaTbody.querySelector('tbody')) metaTbody.querySelector('tbody').innerHTML = "";

    const msgEl = document.getElementById('viewer-video-msg');
    if (msgEl) {
        delete msgEl.dataset.notice;
        msgEl.innerHTML = VIEWER_LOADER_HTML;
        msgEl.style.display = "block";
    }
    if (typeof updateNavUI === 'function') updateNavUI();

    if (!studyName) return;

    // Metadata schema must be ready before loading items, otherwise renderMetadata
    // skips all fields (schemaMap is empty). Load metadata first, then fetch IDs.
    await loadViewerMetadata();
    applyViewerFilters();
}

async function loadViewerMetadata() {
    if (!viewerData.activeStudy) return;

    const filterContainer = document.getElementById('viewer-filters');
    const funLoader = '<div class="fun-loader-container"><div class="fun-loader"><div></div><div></div><div></div><div></div><div></div></div><div class="loading-text">Loading...</div></div>';
    filterContainer.innerHTML = funLoader;

    const study = viewerData.activeStudy;
    const studyParam = encodeURIComponent(study);

    // Two-phase fetch: base returns the static filter shape from a cached JSON
    // (no parquet load); overlay returns the per-user dynamic columns and
    // resolves a moment later. Filter panel renders as soon as base lands.
    const basePromise = fetch(`/api/explore/metadata/base?study=${studyParam}`).then(r => r.json());
    const overlayPromise = fetch(`/api/explore/metadata/overlay?study=${studyParam}&context=viewer`).then(r => r.json());

    let baseData;
    try {
        baseData = await basePromise;
    } catch (e) {
        console.error(e);
        filterContainer.innerHTML = '<div style="color:var(--color-danger); text-align:center;">Failed to load metadata</div>';
        return;
    }

    if (baseData.error) {
        filterContainer.innerHTML = `<div style="color:var(--color-danger); text-align:center;">${baseData.error}</div>`;
        return;
    }

    if (viewerData.activeStudy !== study) return;

    const infoSpan = document.getElementById('viewer-file-info');
    if (infoSpan) {
        infoSpan.innerText = "";
    }

    viewerData.metadata = baseData;
    renderViewerFilters(baseData);

    overlayPromise.then(overlay => {
        if (!overlay || overlay.error) return;
        if (viewerData.activeStudy !== study) return;
        mergeOverlayIntoViewerMetadata(overlay);
        renderViewerFilters(viewerData.metadata);
    }).catch(e => {
        console.error("Overlay metadata fetch failed:", e);
    });
}


function mergeOverlayIntoViewerMetadata(overlay) {
    const m = viewerData.metadata;
    if (!m || !overlay) return;

    Object.assign(m, overlay.columns || {});

    if (!m.schema_map) m.schema_map = {};
    Object.assign(m.schema_map, overlay.schema_map || {});

    if (!m.filter_priority) m.filter_priority = [];
    if (!m.display_priority) m.display_priority = [];

    (overlay.filter_priority_prepend || []).slice().reverse().forEach(col => {
        const idx = m.filter_priority.indexOf(col);
        if (idx > -1) m.filter_priority.splice(idx, 1);
        m.filter_priority.unshift(col);
    });
    (overlay.display_priority_prepend || []).slice().reverse().forEach(col => {
        const idx = m.display_priority.indexOf(col);
        if (idx > -1) m.display_priority.splice(idx, 1);
        m.display_priority.unshift(col);
    });

    if (overlay.stats_overlay && Object.keys(overlay.stats_overlay).length > 0) {
        if (!m.total_stats) m.total_stats = {};
        Object.assign(m.total_stats, overlay.stats_overlay);
    }
}

// Initialize expanded state from localStorage (sections default to collapsed)
if (!viewerData.expandedFilters) {
    try {
        viewerData.expandedFilters = JSON.parse(localStorage.getItem('viewer_expanded_filters') || '[]');
    } catch (e) {
        viewerData.expandedFilters = [];
    }
}

function renderViewerFilters(metadata) {
    const container = document.getElementById('viewer-filters');
    container.innerHTML = '';

    // Per-user composed filter set (same 'filter' surface as the Explore tab).
    const priority = (window.VariablePrefs && metadata.all_variables_order)
        ? VariablePrefs.effective('filter', metadata.all_variables_order, metadata.filter_priority || [])
        : metadata.filter_priority;
    let availableCols = [];

    if (priority && priority.length > 0) {
        availableCols = priority.filter(c => metadata[c]);
    } else {
        availableCols = Object.keys(metadata).sort().filter(c => c !== 'total_stats');
    }

    // "Customize variables" gear (shared filter surface) — mounted next to the
    // (The per-tab filter gear moved to My Stuff -> Preferences; changes made
    // there reach this tab via the 'fyp:variable-prefs-changed' event.)

    // Hide categorical/list filters with 0 or 1 unique values (no filtering possible)
    availableCols = availableCols.filter(col => {
        const info = metadata[col];
        if (info && (info.type === 'category' || info.type === 'list') && info.values && info.values.length <= 1) {
            return false;
        }
        return true;
    });

    const schemaMap = metadata.schema_map || {};

    // Group by Section
    const sections = {};
    const generalSection = "General";

    availableCols.forEach(col => {
        let section = generalSection;
        if (schemaMap[col] && schemaMap[col].section) {
            section = schemaMap[col].section;
            if (!section || section.trim() === "") section = generalSection;
        }
        if (!sections[section]) sections[section] = [];
        sections[section].push(col);
    });

    // Sort Sections by the backend's hard-coded section_order; sections not
    // listed fall after, alphabetically. Variables within a section already
    // arrive pre-sorted (categorical-before-numerical, then alphabetical).
    const sectionOrder = metadata.section_order || [];
    const sectionRank = (secName) => {
        const i = sectionOrder.indexOf(secName);
        return i === -1 ? sectionOrder.length : i;
    };
    let sectionNames = Object.keys(sections).sort((a, b) => {
        const ra = sectionRank(a), rb = sectionRank(b);
        if (ra !== rb) return ra - rb;
        return a.localeCompare(b);
    });

    // Move "Annotation Status" to the end
    const annIdx = sectionNames.indexOf('Annotation Status');
    if (annIdx > -1) {
        sectionNames.splice(annIdx, 1);
        sectionNames.push('Annotation Status');
    }

    // Helper: populate a section body lazily (only when expanded)
    const populateSectionBody = (body, vars) => {
        if (body.dataset.populated === '1') return;
        body.dataset.populated = '1';
        const inner = document.createDocumentFragment();
        vars.forEach(col => {
            const wrapper = renderViewerFilterColumn(col, metadata, schemaMap);
            if (wrapper) inner.appendChild(wrapper);
        });
        body.appendChild(inner);
    };

    // Render Sections (default collapsed, store expanded list)
    const fragment = document.createDocumentFragment();
    sectionNames.forEach(sec => {
        const vars = sections[sec];
        if (vars.length === 0) return;

        // Check if any filter in this section is active
        const hasActiveFilter = vars.some(col => !!viewerData.filters[col]);

        // Section Container
        const sectionDiv = document.createElement('div');
        sectionDiv.className = 'filter-section';
        sectionDiv.dataset.columns = JSON.stringify(vars);
        sectionDiv.style.marginBottom = '10px';
        sectionDiv.style.border = '1px solid var(--color-border)';
        sectionDiv.style.borderRadius = '4px';
        sectionDiv.style.overflow = 'hidden';

        // Header
        const header = document.createElement('div');
        header.className = 'filter-section-header font-bold' + (hasActiveFilter ? ' has-active-filter' : '');
        header.style.padding = '8px 10px';
        header.style.cursor = 'pointer';
        header.style.color = 'var(--color-text-primary)';
        header.style.userSelect = 'none';
        header.style.display = 'flex';
        header.style.alignItems = 'center';

        const isExpanded = viewerData.expandedFilters.includes(sec);
        const arrow = isExpanded ? '&#9662;' : '&#9656;'; // Down vs Right

        header.innerHTML = `<span style="margin-right:8px; width:15px; display:inline-block;">${arrow}</span> ${sec}`;

        // Body (Variables)
        const body = document.createElement('div');
        body.style.padding = '10px';
        body.style.background = 'var(--color-bg-surface)';
        body.style.display = isExpanded ? 'block' : 'none';

        // Populate eagerly only if expanded; otherwise defer until first expand
        if (isExpanded) {
            populateSectionBody(body, vars);
        }

        // Toggle Logic
        header.onclick = () => {
            const currentlyHidden = body.style.display === 'none';
            if (currentlyHidden) {
                // Lazy populate on first expand
                populateSectionBody(body, vars);
                body.style.display = 'block';
                header.innerHTML = `<span style="margin-right:8px; width:15px; display:inline-block;">&#9662;</span> ${sec}`;
                // Add to expanded list
                if (!viewerData.expandedFilters.includes(sec)) {
                    viewerData.expandedFilters.push(sec);
                }
            } else {
                body.style.display = 'none';
                header.innerHTML = `<span style="margin-right:8px; width:15px; display:inline-block;">&#9656;</span> ${sec}`;
                // Remove from expanded list
                viewerData.expandedFilters = viewerData.expandedFilters.filter(s => s !== sec);
            }
            // Persist
            localStorage.setItem('viewer_expanded_filters', JSON.stringify(viewerData.expandedFilters));
        };

        sectionDiv.appendChild(header);
        sectionDiv.appendChild(body);
        fragment.appendChild(sectionDiv);
    });
    container.appendChild(fragment);
}


function renderViewerFilterColumn(col, metadata, schemaMap) {
    const info = metadata[col];
    if (!info) return null;

    const wrapper = document.createElement('div');
    wrapper.className = 'filter-group';
    wrapper.style.marginBottom = '15px';
    wrapper.style.borderBottom = '1px solid var(--color-border-subtle)';
    wrapper.style.paddingBottom = '10px';

    const label = document.createElement('label');

    let displayName = col;
    if (schemaMap && schemaMap[col] && schemaMap[col].display_name) {
        displayName = schemaMap[col].display_name;
    }

    label.innerText = displayName;
    label.classList.add('font-bold');
    label.style.display = 'block';
    label.style.marginBottom = '5px';
    label.style.color = 'var(--color-text-primary)';
    wrapper.appendChild(label);

    if (info.type === 'number') {
        const sliderDiv = document.createElement('div');
        sliderDiv.style.marginBottom = '10px';
        sliderDiv.style.marginLeft = '5px';
        sliderDiv.style.marginRight = '5px';
        wrapper.appendChild(sliderDiv);

        // Min/Max Labels
        const labelRow = document.createElement('div');
        labelRow.style.display = 'flex';
        labelRow.style.justifyContent = 'space-between';
        labelRow.classList.add('text-sm');
        labelRow.style.color = 'var(--color-text-muted)';
        labelRow.style.marginTop = '-5px'; // Tweak spacing

        const minLabel = document.createElement('span');
        const maxLabel = document.createElement('span');

        labelRow.appendChild(minLabel);
        labelRow.appendChild(maxLabel);
        wrapper.appendChild(labelRow);

        // Frequency-scaled slider: backend-supplied percentile pivots become
        // non-linear noUiSlider range stops, so equal slider travel covers
        // equal data mass and all values stay in original units. Falls back
        // to log/linear scaling when quantiles are unavailable.
        const qRange = buildQuantileSliderRange(info);
        const useLog = !qRange && info.log === true && info.min >= 0;
        const logOff = (info.log_offset > 0) ? info.log_offset : 1;
        const toLog = (v) => Math.log10(v + logOff);
        const fromLog = (v) => Math.max(0, Math.pow(10, v) - logOff);
        const sliderToValue = (raw) => qRange ? raw : (useLog ? fromLog(raw) : raw);

        // Current Values (linear space)
        let currentMin = info.min;
        let currentMax = info.max;

        if (viewerData.filters[col] && viewerData.filters[col].value) {
            if (viewerData.filters[col].value.min !== undefined) currentMin = viewerData.filters[col].value.min;
            if (viewerData.filters[col].value.max !== undefined) currentMax = viewerData.filters[col].value.max;
        }

        // Helper format. A capped top bound (extreme outliers above the 99th
        // percentile) renders open-ended: "0.056+".
        const fmt = (n) => formatMetricNumber(n);
        const fmtMax = (n) => (info.max_capped && n >= info.max) ? fmt(n) + '+' : fmt(n);

        minLabel.innerText = fmt(currentMin);
        maxLabel.innerText = fmtMax(currentMax);

        // Slider range and start values
        const sliderMin = useLog ? toLog(info.min) : info.min;
        const sliderMax = useLog ? toLog(info.max) : info.max;
        const sliderStartMin = useLog ? toLog(currentMin) : currentMin;
        const sliderStartMax = useLog ? toLog(currentMax) : currentMax;

        // Initialize Slider
        if (typeof noUiSlider !== 'undefined') {
            if (info.min >= info.max) {
                sliderDiv.style.display = 'none';
            } else {
                noUiSlider.create(sliderDiv, {
                    start: [sliderStartMin, sliderStartMax],
                    connect: true,
                    range: qRange || {
                        'min': sliderMin,
                        'max': sliderMax
                    },
                    step: qRange ? undefined : (useLog ? (sliderMax - sliderMin) / 200 : ((info.max - info.min) > 100 ? 1 : ((info.max - info.min) / 100))),
                    // Full float precision: the default format rounds to
                    // 2 decimals, which destroys per-play ratio values.
                    format: { to: (v) => String(v), from: (v) => Number(v) },
                });

                sliderDiv.noUiSlider.on('update', function (values, handle) {
                    const display = sliderToValue(parseFloat(values[handle]));
                    if (handle === 0) {
                        minLabel.innerText = fmt(display);
                    } else {
                        maxLabel.innerText = fmtMax(display);
                    }
                });

                sliderDiv.noUiSlider.on('change', function (values, handle) {
                    const vMin = sliderToValue(parseFloat(values[0]));
                    const vMax = sliderToValue(parseFloat(values[1]));

                    // Only apply bounds that actually bind. A handle at the
                    // (possibly capped) top applies no upper bound, so outliers
                    // above the cap stay included.
                    const atMin = vMin <= info.min;
                    const atMax = vMax >= info.max;
                    if (atMin && atMax) {
                        delete viewerData.filters[col];
                    } else {
                        if (!viewerData.filters[col]) viewerData.filters[col] = { type: 'number', value: {} };
                        viewerData.filters[col].value = {};
                        if (!atMin) viewerData.filters[col].value.min = vMin;
                        if (!atMax) viewerData.filters[col].value.max = vMax;
                    }

                    updateViewerStats();
                    updateViewerFilterHighlights();
                });
            }
        } else {
            minLabel.innerText = "Error: Slider lib missing";
        }

    } else if (info.type === 'category' || info.type === 'list') {
        // Wrap label in header row with sort toggle
        const headerRow = document.createElement('div');
        headerRow.style.cssText = 'display:flex; align-items:center; justify-content:space-between;';
        label.style.marginBottom = '0';
        wrapper.removeChild(label);
        headerRow.appendChild(label);

        const sortBtn = document.createElement('button');
        sortBtn.className = 'filter-sort-toggle meta-tooltip';
        sortBtn.dataset.tooltip = 'Sort A\u2013Z';
        sortBtn.dataset.sortMode = 'freq';
        sortBtn.textContent = '#\u2193';
        headerRow.appendChild(sortBtn);
        wrapper.appendChild(headerRow);

        const listContainer = document.createElement('div');
        listContainer.style.maxHeight = '150px';
        listContainer.style.overflowY = 'auto';
        listContainer.style.background = 'var(--color-bg-surface)';
        listContainer.style.border = '1px solid var(--color-border)';
        listContainer.style.padding = '5px';

        info.values.forEach(val => {
            const item = document.createElement('div');
            item.style.display = 'flex';
            item.style.alignItems = 'center';
            item.className = 'filter-checkbox-item';

            let actualValue = val;
            let displayValue = val;
            let sortLabel = String(val);
            let sortCount = 0;

            if (typeof val === 'object' && val !== null && val.value !== undefined) {
                actualValue = val.value;
                const lbl = val.label || val.value;
                displayValue = `${lbl} (${val.count.toLocaleString()})`;
                sortLabel = String(lbl);
                sortCount = val.count;
            }

            item.dataset.sortLabel = sortLabel.toLowerCase();
            item.dataset.sortCount = sortCount;

            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.value = actualValue;
            cb.dataset.rawValue = actualValue;
            cb.style.marginRight = '5px';

            if (viewerData.filters[col] && Array.isArray(viewerData.filters[col].value)) {
                if (viewerData.filters[col].value.includes(actualValue)) {
                    cb.checked = true;
                }
            }

            cb.onchange = () => {
                const checked = Array.from(listContainer.querySelectorAll('input:checked')).map(c => c.dataset.rawValue);
                setViewerFilter(col, info.type, 'list', checked);
            };

            const span = document.createElement('span');
            span.innerText = displayValue;
            span.classList.add('text-sm');

            item.appendChild(cb);
            item.appendChild(span);
            listContainer.appendChild(item);
        });

        sortBtn.onclick = () => {
            const items = Array.from(listContainer.querySelectorAll('.filter-checkbox-item'));
            if (sortBtn.dataset.sortMode === 'freq') {
                items.sort((a, b) => a.dataset.sortLabel.localeCompare(b.dataset.sortLabel));
                sortBtn.dataset.sortMode = 'alpha';
                sortBtn.textContent = 'A\u2193';
                sortBtn.dataset.tooltip = 'Sort by frequency';
            } else {
                items.sort((a, b) => b.dataset.sortCount - a.dataset.sortCount);
                sortBtn.dataset.sortMode = 'freq';
                sortBtn.textContent = '#\u2193';
                sortBtn.dataset.tooltip = 'Sort A\u2013Z';
            }
            items.forEach(el => listContainer.appendChild(el));
        };

        if (info.total_unique && info.total_unique > info.values.length) {
            const notice = document.createElement('div');
            notice.classList.add('text-xs');
            notice.style.cssText = 'color: var(--color-text-faint); padding: 6px 4px 2px; font-style: italic;';
            notice.textContent = `Showing top ${info.values.length} of ${info.total_unique.toLocaleString()} categories`;
            listContainer.appendChild(notice);
        }

        wrapper.appendChild(listContainer);
    }

    return wrapper;
}

function setViewerFilter(col, type, subtype, value) {
    if (!viewerData.filters[col]) {
        viewerData.filters[col] = { type: type, value: (type === 'number' ? {} : []) };
    }

    if (subtype === 'na') {
        viewerData.filters[col].na = value;
        if (!viewerData.filters[col].na) delete viewerData.filters[col].na;
    } else if (type === 'number') {
        if (value === "") delete viewerData.filters[col].value[subtype];
        else viewerData.filters[col].value[subtype] = parseFloat(value);
    } else {
        if (value.length === 0) viewerData.filters[col].value = [];
        else viewerData.filters[col].value = value;
    }

    // Cleanup
    const hasValue = (type === 'number') ?
        (viewerData.filters[col].value && Object.keys(viewerData.filters[col].value).length > 0) :
        (viewerData.filters[col].value && viewerData.filters[col].value.length > 0);

    const hasNa = !!viewerData.filters[col].na;

    if (!hasValue && !hasNa) {
        delete viewerData.filters[col];
    }

    // We do NOT auto-apply filters here to avoid constant reloading of ID list.
    // User must click "Apply Filters".
    updateViewerFilterHighlights();
}


function updateViewerFilterHighlights() {
    const container = document.getElementById('viewer-filters');
    if (!container) return;

    container.querySelectorAll('.filter-section').forEach(sec => {
        const cols = JSON.parse(sec.dataset.columns || '[]');
        const header = sec.querySelector('.filter-section-header');
        if (!header) return;
        const active = cols.some(col => !!viewerData.filters[col]);
        header.classList.toggle('has-active-filter', active);
    });

    markApplyButtonDirty(true);
}


function markApplyButtonDirty(dirty) {
    const btn = document.getElementById('viewer-apply-filters-btn');
    if (!btn) return;
    if (dirty) {
        btn.textContent = 'Apply Filters';
        btn.classList.remove('btn-save');
        btn.classList.add('btn-primary');
    } else {
        btn.textContent = 'Filters Applied';
        btn.classList.remove('btn-primary');
        btn.classList.add('btn-save');
    }
}

function resetViewerFilters() {
    viewerData.filters = {};
    viewerData.searchQuery = "";

    const searchInput = document.getElementById('viewer-search-input');
    if (searchInput) searchInput.value = "";

    const hideDup = document.getElementById('viewer-hide-duplicates');
    if (hideDup) hideDup.checked = false;

    loadViewerMetadata(); // Re-render filter panel to clear inputs
    applyViewerFilters(); // Re-query the video list with cleared filters
}

function updateViewerDateRange(span) {
    const el = document.getElementById('viewer-date-range');
    if (!el) return;
    if (span && span.first && span.last) {
        // Activity timestamps are research data, so the span is shown on its own
        // clock rather than converted to the viewer's. `basis` says which clock.
        el.textContent = `${fypWallDateTime(span.first)} → ${fypWallDateTime(span.last)}`;
        const basis = span.basis || 'the source timestamp column';
        el.setAttribute('data-tooltip',
            `First and last activity timestamp in the current selection, in ${basis}. `
            + 'Videos are sorted oldest to newest.');
    } else {
        el.textContent = "";
    }
}

// `focusItemId` (optional) asks the server where that video sits in the filtered
// order, so a drill-down can land on it directly instead of on row 0. Returns
// true when the video was found and selected.
async function applyViewerFilters(focusItemId = null) {
    if (!viewerData.activeStudy) return false;

    _clearViewerNotice();
    const hideDuplicates = document.getElementById('viewer-hide-duplicates')?.checked || false;

    try {
        const res = await fetch('/api/video_analysis/ids', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                study: viewerData.activeStudy,
                filters: viewerData.filters,
                search_query: viewerData.searchQuery,
                hide_duplicates: hideDuplicates,
                focus_item_id: focusItemId || undefined,
                offset: 0,
                limit: 1000
            })
        });
        const data = await res.json();

        if (data.error) {
            alert(data.error);
            return false;
        }

        // Capture the currently playing video ID before overwriting the array
        const previousItemId = (viewerData.itemCount > 0 && viewerData.currentIndex >= 0)
            ? viewerData.filteredIds[viewerData.currentIndex]
            : null;

        viewerData.filteredIds = data.ids;
        viewerData.rowIdxs = data.row_idxs || [];
        viewerData.itemCount = data.count; // True total number of matching items
        updateViewerDateRange(data.time_span);
        clearMetadataCache();
        viewerData.currentOffset = data.offset || 0; // The base index of the downloaded chunk
        viewerData.chunkLimit = 1000; // Expected max size of the downloaded chunk
        viewerData.displayIds = data.display_ids || {};

        // Store extra_data indices (returned on first chunk only)
        if (data.extra_data_indices) {
            viewerData.extraDataIndices = new Set(data.extra_data_indices);
        }
        renderSliderMarkers();
        updateSkipButtons();

        // Let the user know if we capped the slider out at 10k
        const isTruncated = data.truncated;

        // Reset to first item
        if (viewerData.itemCount > 0) {

            // A drill-down that named a video wins over both branches below:
            // loadViewerItem pages to the right chunk itself when the position
            // falls outside the 1000 rows just downloaded.
            if (focusItemId && Number.isInteger(data.focus_index)) {
                viewerData.currentIndex = data.focus_index;
                await loadViewerItem(data.focus_index);
                updateNavUI();
                markApplyButtonDirty(false);
                return true;
            }

            // Check if the previously playing video survived the filter change
            const newIndex = previousItemId ? viewerData.filteredIds.indexOf(previousItemId) : -1;

            if (newIndex !== -1) {
                // Video is still in the loaded chunk! Just update index internally and skip reloading the video.
                viewerData.currentIndex = newIndex;
            } else {
                // Video was filtered out, load the first item in the new list.
                viewerData.currentIndex = 0;
                loadViewerItem(0);
            }

            // Re-run nav update to ensure slider logic applies
            updateNavUI();

        } else {
            viewerData.currentIndex = -1;
            updateNavUI();
            // Clear display
            document.getElementById('viewer-video').src = "";
            document.getElementById('viewer-video').src = "";

            // Prefer the server-supplied stale-data message when present so
            // the user knows to refresh the study, not just that the filters
            // happened to match nothing.
            const staleMsg = data.dataset_status && data.dataset_status.message;
            const emptyMsg = staleMsg || "No videos found";
            document.getElementById('viewer-metadata').querySelector('tbody').innerHTML =
                `<tr><td>${emptyMsg}</td></tr>`;
            const msgEl = document.getElementById('viewer-video-msg');
            msgEl.innerHTML = emptyMsg;
            msgEl.style.display = "block";
        }

        markApplyButtonDirty(false);
        return false;

    } catch (e) {
        console.error(e);
        alert("Failed to filter items");
        return false;
    }
}

// ---------------------------------------------------------------------------
// Metadata cache helpers (LRU, max 50 entries)
// ---------------------------------------------------------------------------

function cacheMetadata(itemId, data) {
    const cache = viewerData._metadataCache;
    cache.delete(itemId); // refresh position
    cache.set(itemId, { data, ts: Date.now() });
    if (cache.size > viewerData._metadataCacheMax) {
        cache.delete(cache.keys().next().value); // evict oldest
    }
}

function videoStreamUrl(itemId) {
    // Append the item's platform when cached metadata knows it — the server
    // resolves the media path faster with it, and falls back to probing
    // (platform subpath, then legacy flat path) without it.
    const base = `/api/video/${encodeURIComponent(viewerData.activeStudy)}/${itemId}`;
    const meta = getCachedMetadata(itemId);
    const platform = meta && meta.source_platform;
    return platform ? `${base}?platform=${encodeURIComponent(platform)}` : base;
}

function getCachedMetadata(itemId) {
    const entry = viewerData._metadataCache.get(itemId);
    if (!entry) return null;
    // Expire after 5 minutes
    if (Date.now() - entry.ts > 300_000) {
        viewerData._metadataCache.delete(itemId);
        return null;
    }
    // Refresh LRU position
    viewerData._metadataCache.delete(itemId);
    viewerData._metadataCache.set(itemId, entry);
    return entry.data;
}

function clearMetadataCache() {
    viewerData._metadataCache.clear();
    if (viewerData._inflightItem) {
        viewerData._inflightItem.abort.abort();
        viewerData._inflightItem = null;
    }
    viewerData._preloadedVideoIndex = null;
    const preloadEl = document.getElementById('viewer-video-preload');
    if (preloadEl) preloadEl.removeAttribute('src');
}


// ---------------------------------------------------------------------------
// Prefetch next item metadata + preload its video
// ---------------------------------------------------------------------------

function prefetchNext() {
    const nextIndex = viewerData.currentIndex + 1;
    if (nextIndex >= viewerData.itemCount) return;

    const relIdx = nextIndex - (viewerData.currentOffset || 0);
    const loadedCount = viewerData.filteredIds ? viewerData.filteredIds.length : 0;
    if (relIdx < 0 || relIdx >= loadedCount) return; // outside current chunk

    const nextItemId = viewerData.filteredIds[relIdx];
    const nextRowIdx = viewerData.rowIdxs ? viewerData.rowIdxs[relIdx] : undefined;

    // Cancel a stale in-flight prefetch — but never one that is for the item
    // currently on screen (loadViewerItem may be awaiting it) or already for
    // the target item.
    const currentRel = viewerData.currentIndex - (viewerData.currentOffset || 0);
    const currentItemId = viewerData.filteredIds ? viewerData.filteredIds[currentRel] : undefined;
    const inflight = viewerData._inflightItem;
    if (inflight && inflight.itemId !== nextItemId && inflight.itemId !== currentItemId) {
        inflight.abort.abort();
        viewerData._inflightItem = null;
    }

    // Prefetch metadata (if not already cached or being fetched). The promise
    // is kept so loadViewerItem can await this request instead of issuing a
    // duplicate — the server pays the full item cost per request.
    if (!getCachedMetadata(nextItemId) && !(inflight && inflight.itemId === nextItemId)) {
        const abort = new AbortController();
        const promise = fetch(`/api/video_analysis/item/${encodeURIComponent(viewerData.activeStudy)}/${nextItemId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                row_idx: nextRowIdx,
                filters: viewerData.filters,
                search_query: viewerData.searchQuery
            }),
            signal: abort.signal
        })
            .then(r => r.json())
            .then(data => { if (!data.error) cacheMetadata(nextItemId, data); return data; })
            .catch(() => null); // silently ignore aborts / errors
        viewerData._inflightItem = { itemId: nextItemId, promise, abort };
    }

    // Preload next video in hidden element — skipped when its metadata is
    // already known to say there is no media to stream.
    const nextMeta = getCachedMetadata(nextItemId);
    const nextHasNoMedia = nextMeta && nextMeta.video_downloaded === false;
    const preloadEl = document.getElementById('viewer-video-preload');
    if (preloadEl && viewerData._preloadedVideoIndex !== nextIndex && !nextHasNoMedia) {
        const autoplay = window.userSettings && window.userSettings.video_autostart;
        preloadEl.preload = autoplay ? "auto" : "metadata";
        preloadEl.src = videoStreamUrl(nextItemId);
        viewerData._preloadedVideoIndex = nextIndex;
    }
}


async function loadViewerItem(index) {
    // Ensure index is an integer, sometimes it comes in as a string from slider
    index = parseInt(index);

    if (index < 0 || index >= viewerData.itemCount) {
        console.warn(`[Viewer] Index ${index} out of bounds (0 to ${viewerData.itemCount - 1})`);
        return;
    }

    // Check if the requested index is outside the currently loaded chunk
    const relativeIndex = index - (viewerData.currentOffset || 0);
    const loadedCount = viewerData.filteredIds ? viewerData.filteredIds.length : 0;
    const inRange = relativeIndex >= 0 && relativeIndex < loadedCount;

    if (!inRange) {
        document.getElementById('viewer-status').innerText = `Fetching next chunk...`;

        // Calculate the base offset for the chunk containing this index
        const newOffset = Math.floor(index / (viewerData.chunkLimit || 1000)) * (viewerData.chunkLimit || 1000);
        const hideDuplicates = document.getElementById('viewer-hide-duplicates')?.checked || false;

        try {
            const res = await fetch('/api/video_analysis/ids', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    study: viewerData.activeStudy,
                    filters: viewerData.filters,
                    search_query: viewerData.searchQuery,
                    hide_duplicates: hideDuplicates,
                    offset: newOffset,
                    limit: viewerData.chunkLimit || 1000
                })
            });
            const data = await res.json();
            if (data.error) {
                alert(data.error);
                return;
            }

            viewerData.filteredIds = data.ids;
            viewerData.rowIdxs = data.row_idxs || [];
            viewerData.currentOffset = newOffset;
            viewerData.displayIds = { ...viewerData.displayIds, ...(data.display_ids || {}) }; // Append new displays

            // Re-call loadViewerItem now that the data is loaded
            await loadViewerItem(index);
            return;

        } catch (e) {
            console.error("[Viewer] Failed to load chunk", e);
            document.getElementById('viewer-status').innerText = "Error loading chunk";
            return;
        }
    }

    const itemId = viewerData.filteredIds[relativeIndex];
    const rowIdx = viewerData.rowIdxs ? viewerData.rowIdxs[relativeIndex] : undefined;
    const displayId = viewerData.displayIds[itemId] || itemId;

    // Reuse an in-flight prefetch when it is already fetching this item's
    // metadata (each item request is expensive server-side); abort it only
    // when it is for some other item. Also cancel any stale item fetch.
    const inflightItem = (viewerData._inflightItem && viewerData._inflightItem.itemId === itemId)
        ? viewerData._inflightItem : null;
    if (!inflightItem && viewerData._inflightItem) {
        viewerData._inflightItem.abort.abort();
        viewerData._inflightItem = null;
    }
    if (viewerData._itemFetchAbort) viewerData._itemFetchAbort.abort();

    document.getElementById('viewer-status').innerText = `Loading ${displayId}...`;

    // -------------------------------------------------------------------
    // 1. Start the video stream FIRST — independent of the metadata fetch.
    //    This lets the browser begin requesting bytes immediately rather
    //    than waiting on the per-item metadata round-trip.
    // -------------------------------------------------------------------
    const videoEl = document.getElementById('viewer-video');
    const preloadEl = document.getElementById('viewer-video-preload');
    const videoUrl = videoStreamUrl(itemId);
    const autoplay = window.userSettings && window.userSettings.video_autostart;

    // A notice from the previous item must not linger over this one — restore
    // the loader until this item's stream (or its own notice) takes over.
    const msgEl = document.getElementById('viewer-video-msg');
    if (msgEl.dataset.notice) {
        delete msgEl.dataset.notice;
        msgEl.innerHTML = VIEWER_LOADER_HTML;
        msgEl.style.display = "block";
    }

    if (preloadEl && viewerData._preloadedVideoIndex === index && preloadEl.src) {
        // The hidden element already has this video buffered — swap src
        videoEl.preload = autoplay ? "auto" : "metadata";
        videoEl.src = preloadEl.src;
        preloadEl.removeAttribute('src');
        viewerData._preloadedVideoIndex = null;
    } else {
        videoEl.preload = autoplay ? "auto" : "metadata";
        videoEl.src = videoUrl;
    }

    // Hide loading message once the first frame is available (an explanatory
    // notice is never timeout-hidden — only the loader is).
    videoEl.addEventListener('loadeddata', () => {
        if (!msgEl.dataset.notice) msgEl.style.display = "none";
    }, { once: true });
    setTimeout(() => { if (!msgEl.dataset.notice) msgEl.style.display = "none"; }, 5000);

    // Reactive layer: the stream failed (e.g. 404 — media never downloaded).
    // Prefer the metadata-driven explanation when it is available; fall back
    // to a generic message so the player never dies silently.
    videoEl.addEventListener('error', () => {
        if (viewerData.currentIndex !== index) return;   // stale listener
        if (msgEl.dataset.notice) return;                // notice already shown
        const meta = getCachedMetadata(itemId);
        showVideoNotice(videoNoticeText(meta) || "Video could not be loaded.");
    }, { once: true });

    // Check if tab is visible before playing
    const viewerTab = document.getElementById('video_analysis');
    if (viewerTab && viewerTab.classList.contains('active') && autoplay) {
        videoEl.play().catch(e => console.log("Auto-play blocked or failed:", e));
    }

    updateNavUI();
    // Kick off prefetch of the next item in the background
    prefetchNext();

    // -------------------------------------------------------------------
    // 2. Fetch (or read from cache) the item metadata in parallel with the
    //    video stream. Render the right-side details panel when it lands.
    // -------------------------------------------------------------------
    const cached = getCachedMetadata(itemId);
    if (cached) {
        // Cache hit: render synchronously, no placeholder flash.
        renderMetadata(cached);
        maybeShowNoMediaNotice(cached);
        document.getElementById('viewer-status').innerText = "";
        return;
    }

    // Cache miss: show a placeholder in the details panel so the previous
    // item's metadata isn't shown attached to the new video.
    const tbody = document.getElementById('viewer-metadata').querySelector('tbody');
    tbody.innerHTML = '<tr><td colspan="2" style="color: var(--color-text-muted); padding: 12px; text-align: center;">Loading details…</td></tr>';
    const voteContainer = document.getElementById('viewer-vote-container');
    if (voteContainer) voteContainer.innerHTML = '';

    const requestedIndex = index;

    const handleItem = (item) => {
        // Always cache a successful response — useful for back-navigation
        // even if the user has moved on from this index.
        if (item && !item.error) cacheMetadata(itemId, item);

        // Race guard: only update the panel if the user is still on this index.
        if (viewerData.currentIndex !== requestedIndex) return;

        if (!item || item.error) {
            tbody.innerHTML = '<tr><td colspan="2" style="color: var(--color-danger); padding: 12px; text-align: center;">Error loading details</td></tr>';
            document.getElementById('viewer-status').innerText = "Error loading item";
            return;
        }

        renderMetadata(item);
        maybeShowNoMediaNotice(item);
        document.getElementById('viewer-status').innerText = "";
    };

    // The prefetch is already fetching this item — render its result when it
    // lands instead of issuing a duplicate request.
    if (inflightItem) {
        inflightItem.promise.then(handleItem);
        return;
    }

    const abort = new AbortController();
    viewerData._itemFetchAbort = abort;

    fetch(`/api/video_analysis/item/${encodeURIComponent(viewerData.activeStudy)}/${itemId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            row_idx: rowIdx,
            filters: viewerData.filters,
            search_query: viewerData.searchQuery
        }),
        signal: abort.signal
    })
        .then(r => r.json())
        .then(handleItem)
        .catch(e => {
            if (e.name === 'AbortError') return; // superseded by a newer load
            console.error(e);
            if (viewerData.currentIndex === requestedIndex) {
                tbody.innerHTML = '<tr><td colspan="2" style="color: var(--color-danger); padding: 12px; text-align: center;">Error loading details</td></tr>';
                document.getElementById('viewer-status').innerText = "Error";
            }
        });
}

function linkify(text) {
    if (!text) return '';
    // Simple escape to prevent XSS before inserting as HTML
    const escaped = String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");

    // URL Regex
    const urlRegex = /(https?:\/\/[^\s]+)/g;
    return escaped.replace(urlRegex, function (url) {
        return `<a href="${url}" target="_blank" style="color: var(--color-info); text-decoration: underline;">${url}</a>`;
    });
}

function renderMetadata(item) {
    const tbody = document.getElementById('viewer-metadata').querySelector('tbody');
    tbody.innerHTML = '';
    viewerData.lastMetadataItem = item;

    // Update Platform Link in header
    const voteContainer = document.getElementById('viewer-vote-container');
    if (voteContainer) {
        if (item.platform_url) {
            voteContainer.innerHTML = `<a href="${item.platform_url}" target="_blank" rel="noopener noreferrer" style="color:var(--color-info); text-decoration:underline;">View on platform ↗</a>`;
        } else {
            voteContainer.innerHTML = '';
        }
        // (The detail-panel field gear moved to My Stuff -> Preferences.)
    }

    // Inject Display ID if present at top
    if (item.display_collection_id) {
        const didRow = document.createElement('tr');
        didRow.className = 'detail-display-id';
        didRow.innerHTML = `<td class="font-bold" style="color:var(--color-info);">Display ID</td><td class="font-bold" style="color:var(--color-text-primary); text-align:right;">${item.display_collection_id}</td>`;
        tbody.appendChild(didRow);
    }



    const meta = viewerData.metadata || {};
    const globalDisplay = meta.display_priority || [];
    const schemaMap = meta.schema_map || {};

    // Per-user composition of the detail-panel membership: (global ∪ include)
    // − exclude; the pre-sorted display_priority stays the ordering source.
    const priorityList = (window.VariablePrefs && meta.all_variables_order)
        ? VariablePrefs.effective('display', meta.all_variables_order, globalDisplay)
        : globalDisplay;
    const effectiveDisplay = new Set(priorityList);

    // Group items by Section
    const sections = {};
    const generalSection = "General";

    Object.keys(item).forEach(key => {
        // FILTER: needs a display_name and per-user (or global) membership.
        const schema = schemaMap[key];
        if (!schema || !schema.display_name) {
            return;
        }
        if (window.VariablePrefs && meta.all_variables_order) {
            if (!effectiveDisplay.has(key)) return;
        } else if (schema.web_display_prio === undefined) {
            return;
        }

        let section = generalSection;
        if (schema.section) {
            section = schema.section;
            if (!section || section.trim() === "") section = generalSection;
        }

        if (!sections[section]) sections[section] = [];
        sections[section].push(key);
    });

    // Sort Sections by the backend's hard-coded section_order; sections not
    // listed fall after, alphabetically.
    const sectionOrder = viewerData.metadata && viewerData.metadata.section_order ? viewerData.metadata.section_order : [];
    const sectionRank = (secName) => {
        const i = sectionOrder.indexOf(secName);
        return i === -1 ? sectionOrder.length : i;
    };
    let sectionNames = Object.keys(sections).sort((a, b) => {
        const ra = sectionRank(a), rb = sectionRank(b);
        if (ra !== rb) return ra - rb;
        return a.localeCompare(b);
    });

    // Sort variables within sections (display_priority already arrives
    // pre-sorted: categorical-before-numerical, then alphabetical)
    sectionNames.forEach(sec => {
        sections[sec].sort((a, b) => {
            const idxA = priorityList.indexOf(a);
            const idxB = priorityList.indexOf(b);

            // If both in list, sort by index (Priority)
            if (idxA !== -1 && idxB !== -1) return idxA - idxB;

            // If only A in list, A comes first
            if (idxA !== -1) return -1;

            // If only B in list, B comes first
            if (idxB !== -1) return 1;

            // Neither in list: alphabetical fallback
            return a.localeCompare(b);
        });
    });


    // Render
    sectionNames.forEach(sec => {
        const keys = sections[sec];
        if (keys.length === 0) return;

        const headerRow = document.createElement('tr');
        headerRow.className = 'detail-section-header';

        const isCollapsed = viewerData.collapsedDetailSections.has(sec);
        const headerCell = document.createElement('td');
        headerCell.innerHTML = `${isCollapsed ? '&#9656;' : '&#9662;'} ${sec}`;
        headerRow.appendChild(headerCell);

        tbody.appendChild(headerRow);

        // Variables
        const rowGroups = [];
        keys.forEach(key => {
            const tr = document.createElement('tr');
            tr.className = 'detail-row';
            tr.style.display = isCollapsed ? 'none' : 'flex';
            const tdKey = document.createElement('td');
            tdKey.className = 'detail-key';
            tdKey.style.cursor = 'pointer';
            const itemIdStr = String(item['item_id']);
            const itemTags = (viewerData.userTags[itemIdStr]?.[key]) || [];

            // Shared Annotations
            const sharedData = item['shared_annotations']?.[key] || {};
            const hasShared = Object.keys(sharedData).length > 0;

            // Determine Display Name
            let displayName = key;
            if (schemaMap[key] && schemaMap[key].display_name) {
                displayName = schemaMap[key].display_name;
            }

            // Set Display Text Logic
            // Priorities:
            // 1. Own Tags -> Green
            // 2. Shared Tags -> Blue
            // 3. Both -> Green Text + Blue Icon/Indicator

            let displayText = displayName;
            let styleColor = '';
            let styleWeightClass = '';
            let decoration = '';
            let suffix = '';

            const myCount = itemTags.length;

            // Check for Notes/CC (My)
            const notesKey = `${key}__NOTES`;
            const itemNotes = viewerData.userTags[itemIdStr]?.[notesKey];
            const ccKey = `${key}__CLOSED_TAGGING`;
            const itemCC = viewerData.userTags[itemIdStr]?.[ccKey];
            const hasMyAnnotation = myCount > 0 || itemNotes || itemCC;

            if (hasMyAnnotation) {
                styleColor = 'var(--color-success)';
                styleWeightClass = 'font-bold';
                if (myCount > 0) displayText += ` [${myCount}]`;

                if (itemNotes || itemCC) {
                    decoration = 'underline dotted var(--color-success)';
                }
            } else if (hasShared) {
                // Only shared
                styleColor = 'var(--color-info)';
                styleWeightClass = 'font-normal'; // Or bold? Let's keep normal but colored to distinguish from mine
                // Count total other tags?
                let otherTagCount = 0;
                Object.values(sharedData).forEach(u => {
                    if (u.tags) otherTagCount += u.tags.length;
                });
                if (otherTagCount > 0) displayText += ` [${otherTagCount}]`;

                decoration = 'underline dotted var(--color-info)'; // Shared implie annotation
            }

            if (styleColor) tdKey.style.color = styleColor;
            if (styleWeightClass) tdKey.classList.add(styleWeightClass);
            if (decoration) tdKey.style.textDecoration = decoration;

            // If BOTH, add indicator for shared
            if (hasMyAnnotation && hasShared) {
                suffix = ' <span style="color:var(--color-info)" title="Annotated by others">👥</span>';
            } else if (hasShared && !hasMyAnnotation) {
                suffix = ' <span style="color:var(--color-info)" title="Annotated by others">👥</span>';
            }

            tdKey.innerHTML = displayText + suffix;


            // Construct Tooltip (Tags + Description)
            let tooltipParts = [];

            // 1. My Annotations
            if (hasMyAnnotation) {
                tooltipParts.push("--- MY ANNOTATIONS ---");
                if (myCount > 0) tooltipParts.push(`Tags: ${itemTags.join(', ')}`);
                if (itemCC) tooltipParts.push(`Closed Tagging: ${itemCC}`);
                if (itemNotes) tooltipParts.push(`Notes: ${itemNotes}`);
                tooltipParts.push(""); // Spacer
            }

            // 2. Shared Annotations
            if (hasShared) {
                tooltipParts.push("--- SHARED ANNOTATIONS ---");

                // Aggregate anonymously
                let allTags = new Set();
                let allNotes = [];
                let allClosed = new Set();

                // Iterate sharedData values (which are user objects)
                Object.values(sharedData).forEach(uData => {
                    if (uData.tags && Array.isArray(uData.tags)) {
                        uData.tags.forEach(t => allTags.add(t));
                    }
                    if (uData.notes) allNotes.push(uData.notes);
                    if (uData.closed) allClosed.add(uData.closed);
                });

                if (allTags.size > 0) {
                    tooltipParts.push(`Tags: ${Array.from(allTags).sort().join(', ')}`);
                }
                if (allClosed.size > 0) {
                    tooltipParts.push(`Closed: ${Array.from(allClosed).join(', ')}`);
                }
                if (allNotes.length > 0) {
                    tooltipParts.push("Notes:");
                    allNotes.forEach(n => tooltipParts.push(`- ${n}`));
                }

                tooltipParts.push("");
            }

            // 3. Description
            if (schemaMap[key] && schemaMap[key].description) {
                if (tooltipParts.length > 0) tooltipParts.push("--- DESCRIPTION ---");
                tooltipParts.push(schemaMap[key].description);
            }

            if (tooltipParts.length > 0) {
                tdKey.classList.add('meta-tooltip');
                // Join with newline. CSS handles whitespace: pre-wrap
                tdKey.setAttribute('data-tooltip', tooltipParts.join('\n'));
                tdKey.removeAttribute('title');
            } else {
                tdKey.title = "Click to add tags";
            }

            tdKey.onclick = (e) => {
                e.stopPropagation();
                openTaggingModal(itemIdStr, key, itemTags);
            };
            // ---------------

            const tdVal = document.createElement('td');
            tdVal.className = 'detail-val';

            let val = item[key];
            let displayVal = '';

            if (val === null || val === undefined) {
                displayVal = '';
            } else if (Array.isArray(val)) {
                displayVal = val.join(', ');
            } else if (typeof val === 'number') {
                if (key === 'item_id' || key === 'video_id') {
                    displayVal = String(val);
                } else if (_isTimestampKey(key)) {
                    // An epoch number is anchored to UTC, so it is an instant.
                    displayVal = fypFmtDateTime(val, String(val));
                } else {
                    displayVal = val.toLocaleString();
                }
            } else if (typeof val === 'object') {
                displayVal = JSON.stringify(val);
            } else if (typeof val === 'string' && _isTimestampKey(key)) {
                // Offset-bearing values render in the viewer's timezone; the
                // zone-less participant stamps (local_timestamp, local_date)
                // render verbatim on the donor's own clock.
                displayVal = fypFmtAuto(val, val);
            } else {
                displayVal = String(val);
            }

            const wordLimit = 25;
            const words = displayVal.split(/\s+/);
            if (words.length > wordLimit) {
                const truncated = words.slice(0, wordLimit).join(' ');
                const truncSpan = document.createElement('span');
                truncSpan.innerHTML = linkify(truncated);
                const moreBtn = document.createElement('span');
                moreBtn.textContent = ' [...more]';
                moreBtn.style.color = 'var(--color-info)';
                moreBtn.style.cursor = 'pointer';
                moreBtn.classList.add('text-xs');
                let expanded = false;
                moreBtn.onclick = (e) => {
                    e.stopPropagation();
                    expanded = !expanded;
                    truncSpan.innerHTML = linkify(expanded ? displayVal : truncated);
                    moreBtn.textContent = expanded ? ' [less]' : ' [...more]';
                };
                tdVal.appendChild(truncSpan);
                tdVal.appendChild(moreBtn);
            } else {
                tdVal.innerHTML = linkify(displayVal);
            }

            tr.appendChild(tdKey);
            tr.appendChild(tdVal);
            tbody.appendChild(tr);
            rowGroups.push(tr);
        });

        // Click handler: toggle collapsed state (sections are expanded by
        // default, so the set tracks user-collapsed sections).
        headerRow.onclick = () => {
            const nowCollapsed = rowGroups[0].style.display !== 'none';
            rowGroups.forEach(r => r.style.display = nowCollapsed ? 'none' : 'flex');
            headerCell.innerHTML = nowCollapsed ? `&#9656; ${sec}` : `&#9662; ${sec}`;
            if (nowCollapsed) {
                viewerData.collapsedDetailSections.add(sec);
            } else {
                viewerData.collapsedDetailSections.delete(sec);
            }
            try {
                localStorage.setItem(
                    'viewer_collapsed_detail_sections',
                    JSON.stringify([...viewerData.collapsedDetailSections])
                );
            } catch (e) { /* localStorage disabled — fine */ }
        };
    });
}


function nextVideo() {
    if (viewerData.currentIndex < viewerData.itemCount - 1) {
        viewerData.currentIndex++;
        loadViewerItem(viewerData.currentIndex);
    }
}

function prevVideo() {
    if (viewerData.currentIndex > 0) {
        viewerData.currentIndex--;
        loadViewerItem(viewerData.currentIndex);
    }
}

function updateNavUI() {
    const indexStr = viewerData.currentIndex >= 0 ? (viewerData.currentIndex + 1) : 0;
    const count = viewerData.itemCount;

    document.getElementById('viewer-index').innerText = `${indexStr} / ${count}`;
    document.getElementById('viewer-index').title = "";

    document.getElementById('viewer-status').innerText = "Ready";

    updateSkipButtons();

    // Update Slider
    const slider = document.getElementById('viewer-slider');
    if (slider) {
        if (count > 0) {
            slider.disabled = false;
            slider.max = count;
            slider.value = indexStr; // 1-based usually for range if min=1
            // If currentIndex is -1 (empty), value 0? min is 1.
            if (viewerData.currentIndex === -1) {
                slider.value = 1;
                slider.disabled = true;
            }
        } else {
            slider.disabled = true;
            slider.max = 1;
            slider.value = 1;
        }
    }
}

function playViewerVideo() {
    const video = document.getElementById('viewer-video');
    if (video && video.src && !video.paused) {
        // Already playing
    } else if (video && video.src) {
        video.play().catch(e => console.log("Play failed:", e));
    }
}

function pauseViewerVideo() {
    const video = document.getElementById('viewer-video');
    if (video && !video.paused) {
        video.pause();
    }
}

// --- Tagging Logic ---
function openTaggingModal(itemId, variable, currentTags) {
    viewerData.activeModal = { item_id: itemId, variable: variable, currentTags: [...currentTags] };

    // Determine Display Name for Title
    let displayName = variable;
    const schemaMap = viewerData.metadata && viewerData.metadata.schema_map ? viewerData.metadata.schema_map : {};
    if (schemaMap[variable] && schemaMap[variable].display_name) {
        displayName = schemaMap[variable].display_name;
    }

    document.getElementById('tagging-modal-title').innerText = `Tags and Notes for ${displayName}`;
    document.getElementById('tagging-input').value = "";

    // Load Notes
    const notesKey = `${variable}__NOTES`;
    const notes = viewerData.userTags[itemId]?.[notesKey] || "";
    const notesArea = document.getElementById('tagging-notes');
    if (notesArea) notesArea.value = notes;

    // Closed Tagging Setup
    const ccContainer = document.getElementById('tagging-closed-tagging-container');
    const ccSelect = document.getElementById('tagging-closed-tagging');

    if (ccContainer && ccSelect) {
        // Check metadata for accepted_labels
        const schema = viewerData.metadata?.schema_map?.[variable];
        const acceptedLabels = schema?.accepted_labels; // Array of strings if present

        if (acceptedLabels && Array.isArray(acceptedLabels) && acceptedLabels.length > 0) {
            ccContainer.style.display = 'block';
            ccSelect.innerHTML = '<option value="" disabled selected>Select an option...</option>'; // Reset

            acceptedLabels.forEach(label => {
                const opt = document.createElement('option');
                opt.value = label;
                opt.innerText = label;
                ccSelect.appendChild(opt);
            });

            // Set existing value if any
            const ccKey = `${variable}__CLOSED_TAGGING`;
            const existingCC = viewerData.userTags[itemId]?.[ccKey];
            if (existingCC) {
                ccSelect.value = existingCC;
            } else {
                ccSelect.selectedIndex = 0; // Select placeholder
            }

        } else {
            ccContainer.style.display = 'none';
        }
    }

    // Calculate global available tags
    let allTags = new Set();
    // userTags structure is now { item_id: { variable: [tags...] } }
    Object.values(viewerData.userTags || {}).forEach(itemVars => {
        Object.values(itemVars).forEach(tagList => {
            if (Array.isArray(tagList)) tagList.forEach(t => allTags.add(t));
        });
    });
    viewerData.activeModal.allTags = Array.from(allTags).sort();

    renderModalChips();
    document.getElementById('tagging-modal').style.display = "flex";
    document.getElementById('tagging-input').focus();
}

function closeTaggingModal() {
    document.getElementById('tagging-modal').style.display = "none";
}

function renderModalChips() {
    const container = document.getElementById('tagging-quick-select');
    container.innerHTML = "";

    const { currentTags, allTags } = viewerData.activeModal;

    // Merge current tags with all historical tags
    const displayTags = new Set([...currentTags, ...(allTags || [])]);
    const sortedTags = Array.from(displayTags).sort();

    sortedTags.forEach(tag => {
        const isSelected = currentTags.includes(tag);
        const chip = document.createElement('div');

        // Style: Blue if selected, Gray if available
        const bg = isSelected ? 'var(--chip-selected-bg)' : 'var(--chip-bg)';
        const color = 'var(--chip-selected-text)';
        const border = isSelected ? '1px solid var(--chip-selected-border)' : '1px solid var(--chip-border)';

        chip.style.cssText = `background:${bg};color:${color};border:${border};padding:4px 10px;border-radius:12px;display:flex;gap:5px;align-items:center;cursor:pointer;user-select:none;transition:all 0.1s;`;
        chip.classList.add('text-sm');

        // Chip content
        if (isSelected) {
            chip.innerHTML = `<span>${tag}</span><span class="font-bold" style="margin-left:4px;">×</span>`;
            chip.onclick = () => removeTag(tag);
        } else {
            chip.innerHTML = `<span>${tag}</span>`;
            chip.onclick = () => addTag(tag);
        }

        container.appendChild(chip);
    });
}

function addTag(tag) {
    if (!viewerData.activeModal.currentTags.includes(tag)) {
        viewerData.activeModal.currentTags.push(tag);
        renderModalChips();
    }
}

function removeTag(tag) {
    viewerData.activeModal.currentTags = viewerData.activeModal.currentTags.filter(t => t !== tag);
    renderModalChips();
}

async function saveTaggingModal() {
    // Check for pending input and add it if present
    const tagInput = document.getElementById('tagging-input');
    if (tagInput && tagInput.value.trim()) {
        const val = tagInput.value.trim();
        const tags = val.split(';').map(t => t.trim()).filter(t => t.length > 0);

        tags.forEach(tag => {
            if (!viewerData.activeModal.currentTags.includes(tag)) {
                viewerData.activeModal.currentTags.push(tag);
            }
        });
    }

    // Get Notes
    const notesInput = document.getElementById('tagging-notes');
    const notesVal = notesInput ? notesInput.value : "";

    // Get Closed Tagging
    const ccSelect = document.getElementById('tagging-closed-tagging');
    const ccContainer = document.getElementById('tagging-closed-tagging-container');
    let ccVal = null;
    if (ccContainer && ccContainer.style.display !== 'none' && ccSelect) {
        if (ccSelect.value) ccVal = ccSelect.value;
    }

    const { item_id, variable, currentTags } = viewerData.activeModal;
    if (!item_id) return; // Study not strictly required for key, but good to have active

    try {
        const res = await fetch('/api/video_analysis/tags/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                study: viewerData.activeStudy, // Optional now
                item_id: item_id,
                variable: variable,
                tags: currentTags,
                notes: notesVal,
                closed_tagging: ccVal
            })
        });

        if (res.ok) {
            // Update local state (Global structure)
            if (!viewerData.userTags[item_id]) viewerData.userTags[item_id] = {};
            viewerData.userTags[item_id][variable] = currentTags;

            // Update local Notes state
            const notesKey = `${variable}__NOTES`;
            if (notesVal && notesVal.trim()) {
                viewerData.userTags[item_id][notesKey] = notesVal.trim();
            } else {
                delete viewerData.userTags[item_id][notesKey];
            }

            // Update local Closed Tagging state
            const ccKey = `${variable}__CLOSED_TAGGING`;
            if (ccVal) {
                viewerData.userTags[item_id][ccKey] = ccVal;
            } else {
                delete viewerData.userTags[item_id][ccKey];
            }

            loadViewerItem(viewerData.currentIndex); // Re-render
            closeTaggingModal();
        }
    } catch (e) { console.error(e); alert("Error saving tags"); }
}

// ── Extra-data slider markers & skip navigation ──

function renderSliderMarkers() {
    const container = document.getElementById('viewer-slider-markers');
    if (!container) return;
    container.innerHTML = '';

    const count = viewerData.itemCount;
    const indices = viewerData.extraDataIndices;
    if (!count || !indices || indices.size === 0) return;

    // Binned histogram: divide the index range into fixed bins and render
    // a bar per bin whose height and opacity reflect the extra_data density.
    const numBins = Math.min(80, count);
    const binSize = count / numBins;
    const padPx = 8; // approximate range-thumb half-width
    const binWidth = (100 / numBins);

    // Count extra_data items per bin
    const bins = new Array(numBins).fill(0);
    indices.forEach(idx => {
        const bin = Math.min(Math.floor(idx / binSize), numBins - 1);
        bins[bin]++;
    });

    const maxCount = Math.max(...bins);
    if (maxCount === 0) return;

    bins.forEach((cnt, i) => {
        if (cnt === 0) return;
        const ratio = cnt / maxCount; // 0..1 normalised density
        const bar = document.createElement('div');
        bar.className = 'slider-extra-bin';
        // Position: proportional left within the padded track area
        bar.style.left = `calc(${padPx}px + (100% - ${2 * padPx}px) * ${i / numBins})`;
        bar.style.width = `calc((100% - ${2 * padPx}px) * ${1 / numBins})`;
        // Height and opacity scale with density
        const minH = 4;
        const maxH = 16;
        bar.style.height = `${minH + (maxH - minH) * ratio}px`;
        bar.style.opacity = 0.3 + 0.7 * ratio;
        bar.title = `${cnt} engagement activit${cnt === 1 ? 'y' : 'ies'}`;
        container.appendChild(bar);
    });
}


function updateSkipButtons() {
    const idx = viewerData.currentIndex;
    const indices = viewerData.extraDataIndices;
    const prevBtn = document.getElementById('viewer-skip-prev');
    const nextBtn = document.getElementById('viewer-skip-next');
    if (!prevBtn || !nextBtn) return;

    if (!indices || indices.size === 0) {
        prevBtn.disabled = true;
        nextBtn.disabled = true;
        prevBtn.title = "No engagement activities in current results";
        nextBtn.title = "No engagement activities in current results";
        return;
    }

    // Check if there's a previous/next extra_data item from current position
    let hasPrev = false;
    for (let i = idx - 1; i >= 0; i--) {
        if (indices.has(i)) { hasPrev = true; break; }
    }
    let hasNext = false;
    for (let i = idx + 1; i < viewerData.itemCount; i++) {
        if (indices.has(i)) { hasNext = true; break; }
    }

    prevBtn.disabled = !hasPrev;
    nextBtn.disabled = !hasNext;
    prevBtn.title = hasPrev ? "Previous activity with engagement data" : "No earlier engagement activities";
    nextBtn.title = hasNext ? "Next activity with engagement data" : "No later engagement activities";
}


function nextExtraData() {
    const indices = viewerData.extraDataIndices;
    if (!indices || indices.size === 0) return;

    for (let i = viewerData.currentIndex + 1; i < viewerData.itemCount; i++) {
        if (indices.has(i)) {
            viewerData.currentIndex = i;
            loadViewerItem(i);
            return;
        }
    }
}


function prevExtraData() {
    const indices = viewerData.extraDataIndices;
    if (!indices || indices.size === 0) return;

    for (let i = viewerData.currentIndex - 1; i >= 0; i--) {
        if (indices.has(i)) {
            viewerData.currentIndex = i;
            loadViewerItem(i);
            return;
        }
    }
}


// Theme changes are handled automatically via var() CSS references


// The 'filter' surface is shared with the Explore tab, so a "Customize
// variables" change made there (or here) must refresh this tab's filter list
// even while Video Analysis is hidden. Without this, the panel kept the stale
// variable set until the study was reloaded.
window.addEventListener('fyp:variable-prefs-changed', (ev) => {
    if (!viewerData || !viewerData.metadata) return;
    const surface = ev.detail && ev.detail.surface;
    if (!surface || surface === 'filter') {
        renderViewerFilters(viewerData.metadata);
    }
    if ((!surface || surface === 'display') && viewerData.lastMetadataItem) {
        renderMetadata(viewerData.lastMetadataItem);
    }
});
