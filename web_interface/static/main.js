// CSS variable helper for dynamic JS styling
function getCSSVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Format a numeric metric for display: large values as rounded integers with
// thousands separators, small values (e.g. per-play ratios) with 3 significant
// digits so they don't collapse to 0.
function formatMetricNumber(n) {
    if (n === null || n === undefined || !isFinite(n)) return String(n);
    const abs = Math.abs(n);
    if (abs >= 100 || Number.isInteger(n)) return Math.round(n).toLocaleString();
    if (abs === 0) return '0';
    return Number(n.toPrecision(3)).toLocaleString(undefined, { maximumFractionDigits: 6 });
}

// Build a frequency-scaled noUiSlider range from backend percentile pivots
// (info.quantiles: {"25": value, ...}). Each segment between pivots holds an
// equal share of the data, so slider travel matches data density. Returns
// null when no usable interior pivots exist (caller falls back to log/linear).
function buildQuantileSliderRange(info) {
    const q = info.quantiles;
    if (!q || info.min === undefined || info.max === undefined || info.min >= info.max) return null;
    const pivots = Object.entries(q)
        .map(([p, v]) => [parseFloat(p), Math.min(Math.max(v, info.min), info.max)])
        .filter(([p]) => isFinite(p) && p > 0 && p < 100)
        .sort((a, b) => a[0] - b[0]);
    const range = { 'min': info.min, 'max': info.max };
    let last = info.min;
    let added = 0;
    for (const [p, v] of pivots) {
        if (v > last && v < info.max) {
            range[p + '%'] = v;
            last = v;
            added++;
        }
    }
    return added > 0 ? range : null;
}

// Poll intervals
// The updateStatus interval is now handled within window.onload

// --- Global CSRF Interaction ---
(function () {
    const originalFetch = window.fetch;
    window.fetch = function (url, options) {
        options = options || {};
        const method = options.method ? options.method.toUpperCase() : 'GET';
        if (method === 'POST' || method === 'PUT' || method === 'DELETE' || method === 'PATCH') {
            const csrfToken = document.querySelector('meta[name="csrf-token"]').getAttribute('content');
            if (csrfToken) {
                options.headers = options.headers || {};
                // If headers is an instance of Headers, append; otherwise set property
                if (options.headers instanceof Headers) {
                    options.headers.append('X-CSRFToken', csrfToken);
                } else {
                    options.headers['X-CSRFToken'] = csrfToken;
                }
            }
        }
        return originalFetch.apply(this, arguments).then(function (response) {
            if (response.status === 401) {
                window.location.href = '/login';
                return Promise.reject(new Error('Session expired'));
            }
            return response;
        });
    };
})();


// --- Theme Toggle ---
// Colour tokens that Plotly figures bake in as resolved values (Plotly cannot
// read CSS var() references). _rethemePlotlyCharts() remaps these after a
// switch in any figure its own tab did not re-render.
const _THEME_CHART_TOKENS = [
    '--chart-bg', '--chart-text', '--chart-grid', '--chart-zeroline',
    '--chart-grid-line', '--chart-overlay-line', '--chart-annotation-text',
    '--chart-regression-line', '--chart-heatmap-mid', '--chart-badge-bg',
    '--color-bg-primary', '--color-bg-surface', '--color-bg-elevated',
    '--color-text-primary', '--color-text-secondary', '--color-text-tertiary',
    '--color-text-muted', '--color-text-faint', '--color-border', '--color-border-strong',
    '--color-accent', '--color-success', '--color-info', '--color-danger', '--white',
];

function _snapshotThemeTokens() {
    const out = {};
    _THEME_CHART_TOKENS.forEach(t => { out[t] = getCSSVar(t); });
    return out;
}

function toggleTheme() {
    const html = document.documentElement;
    const current = html.getAttribute('data-theme') || 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    const before = _snapshotThemeTokens();
    // A re-render (Plotly.react/newPlot) installs a new layout object, so
    // comparing against these tells the safety net which figures to skip.
    const layouts = new Map();
    document.querySelectorAll('.js-plotly-plot').forEach(gd => layouts.set(gd, gd.layout));
    html.setAttribute('data-theme', next);
    localStorage.setItem('fyp-theme', next);
    updateThemeIcon(next);
    // Each tab re-renders its own charts from its listener (they read the
    // new token values); the safety net then patches whatever is left over.
    window.dispatchEvent(new CustomEvent('theme-changed', { detail: { theme: next } }));
    _rethemePlotlyCharts(before, _snapshotThemeTokens(), layouts);
}

function updateThemeIcon(theme) {
    const btn = document.getElementById('theme-toggle-btn');
    if (!btn) return;
    const label = theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
    btn.setAttribute('aria-label', label);
    btn.title = label;
}

// Safety net for Plotly figures whose tab has no 'theme-changed' re-render,
// or whose re-render was skipped (no cached data, an early return). Every
// colour in the figure's layout and traces that still equals an OLD token
// value is swapped for the NEW one and the figure is redrawn in place, which
// keeps zoom, selection and event handlers. Figures a tab already re-rendered
// (their layout object is no longer the one in `layouts`) are left alone: an
// old value can also be a correct new one (light --color-bg-surface and
// --white are both #ffffff), and swapping it would recolour fresh output —
// white semantic-map labels turned dark grey on a switch to dark.
function _rethemePlotlyCharts(before, after, layouts) {
    if (typeof Plotly === 'undefined') return;
    // Old value -> new value. Several tokens share a value in one theme but
    // not the other (--chart-bg and --color-bg-primary are both #1F2228 in
    // the dark theme), so the FIRST token listed wins: the chart-specific
    // tokens come first, and they are what a chart most likely meant.
    const map = new Map();
    _THEME_CHART_TOKENS.forEach(t => {
        const o = (before[t] || '').toLowerCase(), n = after[t] || '';
        if (o && n && o !== n.toLowerCase() && !map.has(o)) map.set(o, n);
    });
    if (!map.size) return;
    const swap = (v) => {
        const n = map.get(v.trim().toLowerCase());
        return n === undefined ? v : n;
    };
    // Recursive in-place remap of every string leaf; true if anything changed.
    const walk = (node, depth) => {
        if (!node || typeof node !== 'object' || depth > 12) return false;
        let changed = false;
        const keys = Array.isArray(node) ? node.keys() : Object.keys(node);
        for (const k of keys) {
            if (typeof k === 'string' && k.charAt(0) === '_') continue;  // Plotly bookkeeping
            const v = node[k];
            if (typeof v === 'string') {
                const w = swap(v);
                if (w !== v) { node[k] = w; changed = true; }
            } else if (walk(v, depth + 1)) {
                changed = true;
            }
        }
        return changed;
    };
    document.querySelectorAll('.js-plotly-plot').forEach(gd => {
        if (!gd.layout || !gd.data) return;
        if (layouts && layouts.get(gd) !== gd.layout) return;
        try {
            const a = walk(gd.layout, 0);
            const b = walk(gd.data, 0);
            if (a || b) Plotly.react(gd, gd.data, gd.layout, gd._context);
        } catch (e) {
            console.warn('theme: could not re-theme a chart', e);
        }
    });
}

// Apply saved theme immediately (before onload to avoid flash)
(function () {
    const saved = localStorage.getItem('fyp-theme');
    if (saved && saved !== document.documentElement.getAttribute('data-theme')) {
        document.documentElement.setAttribute('data-theme', saved);
    }
})();

// Initial load
window.onload = function () {
    // Apply theme icon
    const theme = document.documentElement.getAttribute('data-theme') || 'dark';
    updateThemeIcon(theme);

    updateStatus();
    // 1s ticker; _pollStatusTick stretches the effective cadence to 5s while
    // nothing is in flight (see _statusPollDelayMs).
    setInterval(_pollStatusTick, 1000);

    // Load study definitions for dropdowns
    loadDefinedStudies();

    // Load User Settings
    loadUserSettings();

    // Listener for build study name change
    const buildStudySelect = document.getElementById('build-study-name');
    if (buildStudySelect) {
        buildStudySelect.addEventListener('change', function () {
            fetchStudyFiles(this.value);
        });
    }
};

// --- User Settings Logic ---
window.userSettings = {};

async function loadUserSettings() {
    try {
        const res = await fetch('/api/user/settings');
        if (res.ok) {
            window.userSettings = await res.json();
            // Trigger UI update if settings tab is open (or just generic event)
            if (typeof renderSettingsUI === 'function') {
                renderSettingsUI();
            }
        }
    } catch (e) {
        console.error("Failed to load user settings", e);
    }
}

async function saveUserSettings(newSettings) {
    try {
        const res = await fetch('/api/user/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(newSettings)
        });
        const data = await res.json();
        if (data.status === 'success') {
            window.userSettings = { ...window.userSettings, ...newSettings };
        } else {
            console.error("Error saving settings:", data.error);
        }
    } catch (e) {
        console.error("Failed to save settings", e);
    }
}

// --- Platform links ---

// Build the "open on platform" URL for an item from its source_platform, using
// the registry-derived templates injected by the server. Returns null for an
// unknown/absent platform so callers can hide the affordance rather than open a
// wrong link — the Semantic Space map used to hardcode TikTok for every dot.
function fypPlatformUrl(platform, itemId) {
    const templates = window.PLATFORM_URL_TEMPLATES || {};
    const template = platform ? templates[platform] : null;
    if (!template || !itemId) return null;
    return template.replace('{item_id}', encodeURIComponent(itemId));
}

// Home-tab getting-started panel: hide now, persist the one-shot dismissal.
async function dismissGettingStarted() {
    const panel = document.getElementById('getting-started-panel');
    if (panel) panel.style.display = 'none';
    await saveUserSettings({ getting_started_dismissed: true });
}

// The reverse, offered from My stuff -> Preferences and the help modal. The
// panel is rendered by Jinja on page load, so it only exists in the DOM when it
// was dismissed during this same session; otherwise a reload is what brings it
// back.
async function restoreGettingStarted() {
    await saveUserSettings({ getting_started_dismissed: false });
    if (typeof renderSettingsUI === 'function') renderSettingsUI();
    const panel = document.getElementById('getting-started-panel');
    if (panel) {
        panel.style.display = '';
        _navigateToTabPage('home');
        panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
    }
    window.location.hash = '#home';
    window.location.reload();
}

// --- Tag Management ---
// Show / hide the "My Video Tags" sidebar item — like "My Tasks", it only
// earns a menu slot once the user actually has something on the page.
function _setVideoTagsMenuVisible(visible) {
    const item = document.querySelector('#my_stuff .dm-sidebar-item[data-page="my-stuff-page-video-tags"]');
    if (!item) return;
    item.style.display = visible ? '' : 'none';
    // Refresh the mobile subnav so the item (dis)appears there too.
    if (typeof _buildTabSubnavs === 'function') _buildTabSubnavs();
}

async function loadAndRenderUserTags() {
    const container = document.getElementById('settings-tags-container');
    if (!container) return;

    container.innerHTML = '<span style="color: var(--color-text-tertiary);">Loading...</span>';

    try {
        const res = await fetch('/api/video_analysis/tags');
        const tagsData = await res.json();

        // Flatten and Count
        const tagCounts = {};
        Object.values(tagsData).forEach(item => {
            Object.values(item).forEach(tagList => {
                if (Array.isArray(tagList)) {
                    tagList.forEach(t => {
                        tagCounts[t] = (tagCounts[t] || 0) + 1;
                    });
                }
            });
        });

        // Sort by Count Descending
        const sortedTags = Object.entries(tagCounts).sort((a, b) => b[1] - a[1]);

        _setVideoTagsMenuVisible(sortedTags.length > 0);

        if (sortedTags.length === 0) {
            container.innerHTML = '<span class="italic" style="color: var(--color-text-faint);">No tags found.</span>';
            return;
        }

        container.innerHTML = '';
        sortedTags.forEach(([tag, count]) => {
            const chip = document.createElement('div');
            chip.style.cssText = `
                background: var(--color-border-subtle);
                color: var(--color-text-primary);
                border: 1px solid var(--color-border-strong);
                padding: 4px 10px;
                border-radius: 12px;
                display: flex;
                gap: 8px;
                align-items: center;
            `;
            chip.classList.add('text-sm');

            chip.innerHTML = `
                <span>${escapeHtml(tag)} <span class="text-xs" style="color: var(--color-text-muted);">(${escapeHtml(count)})</span></span>
                <span class="delete-tag-btn font-bold" style="cursor: pointer; color: var(--color-danger-soft);" title="Delete Tag">×</span>
            `;

            chip.querySelector('.delete-tag-btn').onclick = () => deleteUserTag(tag);
            container.appendChild(chip);
        });

    } catch (e) {
        console.error(e);
        container.innerHTML = '<span style="color: var(--color-danger-soft);">Error loading tags.</span>';
    }
}

async function deleteUserTag(tagName) {
    if (!(await showAppConfirm(
        `Are you sure you want to delete the tag "${tagName}"? This will remove it from all videos and cannot be undone.`,
        { title: 'Delete tag', okLabel: 'Delete', danger: true }))) {
        return;
    }

    try {
        // Tag name needs to be URL encoded properly, but Flask path param handles basic, 
        // explicit encodeURIComponent is safer for special chars.
        const res = await fetch(`/api/video_analysis/tags/${encodeURIComponent(tagName)}`, {
            method: 'DELETE'
        });
        const data = await res.json();

        if (data.status === 'success') {
            // Reload tags
            loadAndRenderUserTags();
        } else {
            showAppAlert("Error deleting tag: " + (data.message || "Unknown error"));
        }

    } catch (e) {
        console.error(e);
        showAppAlert("Failed to delete tag.");
    }
}

const _LARGE_LOAD_THRESHOLD = 100000;

function showLargeStudyLoadWarning(studyName, uniqueVideos) {
    return new Promise(resolve => {
        const overlay = document.getElementById('large-load-warning');
        const textEl = document.getElementById('large-load-warning-text');
        textEl.innerHTML = `<strong>${escapeHtml(studyName)}</strong> contains ${uniqueVideos.toLocaleString()} items. ` +
            `Loading this study may take a moment and app performance may be affected.`;
        overlay.classList.add('visible');
        document.getElementById('large-load-warning-back').onclick = () => {
            overlay.classList.remove('visible');
            resolve(false);
        };
        document.getElementById('large-load-warning-continue').onclick = () => {
            overlay.classList.remove('visible');
            resolve(true);
        };
    });
}

async function loadDefinedStudies() {
    try {
        const response = await fetch('/api/studies/defined');
        const studies = await response.json();

        const dropdownIds = [
            'global-study-name',
            'build-study-name'
        ];

        dropdownIds.forEach(id => {
            const select = document.getElementById(id);
            if (select) {
                const currentValue = select.value;

                // Keep the first "Select..." option if it exists and value is empty
                let hasDefault = false;
                if (select.options.length > 0 && select.options[0].value === "") {
                    hasDefault = true;
                }

                // Clear existing options except default
                select.innerHTML = '';
                if (hasDefault) {
                    const defaultOption = document.createElement('option');
                    defaultOption.value = "";
                    defaultOption.text = "Select a study...";
                    defaultOption.disabled = true;
                    defaultOption.selected = true;
                    select.appendChild(defaultOption);
                }

                studies.forEach(study => {
                    const option = document.createElement('option');
                    option.value = study;
                    option.text = study;
                    select.appendChild(option);
                });

                // Preserve previous selection if still available
                if (currentValue && studies.includes(currentValue)) {
                    select.value = currentValue;
                }
            }
        });

    } catch (e) {
        console.error("Error loading defined studies:", e);
    }
}

let _appDialogResolver = null;

function _closeAppDialog(result) {
    const overlay = document.getElementById('app-dialog-overlay');
    if (overlay) overlay.classList.remove('visible');
    document.removeEventListener('keydown', _appDialogKeydown);
    if (_appDialogResolver) {
        const r = _appDialogResolver;
        _appDialogResolver = null;
        r(result);
    }
}

function _appDialogKeydown(e) {
    if (e.key === 'Escape') _closeAppDialog(false);
    else if (e.key === 'Enter') _closeAppDialog(true);
}

function _showAppDialog({ message, title = null, okLabel = 'OK', cancelLabel = null, danger = false }) {
    const overlay = document.getElementById('app-dialog-overlay');
    // Defensive fallback if the markup isn't on the page.
    if (!overlay) {
        if (cancelLabel === null) { window.alert(message); return Promise.resolve(true); }
        return Promise.resolve(window.confirm(message));
    }
    // Resolve any dialog already open as cancelled before showing a new one.
    if (_appDialogResolver) _closeAppDialog(false);

    const titleEl = document.getElementById('app-dialog-title');
    const textEl = document.getElementById('app-dialog-text');
    const okBtn = document.getElementById('app-dialog-ok-btn');
    const cancelBtn = document.getElementById('app-dialog-cancel-btn');

    titleEl.textContent = title || '';
    titleEl.style.display = title ? 'block' : 'none';
    textEl.textContent = message == null ? '' : String(message);
    okBtn.textContent = okLabel || 'OK';
    okBtn.className = danger ? 'btn-stop' : 'btn-primary';
    cancelBtn.textContent = cancelLabel || 'Cancel';
    cancelBtn.style.display = cancelLabel === null ? 'none' : '';

    okBtn.onclick = () => _closeAppDialog(true);
    cancelBtn.onclick = () => _closeAppDialog(false);
    overlay.onclick = (e) => { if (e.target === overlay) _closeAppDialog(false); };

    document.addEventListener('keydown', _appDialogKeydown);
    overlay.classList.add('visible');
    setTimeout(() => { try { okBtn.focus(); } catch (_) { } }, 50);
    return new Promise(resolve => { _appDialogResolver = resolve; });
}

// Pretty alert: one OK button. Resolves when dismissed. Safe to fire-and-forget.
function showAppAlert(message, opts = {}) {
    return _showAppDialog({ ...opts, message, cancelLabel: null });
}

// Pretty confirm: OK + Cancel. Resolves to true (OK) / false (Cancel/Esc/backdrop).
function showAppConfirm(message, opts = {}) {
    return _showAppDialog({
        okLabel: 'OK',
        cancelLabel: 'Cancel',
        ...opts,
        message,
    });
}


function openTab(evt, tabName) {
    // Hide all tab panes
    const tabPanes = document.getElementsByClassName("tab-pane");
    for (let i = 0; i < tabPanes.length; i++) {
        tabPanes[i].className = tabPanes[i].className.replace(" active", "");
    }

    // Remove active class from all buttons
    const tabButtons = document.getElementsByClassName("tab-button");
    for (let i = 0; i < tabButtons.length; i++) {
        tabButtons[i].className = tabButtons[i].className.replace(" active", "");
    }

    // Show current tab and activate button
    const tab = document.getElementById(tabName);
    if (tab) {
        tab.className += " active";
    }
    if (evt && evt.currentTarget) {
        evt.currentTarget.className += " active";
    }

    // Lazy tab loading: a pane hydrated while hidden records a pending load;
    // flush it on activation (see ensure*Loaded in each tab's JS).
    if (tabName === 'explore' && typeof window.ensureExploreLoaded === 'function') {
        window.ensureExploreLoaded();
    }
    if (tabName === 'correlations' && typeof window.ensureCorrelationsLoaded === 'function') {
        window.ensureCorrelationsLoaded();
    }
    if (tabName === 'timelines' && typeof window.ensureTimelinesLoaded === 'function') {
        window.ensureTimelinesLoaded();
    }

    // Video Viewer Logic integration
    if (tabName !== 'video_analysis') {
        if (typeof pauseViewerVideo === 'function') pauseViewerVideo();
    } else {
        const proceedWithViewer = () => {
            // Drill-down from Explore tab: apply pending filters before anything else
            if (typeof checkPendingDrillDown === 'function') {
                checkPendingDrillDown();
            }

            if (typeof playViewerVideo === 'function') {
                // Check User Settings for Autostart
                // If undefined, default to false (autostart is off by default)
                if (window.userSettings && window.userSettings.video_autostart) {
                    playViewerVideo();
                }
            }
        };
        if (typeof window.ensureViewerLoaded === 'function') {
            window.ensureViewerLoaded().then(proceedWithViewer);
        } else {
            proceedWithViewer();
        }
    }



    // My stuff tab logic (preferences / tags / profile forms)
    if (tabName === 'my_stuff' && typeof renderSettingsUI === 'function') {
        renderSettingsUI();
    }

    // Semantic Space - lazy-load the global video map on first open
    if (tabName === 'semantic_space' && typeof initSemanticSpace === 'function') {
        initSemanticSpace();
    }

    // Sessions - lazy init on first open; pause its episode players on tab-away
    if (tabName !== 'sessions') {
        if (typeof pauseSessionsVideos === 'function') pauseSessionsVideos();
    } else if (typeof initSessions === 'function') {
        initSessions();
    }

    // Trigger window resize so any charts (Plotly, etc.) can recalculate their width now that their container is visible
    setTimeout(() => {
        window.dispatchEvent(new Event('resize'));
    }, 100);

    // Update the mobile current-tab label next to the hamburger
    _setCurrentTabLabel(tabName, evt);

    // Close mobile nav drawer (if open) once a tab is selected
    closeNavDrawer();

    // Keep the URL hash in sync (a sub-page opener may refine it to
    // #tab/sub-page right after).
    if (!_applyingHashNav) {
        history.replaceState(null, '', `#${tabName}`);
    }
}

// ============================================================
// Hash deep-linking (#tab or #tab/sub-page, e.g. #admin/backends)
// ============================================================

// Sidebar page-id prefix per tab with sub-pages; the hash carries the id
// minus this prefix ("admin-page-backends" → "#admin/backends").
const _HASH_PAGE_PREFIX = {
    admin: 'admin-page-',
    data_management: 'dm-page-',
    my_stuff: 'my-stuff-page-',
};

// True while a hash is being applied, so openTab / the sub-page openers
// don't rewrite the hash mid-navigation.
let _applyingHashNav = false;

// Called by the sub-page openers (openAdminPage / openDataManagementPage /
// openMyStuffPage) after they switch pages.
function updateSubPageHash(tabId, pageId) {
    if (_applyingHashNav) return;
    const prefix = _HASH_PAGE_PREFIX[tabId];
    if (!prefix || !pageId) return;
    const slug = pageId.startsWith(prefix) ? pageId.slice(prefix.length) : pageId;
    history.replaceState(null, '', `#${tabId}/${slug}`);
}

// Switch to a tab (and optionally one of its sidebar sub-pages) the same way
// a user click would — used by the mobile sub-nav and by hash navigation.
function _navigateToTabPage(tabId, pageId) {
    const pane = document.getElementById(tabId);
    if (!pane) return;
    const activePane = document.querySelector('.tab-pane.active');
    if (!activePane || activePane.id !== tabId) {
        // Call openTab directly (no event) to avoid re-triggering the
        // capture-phase expand handler on the tab button.
        openTab(null, tabId);
        // openTab clears all .tab-button.active and only re-applies from
        // evt.currentTarget; manually set the active state and label.
        const tabBtn = document.querySelector(
            `.tab-button[data-tab="${tabId}"], .tab-button[data-subpages-for="${tabId}"]`);
        if (tabBtn) {
            tabBtn.classList.add('active');
            const label = document.getElementById('current-tab-label');
            if (label) label.textContent = tabBtn.textContent.trim();
        }
        if (tabId === 'admin' && typeof loadUsers === 'function') {
            loadUsers();
        }
    }
    if (pageId) {
        const sidebarItem = pane.querySelector(`.dm-sidebar .dm-sidebar-item[data-page="${pageId}"]`);
        if (sidebarItem) sidebarItem.click();
    }
}

// Sub-page slugs that were renamed; old deep links keep working.
const _HASH_SLUG_ALIASES = {
    'data_management/enrichment': 'scrape',
};

function _applyHashNavigation() {
    // Optional third segment = a page action ("#my_stuff/my-collections/upload"
    // from the participation wizard). Stashed for the page's own JS to consume
    // once its content has loaded (see loadMyCollections).
    const m = (location.hash || '').match(/^#([a-z_]+)(?:\/([a-z0-9-]+))?(?:\/([a-z]+))?$/);
    if (!m) return;
    const tabId = m[1];
    let slug = m[2];
    window.PENDING_SUBPAGE_ACTION = m[3] || null;
    if (slug) slug = _HASH_SLUG_ALIASES[`${tabId}/${slug}`] || slug;
    const pane = document.getElementById(tabId);
    // Only navigate to real tab panes the current user can see.
    if (!pane || !pane.classList.contains('tab-pane')) return;
    const pageId = slug ? (_HASH_PAGE_PREFIX[tabId] || '') + slug : null;
    _applyingHashNav = true;
    try {
        _navigateToTabPage(tabId, pageId);
    } finally {
        _applyingHashNav = false;
    }
}

const _TAB_TITLE_MAP = {
    home: 'Home',
    explore: 'Explore',
    timelines: 'Timelines',
    video_analysis: 'Video Analysis',
    correlations: 'Correlations',
    semantic_space: 'Semantic Space',
    sessions: 'Sessions',
    my_stuff: 'My stuff',
    data_management: 'Data Pipeline',
    admin: 'Admin'
};

function _setCurrentTabLabel(tabName, evt) {
    const label = document.getElementById('current-tab-label');
    if (!label) return;
    let text = '';
    if (evt && evt.currentTarget && evt.currentTarget.textContent) {
        text = evt.currentTarget.textContent.trim();
    }
    if (!text) text = _TAB_TITLE_MAP[tabName] || tabName;
    label.textContent = text;
}


// ============================================================
// Small-screen drawer helpers (header nav + side panels)
// ============================================================

function _responsiveBackdrop() {
    return document.getElementById('responsive-backdrop');
}

function _isMobileViewport() {
    return window.matchMedia('(max-width: 1100px)').matches;
}

function _showBackdrop() {
    const bd = _responsiveBackdrop();
    if (bd) bd.classList.add('is-visible');
}

function _hideBackdropIfIdle() {
    const bd = _responsiveBackdrop();
    if (!bd) return;
    const navOpen = document.getElementById('main-tab-nav')?.classList.contains('is-open');
    const anyPanelOpen = !!document.querySelector('[data-mobile-drawer].is-open');
    if (!navOpen && !anyPanelOpen) {
        bd.classList.remove('is-visible');
    }
}

function toggleNavDrawer() {
    const nav = document.getElementById('main-tab-nav');
    if (!nav) return;
    const willOpen = !nav.classList.contains('is-open');

    // Close any open panel drawers first
    document.querySelectorAll('[data-mobile-drawer].is-open').forEach(p => p.classList.remove('is-open'));

    nav.classList.toggle('is-open', willOpen);
    const hamburger = document.getElementById('nav-hamburger');
    if (hamburger) hamburger.setAttribute('aria-expanded', String(willOpen));

    if (willOpen) {
        _showBackdrop();
        _syncTabSubnavExpansion();
    } else {
        _hideBackdropIfIdle();
    }
}

// ----- Two-level mobile menu (chevron + nested sub-pages) -----

function _buildTabSubnavs() {
    document.querySelectorAll('.tab-subnav[data-subpages-of]').forEach(ul => {
        const tabId = ul.getAttribute('data-subpages-of');
        const pane = document.getElementById(tabId);
        if (!pane) return;
        // Fresh build — clear any prior content (idempotent). Walk items and
        // section headers in document order so groups carry over.
        ul.innerHTML = '';
        pane.querySelectorAll('.dm-sidebar .dm-sidebar-item, .dm-sidebar .dm-sidebar-group').forEach(src => {
            if (src.classList.contains('dm-sidebar-group')) {
                const li = document.createElement('li');
                li.className = 'tab-subnav-group';
                li.textContent = src.textContent.trim();
                ul.appendChild(li);
                return;
            }
            // External reference links (My stuff -> Information): mirror them
            // as anchors, not sub-page rows. A distinct class keeps them out of
            // the .tab-subnav-item handler below, which preventDefault()s.
            if (src.tagName === 'A') {
                const li = document.createElement('li');
                li.className = 'tab-subnav-linkitem';
                const a = document.createElement('a');
                a.href = src.href;
                a.target = src.target || '_blank';
                a.rel = 'noopener';
                a.title = src.title || '';
                a.innerHTML = src.innerHTML;
                li.appendChild(a);
                ul.appendChild(li);
                return;
            }
            const pageId = src.getAttribute('data-page');
            if (!pageId) return;
            // Skip items hidden by feature logic (e.g. "My Tasks" until the
            // user has coding invitations) — rebuilt when they are unhidden.
            if (src.style.display === 'none') return;
            const li = document.createElement('li');
            li.className = 'tab-subnav-item';
            li.setAttribute('data-target-tab', tabId);
            li.setAttribute('data-target-page', pageId);
            if (src.classList.contains('active')) {
                li.classList.add('active');
            }
            // Read the label without any appended badge (e.g. task count).
            const clone = src.cloneNode(true);
            clone.querySelectorAll('.hc-tab-badge').forEach(b => b.remove());
            li.textContent = clone.textContent.trim();
            ul.appendChild(li);
        });
    });
}

function _collapseAllTabSubnavs(exceptTabId) {
    document.querySelectorAll('.tab-button.has-subpages').forEach(btn => {
        const tabId = btn.getAttribute('data-subpages-for');
        if (tabId === exceptTabId) return;
        btn.classList.remove('is-expanded');
        const ul = document.querySelector(`.tab-subnav[data-subpages-of="${tabId}"]`);
        if (ul) ul.hidden = true;
    });
}

function _toggleTabSubnav(button) {
    const tabId = button.getAttribute('data-subpages-for');
    if (!tabId) return;
    const ul = document.querySelector(`.tab-subnav[data-subpages-of="${tabId}"]`);
    if (!ul) return;
    const willExpand = !button.classList.contains('is-expanded');
    _collapseAllTabSubnavs(tabId);
    button.classList.toggle('is-expanded', willExpand);
    ul.hidden = !willExpand;
}

function _syncTabSubnavExpansion() {
    // When the drawer opens, expand the sub-nav for the currently-active tab
    // (if it has sub-pages) so the user sees where they are.
    const activePane = document.querySelector('.tab-pane.active');
    if (!activePane) {
        _collapseAllTabSubnavs(null);
        return;
    }
    const activeTabId = activePane.id;
    const expandBtn = document.querySelector(`.tab-button.has-subpages[data-subpages-for="${activeTabId}"]`);
    if (!expandBtn) {
        _collapseAllTabSubnavs(null);
        return;
    }
    _collapseAllTabSubnavs(activeTabId);
    expandBtn.classList.add('is-expanded');
    const ul = document.querySelector(`.tab-subnav[data-subpages-of="${activeTabId}"]`);
    if (ul) {
        ul.hidden = false;
        // Mirror the .active state from the original sidebar onto the sub-rows.
        const activeSidebarItem = activePane.querySelector('.dm-sidebar .dm-sidebar-item.active');
        const activePage = activeSidebarItem ? activeSidebarItem.getAttribute('data-page') : null;
        ul.querySelectorAll('.tab-subnav-item').forEach(li => {
            li.classList.toggle('active', li.getAttribute('data-target-page') === activePage);
        });
    }
}

function closeNavDrawer() {
    const nav = document.getElementById('main-tab-nav');
    if (!nav || !nav.classList.contains('is-open')) return;
    nav.classList.remove('is-open');
    const hamburger = document.getElementById('nav-hamburger');
    if (hamburger) hamburger.setAttribute('aria-expanded', 'false');
    _hideBackdropIfIdle();
}

function toggleMobileDrawer(targetSelector) {
    if (!targetSelector) return;
    const target = document.querySelector(targetSelector);
    if (!target) return;

    const willOpen = !target.classList.contains('is-open');

    // Close any other open drawers (panels + nav) — single-drawer-at-a-time policy
    document.querySelectorAll('[data-mobile-drawer].is-open').forEach(p => {
        if (p !== target) p.classList.remove('is-open');
    });
    closeNavDrawer();

    target.classList.toggle('is-open', willOpen);
    if (willOpen) {
        _showBackdrop();
    } else {
        _hideBackdropIfIdle();
    }
}

function closeAllResponsiveDrawers() {
    document.querySelectorAll('[data-mobile-drawer].is-open').forEach(p => p.classList.remove('is-open'));
    closeNavDrawer();
    _hideBackdropIfIdle();
}

// Wire two-level mobile menu and auto-close behaviour.
document.addEventListener('DOMContentLoaded', function () {
    // Backwards-compat: any remaining .mobile-drawer-trigger buttons still work.
    document.querySelectorAll('.mobile-drawer-trigger').forEach(btn => {
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            const target = this.getAttribute('data-drawer-target');
            toggleMobileDrawer(target);
        });
    });

    // Build the nested sub-page lists once on load.
    _buildTabSubnavs();

    // Deep links: honour a #tab/sub-page hash on load and on back/forward.
    _applyHashNavigation();
    window.addEventListener('hashchange', _applyHashNavigation);

    // Intercept clicks on top-level tab buttons that have sub-pages: on mobile
    // they expand the nested list instead of navigating. Capture phase +
    // stopImmediatePropagation blocks the inline onclick on the same element.
    document.querySelectorAll('.tab-button.has-subpages').forEach(btn => {
        btn.addEventListener('click', function (e) {
            if (!_isMobileViewport()) return;
            e.stopImmediatePropagation();
            e.preventDefault();
            _toggleTabSubnav(this);
        }, true);
    });

    // Sub-nav item click → switch tab (if needed) and open the sub-page.
    document.addEventListener('click', function (e) {
        const sub = e.target.closest('.tab-subnav-item');
        if (!sub) return;
        e.stopPropagation();
        e.preventDefault();
        const tabId = sub.getAttribute('data-target-tab');
        const pageId = sub.getAttribute('data-target-page');
        if (!document.getElementById(tabId)) return;
        _navigateToTabPage(tabId, pageId);
        // Mirror active state onto the sub-rows.
        sub.parentElement.querySelectorAll('.tab-subnav-item').forEach(li => li.classList.remove('active'));
        sub.classList.add('active');
        closeNavDrawer();
    });

    // Reference links mirrored into the sub-nav open in a new tab; close the
    // drawer so the Hub isn't left behind a half-open overlay.
    document.addEventListener('click', function (e) {
        if (e.target.closest('.tab-subnav-linkitem')) closeNavDrawer();
    });

    // When a sidebar item inside a mobile drawer is clicked, close the drawer
    // so the user sees the page they just selected.
    document.addEventListener('click', function (e) {
        if (!_isMobileViewport()) return;
        const item = e.target.closest('.dm-sidebar-item');
        if (item) {
            // small delay so the sidebar's own click handler runs first
            setTimeout(closeAllResponsiveDrawers, 0);
        }
    });

    // Close on Escape
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') closeAllResponsiveDrawers();
    });

    // If user resizes from mobile to desktop while a drawer is open, clean up
    window.addEventListener('resize', function () {
        if (!_isMobileViewport()) closeAllResponsiveDrawers();
    });
});

// --- Presentation mode (wide screens) ---
// Hides the header + tab menu behind a thin strip at the top of the window;
// hovering the strip slides them back in (style.css does the sliding).
// Session-only on purpose: a presentation ends, the next visit starts normal.
function toggleFocusMode(force) {
    const on = typeof force === 'boolean' ? force : !document.body.classList.contains('is-focus-mode');
    document.body.classList.toggle('is-focus-mode', on);
    const btn = document.getElementById('focus-toggle-btn');
    if (btn) {
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        const label = on ? 'Show the header and menu' : 'Hide the header and menu';
        btn.setAttribute('aria-label', label);
        btn.title = on ? label : label + ' (Esc restores them)';
        // Leaving focus on the button would hold the chrome open via
        // :focus-within; the menu should slide away as soon as it is hidden.
        if (on) btn.blur();
    }
    // Plotly's responsive figures only resize on a window event; the content
    // area just changed height without one. Fire it once the slide is over.
    setTimeout(() => window.dispatchEvent(new Event('resize')), 250);
}

document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && document.body.classList.contains('is-focus-mode')) toggleFocusMode(false);
});

// Peeking: CSS :hover on the strip does the sliding on its own; this mirrors
// it in a body class so a tap on the strip (no hover on touch screens) and
// the pointer's hand-off from strip to chrome are both covered.
document.addEventListener('DOMContentLoaded', function () {
    const peek = document.getElementById('focus-peek');
    const chrome = document.getElementById('app-chrome');
    if (!peek || !chrome) return;
    const show = () => document.body.classList.add('is-peeking');
    const hide = () => document.body.classList.remove('is-peeking');
    peek.addEventListener('mouseenter', show);
    peek.addEventListener('click', show);
    chrome.addEventListener('mouseleave', hide);
    // The chrome slides over the strip, so leaving the strip downward lands
    // on the chrome (kept open by its own hover); leaving any other way hides.
    peek.addEventListener('mouseleave', (e) => {
        if (!(e.relatedTarget && chrome.contains(e.relatedTarget))) hide();
    });
});

// Expose for inline handlers
window.toggleFocusMode = toggleFocusMode;
window.toggleNavDrawer = toggleNavDrawer;
window.toggleMobileDrawer = toggleMobileDrawer;
window.closeAllResponsiveDrawers = closeAllResponsiveDrawers;


async function fetchStudyFiles(studyName) {
    if (!studyName) return;

    const container = document.getElementById('study-export-files-container');
    if (!container) return;

    container.innerHTML = '<p>Loading...</p>';

    try {
        const res = await fetch(`/api/study_files/${studyName}`);
        const files = await res.json();

        if (files.error) {
            container.innerHTML = `<p style="color: var(--color-danger-soft);">Error: ${files.error}</p>`;
            return;
        }

        let html = '<ul style="list-style: none; padding-left: 0; margin-top: 5px;">';
        // Order: HALF_BAKED, UNIQUE, LOG, RECODED, PCA (Custom order if desired, or just iterate)
        const order = ["HALF_BAKED", "UNIQUE", "LOG", "RECODED", "PCA"];

        order.forEach(category => {
            if (files[category]) {
                // Human-readable label per category key; unknown keys show as-is.
                const labelMap = {
                    "HALF_BAKED": "Half-Baked Datasets",
                    "UNIQUE": "Unique Subsets",
                    "LOG": "Event Log",
                    "RECODED": "Recoded Log",
                    "PCA": "PCA Scores"
                };
                const label = labelMap[category] || category;

                html += `<li style="margin-bottom: 5px;">
                   <strong style="color: var(--color-text-primary);">${label}:</strong> <span style="color: var(--color-text-tertiary);">${files[category]}</span>
               </li>`;
            }
        });
        html += '</ul>';
        container.innerHTML = html;

    } catch (e) {
        console.error(e);
        container.innerHTML = `<p style="color: var(--color-danger-soft);">Failed to load files.</p>`;
    }
}
