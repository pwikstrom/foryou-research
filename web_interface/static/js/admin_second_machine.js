/**
 * Admin → Backends → "Second machine".
 *
 * The switch (admin setting `second_machine_annotates`) and, while it is on,
 * the annotation backlog's state and controls. Reads /api/manage/second-machine
 * and posts to /api/manage/second-machine/backlog. The lanes and what the
 * backlog is: web_interface/services/annotation_backlog.py.
 */
(function () {
    "use strict";

    const el = (id) => document.getElementById(id);
    const n = (v) => (typeof v === "number" ? v.toLocaleString() : "-");
    let _paused = false;

    function renderState(b) {
        const box = el("second-machine-state");
        if (!box) return;
        if (!b) {
            box.textContent = "";
            return;
        }
        _paused = !!b.paused;
        const lanes = Object.entries(b.lanes || {}).map(([k, v]) => `${k} ${n(v)}`).join(", ");
        const lines = [
            `Backlog: ${n(b.remaining)} videos left${b.paused ? " (PAUSED)" : ""}; `
                + `${n(b.claimed)} being annotated now; ${n(b.failed)} failed.`,
            `Lanes (taken in this order): ${lanes}`,
        ];
        const w = b.worker;
        if (!w) {
            lines.push("The second machine has never reported.");
        } else {
            const done = (w.ok || 0) + (w.fail || 0);
            lines.push(
                `${b.running ? "Running" : "NOT running"} on ${w.host || "?"} `
                    + `(${w.backend || "?"}, version ${w.annotation_version || "?"}).`
            );
            lines.push(
                `Since ${new Date(w.started_at).toLocaleString()}: ${n(done)} done, `
                    + `${n(w.fail || 0)} failed, ${n(w.dropped || 0)} no longer needed.`
            );
            if (w.seconds_per_video) {
                const days = (b.remaining * w.seconds_per_video) / 86400;
                lines.push(`${w.seconds_per_video} s per video; about ${days.toFixed(1)} day(s) for what is left.`);
            }
            lines.push(`Last heard ${new Date(w.heartbeat_at).toLocaleString()}.` + (w.note ? ` Note: ${w.note}` : ""));
        }
        box.textContent = lines.join("\n");
        const pause = el("second-machine-pause");
        if (pause) pause.textContent = _paused ? "Resume" : "Pause";
    }

    function fillSelect(select, values, keep) {
        if (!select) return;
        const current = keep ? select.value : "";
        select.replaceChildren(...values.map((v) => {
            const o = document.createElement("option");
            o.value = v.value;
            o.textContent = v.label;
            return o;
        }));
        if (current && values.some((v) => v.value === current)) select.value = current;
    }

    async function loadSecondMachine() {
        const cb = el("setting-second-machine");
        if (!cb) return;
        try {
            const data = await getJSON("/api/manage/second-machine");
            cb.checked = !!data.enabled;
            cb.disabled = false;
            el("second-machine-panel").style.display = data.enabled ? "block" : "none";
            if (!data.enabled) return;
            fillSelect(el("second-machine-study"),
                [{ value: "", label: "-- Select study --" }].concat((data.studies || []).map((s) => ({ value: s, label: s }))), true);
            fillSelect(el("second-machine-lane"), (data.lanes || []).map((l) => ({ value: l, label: l })), true);
            renderState(data.backlog);
        } catch (e) {
            console.error("loadSecondMachine:", e);
            el("second-machine-result").textContent = `Could not load: ${e.message}`;
        }
    }

    async function saveSecondMachineSetting(cb) {
        const status = el("setting-second-machine-status");
        const desired = cb.checked;
        cb.disabled = true;
        if (status) status.textContent = "Saving…";
        try {
            await postJSON("/api/admin/settings", { second_machine_annotates: desired }, "PUT");
            if (status) status.textContent = "Saved";
            setTimeout(() => { if (status && status.textContent === "Saved") status.textContent = ""; }, 2000);
        } catch (e) {
            console.error("saveSecondMachineSetting:", e);
            cb.checked = !desired;
            if (status) status.textContent = "Failed, reverted";
        }
        await loadSecondMachine();
    }

    async function secondMachineAction(action, btn) {
        const result = el("second-machine-result");
        const lane = el("second-machine-lane").value;
        const body = { action };
        if (action === "pause" && _paused) body.action = "resume";
        if (action === "add" || action === "remove") {
            body.study = el("second-machine-study").value;
            body.collection = el("second-machine-collection").value.trim();
            if (!!body.study === !!body.collection) {
                result.textContent = "Choose a study or type a collection id (one of the two).";
                return;
            }
            if (action === "add") {
                body.lane = lane;
                body.front = el("second-machine-front").checked;
            }
        }
        if (action === "empty") {
            body.lane = lane;
            const ok = await showAppConfirm(
                `Empty the ${lane} lane of the backlog? Videos already annotated are not affected.`,
                { title: "Empty lane", okLabel: "Empty", danger: true }
            );
            if (!ok) return;
        }
        if (btn) btn.disabled = true;
        result.textContent = "Working…";
        try {
            const data = await postJSON("/api/manage/second-machine/backlog", body);
            result.textContent = data.message || "Done.";
            renderState(data.backlog);
        } catch (e) {
            result.textContent = `Failed: ${(e.body && e.body.error) || e.message}`;
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    window.loadSecondMachine = loadSecondMachine;
    window.saveSecondMachineSetting = saveSecondMachineSetting;
    window.secondMachineAction = secondMachineAction;

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", loadSecondMachine);
    } else {
        loadSecondMachine();
    }
})();
