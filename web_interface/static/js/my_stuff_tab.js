    const MY_STUFF_PAGE_PERM_MAP = {
        'my-stuff-page-studies':     'tab.my_stuff.my_studies',
        'my-stuff-page-tasks':       'tab.my_stuff.tasks',
        'my-stuff-page-preferences': 'tab.my_stuff.preferences',
        'my-stuff-page-video-tags':  'tab.my_stuff.video_tags',
        'my-stuff-page-profile':     'tab.my_stuff.profile',
    };

    function openMyStuffPage(pageId, clickedItem) {
        // Defense in depth — if the matching permission isn't granted, refuse.
        const requiredPerm = MY_STUFF_PAGE_PERM_MAP[pageId];
        if (requiredPerm && Array.isArray(window.USER_PERMS) && !window.USER_PERMS.includes(requiredPerm)) {
            return;
        }

        document.querySelectorAll('#my_stuff .dm-page').forEach(page => {
            page.classList.remove('active');
        });

        document.querySelectorAll('#my_stuff .dm-sidebar-item').forEach(item => {
            item.classList.remove('active');
        });

        const page = document.getElementById(pageId);
        if (page) {
            page.classList.add('active');
        }

        if (clickedItem) {
            clickedItem.classList.add('active');
        }

        if (typeof updateSubPageHash === 'function') {
            updateSubPageHash('my_stuff', pageId);
        }
    }

    // Called when the My stuff tab is opened or user settings are loaded.
    // The name is a cross-file contract: main.js calls renderSettingsUI() from
    // loadUserSettings() and openTab().
    function renderSettingsUI() {
        const autostart = document.getElementById('setting-video-autostart');
        if (autostart) {
            // Default to false if not set
            autostart.checked = !!(window.userSettings && window.userSettings.video_autostart);

            // Share Annotations (opt-in: unset reads as off)
            const shareAnnotations = document.getElementById('setting-share-annotations');
            if (shareAnnotations) {
                shareAnnotations.checked = !!(window.userSettings && window.userSettings.share_annotations);
            }

            // Big dots in correlations
            const bigDots = document.getElementById('setting-big-dots');
            if (bigDots) {
                bigDots.checked = !!(window.userSettings && window.userSettings.big_dots);
            }

            // Timelines: include empty dates (default false)
            const tlIncludeEmpty = document.getElementById('setting-timelines-include-empty-dates');
            if (tlIncludeEmpty) {
                tlIncludeEmpty.checked = !!(window.userSettings && window.userSettings.timelines_include_empty_dates);
            }

            // Timelines: include pre-activity (default false)
            const tlIncludePre = document.getElementById('setting-timelines-include-pre-activity');
            if (tlIncludePre) {
                tlIncludePre.checked = !!(window.userSettings && window.userSettings.timelines_include_pre_activity);
            }
        }

        // Load tags (My Video Tags page may be permission-hidden)
        if (document.getElementById('settings-tags-container') && typeof loadAndRenderUserTags === 'function') {
            loadAndRenderUserTags();
        }

        // Sync theme checkbox with current theme
        const themeToggle = document.getElementById('setting-theme-toggle');
        if (themeToggle) {
            const currentTheme = document.documentElement.getAttribute('data-theme') || 'dark';
            themeToggle.checked = currentTheme === 'dark';
        }

        renderVariablePrefsStatus();
        loadProfileForm();
    }

    function renderVariablePrefsStatus() {
        const el = document.getElementById('variable-prefs-status');
        if (!el || !window.VariablePrefs) return;
        const customized = ['filter', 'display', 'timeline', 'viz'].filter(s => VariablePrefs.isCustomized(s));
        el.textContent = customized.length
            ? `Active customizations: ${customized.join(', ')}.`
            : 'No active customizations.';
    }

    async function resetVariablePrefs() {
        if (!window.VariablePrefs) return;
        await VariablePrefs.resetAll();
        renderVariablePrefsStatus();
    }

    function autoSaveSettings() {
        const s = {
            video_autostart: document.getElementById('setting-video-autostart').checked,
            share_annotations: document.getElementById('setting-share-annotations').checked,
            big_dots: document.getElementById('setting-big-dots').checked,
            timelines_include_empty_dates: document.getElementById('setting-timelines-include-empty-dates').checked,
            timelines_include_pre_activity: document.getElementById('setting-timelines-include-pre-activity').checked
        };
        saveUserSettings(s).then(() => {
            if (typeof updatePcaPlot === 'function') updatePcaPlot();
        });
    }

    // --- Profile page ---

    function loadProfileForm() {
        const emailEl = document.getElementById('profile-email');
        const nameEl = document.getElementById('profile-display-username');
        if (!emailEl || !nameEl) return;
        fetch('/api/user/profile')
            .then(r => r.ok ? r.json() : Promise.reject(new Error('Failed to load profile')))
            .then(data => {
                emailEl.textContent = data.email || '';
                nameEl.value = data.display_username || '';
            })
            .catch(() => {
                emailEl.textContent = 'Unable to load profile.';
            });
    }

    async function saveProfile() {
        const nameEl = document.getElementById('profile-display-username');
        const status = document.getElementById('profile-status');
        if (!nameEl) return;
        if (status) {
            status.style.color = 'var(--color-text-muted)';
            status.textContent = 'Saving…';
        }
        try {
            const response = await fetch('/api/user/profile', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ display_username: nameEl.value })
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.error || 'Save failed');
            // Update the header immediately so the change is visible without a reload.
            const headerUser = document.querySelector('.auth-header-user-name');
            if (headerUser) headerUser.textContent = nameEl.value.trim();
            if (status) {
                status.style.color = 'var(--color-success)';
                status.textContent = 'Saved';
                setTimeout(() => { if (status.textContent === 'Saved') status.textContent = ''; }, 2000);
            }
        } catch (e) {
            if (status) {
                status.style.color = 'var(--color-danger)';
                status.textContent = e.message;
            }
        }
    }
