// Pre-upload donation review: parse the participant's platform export
// entirely in the browser, render per-section cards with row-level
// delete/restore, and produce a pruned artifact for upload. Driven by the
// per-platform `review` manifest served by /api/my/collections/upload/sources
// (built in Python next to the ingest parsers, so the section list can never
// drift from what the pipeline reads). Privacy invariants: nothing here makes
// a network request, and the pruned file is rebuilt from kept rows only —
// deleted rows and (for TikTok) unlisted sections are absent, not flagged.
(function () {
    'use strict';

    // ------------------------------------------------------------------
    // Parsing helpers
    // ------------------------------------------------------------------

    function isPlainDict(x) {
        return x !== null && typeof x === 'object' && !Array.isArray(x);
    }

    function pad2(n) { return n < 10 ? '0' + n : '' + n; }

    // Local-time stamp without toLocaleString — that call costs ~5-10µs each,
    // which matters when a 300K-row export is formatted at model-build time.
    function fastStamp(d) {
        if (isNaN(d.getTime())) return '';
        return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
               `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
    }

    function fmtEpoch(ts) {
        const n = Number(ts);
        if (!isFinite(n) || n <= 0) return '';
        return fastStamp(new Date(n * 1000));
    }

    function fmtIso(s) {
        if (!s) return '';
        const d = new Date(s);
        return isNaN(d.getTime()) ? String(s) : fastStamp(d);
    }

    function newSection(base) {
        return Object.assign({
            included: true, rowDelete: true, toggleOnly: false, note: '',
            columns: [], rows: [], itemIdx: [], deleted: new Set(),
            selected: new Set(), filter: '', renderLimit: 0, open: false,
            // Per-row link target for the hrefCol column; URL-looking cell
            // values in any column linkify automatically.
            hrefs: [], hrefCol: -1,
        }, base);
    }

    // --- TikTok: whole-document JSON walk -----------------------------

    function buildJsonSectionsModel(root, review, file) {
        // Mirror fyp/ingest/tiktok.py load_single_raw: iterative DFS over the
        // document; every non-empty list containing dict items is a candidate
        // section keyed by its (lowercased) immediate parent key. Section
        // classification mirrors process_single: "chat history with" keys are
        // always dropped, whitelisted ids become review sections, a list whose
        // records' second key is "ip" is login history, everything else is
        // stripped from the upload.
        const byId = {};
        for (const s of review.sections) byId[s.id] = s;
        const ipRule = review.sections.find(s => s.id_rule === 'second_key_ip') || null;

        const sections = [];
        const stripped = [];
        const stack = [{ key: null, obj: root, path: [] }];
        while (stack.length) {
            const { key, obj, path } = stack.pop();
            if (Array.isArray(obj)) {
                if (!obj.some(it => isPlainDict(it) && Object.keys(it).length)) continue;
                const lowered = (key || '').toLowerCase();
                const firstDict = obj.find(it => isPlainDict(it) && Object.keys(it).length);
                const keys = Object.keys(firstDict);
                let manifestEntry = null;
                if (lowered.indexOf('chat history with') !== -1) {
                    manifestEntry = null;  // DMs: never uploaded
                } else if (byId[lowered]) {
                    manifestEntry = byId[lowered];
                } else if (ipRule && keys.length > 1 && keys[1].toLowerCase() === 'ip') {
                    manifestEntry = ipRule;
                }
                const nDicts = obj.reduce((n, it) => n + (isPlainDict(it) && Object.keys(it).length ? 1 : 0), 0);
                if (!manifestEntry) {
                    stripped.push({ key: key || '(unnamed)', count: nDicts });
                    continue;
                }
                const sec = newSection({
                    id: manifestEntry.id,
                    matchId: lowered,
                    title: manifestEntry.title || key,
                    rowDelete: manifestEntry.row_delete !== false,
                    columns: keys.slice(0, 2),
                    listRef: obj,
                    path: path.concat([key]),
                });
                obj.forEach((it, i) => {
                    if (!isPlainDict(it) || !Object.keys(it).length) return;
                    const vals = Object.values(it);
                    sec.rows.push([String(vals[0] ?? ''), String(vals[1] ?? '')]);
                    sec.itemIdx.push(i);
                });
                sections.push(sec);
            } else if (isPlainDict(obj)) {
                for (const k of Object.keys(obj)) {
                    stack.push({ key: k, obj: obj[k], path: path.concat(key === null ? [] : [key]) });
                }
            }
        }
        // Stable order: manifest order first, then by discovery for duplicates.
        const order = review.sections.map(s => s.id);
        sections.sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id));
        stripped.sort((a, b) => b.count - a.count);
        return {
            kind: 'json_sections', file, review, root, sections, stripped,
            strippedOther: true,  // profile/settings scalars are always dropped by the rebuild
        };
    }

    // --- Instagram record schemas (mirror instagram.py _records/_extract) --

    function igRecords(payload) {
        if (payload == null) return { list: [], wrapKey: null, single: false };
        if (Array.isArray(payload)) return { list: payload, wrapKey: null, single: false };
        if (isPlainDict(payload)) {
            if ('label_values' in payload || 'string_list_data' in payload) {
                return { list: [payload], wrapKey: null, single: true };
            }
            for (const k of Object.keys(payload)) {
                const v = payload[k];
                if (Array.isArray(v) && v.length && isPlainDict(v[0])) {
                    return { list: v, wrapKey: k, single: false };
                }
            }
        }
        return { list: [], wrapKey: null, single: false };
    }

    function igExtract(record) {
        let url = null, author = null, timestamp = record.timestamp ?? null;
        if (record.label_values) {
            for (const lv of record.label_values) {
                if (lv.label === 'URL' && !url) url = lv.value || lv.href;
                else if (lv.title === 'Owner') {
                    for (const outer of lv.dict || []) {
                        for (const inner of outer.dict || []) {
                            if (inner.label === 'Username' && !author) author = inner.value;
                            else if (inner.label === 'Name' && !author) author = inner.value;
                        }
                    }
                }
            }
        } else {
            author = record.title || null;
            const entries = record.string_list_data || [];
            const first = (entries.length && isPlainDict(entries[0])) ? entries[0] : {};
            url = first.href || null;
            if (timestamp == null) timestamp = first.timestamp ?? null;
            if (timestamp == null && isPlainDict(record.string_map_data)) {
                for (const entry of Object.values(record.string_map_data)) {
                    if (isPlainDict(entry) && entry.timestamp) { timestamp = entry.timestamp; break; }
                }
            }
        }
        return { url, author, timestamp };
    }

    // --- CSV: record-range scanner (RFC 4180) -------------------------

    function scanCsv(text) {
        // Track quote state and record the [start, end) char range of every
        // logical record (end includes the record's line terminator). Pruning
        // concatenates header + kept ranges verbatim, so quoted commas and
        // embedded newlines can never be corrupted by re-serialization.
        const ranges = [];
        let start = 0, inQuotes = false;
        for (let i = 0; i < text.length; i++) {
            const ch = text[i];
            if (ch === '"') inQuotes = !inQuotes;
            else if (ch === '\n' && !inQuotes) {
                ranges.push([start, i + 1]);
                start = i + 1;
            }
        }
        if (start < text.length) ranges.push([start, text.length]);
        return ranges;
    }

    function csvFields(text, range) {
        const fields = [];
        let cur = '', inQuotes = false;
        for (let i = range[0]; i < range[1]; i++) {
            const ch = text[i];
            if (inQuotes) {
                if (ch === '"') {
                    if (text[i + 1] === '"') { cur += '"'; i++; } else inQuotes = false;
                } else cur += ch;
            } else if (ch === '"') inQuotes = true;
            else if (ch === ',') { fields.push(cur); cur = ''; }
            else if (ch === '\r' || ch === '\n') { /* terminator */ }
            else cur += ch;
        }
        fields.push(cur);
        return fields;
    }

    // --- Zip-member model (Instagram / YouTube) -----------------------

    async function buildZipMembersModel(file, review, onStatus) {
        const zipLib = await DonationZip.loadZipLib();
        let reader = null;
        try {
            reader = new zipLib.ZipReader(new zipLib.BlobReader(file));
            const entries = await reader.getEntries();
            // Suffix match, first match per section wins (read_zip_members semantics).
            const matched = [];
            const taken = new Set();
            for (const secDef of review.sections) {
                for (const entry of entries) {
                    if (entry.directory || taken.has(entry.filename)) continue;
                    if (entry.filename.endsWith(secDef.id)) {
                        matched.push({ secDef, entry });
                        taken.add(entry.filename);
                        break;
                    }
                }
            }
            if (!matched.length) {
                const err = new Error('no expected members');
                err.code = 'no_members';
                throw err;
            }
            // JSON watch history beats the HTML fallback (server prefers JSON;
            // the HTML member would never be read, so it is not uploaded).
            const hasJsonWatch = matched.some(m => m.secDef.parser === 'youtube_watch_json');
            const useful = matched.filter(m => !(hasJsonWatch && m.secDef.parser === 'opaque'));

            const sections = [];
            for (const { secDef, entry } of useful) {
                onStatus(`Reading ${entry.filename}...`);
                const sec = newSection({
                    id: secDef.id,
                    title: secDef.title || secDef.id,
                    rowDelete: secDef.row_delete !== false,
                    toggleOnly: !!secDef.toggle_only,
                    note: secDef.note || '',
                    memberFilename: entry.filename,
                    parser: secDef.parser,
                });
                if (secDef.parser === 'opaque') {
                    const blob = await entry.getData(new zipLib.BlobWriter());
                    sec.opaqueBlob = blob;
                    const text = await blob.text();
                    sec.opaqueCount = (text.match(/watch\?v=/g) || []).length;
                } else if (secDef.parser === 'csv') {
                    const text = await entry.getData(new zipLib.TextWriter());
                    const ranges = scanCsv(text);
                    sec.csvText = text;
                    sec.csvHeader = ranges.length ? ranges[0] : null;
                    sec.csvRanges = ranges.slice(1).filter(r => text.slice(r[0], r[1]).trim() !== '');
                    const headers = sec.csvHeader ? csvFields(text, sec.csvHeader).map(String) : [];
                    // Show the most meaningful columns first (timestamp, video
                    // id, free text), capped at 4 — Takeout CSVs put the
                    // interesting columns last.
                    const rank = (h) => {
                        const l = h.toLowerCase();
                        if (l.includes('timestamp')) return 0;
                        if (l === 'video id') return 1;
                        if (l.includes('text')) return 2;
                        return 3;
                    };
                    sec.colIdx = headers.map((h, i) => [rank(h), i])
                        .sort((a, b) => a[0] - b[0] || a[1] - b[1]).slice(0, 4).map(p => p[1]);
                    sec.columns = sec.colIdx.map(i => headers[i]);
                    const videoIdx = headers.findIndex(h => h.toLowerCase() === 'video id');
                    const commentIdx = headers.findIndex(h => h.toLowerCase() === 'comment id');
                    sec.hrefCol = sec.colIdx.indexOf(videoIdx);
                    for (const r of sec.csvRanges) {
                        const fields = csvFields(text, r);
                        sec.rows.push(sec.colIdx.map(i => String(fields[i] ?? '')));
                        let href = null;
                        if (videoIdx !== -1 && fields[videoIdx]) {
                            href = `https://www.youtube.com/watch?v=${encodeURIComponent(fields[videoIdx])}`;
                            if (commentIdx !== -1 && fields[commentIdx]) {
                                href += `&lc=${encodeURIComponent(fields[commentIdx])}`;
                            }
                        }
                        sec.hrefs.push(href);
                    }
                    sec.itemIdx = sec.rows.map((_, i) => i);
                } else if (secDef.parser === 'youtube_watch_json') {
                    const text = await entry.getData(new zipLib.TextWriter());
                    const payload = JSON.parse(text);
                    if (!Array.isArray(payload)) throw new Error(`${entry.filename} is not a JSON array`);
                    sec.listRef = payload;
                    sec.columns = ['When', 'Video', 'Channel', ''];
                    sec.hrefCol = 1;  // the Video cell opens rec.titleUrl
                    payload.forEach((rec, i) => {
                        if (!isPlainDict(rec)) return;
                        const isAd = (rec.details || []).some(d => isPlainDict(d) && d.name === 'From Google Ads');
                        const title = String(rec.title || '').replace(/^Watched /, '');
                        const channel = (rec.subtitles && rec.subtitles[0] && rec.subtitles[0].name) || '';
                        sec.rows.push([fmtIso(rec.time), title, String(channel), isAd ? 'Ad' : '']);
                        sec.hrefs.push(rec.titleUrl || null);
                        sec.itemIdx.push(i);
                    });
                } else {  // instagram_records
                    const text = await entry.getData(new zipLib.TextWriter());
                    const payload = JSON.parse(text);
                    const { list, wrapKey, single } = igRecords(payload);
                    sec.listRef = list;
                    sec.payloadRef = payload;
                    sec.wrapKey = wrapKey;
                    sec.single = single;
                    sec.columns = ['When', 'Post', 'Account'];
                    list.forEach((rec, i) => {
                        if (!isPlainDict(rec)) return;
                        const ex = igExtract(rec);
                        sec.rows.push([fmtEpoch(ex.timestamp), String(ex.url || ''), String(ex.author || '')]);
                        sec.itemIdx.push(i);
                    });
                }
                if (sec.rows.length || sec.toggleOnly) sections.push(sec);
            }
            return { kind: 'zip_members', file, review, zipLib, sections, stripped: [], strippedOther: false };
        } finally {
            if (reader) { try { await reader.close(); } catch (e) { /* closed */ } }
        }
    }

    async function buildReviewModel(file, source, onStatus) {
        onStatus = onStatus || function () {};
        const review = source.review;
        if (!review) throw new Error('no review manifest for this platform');
        if (review.kind === 'json_sections') {
            onStatus('Reading your export in your browser…');
            const root = JSON.parse(await file.text());
            if (!isPlainDict(root)) throw new Error('export is not a JSON object');
            const model = buildJsonSectionsModel(root, review, file);
            if (!model.sections.length) {
                const err = new Error('no recognisable sections');
                err.code = 'no_members';
                throw err;
            }
            return model;
        }
        if (review.kind === 'zip_members') {
            return await buildZipMembersModel(file, review, onStatus);
        }
        throw new Error(`unknown review kind '${review.kind}'`);
    }

    // ------------------------------------------------------------------
    // Pruned artifact
    // ------------------------------------------------------------------

    function keptItems(sec) {
        // Original list items surviving the review, in order.
        const kept = [];
        for (let r = 0; r < sec.itemIdx.length; r++) {
            if (!sec.deleted.has(r)) kept.push(sec.listRef[sec.itemIdx[r]]);
        }
        return kept;
    }

    function keptCount(sec) {
        if (sec.toggleOnly) return sec.included ? (sec.opaqueCount || 0) : 0;
        return sec.included ? sec.rows.length - sec.deleted.size : 0;
    }

    async function buildPrunedFile(model) {
        if (model.kind === 'json_sections') {
            // Rebuild a minimal document holding ONLY the kept rows of the
            // whitelisted sections at their original key paths. Everything
            // else — unlisted sections, profile fields, settings scalars —
            // is absent from the upload by construction.
            const out = {};
            for (const sec of model.sections) {
                const kept = keptItems(sec);
                if (!kept.length) continue;
                let node = out;
                const path = sec.path;
                for (let i = 0; i < path.length - 1; i++) {
                    const k = path[i];
                    if (!isPlainDict(node[k])) node[k] = {};
                    node = node[k];
                }
                const leaf = path[path.length - 1];
                // Two lists can share a lowercased key at different paths; if
                // the exact path collides, append (extremely unlikely in real
                // exports, but never silently drop kept rows).
                if (Array.isArray(node[leaf])) node[leaf] = node[leaf].concat(kept);
                else node[leaf] = kept;
            }
            const json = JSON.stringify(out);
            return new File([json], model.file.name,
                { type: 'application/json', lastModified: model.file.lastModified });
        }

        // zip_members: fresh zip with rewritten member contents.
        const zipLib = model.zipLib;
        const writer = new zipLib.ZipWriter(new zipLib.BlobWriter('application/zip'));
        let added = 0;
        for (const sec of model.sections) {
            if (!sec.included) continue;
            if (sec.toggleOnly) {
                await writer.add(sec.memberFilename, new zipLib.BlobReader(sec.opaqueBlob));
                added++;
                continue;
            }
            if (sec.rows.length - sec.deleted.size === 0) continue;
            let content;
            if (sec.parser === 'csv') {
                const parts = sec.csvHeader ? [sec.csvText.slice(sec.csvHeader[0], sec.csvHeader[1])] : [];
                for (let r = 0; r < sec.csvRanges.length; r++) {
                    if (!sec.deleted.has(r)) {
                        parts.push(sec.csvText.slice(sec.csvRanges[r][0], sec.csvRanges[r][1]));
                    }
                }
                content = parts.join('');
            } else if (sec.parser === 'youtube_watch_json') {
                content = JSON.stringify(keptItems(sec));
            } else {  // instagram_records
                const kept = keptItems(sec);
                if (sec.single) {
                    content = JSON.stringify(kept[0]);
                } else if (sec.wrapKey) {
                    const wrapped = {};
                    for (const k of Object.keys(sec.payloadRef)) {
                        wrapped[k] = (k === sec.wrapKey) ? kept : sec.payloadRef[k];
                    }
                    content = JSON.stringify(wrapped);
                } else {
                    content = JSON.stringify(kept);
                }
            }
            await writer.add(sec.memberFilename, new zipLib.TextReader(content));
            added++;
        }
        const outBlob = await writer.close();
        if (!added) throw new Error('nothing left to upload');
        return new File([outBlob], model.file.name,
            { type: 'application/zip', lastModified: model.file.lastModified });
    }

    // ------------------------------------------------------------------
    // Viability: mirror the server's minimum-rows gate so the donor is
    // warned before upload instead of hitting the 422 backstop.
    // ------------------------------------------------------------------

    function viability(model) {
        const v = model.review.viability || {};
        if (v.section) {
            const kept = model.sections
                .filter(s => s.matchId === v.section || s.id === v.section)
                .reduce((n, s) => n + keptCount(s), 0);
            if (kept < (v.min_rows || 1)) return v.message || 'Too few items would remain.';
        } else if (v.min_total_rows) {
            const total = model.sections.reduce((n, s) => n + keptCount(s), 0);
            if (total < v.min_total_rows) return v.message || 'Too few items would remain.';
        }
        return null;
    }

    function totalKept(model) {
        return model.sections.reduce((n, s) => n + keptCount(s), 0);
    }

    // ------------------------------------------------------------------
    // Rendering
    // ------------------------------------------------------------------

    const CHUNK = 200;
    // Hard cap on rendered <tr>s per section: a 300K-row export must never be
    // able to scroll 300K rows into the DOM. Search and select-all always
    // operate on the full model, so nothing is hidden from the review itself.
    const RENDER_CAP = 2000;

    function visibleRowIdx(sec) {
        // Row indices surviving deletion and the search filter, in order.
        const out = [];
        const q = sec.filter.toLowerCase();
        for (let r = 0; r < sec.rows.length; r++) {
            if (sec.deleted.has(r)) continue;
            if (q && sec.rows[r].join(' ').toLowerCase().indexOf(q) === -1) continue;
            out.push(r);
        }
        return out;
    }

    function renderReview(container, model, callbacks) {
        // callbacks: {onChange, consentHtml}. onChange fires whenever counts,
        // viability, or consent change; consentHtml (when given) renders a
        // read-and-understood consent block whose checkbox gates sharing —
        // onChange reports it as consentOk.
        const state = { container, model, callbacks: callbacks || {}, consentOk: true };
        container.innerHTML = `
            <div class="rev-intro text-sm">Nothing has been uploaded yet. Everything below was read
                on your device — open a section to check it, and remove anything you don't want to share.</div>
            <div class="rev-sections"></div>`;
        const host = container.querySelector('.rev-sections');
        model.sections.forEach((sec, i) => {
            sec.open = (i === 0);
            const card = document.createElement('div');
            card.className = 'rev-card';
            host.appendChild(card);
            renderCard(card, sec, state);
        });
        if (model.stripped.length || model.strippedOther) host.appendChild(strippedCard(model));
        if (state.callbacks.consentHtml) renderConsent(container, state);
        notifyChange(state);
        return state;
    }

    function renderConsent(container, state) {
        state.consentOk = false;
        const div = document.createElement('div');
        div.className = 'rev-consent';
        div.innerHTML = `
            <div class="rev-consent-text text-sm" style="display: none;">${state.callbacks.consentHtml}</div>
            <label class="rev-consent-label text-sm">
                <input type="checkbox" class="rev-consent-check">
                <span>I have read and understood the
                    <button type="button" class="rev-consent-toggle">consent information</button>
                    and I want to share the items above with the research team.</span>
            </label>`;
        container.appendChild(div);
        const text = div.querySelector('.rev-consent-text');
        const toggle = div.querySelector('.rev-consent-toggle');
        toggle.onclick = () => {
            const open = text.style.display === 'none';
            text.style.display = open ? 'block' : 'none';
            if (open) text.scrollIntoView({ block: 'nearest' });
        };
        div.querySelector('.rev-consent-check').onchange = (e) => {
            state.consentOk = e.target.checked;
            notifyChange(state);
        };
    }

    function notifyChange(state) {
        if (state.callbacks.onChange) {
            state.callbacks.onChange({
                totalKept: totalKept(state.model),
                viabilityError: viability(state.model),
                consentOk: state.consentOk,
            });
        }
    }

    function countLine(sec) {
        const total = sec.toggleOnly ? (sec.opaqueCount || 0) : sec.rows.length;
        return `<strong>${keptCount(sec).toLocaleString()}</strong> of ${total.toLocaleString()} items will be shared`;
    }

    function strippedCard(model) {
        const div = document.createElement('div');
        div.className = 'rev-card rev-card-muted';
        const nItems = model.stripped.reduce((n, s) => n + s.count, 0);
        const detail = model.stripped.length
            ? ` — including ${model.stripped.length.toLocaleString()} section${model.stripped.length === 1 ? '' : 's'}` +
              ` with ${nItems.toLocaleString()} item${nItems === 1 ? '' : 's'}`
            : '';
        div.innerHTML = `
            <div class="rev-card-head"><span class="rev-title">Not included in your donation</span></div>
            <div class="rev-muted-body text-sm">
                Direct messages, profile details, settings and everything else the study
                does not use stay on your device${detail}. Only the sections above are uploaded.
            </div>`;
        return div;
    }

    function renderCard(card, sec, state) {
        if (sec.toggleOnly) return renderToggleCard(card, sec, state);
        const showSearch = sec.rows.length > 50;
        card.innerHTML = `
            <div class="rev-card-head">
                <button type="button" class="rev-disclose" aria-expanded="${sec.open}">
                    <span class="rev-chevron">${sec.open ? '▾' : '▸'}</span>
                    <span class="rev-title">${escapeHtml(sec.title)}</span>
                </button>
                <span class="rev-count text-sm">${countLine(sec)}</span>
                <span class="rev-actions">
                    ${sec.deleted.size ? `<button type="button" class="rev-chip rev-restore" title="Restore ${sec.deleted.size} removed rows">↺ ${sec.deleted.size} removed — restore</button>` : ''}
                </span>
            </div>
            <div class="rev-body" style="display: ${sec.open ? 'block' : 'none'};">
                ${showSearch ? '<input type="search" class="rev-search" placeholder="Search this section…">' : ''}
                <div class="rev-toolbar">
                    <button type="button" class="rev-chip rev-delete" disabled>Remove selected</button>
                </div>
                <div class="rev-table-wrap"></div>
            </div>`;

        const body = card.querySelector('.rev-body');
        card.querySelector('.rev-disclose').onclick = () => {
            sec.open = !sec.open;
            card.querySelector('.rev-chevron').textContent = sec.open ? '▾' : '▸';
            card.querySelector('.rev-disclose').setAttribute('aria-expanded', String(sec.open));
            body.style.display = sec.open ? 'block' : 'none';
            if (sec.open && !card.querySelector('.rev-table-wrap table')) renderTable(card, sec, state);
        };
        const restore = card.querySelector('.rev-restore');
        if (restore) restore.onclick = () => {
            sec.deleted.clear();
            renderCard(card, sec, state);
            notifyChange(state);
        };
        const search = card.querySelector('.rev-search');
        if (search) {
            search.value = sec.filter;
            search.oninput = () => {
                sec.filter = search.value;
                sec.selected.clear();
                renderTable(card, sec, state);
            };
        }
        card.querySelector('.rev-delete').onclick = () => {
            for (const r of sec.selected) sec.deleted.add(r);
            sec.selected.clear();
            renderCard(card, sec, state);
            notifyChange(state);
        };
        if (sec.open) renderTable(card, sec, state);
    }

    function renderToggleCard(card, sec, state) {
        card.innerHTML = `
            <div class="rev-card-head">
                <span class="rev-title" style="padding-left: 18px;">${escapeHtml(sec.title)}</span>
                <span class="rev-count text-sm">${countLine(sec)}</span>
                <span class="rev-actions">
                    <label class="rev-toggle text-sm">
                        <input type="checkbox" ${sec.included ? 'checked' : ''}> Include
                    </label>
                </span>
            </div>
            ${sec.note ? `<div class="rev-muted-body text-sm">${escapeHtml(sec.note)}</div>` : ''}`;
        card.querySelector('input[type="checkbox"]').onchange = (e) => {
            sec.included = e.target.checked;
            card.querySelector('.rev-count').innerHTML = countLine(sec);
            notifyChange(state);
        };
    }

    function renderTable(card, sec, state) {
        const wrap = card.querySelector('.rev-table-wrap');
        const visible = visibleRowIdx(sec);
        const renderMax = Math.min(visible.length, RENDER_CAP);
        sec.renderLimit = Math.min(CHUNK, renderMax);
        if (!visible.length) {
            wrap.innerHTML = '<div class="rev-empty text-sm">This table is empty.</div>';
            syncToolbar(card, sec);
            return;
        }
        const head = ['<th class="rev-check-col"><input type="checkbox" class="rev-select-all"></th>']
            .concat(sec.columns.map(c => `<th>${escapeHtml(String(c))}</th>`)).join('');
        wrap.innerHTML = `
            <table class="rev-table">
                <thead><tr>${head}</tr></thead>
                <tbody></tbody>
            </table>`;
        const tbody = wrap.querySelector('tbody');
        const capNote = () => {
            if (renderMax >= visible.length) return;
            tbody.insertAdjacentHTML('beforeend',
                `<tr class="rev-more-note"><td colspan="${sec.columns.length + 1}">` +
                `Showing the first ${renderMax.toLocaleString()} of ${visible.length.toLocaleString()} rows — ` +
                `use the search box to find specific items. Select-all still covers every row.</td></tr>`);
        };
        appendRows(tbody, sec, visible, 0, sec.renderLimit);
        if (sec.renderLimit >= renderMax) capNote();
        wrap.onscroll = () => {
            if (sec.renderLimit >= renderMax) return;
            if (wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 200) {
                const from = sec.renderLimit;
                sec.renderLimit = Math.min(sec.renderLimit + CHUNK, renderMax);
                appendRows(tbody, sec, visible, from, sec.renderLimit);
                if (sec.renderLimit >= renderMax) capNote();
            }
        };
        const all = wrap.querySelector('.rev-select-all');
        all.onchange = () => {
            // Select-all covers the whole filtered view, not just rendered rows.
            if (all.checked) visible.forEach(r => sec.selected.add(r));
            else sec.selected.clear();
            tbody.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                cb.checked = sec.selected.has(Number(cb.dataset.row));
            });
            syncToolbar(card, sec);
        };
        tbody.onchange = (e) => {
            const cb = e.target;
            if (!cb.matches('input[type="checkbox"]')) return;
            const r = Number(cb.dataset.row);
            if (cb.checked) sec.selected.add(r); else sec.selected.delete(r);
            syncToolbar(card, sec);
        };
        syncToolbar(card, sec);
    }

    function cellHtml(sec, r, j, v) {
        // The designated link column uses the per-row href; any other cell
        // that IS a URL links to itself. Always a new tab, never same-window.
        let href = null;
        if (j === sec.hrefCol && sec.hrefs[r]) href = sec.hrefs[r];
        else if (/^https?:\/\//.test(v)) href = v;
        const text = escapeHtml(v);
        if (!href || !/^https?:\/\//.test(href)) return `<td>${text}</td>`;
        return `<td><a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${text}</a></td>`;
    }

    function appendRows(tbody, sec, visible, from, to) {
        const parts = [];
        for (let i = from; i < to; i++) {
            const r = visible[i];
            const cells = sec.rows[r].map((v, j) => cellHtml(sec, r, j, String(v))).join('');
            parts.push(`<tr><td class="rev-check-col"><input type="checkbox" data-row="${r}" ${sec.selected.has(r) ? 'checked' : ''}></td>${cells}</tr>`);
        }
        tbody.insertAdjacentHTML('beforeend', parts.join(''));
    }

    function syncToolbar(card, sec) {
        const del = card.querySelector('.rev-delete');
        if (del) {
            del.disabled = sec.selected.size === 0;
            del.textContent = sec.selected.size
                ? `Remove selected (${sec.selected.size.toLocaleString()})` : 'Remove selected';
        }
        const all = card.querySelector('.rev-select-all');
        if (all) {
            const nVisible = visibleRowIdx(sec).length;
            all.checked = nVisible > 0 && sec.selected.size >= nVisible;
            all.indeterminate = sec.selected.size > 0 && sec.selected.size < nVisible;
        }
        card.querySelector('.rev-count').innerHTML = countLine(sec);
    }

    window.DonationReview = { buildReviewModel, buildPrunedFile, renderReview, totalKept, viability };
})();
