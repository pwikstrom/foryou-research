// Data Pipeline: Shared state and loaders for the Data Pipeline scripts: studies, collections, roles.
// One of the js/data_management/*.js files; they share one global scope and
// load in a fixed order (templates/index.html).

let allStudies = [];
const savingStudies = new Set(); // Track studies currently being saved
const refreshingStudies = new Map(); // Track studies being refreshed: name → {message}

function loadStudies() {
    fetch('/api/manage/studies')
        .then(response => response.json())
        .then(data => {
            allStudies = data;
            renderStudiesTable();
            if (typeof populateEnrichmentStudySelect === 'function') {
                populateEnrichmentStudySelect(data);
            }
        })
        .catch(err => console.error("Error loading studies:", err));
}

let availableCollections = [];

function loadAvailableCollections() {
    fetch('/api/manage/collections')
        .then(res => res.json())
        .then(data => {
            availableCollections = data;

            const ucEl = document.getElementById('ingest-unique-collections-count');
            if (ucEl) ucEl.textContent = availableCollections.length.toLocaleString();

            loadStudies();

            const editContainer = document.getElementById('edit-activity-list-container');
            if (editContainer) {
                renderEditActivityTable(editContainer);
                const searchInput = document.getElementById('edit-activity-search');
                if (searchInput && searchInput.value) {
                    filterEditActivityCollections(searchInput);
                }
            }
        })
        .catch(err => {
            console.error("Error loading collections list:", err);
            loadStudies();

            const editContainer = document.getElementById('edit-activity-list-container');
            if (editContainer) {
                renderEditActivityTable(editContainer);
                const searchInput = document.getElementById('edit-activity-search');
                if (searchInput && searchInput.value) {
                    filterEditActivityCollections(searchInput);
                }
            }
        });
}

// Global cache for roles
let systemRoles = [];

function loadSystemRoles(callback) {
    fetch('/api/admin/roles')
        .then(res => res.json())
        .then(data => {
            // /api/admin/roles now returns [{name, permissions}]; downstream
            // code (access dropdown etc.) expects a list of role-name strings.
            systemRoles = Array.isArray(data)
                ? data.map(r => (typeof r === 'string' ? r : r.name)).filter(Boolean)
                : [];
            if (callback) callback();
        })
        .catch(err => {
            // Non-admins can't read /api/admin/roles (it returns an HTML
            // login/403 page, not JSON). Roles only drive the admin-only
            // access dropdown, so fall back to an empty list and still run
            // the callback — otherwise the study modal never opens for them.
            console.error("Error loading roles:", err);
            systemRoles = [];
            if (callback) callback();
        });
}

