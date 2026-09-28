/*
 * dom_utils.js — DOM helpers shared by every app script.
 *
 * All app scripts are classic scripts sharing one global scope, so a helper
 * defined in two files silently resolves to whichever loaded last — and which
 * files load depends on the viewer's permissions. Shared helpers therefore live
 * here, once, loaded from base.html <head> before any body script.
 * Guard: tests/unit/test_js_global_collisions.py.
 */

// HTML-escape a value before inserting it into innerHTML — element content or
// a quoted attribute value. null/undefined render as the empty string.
function escapeHtml(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

let _toastContainer = null;

// Transient bottom-right notice. level: 'success' | 'warning' | 'error'.
function showToast(message, level = 'success', duration = 5000) {
    if (!_toastContainer) {
        _toastContainer = document.createElement('div');
        _toastContainer.id = 'dm-toast-container';
        _toastContainer.style.cssText = 'position: fixed; bottom: 24px; right: 24px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; pointer-events: none;';
        document.body.appendChild(_toastContainer);
    }

    const colorVar = level === 'error'
        ? 'var(--color-danger)'
        : level === 'warning'
            ? 'var(--color-warning)'
            : 'var(--color-success-light, var(--color-text-primary))';

    const toast = document.createElement('div');
    toast.className = 'text-sm';
    toast.style.cssText = `
        background: var(--color-bg-elevated, var(--color-bg-input));
        color: var(--color-text-primary);
        border-left: 4px solid ${colorVar};
        padding: 12px 16px;
        border-radius: 4px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.25);
        max-width: 420px;
        opacity: 0;
        transform: translateX(20px);
        transition: opacity 0.2s ease, transform 0.2s ease;
        pointer-events: auto;
    `;
    toast.textContent = message;
    _toastContainer.appendChild(toast);

    // Defer the target opacity/transform to a later tick so the browser has
    // a chance to paint the initial (faded) state first. Setting them in the
    // same tick — even after a reflow — gets collapsed into a single paint
    // by Chrome and the transition is skipped.
    setTimeout(() => {
        toast.style.opacity = '1';
        toast.style.transform = 'translateX(0)';
    }, 16);

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(20px)';
        setTimeout(() => toast.remove(), 250);
    }, duration);
}
