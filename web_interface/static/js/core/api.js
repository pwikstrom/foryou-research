/*
 * api.js — JSON helpers for the app's own /api endpoints.
 *
 * Both resolve to the parsed JSON body, or throw an Error carrying the
 * server's message (`error`, then `message`, then a joined `errors` list, then
 * the HTTP status text) plus `.status` and `.body`, so a caller can branch on
 * a 409 or read extra fields. The CSRF header is added by main.js's fetch
 * wrapper. Loaded in base.html <head> for app pages; public pages do not get it.
 */

async function _apiError(res) {
    const body = await res.json().catch(() => ({}));
    const err = new Error(
        body.error || body.message || (body.errors || []).join("; ") || res.statusText);
    err.status = res.status;
    err.body = body;
    return err;
}

// GET `url` and return its JSON body.
async function getJSON(url) {
    const res = await fetch(url);
    if (!res.ok) throw await _apiError(res);
    return res.json();
}

// Send `payload` as JSON (POST unless `method` says otherwise; no body when
// `payload` is undefined) and return the JSON reply, or {} when it has none.
async function postJSON(url, payload, method = "POST") {
    const res = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    if (!res.ok) throw await _apiError(res);
    return res.json().catch(() => ({}));
}
