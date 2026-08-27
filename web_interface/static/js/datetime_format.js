/*
 * datetime_format.js — the single place that decides how the UI renders a date
 * or a time.
 *
 * Two kinds of value cross the API boundary and they are NOT interchangeable:
 *
 *   Instants — a moment in system time: a task ran, a study was refreshed, a
 *   user logged in, an annotation version was created. The server sends these
 *   as offset-aware ISO-8601 ("2026-07-28T03:14:15+00:00") or as an epoch
 *   number. They render in the VIEWER's timezone, because that is the clock the
 *   person reading the screen is using.
 *
 *   Wall-clock stamps — participant activity times (local_timestamp,
 *   local_date, first_event_ts, period labels). These are already expressed in
 *   the donor's own timezone and deliberately carry no offset. Converting them
 *   to the viewer's zone would move a 9pm scroll session to a different hour of
 *   a different day, so they render verbatim.
 *
 * Because the server is consistent about the offset, the two cases are
 * distinguishable from the value alone: an offset (or a bare number) means
 * instant, no offset means wall clock. fypFmtAuto() applies that rule and is
 * the right default for generic metadata panels.
 *
 * Instant helpers are named fypFmt*, wall-clock helpers fypWall*.
 */

(function (global) {
    'use strict';

    const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

    // A trailing "Z" or "+HH:MM"/"-HHMM" is what makes a string an instant.
    const OFFSET_RE = /(?:Z|z|[+-]\d{2}:?\d{2})$/;

    // YYYY-MM-DD, optionally followed by a time, with "T" or a space separator.
    const WALL_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?/;

    // Epoch values below this are seconds, above are milliseconds. 1e11 seconds
    // is year 5138 and 1e11 milliseconds is 1973, so nothing real is ambiguous.
    const EPOCH_MS_THRESHOLD = 1e11;

    const pad2 = (n) => String(n).padStart(2, '0');


    /**
     * True when the value is a zone-less date/date-time string, i.e. a
     * participant wall-clock stamp rather than an instant.
     */
    function fypIsWallClock(value) {
        if (typeof value !== 'string') return false;
        const s = value.trim();
        return WALL_RE.test(s) && !OFFSET_RE.test(s);
    }


    /**
     * Coerce an instant into a Date, or null when it is not parseable.
     *
     * Accepts a Date, an epoch number (seconds or milliseconds), or an ISO
     * string. A zone-less ISO string is read as UTC: the server stamps every
     * instant offset-aware, so a naive one can only come from stored history
     * written before that was true, and those were all UTC.
     */
    function fypParseInstant(value) {
        if (value == null || value === '') return null;
        if (value instanceof Date) return isNaN(value.getTime()) ? null : value;

        if (typeof value === 'number') {
            if (!isFinite(value)) return null;
            const ms = Math.abs(value) < EPOCH_MS_THRESHOLD ? value * 1000 : value;
            const d = new Date(ms);
            return isNaN(d.getTime()) ? null : d;
        }

        if (typeof value !== 'string') return null;
        let s = value.trim();
        if (s === '') return null;

        // A bare numeric string is an epoch, not an ISO date.
        if (/^-?\d+(\.\d+)?$/.test(s)) return fypParseInstant(Number(s));

        if (WALL_RE.test(s) && !OFFSET_RE.test(s)) {
            // Normalise "2026-07-28 03:14:15" to a form every browser parses as
            // UTC rather than as the viewer's local time.
            s = s.replace(' ', 'T') + 'Z';
        }
        const d = new Date(s);
        return isNaN(d.getTime()) ? null : d;
    }


    /** Split a wall-clock string into calendar parts without any conversion. */
    function _wallParts(value) {
        if (value == null) return null;
        const m = WALL_RE.exec(String(value).trim());
        if (!m) return null;
        return {
            year: Number(m[1]),
            month: Number(m[2]),
            day: Number(m[3]),
            hours: m[4] === undefined ? null : Number(m[4]),
            minutes: m[5] === undefined ? null : Number(m[5]),
            seconds: m[6] === undefined ? null : Number(m[6]),
        };
    }


    /** "28-Jul-2026 14:32" in the viewer's timezone. */
    function fypFmtDateTime(value, fallback) {
        const d = fypParseInstant(value);
        if (!d) return fallback === undefined ? '' : fallback;
        return `${pad2(d.getDate())}-${MONTHS[d.getMonth()]}-${d.getFullYear()}`
            + ` ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
    }


    /**
     * "28-Jul 14:32" in the viewer's timezone — the year-less form, for the
     * cramped process/status cards where the year is never in doubt.
     */
    function fypFmtDateTimeShort(value, fallback) {
        const d = fypParseInstant(value);
        if (!d) return fallback === undefined ? '' : fallback;
        return `${pad2(d.getDate())}-${MONTHS[d.getMonth()]}`
            + ` ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
    }


    /** "28-Jul-2026" in the viewer's timezone. */
    function fypFmtDate(value, fallback) {
        const d = fypParseInstant(value);
        if (!d) return fallback === undefined ? '' : fallback;
        return `${pad2(d.getDate())}-${MONTHS[d.getMonth()]}-${d.getFullYear()}`;
    }


    /**
     * "28-Jul-26" in the viewer's timezone — the two-digit-year form, for wide
     * tables where a date column has to give ground. Only for a column whose
     * century is never in doubt; pair it with a full-form title attribute.
     */
    function fypFmtDateShort(value, fallback) {
        const d = fypParseInstant(value);
        if (!d) return fallback === undefined ? '' : fallback;
        return `${pad2(d.getDate())}-${MONTHS[d.getMonth()]}-${pad2(d.getFullYear() % 100)}`;
    }


    /**
     * "28-Jul-2026 14:32:07 AEST" — the unabbreviated form, with seconds and an
     * explicit zone. Use it in tooltips and title attributes so the exact
     * instant is always recoverable from a screen that shows the short form.
     */
    function fypFmtDateTimeFull(value, fallback) {
        const d = fypParseInstant(value);
        if (!d) return fallback === undefined ? '' : fallback;
        return `${pad2(d.getDate())}-${MONTHS[d.getMonth()]}-${d.getFullYear()}`
            + ` ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
            + ` ${fypTimeZoneLabel()}`;
    }


    /** "just now" / "5m ago" / "3h ago" / "2d ago", relative to the viewer's clock. */
    function fypFmtRelative(value, fallback) {
        const d = fypParseInstant(value);
        if (!d) return fallback === undefined ? '' : fallback;
        const seconds = (Date.now() - d.getTime()) / 1000;
        if (!isFinite(seconds) || seconds < 0) return fallback === undefined ? '' : fallback;
        if (seconds < 90) return 'just now';
        if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
        if (seconds < 129600) return `${Math.round(seconds / 3600)}h ago`;
        return `${Math.round(seconds / 86400)}d ago`;
    }


    /** The viewer's timezone abbreviation ("AEST", "CEST"), for labelling. */
    function fypTimeZoneLabel() {
        try {
            const parts = new Intl.DateTimeFormat(undefined, { timeZoneName: 'short' })
                .formatToParts(new Date());
            const zone = parts.find((p) => p.type === 'timeZoneName');
            if (zone && zone.value) return zone.value;
        } catch (e) {
            // Fall through to the numeric offset below.
        }
        const mins = -new Date().getTimezoneOffset();
        const sign = mins >= 0 ? '+' : '-';
        return `UTC${sign}${pad2(Math.floor(Math.abs(mins) / 60))}:${pad2(Math.abs(mins) % 60)}`;
    }


    /**
     * "28-Jul-2026 14:32:07" exactly as stored — no timezone conversion.
     * Seconds render whenever the stored stamp carries them: participant
     * activity is second-resolution data (plays inside a binge are often
     * seconds apart), so truncating to minutes loses research information.
     */
    function fypWallDateTime(value, fallback) {
        const p = _wallParts(value);
        if (!p) return fallback === undefined ? '' : fallback;
        const date = `${pad2(p.day)}-${MONTHS[p.month - 1]}-${p.year}`;
        if (p.hours === null) return date;
        const time = `${date} ${pad2(p.hours)}:${pad2(p.minutes)}`;
        return p.seconds === null ? time : `${time}:${pad2(p.seconds)}`;
    }


    /** "28-Jul-2026" exactly as stored — no timezone conversion. */
    function fypWallDate(value, fallback) {
        const p = _wallParts(value);
        if (!p) return fallback === undefined ? '' : fallback;
        return `${pad2(p.day)}-${MONTHS[p.month - 1]}-${p.year}`;
    }


    /** "28-Jul-26" exactly as stored — the two-digit-year form of fypWallDate. */
    function fypWallDateShort(value, fallback) {
        const p = _wallParts(value);
        if (!p) return fallback === undefined ? '' : fallback;
        return `${pad2(p.day)}-${MONTHS[p.month - 1]}-${pad2(p.year % 100)}`;
    }


    /**
     * "2026-07-28" exactly as stored — no timezone conversion. For values that
     * feed date inputs and API filters rather than prose.
     */
    function fypWallIsoDate(value, fallback) {
        const p = _wallParts(value);
        if (p) return `${p.year}-${pad2(p.month)}-${pad2(p.day)}`;
        // An offset-aware value still has to resolve to a calendar day; use the
        // viewer's, matching what fypFmtDate() would show.
        const d = fypParseInstant(value);
        if (!d) return fallback === undefined ? null : fallback;
        return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
    }


    /**
     * Format by inspecting the value: offset-aware and epoch values are shown
     * as instants in the viewer's timezone, zone-less strings verbatim. Use for
     * generic panels that display whatever timestamp column happens to exist.
     */
    function fypFmtAuto(value, fallback) {
        if (fypIsWallClock(value)) return fypWallDateTime(value, fallback);
        return fypFmtDateTime(value, fallback);
    }


    /**
     * Fill in server-rendered timestamps. A template emits the raw instant as
     * `<span data-fyp-instant="{{ iso }}"></span>` and this rewrites it in the
     * viewer's timezone once the page loads, so Jinja pages follow the same
     * convention as the JS-rendered ones without the server guessing a zone.
     */
    function fypUpgradeInstantElements(root) {
        const scope = root || document;
        scope.querySelectorAll('[data-fyp-instant]').forEach((el) => {
            const raw = el.getAttribute('data-fyp-instant');
            const text = el.dataset.fypFormat === 'date'
                ? fypFmtDate(raw)
                : fypFmtDateTime(raw);
            if (!text) return;
            el.textContent = text;
            el.setAttribute('title', fypFmtDateTimeFull(raw));
        });
    }

    document.addEventListener('DOMContentLoaded', () => fypUpgradeInstantElements());

    global.fypIsWallClock = fypIsWallClock;
    global.fypParseInstant = fypParseInstant;
    global.fypFmtDateTime = fypFmtDateTime;
    global.fypFmtDateTimeShort = fypFmtDateTimeShort;
    global.fypFmtDate = fypFmtDate;
    global.fypFmtDateShort = fypFmtDateShort;
    global.fypFmtDateTimeFull = fypFmtDateTimeFull;
    global.fypFmtRelative = fypFmtRelative;
    global.fypFmtAuto = fypFmtAuto;
    global.fypTimeZoneLabel = fypTimeZoneLabel;
    global.fypWallDateTime = fypWallDateTime;
    global.fypWallDate = fypWallDate;
    global.fypWallDateShort = fypWallDateShort;
    global.fypWallIsoDate = fypWallIsoDate;
    global.fypUpgradeInstantElements = fypUpgradeInstantElements;
})(window);
