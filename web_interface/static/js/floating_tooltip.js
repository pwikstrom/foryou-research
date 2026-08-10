/*
 * floating_tooltip.js — renders every `.meta-tooltip[data-tooltip]` into one
 * fixed-position element on <body> instead of the element's own ::after.
 *
 * Why: a ::after tooltip lives inside its trigger's box, so any ancestor with
 * `overflow: hidden|auto` clips it. Two of those ancestors are load-bearing and
 * cannot simply be dropped — the Video Analysis details panel scrolls
 * (`overflow-y: auto`) and its key cells clip long variable names
 * (`.detail-row > td { overflow: hidden }`) — so those tooltips were invisible,
 * and the ones on control bars at the top of a scrolling tab pane were cut off
 * because they open upward.
 *
 * A single fixed element escapes every clipping ancestor, and can flip
 * above/below and clamp sideways to stay on screen. The stylesheet suppresses
 * the ::after rendering while this script is active
 * (`html.fyp-floating-tooltips`), so exactly one tooltip is ever drawn.
 *
 * The class placement is the contract: an element opts in by carrying
 * `.meta-tooltip` and a non-empty `data-tooltip`. Two optional modifiers, kept
 * for compatibility with the CSS they were written for:
 *   .tooltip-below           — open downward first instead of upward
 *   .tooltip-right-anchored  — align the tooltip's right edge to the trigger's
 */

(function (global) {
    'use strict';

    const GAP = 6;      // px between trigger and tooltip
    const MARGIN = 8;   // px minimum clearance from the viewport edge

    let tipEl = null;
    let activeTrigger = null;


    function _ensureEl() {
        if (tipEl && tipEl.isConnected) return tipEl;
        tipEl = document.createElement('div');
        tipEl.id = 'fyp-tooltip';
        tipEl.setAttribute('role', 'tooltip');
        document.body.appendChild(tipEl);
        return tipEl;
    }


    /** The tooltip-bearing element at/above `node`, or null. */
    function _triggerFor(node) {
        if (!node || typeof node.closest !== 'function') return null;
        const el = node.closest('.meta-tooltip[data-tooltip]');
        if (!el) return null;
        const text = el.getAttribute('data-tooltip');
        return (text && text.trim()) ? el : null;
    }


    function hide() {
        activeTrigger = null;
        if (tipEl) tipEl.classList.remove('is-visible');
    }


    function _place(trigger) {
        const el = _ensureEl();
        const r = trigger.getBoundingClientRect();
        // A trigger scrolled out of its own container has a zero box — nothing
        // to anchor to, so say nothing rather than park a tooltip at 0,0.
        if (r.width === 0 && r.height === 0) {
            hide();
            return;
        }

        const t = el.getBoundingClientRect();
        const vw = document.documentElement.clientWidth;
        const vh = document.documentElement.clientHeight;

        // Vertical: honour the .tooltip-below preference, then fall back to
        // whichever side actually has room.
        const preferBelow = trigger.classList.contains('tooltip-below');
        const fitsAbove = r.top - t.height - GAP >= MARGIN;
        const fitsBelow = r.bottom + t.height + GAP <= vh - MARGIN;
        const below = preferBelow ? (fitsBelow || !fitsAbove) : (!fitsAbove && fitsBelow);
        let top = below ? r.bottom + GAP : r.top - t.height - GAP;
        top = Math.min(Math.max(top, MARGIN), Math.max(MARGIN, vh - t.height - MARGIN));

        // Horizontal: anchored to whichever trigger edge the caller asked for,
        // then clamped so a wide tooltip near a viewport edge stays readable.
        let left = trigger.classList.contains('tooltip-right-anchored')
            ? r.right - t.width
            : r.left;
        left = Math.min(Math.max(left, MARGIN), Math.max(MARGIN, vw - t.width - MARGIN));

        el.style.top = `${Math.round(top)}px`;
        el.style.left = `${Math.round(left)}px`;
    }


    function show(trigger) {
        const el = _ensureEl();
        activeTrigger = trigger;
        el.textContent = trigger.getAttribute('data-tooltip');
        // Measure at final width before positioning: the element must already be
        // laid out, so make it visible-but-transparent for this frame.
        el.style.top = '-9999px';
        el.style.left = '-9999px';
        el.classList.add('is-visible');
        _place(trigger);
    }


    document.addEventListener('pointerover', (ev) => {
        const trigger = _triggerFor(ev.target);
        if (!trigger) return;
        if (trigger !== activeTrigger) show(trigger);
    });

    document.addEventListener('pointerout', (ev) => {
        if (!activeTrigger) return;
        // Ignore moves that stay inside the same trigger's subtree.
        const to = ev.relatedTarget;
        if (to && activeTrigger.contains(to)) return;
        hide();
    });

    // Keyboard/AT parity: focusing a tooltip-bearing control reveals it too.
    document.addEventListener('focusin', (ev) => {
        const trigger = _triggerFor(ev.target);
        if (trigger) show(trigger);
    });
    document.addEventListener('focusout', hide);

    // A tooltip anchored to a fixed viewport position goes stale the moment
    // anything moves, and a click means the user is done reading.
    document.addEventListener('pointerdown', hide, true);
    document.addEventListener('scroll', hide, true);
    global.addEventListener('resize', hide);

    document.documentElement.classList.add('fyp-floating-tooltips');

    global.fypHideTooltip = hide;
})(window);
