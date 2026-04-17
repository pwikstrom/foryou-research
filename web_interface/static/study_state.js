// Global study state — single source of truth for the active study across the
// Explore, Video Analysis, Correlations, and Timelines tabs.
//
// Other modules consume this via:
//   - window.studyState.current         (string | null)
//   - window.studyState.stats[name]     (per-study stats from /api/studies/defined?detail=true)
//   - window.studyState.studies         (string[])
//   - window.studyState.ready           (Promise — resolves after initial hydration)
// And listen for study changes via:
//   document.addEventListener('study:changed', (e) => { e.detail.study; e.detail.previous; });

const _LARGE_LOAD_THRESHOLD_STUDY_STATE = 100000;
const _STUDY_STORAGE_KEY = 'fyp.activeStudy';

window.studyState = {
    current: null,
    previous: null,
    stats: {},
    studies: [],
    ready: null,
    _readyResolve: null,
    _lastUserPick: null
};

window.studyState.ready = new Promise((resolve) => {
    window.studyState._readyResolve = resolve;
});

function _getGlobalStudySelect() {
    return document.getElementById('global-study-select');
}

function _populateStudySelect(select, studies, current) {
    if (!select) return;
    select.innerHTML = '';
    if (!studies || studies.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.disabled = true;
        opt.selected = true;
        opt.textContent = 'No studies available';
        select.appendChild(opt);
        return;
    }
    studies.forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        if (name === current) opt.selected = true;
        select.appendChild(opt);
    });
}

async function _showLargeStudyConfirm(studyName, uniqueVideos) {
    // Reuse the existing #large-load-warning overlay defined in index.html
    if (typeof showLargeStudyLoadWarning === 'function') {
        return await showLargeStudyLoadWarning(studyName, uniqueVideos);
    }
    return true;
}

async function setActiveStudy(name, options = {}) {
    const { silent = false } = options;
    const select = _getGlobalStudySelect();
    const previous = window.studyState.current;

    if (!name) {
        window.studyState.previous = previous;
        window.studyState.current = null;
        try { localStorage.removeItem(_STUDY_STORAGE_KEY); } catch (e) { /* ignore */ }
        if (!silent) {
            document.dispatchEvent(new CustomEvent('study:changed', {
                detail: { study: null, previous: previous }
            }));
        }
        return true;
    }

    if (name === previous) {
        if (select && select.value !== name) select.value = name;
        return true;
    }

    // Size guard for user-initiated changes.
    if (!silent) {
        const stats = window.studyState.stats[name] || {};
        const uniqueVids = stats.unique_videos || 0;
        if (uniqueVids > _LARGE_LOAD_THRESHOLD_STUDY_STATE) {
            const proceed = await _showLargeStudyConfirm(name, uniqueVids);
            if (!proceed) {
                // Revert dropdown to previous selection and bail.
                if (select) {
                    select.value = previous || '';
                }
                return false;
            }
        }
    }

    window.studyState.previous = previous;
    window.studyState.current = name;
    try { localStorage.setItem(_STUDY_STORAGE_KEY, name); } catch (e) { /* ignore */ }

    if (select && select.value !== name) select.value = name;

    if (!silent) {
        document.dispatchEvent(new CustomEvent('study:changed', {
            detail: { study: name, previous: previous }
        }));
    }
    return true;
}

async function loadStudiesGlobal(options = {}) {
    const { preserveCurrent = false } = options;
    const select = _getGlobalStudySelect();

    try {
        const res = await fetch('/api/studies/defined?detail=true');
        const payload = await res.json();

        // Normalise response: may be a list of names or list of {name, stats}.
        const studies = [];
        const statsMap = {};
        (payload || []).forEach(item => {
            if (typeof item === 'string') {
                studies.push(item);
            } else if (item && item.name) {
                studies.push(item.name);
                statsMap[item.name] = item.stats || {};
            }
        });

        window.studyState.studies = studies;
        window.studyState.stats = statsMap;

        // Pick initial study.
        let chosen = null;
        if (preserveCurrent && window.studyState.current && studies.includes(window.studyState.current)) {
            chosen = window.studyState.current;
        } else {
            let stored = null;
            try { stored = localStorage.getItem(_STUDY_STORAGE_KEY); } catch (e) { /* ignore */ }
            if (stored && studies.includes(stored)) {
                chosen = stored;
            } else if (studies.length > 0) {
                chosen = studies[0];
            }
        }

        _populateStudySelect(select, studies, chosen);

        if (select) {
            select.style.display = studies.length > 0 ? '' : 'none';
        }

        if (!preserveCurrent) {
            // Initial hydration: set silently so we don't prompt the size guard
            // and so tabs can init via studyState.ready themselves.
            await setActiveStudy(chosen, { silent: true });
        } else if (chosen && chosen !== window.studyState.current) {
            // After a save/refresh: if the stored current disappeared, swap silently.
            await setActiveStudy(chosen, { silent: true });
        }

    } catch (e) {
        console.error('Failed to load studies:', e);
        if (select) {
            select.innerHTML = '<option disabled selected>Error loading studies</option>';
        }
    }
}

// Public API for other modules (e.g. data_management after a study save).
window.studyState.reload = function () {
    return loadStudiesGlobal({ preserveCurrent: true });
};

document.addEventListener('DOMContentLoaded', function () {
    const select = _getGlobalStudySelect();
    if (select) {
        select.addEventListener('change', async (e) => {
            const nextName = e.target.value;
            const ok = await setActiveStudy(nextName);
            if (!ok) {
                // setActiveStudy already reverted select.value on cancel.
                return;
            }
        });
    }

    loadStudiesGlobal().finally(() => {
        if (window.studyState._readyResolve) {
            window.studyState._readyResolve();
            window.studyState._readyResolve = null;
        }
    });
});
