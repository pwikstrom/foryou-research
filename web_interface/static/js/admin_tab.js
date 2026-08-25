    const ADMIN_PAGE_PERM_MAP = {
        'admin-page-new-users':    'tab.admin.new_users',
        'admin-page-active-users': 'tab.admin.active_users',
        'admin-page-roles':        'tab.admin.roles',
        'admin-page-annotations':  'tab.admin.annotations',
        'admin-page-backends':     'tab.admin.backends',
        'admin-page-versions':     'tab.admin.versions',
        'admin-page-abeval':       'tab.admin.ab_eval',
        'admin-page-humaneval':    'tab.admin.human_eval',
        'admin-page-schema':       'tab.admin.schema',
        'admin-page-stoplist':     'tab.admin.stoplist',
        'admin-page-scrapers':     'tab.admin.scrapers',
        'admin-page-general':      'tab.admin.general',
        'admin-page-system-info':  'tab.admin.system_info',
    };

    function openAdminPage(pageId, clickedItem) {
        // Defense in depth — if the matching permission isn't granted, refuse.
        const requiredPerm = ADMIN_PAGE_PERM_MAP[pageId];
        if (requiredPerm && Array.isArray(window.USER_PERMS) && !window.USER_PERMS.includes(requiredPerm)) {
            return;
        }

        // System info + health are fetched lazily, on first open of the sub-page.
        if (pageId === 'admin-page-system-info' && typeof loadSystemInfo === 'function') {
            loadSystemInfo();
            if (typeof loadSystemHealth === 'function') loadSystemHealth();
        }

        document.querySelectorAll('#admin .dm-page').forEach(page => {
            page.classList.remove('active');
        });

        document.querySelectorAll('#admin .dm-sidebar-item').forEach(item => {
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
            updateSubPageHash('admin', pageId);
        }
    }

    function _hasPerm(key) {
        return Array.isArray(window.USER_PERMS) && window.USER_PERMS.includes(key);
    }

    async function loadUsers() {
        try {
            const response = await fetch('/api/admin/users');
            if (!response.ok) throw new Error('Failed to load users');
            const users = await response.json();

            renderUsers(users);
            loadOrphanParticipants();

            // Only auto-load annotations if the user actually has that sub-page.
            if (_hasPerm('tab.admin.annotations')) {
                const annBody = document.getElementById('annotationsTableBody');
                if (annBody && annBody.rows.length <= 1 && annBody.innerHTML.includes('Click Refresh')) {
                    loadAnnotations();
                }
            }

            // Roles list is needed by Active Users, New Users, the Roles
            // matrix, and the Site Settings default-role dropdown. Settings are
            // needed by Site Settings (edit), New Users (display) and Backends
            // (active backend selections). loadAdminSettings reads
            // window.availableRoles to render the default-role dropdown,
            // so we await loadRoles first to avoid a race.
            const needsRoles = _hasPerm('tab.admin.roles')
                || _hasPerm('tab.admin.active_users')
                || _hasPerm('tab.admin.new_users')
                || _hasPerm('tab.admin.general');
            const needsSettings = _hasPerm('tab.admin.general')
                || _hasPerm('tab.admin.new_users')
                || _hasPerm('tab.admin.backends');

            if (needsRoles) await loadRoles();
            if (needsSettings) loadAdminSettings();
            if (_hasPerm('tab.admin.stoplist')) loadIrrelevantWords();

        } catch (error) {
            console.error('Error:', error);
            alert('Error loading users: ' + error.message);
        }
    }

    async function loadAdminSettings() {
        // Widgets are spread across two sub-pages — fetch once, populate
        // whichever elements happen to be in the DOM for this user.
        try {
            const response = await fetch('/api/admin/settings');
            if (!response.ok) throw new Error('Failed to load settings');
            const data = await response.json();
            const settings = data.settings || {};
            window._adminSettings = settings;

            // General → "Require approval" checkbox
            const cb = document.getElementById('setting-new-user-approval');
            if (cb) {
                cb.checked = !!settings.new_user_admin_approval_required;
                cb.disabled = false;
            }

            // General → "Default role for new users" dropdown
            const roleSelect = document.getElementById('setting-default-new-user-role');
            if (roleSelect && Array.isArray(window.availableRoles)) {
                const roleNames = window.availableRoles.map(r => r.name);
                const current = settings.default_new_user_role || 'viewer';
                roleSelect.innerHTML = roleNames.map(r =>
                    `<option value="${r}" ${r === current ? 'selected' : ''}>${r}</option>`
                ).join('');
                roleSelect.disabled = false;
            }

            // General → "Default study" dropdown. Choices come from the same
            // response (Site Settings holders only); "None" is the empty value.
            // A stored name that no longer matches a study is still offered as
            // a "(missing)" option so the admin can see what to fix — the
            // server already ignores it.
            const studySelect = document.getElementById('setting-default-study');
            if (studySelect && Array.isArray(data.study_names)) {
                const current = settings.default_study || '';
                const names = data.study_names.slice();
                if (current && !names.includes(current)) names.unshift(current);
                const options = ['<option value="">None — no default study</option>'];
                names.forEach(name => {
                    const missing = !data.study_names.includes(name);
                    options.push(
                        `<option value="${escapeHtml(name)}" ${name === current ? 'selected' : ''}>` +
                        `${escapeHtml(name)}${missing ? ' (missing)' : ''}</option>`
                    );
                });
                studySelect.innerHTML = options.join('');
                studySelect.value = current;
                studySelect.disabled = false;
            }

            // General → Cost guardrail caps + Sessions-tab list floors. The
            // server sends the EFFECTIVE floors (admin setting, else the
            // [sessions] config seed), so these fields always show what the
            // Sessions tab is actually applying.
            const capFields = [
                ['setting-queue-cap-annotation', 'queue_cap_annotation_items'],
                ['setting-queue-cap-scrape', 'queue_cap_scrape_items'],
                ['setting-sessions-min-plays', 'sessions_min_plays'],
                ['setting-sessions-min-minutes', 'sessions_min_minutes'],
                ['setting-sessions-min-coverage', 'sessions_min_coverage_pct'],
            ];
            capFields.forEach(([elId, key]) => {
                const input = document.getElementById(elId);
                if (!input) return;
                input.value = (settings[key] !== undefined && settings[key] !== null)
                    ? settings[key] : 0;
                input.disabled = false;
            });

            // New Users → "default role: X" label
            const defaultRoleLabel = document.getElementById('addUserDefaultRoleLabel');
            if (defaultRoleLabel) {
                defaultRoleLabel.textContent = settings.default_new_user_role || 'viewer';
            }

            // General → Machine annotation + Embeddings cards
            populateMachineSettings(settings);
        } catch (e) {
            console.error('loadAdminSettings:', e);
            const status = document.getElementById('setting-new-user-approval-status');
            if (status) status.textContent = 'Failed to load';
        }
    }

    // Activating a contract from the Contracts page may also switch the active
    // annotation backend — refresh the Backends widgets so the select and
    // requirements panel reflect it.
    document.addEventListener('fyp:contract-changed', function () {
        if (_hasPerm('tab.admin.backends')) loadAdminSettings();
    });

    async function saveDefaultNewUserRoleSetting(select) {
        const status = document.getElementById('setting-default-new-user-role-status');
        const desired = select.value;
        // Snapshot the previously-saved value so we can revert on failure.
        const previous = (window._adminSettings || {}).default_new_user_role || 'viewer';

        select.disabled = true;
        if (status) status.textContent = 'Saving…';

        try {
            const response = await fetch('/api/admin/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ default_new_user_role: desired })
            });
            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                throw new Error(data.error || 'Save failed');
            }
            (window._adminSettings || (window._adminSettings = {})).default_new_user_role = desired;
            // Keep the New Users label in sync if it happens to be in the DOM.
            const defaultRoleLabel = document.getElementById('addUserDefaultRoleLabel');
            if (defaultRoleLabel) defaultRoleLabel.textContent = desired;

            if (status) {
                status.textContent = 'Saved';
                setTimeout(() => { if (status.textContent === 'Saved') status.textContent = ''; }, 2000);
            }
        } catch (e) {
            console.error('saveDefaultNewUserRoleSetting:', e);
            select.value = previous; // revert
            if (status) status.textContent = `Failed — reverted (${e.message})`;
        } finally {
            select.disabled = false;
        }
    }

    // The default study is readable by every account, so a failed save must
    // not leave the dropdown showing a sharing state the server never stored.
    async function saveDefaultStudySetting(select) {
        const status = document.getElementById('setting-default-study-status');
        const desired = select.value || '';
        const previous = (window._adminSettings || {}).default_study || '';

        select.disabled = true;
        if (status) status.textContent = 'Saving…';

        try {
            const response = await fetch('/api/admin/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ default_study: desired })
            });
            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                throw new Error(data.error || 'Save failed');
            }
            (window._adminSettings || (window._adminSettings = {})).default_study = desired;
            if (status) {
                status.textContent = desired
                    ? `Saved — '${desired}' is now shared with every role`
                    : 'Saved — no default study';
                setTimeout(() => {
                    if (status.textContent.startsWith('Saved')) status.textContent = '';
                }, 4000);
            }
        } catch (e) {
            console.error('saveDefaultStudySetting:', e);
            select.value = previous; // revert
            if (status) status.textContent = `Failed — reverted (${e.message})`;
        } finally {
            select.disabled = false;
        }
    }

    function saveQueueCapSetting(input, key) {
        return saveNumericSetting(input, key, { integer: true });
    }

    // Sessions-tab list floors — same save path, but minutes/coverage are
    // fractional and coverage is a bounded percentage.
    function saveSessionFloorSetting(input, key, integer, max) {
        return saveNumericSetting(input, key, { integer: !!integer, max });
    }

    // Persist one non-negative numeric setting, reverting the field if the
    // server rejects it — a silently-kept value the server never stored is
    // worse than a visible failure.
    async function saveNumericSetting(input, key, opts) {
        const { integer = true, max } = opts || {};
        const status = document.getElementById(`${input.id}-status`);
        const previous = (window._adminSettings || {})[key] ?? 0;
        const desired = integer ? parseInt(input.value, 10) : parseFloat(input.value);

        if (!Number.isFinite(desired) || desired < 0 || (integer && !Number.isInteger(desired))) {
            input.value = previous;
            if (status) status.textContent = `Must be a non-negative ${integer ? 'integer' : 'number'}`;
            return;
        }
        if (max !== undefined && desired > max) {
            input.value = previous;
            if (status) status.textContent = `Must be ${max} or less`;
            return;
        }

        input.disabled = true;
        if (status) status.textContent = 'Saving…';
        try {
            const response = await fetch('/api/admin/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ [key]: desired })
            });
            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                throw new Error(data.error || 'Save failed');
            }
            (window._adminSettings || (window._adminSettings = {}))[key] = desired;
            if (status) {
                status.textContent = 'Saved';
                setTimeout(() => { if (status.textContent === 'Saved') status.textContent = ''; }, 2000);
            }
        } catch (e) {
            console.error(`saveNumericSetting(${key}):`, e);
            input.value = previous; // revert
            if (status) status.textContent = `Failed — reverted (${e.message})`;
        } finally {
            input.disabled = false;
        }
    }

    async function saveNewUserApprovalSetting(checkbox) {
        const status = document.getElementById('setting-new-user-approval-status');
        const previous = !checkbox.checked; // value before this change
        const desired = checkbox.checked;
        checkbox.disabled = true;
        if (status) status.textContent = 'Saving…';
        try {
            const response = await fetch('/api/admin/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ new_user_admin_approval_required: desired })
            });
            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                throw new Error(data.error || 'Save failed');
            }
            if (status) {
                status.textContent = 'Saved';
                setTimeout(() => { if (status.textContent === 'Saved') status.textContent = ''; }, 2000);
            }
        } catch (e) {
            console.error('saveNewUserApprovalSetting:', e);
            checkbox.checked = previous; // revert
            if (status) status.textContent = 'Failed — reverted';
        } finally {
            checkbox.disabled = false;
        }
    }

    // --- Annotation + embedding backend selection ---
    // ([machine] model/generation parameters are config-file-only — no UI.)

    function populateMachineSettings(settings) {
        // The backend selectors need per-backend availability, not just
        // "module importable" — fetch the requirement checks and render both.
        // Backends is its own sub-page; skip when the user can't see it.
        if (!_hasPerm('tab.admin.backends')) return;
        loadBackendRequirements(settings.annotation_backend || 'gemini');
        loadEmbeddingBackendRequirements(settings.embedding_backend || 'gemini');
    }

    // The embeddings card mirrors the annotation one: same endpoint shape,
    // its own select + requirements panel.
    function loadEmbeddingBackendRequirements(currentBackend) {
        return _loadBackendRequirementsInto(
            '/api/manage/embedding/backends',
            'setting-embedding-backend',
            'embedding-backend-requirements',
            currentBackend,
        );
    }

    function loadBackendRequirements(currentBackend) {
        return _loadBackendRequirementsInto(
            '/api/manage/annotation/backends',
            'setting-annotation-backend',
            'backend-requirements',
            currentBackend,
        );
    }

    async function _loadBackendRequirementsInto(endpoint, selectId, panelId, currentBackend) {
        const backendSelect = document.getElementById(selectId);
        const panel = document.getElementById(panelId);
        try {
            const response = await fetch(endpoint);
            if (!response.ok) throw new Error('Failed to load backends');
            const data = await response.json();
            const backends = data.backends || [];

            if (backendSelect) {
                backendSelect.innerHTML = backends.map(b => {
                    const ok = b.availability && b.availability.ok;
                    const selectable = ok || b.name === currentBackend;
                    // Config-declared variants carry a display label and their
                    // implementing backend id; plain backends show their id.
                    const isVariant = b.backend && b.backend !== b.name;
                    const display = (b.label && b.label !== b.name) ? b.label : b.name;
                    const label = display + (isVariant ? ` [${b.backend}]` : '')
                        + (ok ? '' : ' (requirements not met)');
                    return `<option value="${b.name}" ${b.name === currentBackend ? 'selected' : ''}
                        ${selectable ? '' : 'disabled'}>${label}</option>`;
                }).join('');
                backendSelect.disabled = false;
            }

            // Health panel: one block per backend (including gemini), each
            // with an overall status line plus its detailed requirement checks.
            if (panel) {
                const rows = [];
                backends.forEach((b, i) => {
                    const avail = b.availability || {};
                    const ok = !!avail.ok;
                    const statusColor = ok ? 'var(--color-success)' : 'var(--color-danger)';
                    const statusLabel = ok ? 'available' : 'unavailable';
                    rows.push(`<div class="font-semibold" style="margin: ${i ? '10px' : '0'} 0 4px 0; color: var(--color-text-primary);">
                        ${b.name}
                        ${b.model ? `<span class="font-mono font-normal text-xs" style="color: var(--color-text-muted);">${b.model}</span>` : ''}
                        <span style="color: ${statusColor};">● ${statusLabel}</span>
                        ${b.active ? '<span style="color: var(--color-text-muted);">— active</span>' : ''}
                    </div>`);
                    if (!ok && avail.reason) {
                        rows.push(`<div style="margin-left: 4px; color: var(--color-text-muted);">${avail.reason}</div>`);
                    }
                    for (const c of (avail.checks || [])) {
                        const mark = c.ok ? '✓' : '✗';
                        const color = c.ok ? 'var(--color-success)' : 'var(--color-danger)';
                        rows.push(`<div style="margin-left: 4px;">
                            <span style="color: ${color};">${mark}</span>
                            ${c.name}${c.detail ? ` <span style="color: var(--color-text-muted);">— ${c.detail}</span>` : ''}
                            ${!c.ok && c.fix ? `<div class="font-mono" style="margin-left: 18px; color: var(--color-text-muted);">fix: ${c.fix}</div>` : ''}
                        </div>`);
                    }
                });
                panel.innerHTML = rows.join('');
                panel.style.display = rows.length ? 'block' : 'none';
            }
        } catch (e) {
            console.error('loadBackendRequirements:', e);
            if (backendSelect) {
                backendSelect.innerHTML = `<option value="${currentBackend}" selected>${currentBackend}</option>`;
                backendSelect.disabled = false;
            }
        }
    }

    async function saveMachineSetting(el, key) {
        const status = document.getElementById(el.id + '-status');
        const previous = (window._adminSettings || {})[key];
        let value = el.value;
        // Numeric inputs: empty string clears the override; otherwise send a number.
        if (el.type === 'number' && value !== '') {
            value = Number(value);
            if (Number.isNaN(value)) { if (status) status.textContent = 'Not a number'; return; }
        }
        el.disabled = true;
        if (status) status.textContent = 'Saving…';
        try {
            const response = await fetch('/api/admin/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ [key]: value })
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(data.error || 'Save failed');
            }
            (window._adminSettings || (window._adminSettings = {}))[key] = value;
            if (key === 'annotation_backend') {
                // Spell out what the switch did (the new active av_ comes back
                // from the server) and let the other panels — Versions,
                // Contracts, and this page's requirement cards with their
                // "active" markers — refresh themselves.
                const sw = data.annotation_backend_switch || {};
                if (status) {
                    status.textContent = sw.to
                        ? `Saved — new annotations now use '${sw.to}'`
                          + (sw.annotation_version
                              ? `. Annotation version ${sw.annotation_version} is now active (see Versions).`
                              : '.')
                        : 'Saved (unchanged)';
                }
                document.dispatchEvent(new CustomEvent('fyp:contract-changed'));
            } else if (key === 'embedding_backend') {
                if (status) {
                    status.textContent = 'Saved — to apply, run Embeddings → Refresh and then '
                        + 'Video map → Rebuild under Data Pipeline → Dataset Assembly.';
                }
                loadEmbeddingBackendRequirements(String(value));
            } else if (status) {
                status.textContent = 'Saved';
                setTimeout(() => { if (status.textContent === 'Saved') status.textContent = ''; }, 4000);
            }
        } catch (e) {
            console.error('saveMachineSetting:', key, e);
            el.value = (previous === undefined || previous === null) ? '' : String(previous); // revert
            if (status) status.textContent = `Failed — reverted (${e.message})`;
        } finally {
            el.disabled = false;
        }
    }

    // --- Hashtag stoplist (irrelevant words) ---

    window._iw = window._iw || { words: [], etag: null, dirty: false };

    // Mirrors fyp/irrelevant_words.squeeze: collapse runs of one code point.
    function _iwSqueeze(s) {
        return s.replace(/(.)\1+/gu, '$1');
    }

    // Returns the stoplist entry a cleaned token matches, or null.
    function _iwMatch(token) {
        const t = _iwSqueeze(token);
        for (const w of window._iw.words) {
            if (w.endsWith('*')) {
                if (t.startsWith(_iwSqueeze(w.slice(0, -1)))) return w;
            } else if (t === _iwSqueeze(w)) {
                return w;
            }
        }
        return null;
    }

    function _iwSetDirty(dirty) {
        window._iw.dirty = dirty;
        const btn = document.getElementById('iw-save-btn');
        if (btn) btn.disabled = !dirty;
        const status = document.getElementById('iw-status');
        if (status && dirty) status.textContent = 'Unsaved changes';
    }

    async function loadIrrelevantWords() {
        const pills = document.getElementById('iw-pills');
        if (!pills) return;
        try {
            const response = await fetch('/api/admin/irrelevant_words');
            if (!response.ok) throw new Error('Failed to load stoplist');
            const data = await response.json();
            window._iw.words = data.words || [];
            window._iw.etag = data.etag;
            const input = document.getElementById('iw-add-input');
            if (input) input.disabled = false;
            _iwSetDirty(false);
            const status = document.getElementById('iw-status');
            if (status) status.textContent = '';
            renderIrrelevantWords();
        } catch (e) {
            console.error('loadIrrelevantWords:', e);
            pills.innerHTML = '';
            const err = document.createElement('span');
            err.className = 'text-xs';
            err.style.color = 'var(--color-danger)';
            err.textContent = `Failed to load: ${e.message}`;
            pills.appendChild(err);
        }
    }

    function renderIrrelevantWords() {
        const pills = document.getElementById('iw-pills');
        if (!pills) return;
        const searchEl = document.getElementById('iw-search');
        const query = (searchEl && searchEl.value.trim().toLowerCase()) || '';
        const words = window._iw.words.filter(w => !query || w.includes(query));

        const count = document.getElementById('iw-count');
        if (count) {
            count.textContent = query
                ? `— ${words.length} of ${window._iw.words.length} words`
                : `— ${window._iw.words.length} words`;
        }

        pills.innerHTML = '';
        if (words.length === 0) {
            const empty = document.createElement('span');
            empty.className = 'text-xs';
            empty.style.color = 'var(--color-text-muted)';
            empty.textContent = query ? 'No matching words.' : 'The list is empty.';
            pills.appendChild(empty);
            return;
        }
        // DOM construction (not innerHTML) — entries are free text.
        for (const word of words) {
            const pill = document.createElement('span');
            pill.className = 'text-xs';
            pill.style.cssText =
                'display: inline-flex; align-items: center; gap: 5px; padding: 2px 4px 2px 10px;' +
                'border-radius: 12px; background: var(--color-border); color: var(--color-text-primary);';
            const label = document.createElement('span');
            label.textContent = word;
            if (word.endsWith('*')) {
                label.className = 'font-semibold';
                label.style.color = 'var(--color-accent)';
                pill.classList.add('meta-tooltip');
                pill.setAttribute('data-tooltip', 'Wildcard: matches any hashtag starting with this');
            }
            const remove = document.createElement('button');
            remove.type = 'button';
            remove.className = 'btn-discreet text-xs';
            remove.style.cssText = 'border: none; padding: 0 6px; cursor: pointer; line-height: 1;';
            remove.textContent = '×';
            remove.setAttribute('aria-label', `Remove ${word}`);
            remove.onclick = () => removeIrrelevantWord(word);
            pill.appendChild(label);
            pill.appendChild(remove);
            pills.appendChild(pill);
        }
    }

    function addIrrelevantWords() {
        const input = document.getElementById('iw-add-input');
        const status = document.getElementById('iw-status');
        if (!input || !input.value.trim()) return;

        // Bulk paste friendly: split on commas/semicolons/whitespace/newlines,
        // strip a leading '#', lowercase.
        const raw = input.value.split(/[\s,;]+/).map(w => w.replace(/^#/, '').trim().toLowerCase()).filter(Boolean);
        const rejected = [];
        let added = 0;
        for (const w of raw) {
            if (w === '*' || (w.endsWith('*') && _iwSqueeze(w.slice(0, -1)).length < 2)) {
                rejected.push(w); // bare or too-broad wildcard
                continue;
            }
            // A word already matched by the list is redundant; a wildcard is
            // redundant only when an existing wildcard already covers its prefix
            // (a wildcard is broader than the same bare word, so it must be addable).
            const isWild = w.endsWith('*');
            const core = _iwSqueeze(isWild ? w.slice(0, -1) : w);
            const covered = isWild
                ? window._iw.words.some(x => x.endsWith('*') && core.startsWith(_iwSqueeze(x.slice(0, -1))))
                : _iwMatch(w) !== null;
            if (!covered) {
                window._iw.words.push(w);
                added++;
            }
        }
        window._iw.words.sort();
        input.value = '';
        if (added > 0) _iwSetDirty(true);
        if (status) {
            const parts = [];
            if (added > 0) parts.push(`Added ${added} — unsaved`);
            if (rejected.length > 0) parts.push(`rejected too-broad wildcard: ${rejected.join(', ')}`);
            if (added === 0 && rejected.length === 0) parts.push('Already covered by the list');
            status.textContent = parts.join('; ');
        }
        renderIrrelevantWords();
    }

    function removeIrrelevantWord(word) {
        window._iw.words = window._iw.words.filter(w => w !== word);
        _iwSetDirty(true);
        renderIrrelevantWords();
        testIrrelevantWord((document.getElementById('iw-test') || {}).value || '');
    }

    async function saveIrrelevantWords() {
        const btn = document.getElementById('iw-save-btn');
        const status = document.getElementById('iw-status');
        if (btn) btn.disabled = true;
        if (status) status.textContent = 'Saving…';
        try {
            const response = await fetch('/api/admin/irrelevant_words', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ words: window._iw.words, etag: window._iw.etag })
            });
            const data = await response.json().catch(() => ({}));
            if (response.status === 409) {
                // Concurrent edit — take the server's list and start over.
                window._iw.words = data.words || [];
                window._iw.etag = data.etag;
                _iwSetDirty(false);
                renderIrrelevantWords();
                if (status) status.textContent = 'List was changed elsewhere — reloaded, please redo your edit';
                return;
            }
            if (!response.ok) throw new Error(data.error || 'Save failed');
            window._iw.words = data.words || [];
            window._iw.etag = data.etag;
            _iwSetDirty(false);
            renderIrrelevantWords();
            if (status) {
                status.textContent = 'Saved';
                setTimeout(() => { if (status.textContent === 'Saved') status.textContent = ''; }, 2000);
            }
        } catch (e) {
            console.error('saveIrrelevantWords:', e);
            if (btn) btn.disabled = false;
            if (status) status.textContent = `Failed: ${e.message}`;
        }
    }

    function testIrrelevantWord(value) {
        const result = document.getElementById('iw-test-result');
        if (!result) return;
        const clean = value.trim().toLowerCase().replace(/^#/, '');
        if (!clean) {
            result.textContent = '';
            return;
        }
        const hit = _iwMatch(clean);
        if (hit) {
            result.style.color = 'var(--color-danger)';
            result.textContent = `#${clean} would be dropped (matches “${hit}”)`;
        } else {
            result.style.color = 'var(--color-success)';
            result.textContent = `#${clean} would be kept`;
        }
    }

    // --- Apply stoplist to existing hashtags (background job) ---

    let _iwApplyPoll = null;

    function _iwApplySetStatus(color, text) {
        const status = document.getElementById('iw-apply-status');
        if (!status) return;
        status.style.color = color;
        status.textContent = text;
    }

    async function applyStoplistToExisting() {
        const btn = document.getElementById('iw-apply-btn');
        if (btn) btn.disabled = true;
        _iwApplySetStatus('var(--color-text-muted)', 'Starting…');

        // Baseline the last completion time so we can tell a *fresh* terminal
        // state (fast job finishing before we ever observe 'running') from the
        // stale 'stopped' state left over from a previous run.
        let baselineEnd = null;
        try {
            const pre = await fetch('/api/status');
            if (pre.ok) baselineEnd = ((await pre.json()).retokenise_hashtags || {}).last_run_end_time || null;
        } catch (_) { /* non-fatal */ }

        try {
            const response = await fetch('/api/admin/irrelevant_words/apply', { method: 'POST' });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.message || 'Failed to start');
            _pollIrrelevantWordsApply(baselineEnd);
        } catch (e) {
            console.error('applyStoplistToExisting:', e);
            if (btn) btn.disabled = false;
            _iwApplySetStatus('var(--color-danger)', `Failed: ${e.message}`);
        }
    }

    function _pollIrrelevantWordsApply(baselineEnd) {
        if (_iwApplyPoll) clearInterval(_iwApplyPoll);
        let sawRunning = false;
        _iwApplyPoll = setInterval(async () => {
            try {
                const r = await fetch('/api/status');
                if (!r.ok) return;
                const entry = (await r.json()).retokenise_hashtags;
                if (!entry) return;

                // In-flight — the process reports state='running' while working.
                if (entry.state === 'running') {
                    sawRunning = true;
                    const pct = (entry.progress && entry.progress.percent != null) ? entry.progress.percent : 0;
                    const msg = (entry.progress && entry.progress.message) || 'Working…';
                    _iwApplySetStatus('var(--color-text-muted)', `${pct}% — ${msg}`);
                    return;
                }

                // Not running: only terminal once we've either seen it run or a
                // fresh completion timestamp appeared (guards the startup race).
                const freshlyDone = entry.last_run_end_time && entry.last_run_end_time !== baselineEnd;
                if (!sawRunning && !freshlyDone) return; // still spinning up

                clearInterval(_iwApplyPoll); _iwApplyPoll = null;
                const btn = document.getElementById('iw-apply-btn');
                if (btn) btn.disabled = false;

                if (entry.last_run_outcome === 'Fail') {
                    _iwApplySetStatus('var(--color-danger)', `Failed: ${entry.last_message || 'see logs'}`);
                } else {
                    const d = entry.data || {};
                    _iwApplySetStatus('var(--color-success)',
                        `Updated ${d.rows_changed || 0} hashtag row(s) in ${d.files_changed || 0} file(s). `
                        + 'Now run Consolidate with "Force full rebuild" ticked (Data Pipeline → Dataset Assembly) to apply to studies.');
                }
            } catch (e) {
                console.error('_pollIrrelevantWordsApply:', e);
            }
        }, 2000);
    }

    function _adminEsc(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function _accountBadgeHtml(user) {
        if (!user || user.account_kind !== 'participant') return '';
        let label = user.placeholder ? 'placeholder' : 'participant';
        if (!user.can_login) label += ' · no login';
        const title = user.placeholder
            ? 'Placeholder participant account created from donation data'
            : 'Participant account' + (user.can_login ? '' : ' (no password set — cannot log in)');
        return `<span class="account-badge" title="${_adminEsc(title)}">${_adminEsc(label)}</span>`;
    }

    // --- Orphan placeholder participant accounts (own zero collections) ---

    async function loadOrphanParticipants() {
        const notice = document.getElementById('orphanParticipantsNotice');
        if (!notice) return;
        try {
            const response = await fetch('/api/admin/users/orphan_participants');
            if (!response.ok) throw new Error('Failed to load orphan participants');
            const data = await response.json();
            const orphans = Array.isArray(data.orphans) ? data.orphans : [];
            window._orphanParticipants = orphans;
            if (orphans.length === 0) {
                notice.style.display = 'none';
                return;
            }
            const text = document.getElementById('orphanParticipantsText');
            if (text) {
                text.textContent = `${orphans.length} placeholder participant account${orphans.length === 1 ? '' : 's'} own${orphans.length === 1 ? 's' : ''} no collections.`;
                text.title = orphans.join(', ');
            }
            notice.style.display = 'flex';
        } catch (error) {
            console.error('loadOrphanParticipants:', error);
            notice.style.display = 'none';
        }
    }

    async function removeOrphanParticipants() {
        const orphans = window._orphanParticipants || [];
        if (orphans.length === 0) return;
        const ok = await showAppConfirm(
            `Remove ${orphans.length} orphan placeholder participant account${orphans.length === 1 ? '' : 's'}?\n\n${orphans.join(', ')}`,
            { title: 'Remove orphan accounts', okLabel: 'Remove', danger: true });
        if (!ok) return;
        const btn = document.getElementById('orphanParticipantsBtn');
        if (btn) btn.disabled = true;
        try {
            const response = await fetch('/api/admin/users/orphan_participants', { method: 'POST' });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.error || 'Failed to remove orphan accounts');
            const removed = (data.removed || []).length;
            const failed = data.failed || [];
            let msg = `Removed ${removed} orphan account${removed === 1 ? '' : 's'}.`;
            if (failed.length) msg += ` ${failed.length} could not be removed: ${failed.join(', ')}`;
            showToast(msg, failed.length ? 'warning' : 'success');
        } catch (error) {
            showToast('Error: ' + error.message, 'error');
        } finally {
            if (btn) btn.disabled = false;
            loadUsers();
            loadOrphanParticipants();
        }
    }

    function renderUsers(users) {
        const pendingContainer = document.getElementById('pendingUserList');
        const tableBody = document.getElementById('userTableBody');

        // Either sub-page may be hidden by the permission system — render only
        // what's actually on the page. Bail only if both targets are missing.
        if (!pendingContainer && !tableBody) {
            return;
        }

        // Cache the full payload so column-sort clicks can re-render without
        // re-fetching from the server.
        window._lastUsersPayload = users;

        const pending = users.filter(u => !u.approved);
        const active = users.filter(u => u.approved);

        if (pendingContainer) {
            if (pending.length === 0) {
                pendingContainer.innerHTML = '<p style="color: var(--color-success); margin: 0;">No pending approvals.</p>';
                pendingContainer.style.background = 'var(--color-bg-elevated)';
                pendingContainer.style.borderColor = 'var(--color-success)';
            } else {
                pendingContainer.style.background = 'var(--color-bg-elevated)';
                pendingContainer.style.borderColor = 'var(--color-warning)';
                let pendingHtml = '<ul style="list-style: none; padding: 0; margin: 0;">';
                pending.forEach(u => {
                    // If an approval-request email was sent to an admin, show
                    // when and to whom, so admins know it was already flagged.
                    let noteHtml = '';
                    const n = u.approval_notification;
                    if (n && n.sent_at) {
                        const when = fypFmtDateTime(n.sent_at);
                        const to = n.sent_to || 'an admin';
                        noteHtml = `<div class="text-xs" style="margin-top: 4px; color: var(--color-text-muted);">&#9993; Approval request emailed to <strong>${to}</strong> at ${when}</div>`;
                    }
                    pendingHtml += `
                 <li style="display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 8px 0; border-bottom: 1px dashed var(--color-warning);">
                     <div><span><strong>${u.username}</strong> &mdash; will be assigned the <em>${u.role}</em> role on approval</span>${noteHtml}</div>
                     <button onclick="approveUser('${u.username}')" class="action-btn" style="padding: 6px 12px; flex-shrink: 0;">Approve</button>
                 </li>`;
                });
                pendingHtml += '</ul>';
                pendingContainer.innerHTML = pendingHtml;
            }
        }

        if (!tableBody) {
            return; // No active-users sub-page on this page; nothing left to do.
        }

        window._activeUsersData = active;

        if (active.length === 0) {
            tableBody.innerHTML = '<tr><td colspan="6" style="padding: 16px; text-align: center; color: var(--color-text-faint);">No active users found.</td></tr>';
            _updateActiveUsersCount(0, 0);
            return;
        }

        const visible = _filterActiveUsersData(active);
        _updateActiveUsersCount(visible.length, active.length);

        if (visible.length === 0) {
            tableBody.innerHTML = '<tr><td colspan="6" style="padding: 16px; text-align: center; color: var(--color-text-faint);">No users match the current search or filter.</td></tr>';
            _updateActiveUsersSortIndicators();
            return;
        }

        const sorted = _sortActiveUsersData(visible);

        tableBody.innerHTML = sorted.map(user => {
            const lastLogin = fypFmtDateTime(user.last_login, 'Never');
            const registered = fypFmtDateTime(user.created_at, 'Unknown');
            const safeUser = user.username.replace(/'/g, "\\'");
            const safeDisplayName = String(user.display_username || '')
                .replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
            const collections = Array.isArray(user.collections) ? user.collections : [];
            const collectionsCount = Number(user.collections_count || collections.length || 0);
            const collectionsTitle = collections.length ? _adminEsc(collections.join('\n')) : '';
            return `
        <tr style="border-bottom: 1px solid var(--color-border); cursor: pointer;" onclick="openUserModal('${safeUser}')">
            <td style="padding: 12px 16px;" onclick="event.stopPropagation();">
                <input type="text" value="${safeDisplayName}" minlength="3" maxlength="15"
                    class="font-medium text-sm" data-username="${safeUser}"
                    onchange="updateDisplayUsername('${safeUser}', this)"
                    onclick="event.stopPropagation();"
                    style="width: 130px; padding: 4px 8px; border: 1px solid var(--color-border); border-radius: 4px; background: var(--color-bg-input); color: var(--color-text-primary);">
            </td>
            <td style="padding: 12px 16px;">
                <span class="text-sm" style="color: var(--color-text-muted);">${_adminEsc(user.username)}</span>${_accountBadgeHtml(user)}
            </td>
            <td style="padding: 12px 16px;" onclick="event.stopPropagation();">
                <select onchange="updateRole('${safeUser}', this.value)" onclick="event.stopPropagation();" class="role-select text-xs" data-current-role="${user.role}"
                   style="padding: 4px 8px; border: 1px solid var(--color-border-strong); background: var(--color-bg-input); color: var(--color-text-primary); border-radius: 4px;">
                    <!-- Populated dynamically -->
                </select>
            </td>
            <td style="padding: 12px 16px; color: var(--color-text-primary);">
                <span class="text-xs" style="color: var(--color-text-muted);">${lastLogin}</span>
            </td>
            <td style="padding: 12px 16px; color: var(--color-text-primary);">
                <span class="text-xs" style="color: var(--color-text-muted);">${registered}</span>
            </td>
            <td style="padding: 12px 16px;">
                <span class="text-xs" style="color: var(--color-text-muted);" title="${collectionsTitle}">${collectionsCount > 0 ? collectionsCount : '—'}</span>
            </td>
        </tr>
    `}).join('');

        // After rendering users, populate their role dropdowns
        if (window.availableRoles) {
            const roleNames = window.availableRoles.map(r => r.name);
            sorted.forEach(u => {
                const select = document.querySelector(`select[onchange="updateRole('${u.username.replace(/'/g, "\\'")}', this.value)"]`);
                if (select) {
                    populateRoleSelect(select, roleNames, u.role);
                }
            });
        }

        _updateActiveUsersSortIndicators();
    }

    // --- Search / participant filter for Active Users table ---

    // Participant accounts (created from donation data, incl. the login-less
    // placeholders) outnumber real members, so they get their own toggle.
    function _filterActiveUsersData(users) {
        const searchEl = document.getElementById('activeUsersSearch');
        const participantsEl = document.getElementById('activeUsersIncludeParticipants');
        const needle = (searchEl ? searchEl.value : '').trim().toLowerCase();
        const includeParticipants = participantsEl ? participantsEl.checked : true;

        return users.filter(u => {
            if (!includeParticipants && u.account_kind === 'participant') return false;
            if (!needle) return true;
            const haystack = [u.display_username, u.username, u.role].join(' ').toLowerCase();
            return haystack.includes(needle);
        });
    }

    function _updateActiveUsersCount(shown, total) {
        const el = document.getElementById('activeUsersCount');
        if (!el) return;
        el.textContent = shown === total
            ? `${total} user${total === 1 ? '' : 's'}`
            : `${shown} of ${total} users`;
    }

    // Re-render from the cached payload; no refetch needed to filter.
    function filterActiveUsers() {
        if (Array.isArray(window._lastUsersPayload)) {
            renderUsers(window._lastUsersPayload);
        }
    }

    // --- Sorting for Active Users table ---

    window._activeUsersSort = window._activeUsersSort || { key: 'username', dir: 'asc' };

    function _activeUsersSortValue(user, key) {
        const stats = user.stats || {};
        switch (key) {
            case 'display_username': return (user.display_username || '').toLowerCase();
            case 'username':   return (user.username || '').toLowerCase();
            case 'role':       return (user.role || '').toLowerCase();
            case 'last_login': return user.last_login ? Date.parse(user.last_login) : -Infinity;
            case 'created_at': return user.created_at ? Date.parse(user.created_at) : -Infinity;
            case 'collections': return Number(user.collections_count || (user.collections || []).length || 0);
            case 'videos':     return Number(stats.unique_videos || 0);
            case 'notes':      return Number(stats.notes || 0);
            case 'closed':     return Number(stats.closed_tags || 0);
            case 'open':       return Number(stats.open_tags || 0);
            default:           return 0;
        }
    }

    function _sortActiveUsersData(users) {
        const { key, dir } = window._activeUsersSort;
        const factor = dir === 'desc' ? -1 : 1;
        return [...users].sort((a, b) => {
            const va = _activeUsersSortValue(a, key);
            const vb = _activeUsersSortValue(b, key);
            if (va < vb) return -1 * factor;
            if (va > vb) return  1 * factor;
            return 0;
        });
    }

    function _updateActiveUsersSortIndicators() {
        const { key, dir } = window._activeUsersSort;
        document.querySelectorAll('#activeUsersTable thead th[data-sort-key]').forEach(th => {
            const indicator = th.querySelector('.sort-indicator');
            if (!indicator) return;
            if (th.getAttribute('data-sort-key') === key) {
                indicator.textContent = dir === 'asc' ? ' ▲' : ' ▼';
            } else {
                indicator.textContent = '';
            }
        });
    }

    function sortActiveUsers(key) {
        const cur = window._activeUsersSort;
        if (cur.key === key) {
            cur.dir = cur.dir === 'asc' ? 'desc' : 'asc';
        } else {
            cur.key = key;
            cur.dir = 'asc';
        }
        if (Array.isArray(window._lastUsersPayload)) {
            renderUsers(window._lastUsersPayload);
        }
    }

    // --- User detail modal ---

    function openUserModal(username) {
        const overlay = document.getElementById('userDetailModal');
        if (!overlay) return;

        const user = (window._activeUsersData || []).find(u => u.username === username);
        if (!user) return;

        const stats = user.stats || { notes: 0, closed_tags: 0, open_tags: 0, unique_videos: 0, used_tags: [], user_notes: [] };
        const lastLogin = fypFmtDateTime(user.last_login, 'Never');

        overlay.dataset.username = username;

        document.getElementById('userModalUsername').textContent =
            user.display_username ? `${user.display_username} (${user.username})` : user.username;
        document.getElementById('userModalRole').textContent = user.role;
        document.getElementById('userModalLastLogin').textContent = lastLogin;
        document.getElementById('userModalVideos').textContent = stats.unique_videos || 0;
        document.getElementById('userModalNotes').textContent = stats.notes || 0;
        document.getElementById('userModalClosed').textContent = stats.closed_tags || 0;
        document.getElementById('userModalOpen').textContent = stats.open_tags || 0;

        // Used tags
        const tagsContainer = document.getElementById('userModalUsedTags');
        if (stats.used_tags && stats.used_tags.length > 0) {
            tagsContainer.innerHTML = stats.used_tags.map(t =>
                `<span class="text-xs" style="background: var(--color-border); padding: 2px 8px; border-radius: 12px; color: var(--color-text-primary);">${t}</span>`
            ).join('');
        } else {
            tagsContainer.innerHTML = '<span class="text-xs" style="color: var(--color-text-muted);">None</span>';
        }

        // User notes
        const notesContainer = document.getElementById('userModalUserNotes');
        if (stats.user_notes && stats.user_notes.length > 0) {
            notesContainer.innerHTML = stats.user_notes.map(n =>
                `<div class="text-xs" style="background: var(--color-border); padding: 6px 10px; border-radius: 4px; color: var(--color-text-primary);"><span class="font-mono" style="color:var(--color-text-muted); margin-right: 6px;">${n.item}:</span>${n.text}</div>`
            ).join('');
        } else {
            notesContainer.innerHTML = '<span class="text-xs" style="color: var(--color-text-muted);">None</span>';
        }

        // Account kind / origin
        const accountInfo = document.getElementById('userModalAccountInfo');
        if (accountInfo) {
            const bits = []; // already-escaped HTML fragments
            if (user.account_kind === 'participant') {
                bits.push(user.placeholder ? 'Placeholder participant account' : 'Participant account');
                if (!user.can_login) bits.push('no password set — cannot log in until one is set via Reset Password');
            } else {
                bits.push('Member account');
            }
            if (user.origin && user.origin.source) {
                let originStr = `Created from ${_adminEsc(user.origin.source === 'donation' ? 'donation data' : user.origin.source)}`;
                if (user.origin.at) originStr += ` on ${_adminEsc(fypFmtDateTime(user.origin.at))}`;
                if (user.origin.collection_id) originStr += ` (collection <span class="font-mono">${_adminEsc(user.origin.collection_id)}</span>)`;
                bits.push(originStr);
            }
            accountInfo.innerHTML = bits.join(' · ');
        }

        // Collections owned by this account
        const collectionsEl = document.getElementById('userModalCollections');
        const ownedCollections = Array.isArray(user.collections) ? user.collections : [];
        if (collectionsEl) {
            collectionsEl.innerHTML = ownedCollections.length
                ? ownedCollections.map(c => `<span class="tag-chip font-mono">${_adminEsc(c)}</span>`).join('')
                : '<span class="text-xs" style="color: var(--color-text-muted);">none</span>';
        }

        // Cascade-delete checkbox (only when the account owns collections)
        const cascadeWrap = document.getElementById('userModalCascadeWrap');
        const cascadeBox = document.getElementById('userModalCascadeCollections');
        const cascadeLabel = document.getElementById('userModalCascadeLabel');
        const ownedCount = Number(user.collections_count || ownedCollections.length || 0);
        if (cascadeWrap && cascadeBox && cascadeLabel) {
            cascadeBox.checked = false;
            if (ownedCount > 0) {
                cascadeLabel.textContent = `On delete, also delete ${ownedCount} owned collection${ownedCount === 1 ? '' : 's'}`;
                cascadeWrap.style.display = 'flex';
            } else {
                cascadeWrap.style.display = 'none';
            }
        }

        // Profile form
        const profile = user.profile || {};
        document.querySelectorAll('#userModalProfileForm [data-profile-field]').forEach(el => {
            const key = el.getAttribute('data-profile-field');
            const val = profile[key];
            if (key === 'consent_to_contact') {
                el.value = val === true ? 'true' : (val === false ? 'false' : '');
            } else {
                el.value = val == null ? '' : String(val);
            }
        });
        const profileStatus = document.getElementById('userModalProfileStatus');
        if (profileStatus) profileStatus.textContent = '';

        // Reset log to loading state
        const logList = document.getElementById('userModalLogList');
        logList.innerHTML = '<div class="text-xs" style="color: var(--color-text-muted); padding: 8px 0;">Loading activity…</div>';

        overlay.style.display = 'flex';

        // Fetch the activity log
        fetch(`/api/admin/users/${encodeURIComponent(username)}/log`)
            .then(r => r.ok ? r.json() : Promise.reject(new Error('Failed to load log')))
            .then(data => renderUserLog(data.entries || []))
            .catch(err => {
                logList.innerHTML = `<div class="text-xs" style="color: var(--color-danger); padding: 8px 0;">Failed to load activity: ${err.message}</div>`;
            });
    }

    function renderUserLog(entries) {
        const logList = document.getElementById('userModalLogList');
        if (!entries || entries.length === 0) {
            logList.innerHTML = '<div class="text-xs" style="color: var(--color-text-muted); padding: 8px 0;">No activity recorded yet.</div>';
            return;
        }
        logList.innerHTML = entries.map(e => {
            const ts = fypFmtDateTime(e.timestamp);
            const target = e.target ? ` <span style="color: var(--color-text-muted);">→</span> <span class="font-mono">${e.target}</span>` : '';
            let detailsStr = '';
            if (e.details && Object.keys(e.details).length > 0) {
                detailsStr = ` <span class="text-xxs" style="color: var(--color-text-muted);">(${Object.entries(e.details).map(([k, v]) => `${k}: ${JSON.stringify(v)}`).join(', ')})</span>`;
            }
            const catColor = e.category === 'user_management' ? 'var(--color-info)' : 'var(--color-accent)';
            return `
            <div style="display: grid; grid-template-columns: 160px 1fr; gap: 12px; padding: 6px 0; border-bottom: 1px solid var(--color-border);">
                <span class="text-xxs font-mono" style="color: var(--color-text-muted);">${ts}</span>
                <div class="text-xs" style="color: var(--color-text-primary);">
                    <span class="font-semibold" style="color: ${catColor};">${e.action}</span>${target}${detailsStr}
                </div>
            </div>`;
        }).join('');
    }

    function closeUserModal() {
        const overlay = document.getElementById('userDetailModal');
        if (overlay) overlay.style.display = 'none';
    }

    async function modalResetPassword() {
        const overlay = document.getElementById('userDetailModal');
        const username = overlay && overlay.dataset.username;
        if (!username) return;
        const newPassword = prompt(`Enter new password for ${username}:`);
        if (!newPassword) return;
        try {
            const response = await fetch('/api/admin/users', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'reset_password', username, new_password: newPassword })
            });
            if (!response.ok) {
                const data = await response.json();
                throw new Error(data.error || 'Failed to reset password');
            }
            alert('Password updated successfully');
            closeUserModal();
            loadUsers();
        } catch (error) {
            alert('Error: ' + error.message);
        }
    }

    async function modalSaveProfile() {
        const overlay = document.getElementById('userDetailModal');
        const username = overlay && overlay.dataset.username;
        if (!username) return;
        const status = document.getElementById('userModalProfileStatus');
        const profile = {};
        document.querySelectorAll('#userModalProfileForm [data-profile-field]').forEach(el => {
            const key = el.getAttribute('data-profile-field');
            if (key === 'consent_to_contact') {
                profile[key] = el.value === 'true' ? true : (el.value === 'false' ? false : null);
            } else {
                profile[key] = el.value.trim();
            }
        });
        if (status) { status.style.color = 'var(--color-text-muted)'; status.textContent = 'Saving…'; }
        try {
            const response = await fetch('/api/admin/users', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'set_profile', username, profile })
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.error || 'Failed to save profile');
            // Keep cached payloads in sync so reopening the modal shows the saved values.
            [(window._activeUsersData || []), (window._lastUsersPayload || [])].forEach(list => {
                const u = list.find(x => x.username === username);
                if (u) u.profile = Object.assign({}, u.profile || {}, profile);
            });
            if (status) { status.style.color = 'var(--color-success)'; status.textContent = 'Profile saved.'; }
        } catch (error) {
            if (status) { status.style.color = 'var(--color-danger)'; status.textContent = 'Error: ' + error.message; }
        }
    }

    // Shared delete flow: confirms (stating how many collections the account
    // owns), optionally cascades the collection delete, and surfaces the result.
    async function _deleteUserAccount(username, cascade) {
        const user = (window._activeUsersData || []).find(u => u.username === username) || {};
        const owned = Number(user.collections_count || (user.collections || []).length || 0);
        let msg = `Are you sure you want to delete user "${username}"?`;
        if (owned > 0) {
            msg += `\n\nThis account owns ${owned} collection${owned === 1 ? '' : 's'}. `;
            msg += cascade
                ? `The collection${owned === 1 ? '' : 's'} will ALSO be deleted (runs the collection delete task).`
                : `The collection${owned === 1 ? '' : 's'} will be kept and become unassigned.`;
        } else {
            msg += '\n\nThis account owns no collections.';
        }
        const ok = await showAppConfirm(msg, { title: 'Delete user', okLabel: 'Delete', danger: true });
        if (!ok) return false;

        const url = `/api/admin/users?username=${encodeURIComponent(username)}&cascade_collections=${cascade ? 1 : 0}`;
        const response = await fetch(url, { method: 'DELETE' });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || 'Failed to delete user');

        let summary = data.message || `User "${username}" deleted.`;
        const unlinked = Array.isArray(data.unlinked_collections) ? data.unlinked_collections : [];
        if (!cascade && unlinked.length) {
            summary += ` ${unlinked.length} collection${unlinked.length === 1 ? '' : 's'} unassigned.`;
        }
        let level = 'success';
        if (cascade && data.cascade) {
            if (data.cascade.started) {
                summary += ` Collection delete task started for ${(data.cascade.collection_ids || []).length} collection${(data.cascade.collection_ids || []).length === 1 ? '' : 's'}.`;
            } else {
                summary += ` Collection delete NOT started: ${data.cascade.message || 'unknown reason'}`;
                level = 'warning';
            }
        }
        showToast(summary, level, 8000);
        return true;
    }

    async function modalDeleteUser() {
        const overlay = document.getElementById('userDetailModal');
        const username = overlay && overlay.dataset.username;
        if (!username) return;
        const cascadeBox = document.getElementById('userModalCascadeCollections');
        const cascade = !!(cascadeBox && cascadeBox.checked);
        try {
            const deleted = await _deleteUserAccount(username, cascade);
            if (!deleted) return;
            closeUserModal();
            loadUsers();
            loadOrphanParticipants();
        } catch (error) {
            alert('Error: ' + error.message);
        }
    }

    function getRoleColor(role) {
        switch (role) {
            case 'admin': return 'var(--color-danger)';
            case 'researcher': return 'var(--color-info)';
            case 'viewer': return 'var(--color-text-faint)';
            default: return 'var(--color-text-faint)';
        }
    }

    async function approveUser(username) {
        try {
            const response = await fetch('/api/admin/users', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'approve', username: username })
            });

            if (!response.ok) {
                const data = await response.json();
                throw new Error(data.error || 'Failed to approve user');
            }

            loadUsers(); // Refresh list
        } catch (error) {
            alert('Error: ' + error.message);
        }
    }

    async function resetPassword(username) {
        const newPassword = prompt(`Enter new password for ${username}:`);
        if (!newPassword) return;

        try {
            const response = await fetch('/api/admin/users', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'reset_password', username: username, new_password: newPassword })
            });

            if (!response.ok) {
                const data = await response.json();
                throw new Error(data.error || 'Failed to reset password');
            }

            alert('Password updated successfully');
        } catch (error) {
            alert('Error: ' + error.message);
        }
    }

    async function addUser() {
        const username = document.getElementById('newUsername').value;
        const displayUsername = document.getElementById('newDisplayUsername').value;
        const password = document.getElementById('newPassword').value;

        if (!username || !displayUsername || !password) {
            alert('Please enter email, username and password');
            return;
        }

        try {
            // Role is no longer a per-user choice — the backend applies the
            // configured default role (shown alongside this form).
            const response = await fetch('/api/admin/users', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, display_username: displayUsername, password })
            });

            if (!response.ok) {
                const data = await response.json();
                throw new Error(data.error || 'Failed to add user');
            }

            document.getElementById('newUsername').value = '';
            document.getElementById('newDisplayUsername').value = '';
            document.getElementById('newPassword').value = '';
            loadUsers();
        } catch (error) {
            alert('Error: ' + error.message);
        }
    }

    async function deleteUser(username, cascade = false) {
        try {
            const deleted = await _deleteUserAccount(username, !!cascade);
            if (!deleted) return;
            loadUsers();
            loadOrphanParticipants();
        } catch (error) {
            alert('Error: ' + error.message);
        }
    }

    async function updateDisplayUsername(username, input) {
        const previous = ((window._activeUsersData || []).find(u => u.username === username) || {}).display_username || '';
        const desired = input.value.trim();
        if (desired === previous) return;

        input.disabled = true;
        try {
            const response = await fetch('/api/admin/users', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'set_display_username', username, display_username: desired })
            });
            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                throw new Error(data.error || 'Failed to update username');
            }
            input.value = desired;
            // Keep the cached payloads in sync so sorts/re-renders show the new value.
            [(window._activeUsersData || []), (window._lastUsersPayload || [])].forEach(list => {
                const u = list.find(x => x.username === username);
                if (u) u.display_username = desired;
            });
        } catch (error) {
            alert('Error: ' + error.message);
            input.value = previous; // revert
        } finally {
            input.disabled = false;
        }
    }

    async function updateRole(username, newRole) {
        try {
            const response = await fetch('/api/admin/users', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    action: 'change_role',
                    username: username,
                    role: newRole
                })
            });

            if (!response.ok) {
                const data = await response.json();
                throw new Error(data.error || 'Failed to update role');
            }

            loadUsers();
        } catch (error) {
            alert('Error: ' + error.message);
            loadUsers(); // Reset UI
        }
    }

    async function loadAnnotations() {
        const tableBody = document.getElementById('annotationsTableBody');
        tableBody.innerHTML = '<tr><td colspan="6" style="padding: 16px; text-align: center; color: var(--color-text-muted);">Loading annotations...</td></tr>';

        try {
            const response = await fetch('/api/admin/annotations');
            if (!response.ok) throw new Error('Failed to load annotations');
            const data = await response.json();
            renderAnnotations(data);
        } catch (error) {
            console.error('Error:', error);
            tableBody.innerHTML = `<tr><td colspan="6" style="padding: 16px; text-align: center; color: var(--color-danger);">Error: ${error.message}</td></tr>`;
        }
    }

    function renderAnnotations(items) {
        const tableBody = document.getElementById('annotationsTableBody');
        if (!tableBody) return;

        if (items.length === 0) {
            tableBody.innerHTML = '<tr><td colspan="6" style="padding: 16px; text-align: center; color: var(--color-text-muted);">No annotations found.</td></tr>';
            return;
        }

        tableBody.innerHTML = items.map(item => {
            const stats = item.stats;
            // Safe JSON stringify for the data attribute
            const detailsJson = JSON.stringify(item.details).replace(/"/g, '&quot;');

            return `
            <tr style="border-bottom: 1px solid var(--color-border);">
                <td class="font-mono text-xs" style="padding: 12px 16px; color: var(--color-text-primary);">${item.item_id}</td>
                <td style="padding: 12px 16px; color: var(--color-text-primary);"><strong>${stats.unique_users || 0}</strong></td>
                <td style="padding: 12px 16px; color: var(--color-text-primary);">${stats.notes}</td>
                <td style="padding: 12px 16px; color: var(--color-text-primary);">${stats.closed_tags}</td>
                <td style="padding: 12px 16px; color: var(--color-text-primary);">${stats.open_tags}</td>
                <td style="padding: 12px 16px; text-align: right;">
                    <button onclick="toggleAnnotationDetails('${item.item_id}', this)" class="btn-discreet text-xs" style="padding: 4px 10px;">
                        Expand
                    </button>
                    <!-- Hidden data store for simpler access -->
                    <div id="data-${item.item_id}" style="display:none;">${detailsJson}</div>
                </td>
            </tr>
            <tr id="details-${item.item_id}" style="display: none; background: var(--color-bg-elevated); border-bottom: 2px solid var(--color-border);">
                <td colspan="6" style="padding: 0;">
                    <div id="content-${item.item_id}" style="padding: 16px; color: var(--color-text-primary);">Loading details...</div>
                </td>
            </tr>
            `;
        }).join('');
    }

    function toggleAnnotationDetails(itemId, btn) {
        const row = document.getElementById(`details-${itemId}`);
        const contentDiv = document.getElementById(`content-${itemId}`);
        const dataDiv = document.getElementById(`data-${itemId}`);

        if (!row || !contentDiv || !dataDiv) return;

        if (row.style.display === 'none') {
            // Expand
            row.style.display = 'table-row';
            if (btn) btn.innerText = "Collapse";

            // Render content if not already done
            try {
                const details = JSON.parse(dataDiv.textContent);
                renderAnnotationDetails(contentDiv, details);
            } catch (e) {
                console.error("Error parsing details", e);
                contentDiv.innerHTML = "Error loading details.";
            }
        } else {
            // Collapse
            row.style.display = 'none';
            if (btn) btn.innerText = "Expand";
        }
    }

    function renderAnnotationDetails(container, details) {
        if (Object.keys(details).length === 0) {
            container.innerHTML = '<p class="italic" style="color: var(--color-text-muted);">No details available.</p>';
            return;
        }

        let html = '<div style="display: flex; flex-direction: column; gap: 16px;">';

        for (const [variable, data] of Object.entries(details)) {
            html += `
            <div style="border: 1px solid var(--color-border); background: var(--color-bg-primary); border-radius: 6px; padding: 12px;">
                <h4 class="text-sm" style="margin-top: 0; margin-bottom: 8px; color: var(--color-text-heading); border-bottom: 1px solid var(--color-border); padding-bottom: 6px;">${data.label || variable}</h4>

                <div style="display: flex; flex-direction: column; gap: 8px;">
            `;

            // Open Tags
            if (Object.keys(data.open).length > 0) {
                html += `<div><span class="text-xxs uppercase font-semibold" style="color: var(--color-text-muted);">Tags:</span> <div style="display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px;">`;
                for (const [tag, users] of Object.entries(data.open)) {
                    const count = users.length;
                    const userList = users.join(', ');
                    html += `<span title="Used by: ${userList}" class="text-xs" style="background: var(--color-info); color: var(--color-text-primary); padding: 2px 8px; border-radius: 12px; cursor: help;">${tag} <span style="opacity: 0.7; font-size: 10px;">(${count})</span></span>`;
                }
                html += `</div></div>`;
            }

            // Closed Tags
            if (Object.keys(data.closed).length > 0) {
                html += `<div><span class="text-xxs uppercase font-semibold" style="color: var(--color-text-muted);">Closed Tags:</span> <div style="display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px;">`;
                for (const [val, users] of Object.entries(data.closed)) {
                    const count = users.length;
                    const userList = users.join(', ');
                    html += `<span title="Selected by: ${userList}" class="text-xs" style="background: var(--color-success); color: var(--color-text-primary); padding: 2px 8px; border-radius: 12px; cursor: help;">${val} <span style="opacity: 0.7; font-size: 10px;">(${count})</span></span>`;
                }
                html += `</div></div>`;
            }

            // Notes
            if (data.notes.length > 0) {
                html += `<div><span class="text-xxs uppercase font-semibold" style="color: var(--color-text-muted);">Notes:</span> <ul class="text-xs" style="margin: 4px 0 0 0; padding-left: 20px; color: var(--color-text-primary);">`;
                for (const note of data.notes) {
                    html += `<li title="By: ${note.user}" style="cursor: help; margin-bottom: 4px;">${note.text}</li>`;
                }
                html += `</ul></div>`;
            }

            html += `</div></div>`; // Close card and inner container
        }

        html += '</div>';
        container.innerHTML = html;
    }

    // --- Role Management ---

    async function loadRoles() {
        try {
            const rolesResp = await fetch('/api/admin/roles');
            if (!rolesResp.ok) throw new Error("Failed to load roles");
            const roles = await rolesResp.json();   // [{name, permissions: [...]}]
            window.availableRoles = roles;
            updateRoleSelects(roles);

            // The permission-matrix UI lives on the Roles sub-page and is only
            // useful to users who have it. For other admin sub-pages we just
            // need the role-name list above; skip the catalog fetch (it 403s
            // anyway for non-roles users).
            if (_hasPerm('tab.admin.roles')) {
                const catalogResp = await fetch('/api/admin/permissions/catalog');
                if (!catalogResp.ok) throw new Error("Failed to load permission catalog");
                const catalog = await catalogResp.json();
                window.permissionCatalog = catalog;
                renderRolePermissionMatrix(roles, catalog);
            }
        } catch (e) {
            console.error(e);
            alert("Error loading roles: " + e.message);
        }
    }

    function renderRolePermissionMatrix(roles, catalog) {
        const container = document.getElementById('roleList');
        if (!container) return;

        // Admin always has full access; rendering it as a column wastes space.
        const editableRoles = roles.filter(r => r.name !== 'admin');

        const header = `
            <thead>
                <tr>
                    <th style="text-align:left; padding: 8px 12px; border-bottom: 1px solid var(--color-border); position: sticky; left: 0; background: var(--color-bg-primary); z-index: 1;">Permission</th>
                    ${editableRoles.map(r => `
                        <th style="padding: 8px 12px; border-bottom: 1px solid var(--color-border); text-align: center; min-width: 90px;">
                            <div style="display:flex; flex-direction:column; align-items:center; gap:4px;">
                                <span class="font-medium" style="color: var(--color-text-primary);">${r.name}</span>
                                <span onclick="deleteRole('${r.name}')" class="text-xs" style="cursor:pointer; color: var(--color-danger);" title="Delete role">delete</span>
                            </div>
                        </th>
                    `).join('')}
                </tr>
            </thead>
        `;

        const rows = catalog.map(entry => {
            const cells = editableRoles.map(r => {
                const hasPerm = r.permissions.includes('*') || r.permissions.includes(entry.key);
                return `
                    <td style="text-align:center; padding: 6px 12px; border-bottom: 1px solid var(--color-border);">
                        <input type="checkbox"
                               data-role="${r.name}"
                               data-perm="${entry.key}"
                               ${hasPerm ? 'checked' : ''}
                               onchange="onPermissionToggle(this)">
                    </td>
                `;
            }).join('');
            return `
                <tr>
                    <td class="text-sm" style="padding: 6px 12px; border-bottom: 1px solid var(--color-border); white-space: pre; color: var(--color-text-primary); position: sticky; left: 0; background: var(--color-bg-primary);">${entry.label}</td>
                    ${cells}
                </tr>
            `;
        }).join('');

        container.innerHTML = `
            <div style="overflow-x: auto; max-width: 100%;">
                <table style="border-collapse: collapse; width: 100%;">
                    ${header}
                    <tbody>${rows}</tbody>
                </table>
            </div>
            <div id="rolePermStatus" class="text-xs" style="margin-top: 8px; min-height: 16px; color: var(--color-text-muted);"></div>
        `;
    }

    async function onPermissionToggle(checkbox) {
        const roleName = checkbox.getAttribute('data-role');
        const status = document.getElementById('rolePermStatus');
        const allBoxes = document.querySelectorAll(`input[data-role="${roleName}"]:not([disabled])`);
        const perms = Array.from(allBoxes).filter(b => b.checked).map(b => b.getAttribute('data-perm'));

        checkbox.disabled = true;
        if (status) status.textContent = `Saving permissions for ${roleName}…`;

        try {
            const response = await fetch(`/api/admin/roles/${encodeURIComponent(roleName)}/permissions`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ permissions: perms })
            });
            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                throw new Error(data.error || 'Save failed');
            }
            if (status) {
                status.textContent = `Saved (${roleName}).`;
                setTimeout(() => { if (status.textContent.startsWith('Saved')) status.textContent = ''; }, 2000);
            }
        } catch (e) {
            console.error('onPermissionToggle:', e);
            checkbox.checked = !checkbox.checked; // revert
            if (status) status.textContent = `Error: ${e.message}`;
        } finally {
            checkbox.disabled = false;
        }
    }

    function updateRoleSelects(roles) {
        // Roles are now [{name, permissions}] — extract names for the dropdowns.
        const roleNames = roles.map(r => r.name);

        const newRoleSelect = document.getElementById('newRole');
        if (newRoleSelect) {
            populateRoleSelect(newRoleSelect, roleNames, 'viewer');
        }

        const userSelects = document.querySelectorAll('.role-select');
        userSelects.forEach(sel => {
            const currentRole = sel.getAttribute('data-current-role');
            populateRoleSelect(sel, roleNames, currentRole);
        });
    }

    function populateRoleSelect(select, roleNames, current = null) {
        select.innerHTML = roleNames.map(r =>
            `<option value="${r}" ${r === current ? 'selected' : ''}>${r.charAt(0).toUpperCase() + r.slice(1)}</option>`
        ).join('');
    }

    async function addRole() {
        const nameInput = document.getElementById('newRoleName');
        const roleName = nameInput.value.trim();
        if (!roleName) return;

        try {
            const response = await fetch('/api/admin/roles', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ role_name: roleName })
            });

            if (!response.ok) {
                const data = await response.json();
                throw new Error(data.error || "Failed");
            }

            nameInput.value = '';
            loadRoles();
        } catch (e) {
            alert(e.message);
        }
    }

    async function deleteRole(roleName) {
        if (!confirm(`Are you sure you want to delete role '${roleName}'?`)) return;

        try {
            const response = await fetch(`/api/admin/roles?role_name=${encodeURIComponent(roleName)}`, {
                method: 'DELETE'
            });

            if (!response.ok) {
                const data = await response.json();
                throw new Error(data.error || "Failed");
            }

            loadRoles();
        } catch (e) {
            alert(e.message);
        }
    }

