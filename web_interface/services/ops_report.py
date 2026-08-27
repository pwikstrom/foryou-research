"""Daily admin ops report: collect prod state, write prose, render, email.

The report is a colour-coded status board (green = healthy, yellow = watch,
red = action needed, blue = informational activity) over the same state the
admin panel exposes piecemeal: accounts and logins, worker runs, the
task-failure ledger, queues and pending consolidation/ingest, collections,
scraper cookies and alerts, the structure sentinel, Cloud Run error logs, the
public site, and yt-dlp release drift. A Gemini call turns the collected
checks into a short written assessment (with a deterministic fallback when
Gemini is not configured).

Artifacts land in the cache storage location under ``ops_report/``:
``latest.html`` / ``latest.json`` (served by the Admin → System pane),
dated ``report_<YYYY-MM-DD>.html`` copies, and ``state.json`` (the snapshot
that powers day-over-day diffs). The rendered HTML is finally emailed to
[site].ops_report_email (default: the [site].mail_sender address).

Everything here is read-only against the datasets; the only writes are the
report's own files under ``cache/ops_report/``.
"""

import html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

STATUS_RANK = {"green": 0, "blue": 1, "yellow": 2, "red": 3}
STATUS_LABEL = {"green": "OK", "blue": "Info", "yellow": "Watch", "red": "Action"}
SESSION_COOKIE = {"tiktok": "sessionid", "instagram": "sessionid",
                  "youtube": "__Secure-3PSID"}
REPORT_DIR = "ops_report"
KEEP_DATED_REPORTS = 60


def _now():
    return datetime.now(timezone.utc)


def _local_tz():
    from zoneinfo import ZoneInfo
    from fyp.core.fyp_config import fyp_cf
    try:
        return ZoneInfo(fyp_cf.get("misc", {}).get("TIME_ZONE", "UTC"))
    except Exception:
        return timezone.utc


def _parse_iso(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _local(dt, tz):
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M") if dt else "never"


def _ago(dt, now):
    if dt is None:
        return "never"
    hours = (now - dt).total_seconds() / 3600
    if hours < 1:
        return f"{int(hours * 60)}m ago"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"


# --------------------------------------------------------------- collection

def collect_status(hours_back: int = 24) -> dict:
    """Gather every check into a status document. Never raises: a source that
    cannot be read becomes a red check naming the failure instead."""
    import fyp.data_io as data_io

    now = _now()
    tz = _local_tz()
    day_ago = now - timedelta(hours=hours_back)
    week_ago = now - timedelta(days=7)
    epoch = datetime.min.replace(tzinfo=timezone.utc)

    doc = {
        "generated_at": now.isoformat(),
        "generated_at_local": now.astimezone(tz).strftime("%A %d %B %Y, %H:%M %Z"),
        "stats": [],
        "sections": [],
    }
    state = data_io.load_json(storage_location="cache",
                              filename=f"{REPORT_DIR}/state.json") or {}
    prev_users = set(state.get("users", []))
    prev_collections = set(state.get("collections", []))
    prev_queues = state.get("queues", {})
    prev_run = _parse_iso(state.get("run_at"))
    doc["previous_run_at"] = _local(prev_run, tz) if prev_run else None

    def section(title):
        s = {"title": title, "checks": []}
        doc["sections"].append(s)
        return s

    def check(sec, title, status, summary, details=None):
        sec["checks"].append({"title": title, "status": status,
                              "summary": summary, "details": details or []})

    def stat(label, value, status, sub=""):
        doc["stats"].append({"label": label, "value": value,
                             "status": status, "sub": sub})

    # ---- users & access -------------------------------------------------
    sec = section("Users & access")
    usernames = []
    try:
        from web_interface.security import user_manager
        all_users = user_manager.get_all_users()
        real = [u for u in all_users.values() if not getattr(u, "placeholder", False)]
        usernames = sorted(all_users.keys())
        new_users = [u for u in real
                     if (_parse_iso(u.created_at) or epoch) > day_ago
                     or (prev_users and u.username not in prev_users)]
        pending = [u for u in real if not u.approved]
        logins_24h = sorted(
            [u for u in real if (_parse_iso(u.last_login) or epoch) > day_ago],
            key=lambda u: str(u.last_login), reverse=True)
        logins_7d = [u for u in real if (_parse_iso(u.last_login) or epoch) > week_ago]
        n_part = sum(1 for u in real if getattr(u, "account_kind", "") == "participant")
        check(sec, "Accounts", "blue",
              f"{len(real)} real accounts ({n_part} participants), "
              f"{len(all_users) - len(real)} placeholders")
        if new_users:
            check(sec, "New registrations", "blue",
                  f"{len(new_users)} new since last report",
                  [f"{u.username} — role {u.role}, origin "
                   f"{(getattr(u, 'origin', None) or {}).get('source', '?')}, "
                   f"created {_local(_parse_iso(u.created_at), tz)}, approved={u.approved}"
                   for u in new_users])
        else:
            check(sec, "New registrations", "green", "None since last report")
        if pending:
            check(sec, "Pending approval", "yellow",
                  f"{len(pending)} account(s) awaiting approval",
                  [u.username for u in pending])
        else:
            check(sec, "Pending approval", "green", "No accounts waiting")
        check(sec, "Logins", "blue",
              f"{len(logins_24h)} in last 24h, {len(logins_7d)} in last 7d",
              [f"{u.username} ({u.role}) — {_ago(_parse_iso(u.last_login), now)}"
               for u in logins_24h])
    except Exception as e:
        check(sec, "User store", "red", f"Could not read user accounts: {e}")

    try:
        acts = []
        for name in data_io.listdir(storage_location="users"):
            if not name.endswith("_log.json"):
                continue
            entries = (data_io.load_json(storage_location="users", filename=name)
                       or {}).get("entries", [])
            who = name[:-len("_log.json")]
            for e in entries[-40:]:
                ts = _parse_iso(e.get("timestamp"))
                if ts and ts > day_ago:
                    acts.append(f"{who}: {e.get('action')} → {e.get('target')} "
                                f"({_local(ts, tz)})")
        if acts:
            check(sec, "Admin/user actions (24h)", "blue",
                  f"{len(acts)} logged action(s)", acts)
        else:
            check(sec, "Admin/user actions (24h)", "green", "No logged admin actions")
    except Exception as e:
        check(sec, "Activity logs", "red", f"Could not read activity logs: {e}")

    # ---- workers --------------------------------------------------------
    sec = section("Workers & processes")
    stats_doc = {}
    try:
        from web_interface.process_manager import load_process_stats, process_stats
        load_process_stats()
        stats_doc = dict(process_stats)
        recent, failed_last = [], []
        for key, s in stats_doc.items():
            if not isinstance(s, dict):
                continue
            end = _parse_iso(s.get("last_run_end_time"))
            if end and end > day_ago:
                recent.append((end, key, s))
            if s.get("last_run_outcome") and s["last_run_outcome"] != "Success" \
                    and end and end > week_ago:
                failed_last.append((key, s))
        if recent:
            bad = [k for _, k, s in recent if s.get("last_run_outcome") != "Success"]
            check(sec, "Runs in last 24h", "red" if bad else "blue",
                  f"{len(recent)} worker run(s)"
                  + (f", {len(bad)} failed" if bad else ", all succeeded"),
                  [f"{k}: {s.get('last_run_outcome')} at {_local(end, tz)} "
                   f"({(s.get('last_run_duration') or 0):.0f}s)"
                   for end, k, s in sorted(recent, reverse=True)])
        else:
            check(sec, "Runs in last 24h", "blue", "No worker runs recorded")
        if failed_last:
            check(sec, "Workers whose last run failed", "red",
                  f"{len(failed_last)} worker(s) sitting on a failure",
                  [f"{k}: {s.get('last_run_outcome')} at "
                   f"{_local(_parse_iso(s.get('last_run_end_time')), tz)}"
                   for k, s in failed_last])
        else:
            check(sec, "Workers whose last run failed", "green",
                  "Every worker's last run succeeded")

        stuck, recent_bad_runs = [], []
        for name in data_io.listdir(storage_location="cache"):
            if not name.startswith("proc_logs/") or not name.endswith(".json"):
                continue
            pdoc = data_io.load_json(storage_location="cache", filename=name) or {}
            for run in (pdoc.get("runs") or []):
                started = _parse_iso(run.get("started_at"))
                ended = _parse_iso(run.get("ended_at"))
                if run.get("state") == "running" and started \
                        and started < now - timedelta(hours=6):
                    stuck.append(f"{pdoc.get('key')} — started {_local(started, tz)} "
                                 f"by {run.get('started_by')}")
                if run.get("state") in ("failed", "interrupted", "cancelled") \
                        and (ended or started) and (ended or started) > day_ago:
                    tail = " | ".join((run.get("lines") or [])[-2:])
                    recent_bad_runs.append(
                        f"{pdoc.get('key')} {run.get('state')} "
                        f"(started {_local(started, tz)}) {tail[:180]}")
        if stuck:
            check(sec, "Stuck 'running' states", "yellow",
                  f"{len(stuck)} run(s) marked running >6h — likely an orphaned "
                  "status from a dead container", stuck)
        else:
            check(sec, "Stuck 'running' states", "green", "No orphaned running states")
        if recent_bad_runs:
            check(sec, "Failed/interrupted runs (24h)", "red",
                  f"{len(recent_bad_runs)} run(s)", recent_bad_runs)
        else:
            check(sec, "Failed/interrupted runs (24h)", "green", "None")
    except Exception as e:
        check(sec, "Process logs", "red", f"Could not read worker state: {e}")

    try:
        from web_interface import task_failures
        unack = task_failures.unacknowledged_dead(within_hours=48)
        if unack:
            check(sec, "Task failures (dead-letter)", "red",
                  f"{len(unack)} unacknowledged dead task(s) in 48h",
                  [f"{f.get('task')} at {_local(_parse_iso(f.get('ts')), tz)}: "
                   f"{(f.get('error') or '').strip().splitlines()[0][:160]}"
                   for f in unack])
        else:
            check(sec, "Task failures (dead-letter)", "green", "None in the last 48h")
    except Exception as e:
        check(sec, "Task failures", "red", f"Could not read ledger: {e}")

    # ---- pipeline & queues ---------------------------------------------
    sec = section("Pipeline & queues")
    queue_now = {}
    try:
        from fyp.scrape import scrape_queues
        for platform, length in sorted(scrape_queues.queue_lengths().items()):
            queue_now[f"scrape_{platform}"] = length
            delta = length - prev_queues.get(f"scrape_{platform}", length)
            stat(f"Scrape queue · {platform}", length,
                 "blue" if length else "green",
                 (f"{delta:+d} since last report" if delta else "unchanged")
                 if prev_queues else "")
        ann = data_io.load_json(storage_location="cache",
                                filename="to_annotate.json") or []
        queue_now["annotate"] = len(ann)
        delta = len(ann) - prev_queues.get("annotate", len(ann))
        stat("Annotation queue", len(ann), "blue" if ann else "green",
             (f"{delta:+d} since last report" if delta else "unchanged")
             if prev_queues else "")
        batch = data_io.load_json(storage_location="cache",
                                  filename="annotate_batch_job.json")
        if batch:
            check(sec, "Annotation batch job", "blue", "Batch job in flight",
                  [json.dumps(batch)[:300]])
        else:
            check(sec, "Annotation batch job", "green", "No batch job in flight")
        grew = [k for k, v in queue_now.items()
                if prev_queues and v > prev_queues.get(k, 0) + 100]
        if grew:
            check(sec, "Queue growth", "yellow",
                  "Queue(s) grew by >100 since last report: " + ", ".join(grew))
        else:
            check(sec, "Queue growth", "green", "No unusual queue growth")

        day_stamp_min = (now - timedelta(hours=hours_back)).strftime("%Y%m%d%H%M%S")
        fails = [n for n in data_io.listdir(storage_location="scrape")
                 if n.startswith("scrape_failed_items_")
                 and n[len("scrape_failed_items_"):len("scrape_failed_items_") + 14]
                 >= day_stamp_min]
        if fails:
            check(sec, "New scrape-failure files (24h)", "yellow",
                  f"{len(fails)} file(s)", fails)
        else:
            check(sec, "New scrape-failure files (24h)", "green", "None")
    except Exception as e:
        check(sec, "Queues", "red", f"Could not read queues: {e}")

    try:
        cons = stats_doc.get("consolidate_enrichment") or {}
        last_cons = _parse_iso(cons.get("last_success"))
        pending_files = 0
        for name in data_io.listdir(storage_location="machine_annotations_raw"):
            mtime = data_io.getmtime(storage_location="machine_annotations_raw",
                                     filename=name)
            if mtime is None:
                continue
            mdt = datetime.fromtimestamp(mtime, tz=timezone.utc)
            if last_cons is None or mdt > last_cons:
                pending_files += 1
        level = ("green" if pending_files == 0
                 else ("blue" if pending_files < 50 else "yellow"))
        stat("Pending consolidation", pending_files, level,
             f"last consolidation {_ago(last_cons, now)}")
        check(sec, "Enrichment consolidation", level,
              f"{pending_files} raw annotation file(s) newer than the last "
              f"consolidation ({_local(last_cons, tz)}, {_ago(last_cons, now)})")
    except Exception as e:
        check(sec, "Consolidation", "red", f"Could not compute: {e}")

    try:
        pending_uploads = []
        for loc in ("zeeschuimer_raw", "ddp_raw", "aio_raw",
                    "instagram_raw", "youtube_raw"):
            try:
                manifest = data_io.load_json(storage_location=loc,
                                             filename="ingestion_manifest.json") or {}
            except Exception:
                manifest = {}
            for fn in manifest:
                pending_uploads.append(f"{loc}: {fn}")
        stat("Pending ingest", len(pending_uploads),
             "green" if not pending_uploads else "yellow",
             "uploads not yet ingested")
        if pending_uploads:
            check(sec, "Uploads awaiting ingest", "yellow",
                  f"{len(pending_uploads)} file(s) uploaded but not ingested",
                  pending_uploads)
        else:
            check(sec, "Uploads awaiting ingest", "green", "None")
    except Exception as e:
        check(sec, "Ingest manifests", "red", f"Could not read: {e}")

    # ---- collections ----------------------------------------------------
    sec = section("Collections & ingest")
    current_collections = set()
    try:
        from web_interface.services.study_data import get_collection_tags
        tags = get_collection_tags(force_reload=True) or {}
        current_collections = set(tags.keys())
        added = sorted(current_collections - prev_collections) if prev_collections else []
        removed = sorted(prev_collections - current_collections) if prev_collections else []
        check(sec, "Collections", "blue", f"{len(current_collections)} total")
        if added:
            check(sec, "New collections", "blue",
                  f"{len(added)} added since last report",
                  [c + (f" (owner {tags[c].get('user_id')})"
                        if isinstance(tags.get(c), dict) and tags[c].get("user_id")
                        else "") for c in added])
        else:
            check(sec, "New collections", "green", "None since last report")
        if removed:
            check(sec, "Removed collections", "yellow",
                  f"{len(removed)} removed since last report", removed)
        meta_mtime = data_io.getmtime(storage_location="recoded",
                                      filename="collections_metadata.parquet")
        meta_dt = (datetime.fromtimestamp(meta_mtime, tz=timezone.utc)
                   if meta_mtime else None)
        check(sec, "Last ingest/metadata write", "blue",
              f"{_local(meta_dt, tz)} ({_ago(meta_dt, now)})")
    except Exception as e:
        check(sec, "Collections", "red", f"Could not read: {e}")

    try:
        from fyp.core.structure_sentinel import load_verdicts
        verdicts = load_verdicts() or {}
        files = verdicts.get("files", verdicts) or {}
        bad = {k: v.get("status") for k, v in files.items()
               if isinstance(v, dict)
               and v.get("status") not in (None, "ok", "learning")}
        if bad:
            check(sec, "Structure sentinel", "red",
                  "Warn/quarantine verdicts present",
                  [f"{k}: {s}" for k, s in bad.items()])
        else:
            check(sec, "Structure sentinel", "green", "No warn/quarantine verdicts")
    except Exception as e:
        check(sec, "Structure sentinel", "red", f"Could not read: {e}")

    # ---- scraping health ------------------------------------------------
    sec = section("Scraping health")
    try:
        from fyp.scrape import scraper_alerts
        alerts = scraper_alerts.load_alerts() or {}
        if alerts:
            check(sec, "Persistent scraper alerts", "red",
                  f"{len(alerts)} platform(s) flagged",
                  [f"{p}: {a.get('kind')} — {a.get('message')} "
                   f"(raised {_local(_parse_iso(a.get('raised_at')), tz)}, "
                   f"×{a.get('occurrences')})" for p, a in alerts.items()])
        else:
            check(sec, "Persistent scraper alerts", "green", "None")
    except Exception as e:
        check(sec, "Scraper alerts", "red", f"Could not read: {e}")

    for platform, cookie_name in SESSION_COOKIE.items():
        try:
            from fyp.scrape.scraper_cookies import cookie_health
            h = cookie_health(platform, session_cookie=cookie_name) or {}
            status = h.get("status")
            msg = h.get("message") or status or "unknown"
            level = {"missing": "red", "expired": "red", "expiring_soon": "yellow",
                     "stale": "yellow", "healthy": "green"}.get(status, "yellow")
            days_left = h.get("session_days_left")
            # A stale *file* whose session cookie is still valid for weeks is
            # informational, not actionable — don't wake anyone up for it.
            if status == "stale" and isinstance(days_left, (int, float)) \
                    and days_left > 30:
                level = "green"
                msg = (f"File {h.get('file_age_days', 0):.0f}d old but session "
                       f"valid {days_left:.0f} more days")
            check(sec, f"Cookie · {platform}", level, msg)
        except Exception as e:
            check(sec, f"Cookie · {platform}", "red", f"Could not check: {e}")

    # ---- platform & infrastructure -------------------------------------
    sec = section("Platform & infrastructure")
    try:
        errors_by_service, fivexx = _cloud_run_log_summary(hours_back)
        for svc, lines in errors_by_service.items():
            if lines:
                check(sec, f"ERROR logs (24h) · {svc}", "yellow",
                      f"{len(lines)} ERROR-severity entr(ies) — review",
                      [ln[:220] for ln in lines[:8]])
            else:
                check(sec, f"ERROR logs (24h) · {svc}", "green",
                      "Zero ERROR-severity entries")
        if fivexx:
            check(sec, "HTTP 5xx responses (24h)", "red",
                  f"{len(fivexx)} failed request(s)", [ln[:220] for ln in fivexx[:8]])
        else:
            check(sec, "HTTP 5xx responses (24h)", "green",
                  "Zero 5xx responses, both services")
    except Exception as e:
        check(sec, "Cloud Run logs", "yellow", f"Could not query Cloud Logging: {e}")

    try:
        import requests
        from web_interface.mail_utils import _site
        app_url = str(_site().get("app_url", "") or "").strip()
        if app_url:
            t0 = time.time()
            resp = requests.get(app_url, timeout=45)
            dt = time.time() - t0
            if resp.status_code == 200:
                check(sec, "Public site", "green",
                      f"{app_url} → 200 in {dt:.2f}s")
            else:
                check(sec, "Public site", "red", f"{app_url} → {resp.status_code}")
        else:
            check(sec, "Public site", "blue", "No [site].app_url configured")
    except Exception as e:
        check(sec, "Public site", "red", f"UNREACHABLE: {e}")

    try:
        import requests
        from importlib.metadata import version as pkg_version
        installed = pkg_version("yt-dlp")
        latest = requests.get(
            "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
            timeout=30).json()
        tag = latest.get("tag_name", "?")

        def norm(v):
            return [int(x) for x in re.findall(r"\d+", v or "")]

        if norm(tag) > norm(installed):
            check(sec, "yt-dlp", "yellow",
                  f"Update available: running {installed}, latest {tag} "
                  f"(released {str(latest.get('published_at', '?'))[:10]})")
        else:
            check(sec, "yt-dlp", "green",
                  f"Running {installed} = latest release {tag}")
    except Exception as e:
        check(sec, "yt-dlp", "yellow", f"Could not check releases: {e}")

    # ---- overall --------------------------------------------------------
    worst = "green"
    counts = {"green": 0, "blue": 0, "yellow": 0, "red": 0}
    for s in doc["sections"]:
        for c in s["checks"]:
            counts[c["status"]] = counts.get(c["status"], 0) + 1
            if STATUS_RANK[c["status"]] > STATUS_RANK[worst]:
                worst = c["status"]
    doc["overall"] = worst
    doc["counts"] = counts
    doc["_new_state"] = {
        "run_at": now.isoformat(),
        "users": usernames or state.get("users", []),
        "collections": sorted(current_collections) or state.get("collections", []),
        "queues": queue_now or state.get("queues", {}),
    }
    return doc


def _cloud_run_log_summary(hours_back: int):
    """Query Cloud Logging for ERROR-severity entries per service and 5xx
    requests, using the runtime service account. Raises on failure (the caller
    turns that into a yellow check)."""
    import os

    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    project = os.environ.get("GCP_PROJECT_ID")
    if not project or not os.environ.get("K_SERVICE"):
        return {}, []
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/logging.read"])
    session = AuthorizedSession(creds)
    cutoff = (_now() - timedelta(hours=hours_back)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def query(filter_str, limit=50):
        resp = session.post(
            "https://logging.googleapis.com/v2/entries:list",
            json={"resourceNames": [f"projects/{project}"],
                  "filter": filter_str, "orderBy": "timestamp desc",
                  "pageSize": limit},
            timeout=60)
        resp.raise_for_status()
        return resp.json().get("entries", [])

    errors_by_service = {}
    for svc in ("fyp-data-hub", "fyp-task-runner"):
        entries = query(
            f'resource.type="cloud_run_revision" '
            f'AND resource.labels.service_name="{svc}" '
            f'AND severity>=ERROR AND timestamp>="{cutoff}"')
        errors_by_service[svc] = [
            f"{e.get('timestamp', '')} {e.get('textPayload') or json.dumps(e.get('jsonPayload', {}))[:160]}"
            for e in entries]
    fivexx_entries = query(
        f'resource.type="cloud_run_revision" AND httpRequest.status>=500 '
        f'AND timestamp>="{cutoff}"')
    fivexx = [f"{e.get('timestamp', '')} {e.get('httpRequest', {}).get('status')} "
              f"{e.get('httpRequest', {}).get('requestUrl', '')}"
              for e in fivexx_entries]
    return errors_by_service, fivexx


# --------------------------------------------------------------- narrative

_NARRATIVE_INSTRUCTIONS = """You are writing the morning admin assessment for
"The For You Data Hub", a research data platform. You are given today's status
document as JSON: every check has a status light (green = checked and healthy,
yellow = watch, red = action needed, blue = informational activity).

Write a short markdown assessment for the administrator, in exactly this
structure:

## Action needed
What the administrator should actually do today, based on red (and any
serious yellow) checks. If nothing requires action, say exactly that in one
sentence.

## What the yellows/reds mean
One bullet per yellow or red check, explaining in plain language what it
means and whether it matters. Known context: a run marked "running" for days
in the process logs is usually an orphaned status from a dead container, not
live work; a lone "Uncaught signal: 6" Cloud Run entry with zero 5xx
responses around it is an instance being drained during a deploy, not a
crash under load. Omit this whole section if there are no yellow or red
checks.

## What's fine
A compact prose readout of the healthy side: users/logins/activity, worker
runs, queues and consolidation, cookies, site, dependencies. Complete
sentences, no padding.

Rules: write only the markdown, no preamble. Use ## headings, bullets,
**bold** and `code` only. Never invent facts not present in the JSON. Keep
it under 350 words."""


def build_narrative(doc: dict) -> tuple[str, str]:
    """Return (narrative_markdown, source) where source is 'gemini' or
    'fallback'."""
    compact = {
        "generated_at_local": doc.get("generated_at_local"),
        "overall": doc.get("overall"),
        "counts": doc.get("counts"),
        "stats": doc.get("stats"),
        "sections": [
            {"title": s["title"],
             "checks": [{"title": c["title"], "status": c["status"],
                         "summary": c["summary"],
                         "details": c["details"][:6]}
                        for c in s["checks"]]}
            for s in doc.get("sections", [])],
    }
    try:
        from fyp.core import gemini_client
        from fyp.core.fyp_config import fyp_cf
        mode, _reason = gemini_client.gemini_mode()
        if mode is None:
            raise RuntimeError("Gemini not configured")
        client = gemini_client.make_client(
            location=fyp_cf["machine"]["gemini"].get("location"))
        model = fyp_cf["machine"]["gemini"]["model"]
        prompt = (_NARRATIVE_INSTRUCTIONS + "\n\nStatus document:\n```json\n"
                  + json.dumps(compact, default=str) + "\n```")
        resp = client.models.generate_content(model=model, contents=prompt)
        text = (resp.text or "").strip()
        if text:
            return text, "gemini"
        raise RuntimeError("empty Gemini response")
    except Exception as e:
        logger.warning(f"ops_report: Gemini narrative unavailable ({e}); "
                       "using deterministic fallback")
        return _fallback_narrative(doc), "fallback"


def _fallback_narrative(doc: dict) -> str:
    flagged = [(s["title"], c) for s in doc.get("sections", [])
               for c in s["checks"] if c["status"] in ("yellow", "red")]
    lines = ["## Action needed"]
    reds = [c for _, c in flagged if c["status"] == "red"]
    if reds:
        for c in reds:
            lines.append(f"- **{c['title']}** — {c['summary']}")
    else:
        lines.append("Nothing needs your attention today — no red checks.")
    if flagged:
        lines.append("")
        lines.append("## What the yellows/reds mean")
        for title, c in flagged:
            lines.append(f"- **{c['title']}** ({title}): {c['summary']}")
    counts = doc.get("counts", {})
    lines += ["", "## What's fine",
              f"{counts.get('green', 0)} checks are green and "
              f"{counts.get('blue', 0)} informational; see the board below for "
              "the full detail. (This summary was generated without Gemini — "
              "the model was unavailable or not configured.)"]
    return "\n".join(lines)


# ------------------------------------------------------------------ render

def _esc(s):
    return html.escape(str(s), quote=True)


def _inline_md(s):
    s = _esc(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    return s


def _md_to_html(md: str) -> str:
    out, in_list = [], False
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        if line.startswith("### "):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<h4>{_inline_md(line[4:])}</h4>")
        elif line.startswith("## "):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<h3>{_inline_md(line[3:])}</h3>")
        elif line.lstrip().startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline_md(line.lstrip()[2:])}</li>")
        else:
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<p>{_inline_md(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


_PAGE_CSS = """
:root {
  --bg: #f2f5f6; --surface: #ffffff; --ink: #1c2427; --ink-dim: #5b6a70;
  --line: #dde4e6; --code-bg: #eaeff1;
  --green: #2f9e63; --yellow: #b97d0a; --red: #d64550; --blue: #3f7fd6;
  --green-soft: #e2f2e9; --yellow-soft: #f7edd6; --red-soft: #f9e3e5; --blue-soft: #e3edf9;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #141a1d; --surface: #1c2427; --ink: #e8edee; --ink-dim: #94a4aa;
    --line: #2c363b; --code-bg: #253035;
    --green: #4cc385; --yellow: #e6b04a; --red: #ec6a74; --blue: #6ba3e8;
    --green-soft: #1e3529; --yellow-soft: #3a2f18; --red-soft: #3d2226; --blue-soft: #1f2f44;
  }
}
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--ink); margin: 0;
  font-family: "IBM Plex Sans", "Helvetica Neue", Arial, sans-serif;
  font-size: 15px; line-height: 1.5; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 24px 20px 64px; }
code { font-family: "IBM Plex Mono", ui-monospace, Menlo, monospace;
  font-size: 0.88em; background: var(--code-bg); padding: 1px 5px; border-radius: 4px; }
.masthead { display: flex; flex-wrap: wrap; align-items: center; gap: 14px 22px;
  padding-bottom: 18px; border-bottom: 2px solid var(--line); margin-bottom: 22px; }
.masthead h1 { font-size: 21px; font-weight: 700; letter-spacing: -0.01em; margin: 0;
  display: flex; align-items: center; gap: 12px; }
.overall { display: inline-flex; align-items: center; gap: 8px; font-weight: 600;
  font-size: 14px; padding: 4px 12px 4px 8px; border-radius: 999px; }
.overall-green { background: var(--green-soft); color: var(--green); }
.overall-blue { background: var(--blue-soft); color: var(--blue); }
.overall-yellow { background: var(--yellow-soft); color: var(--yellow); }
.overall-red { background: var(--red-soft); color: var(--red); }
.meta { margin-left: auto; text-align: right;
  font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 12.5px;
  color: var(--ink-dim); display: flex; flex-direction: column; gap: 2px; }
.tallies { display: flex; gap: 14px; align-items: center; }
.tally { display: inline-flex; align-items: center; gap: 6px;
  font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 13px;
  font-variant-numeric: tabular-nums; color: var(--ink-dim); }
.lamp { width: 11px; height: 11px; border-radius: 50%; flex: none;
  display: inline-block; margin-top: 1px; }
.lamp-big { width: 14px; height: 14px; }
.lamp-green  { background: var(--green);  box-shadow: 0 0 0 3px var(--green-soft); }
.lamp-yellow { background: var(--yellow); box-shadow: 0 0 0 3px var(--yellow-soft); }
.lamp-red    { background: var(--red);    box-shadow: 0 0 0 3px var(--red-soft); }
.lamp-blue   { background: var(--blue);   box-shadow: 0 0 0 3px var(--blue-soft); }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px; margin-bottom: 22px; }
.stat { background: var(--surface); border: 1px solid var(--line);
  border-radius: 10px; padding: 12px 14px; }
.stat-top { display: flex; align-items: center; gap: 8px; }
.stat-label { font-size: 11.5px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--ink-dim); }
.stat-value { font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-weight: 500; font-size: 30px; line-height: 1.2; margin-top: 4px;
  font-variant-numeric: tabular-nums; }
.stat-sub { font-size: 12px; color: var(--ink-dim); margin-top: 2px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 14px; align-items: start; }
.card { background: var(--surface); border: 1px solid var(--line);
  border-radius: 12px; padding: 16px 18px; }
.card-head { display: flex; align-items: center; gap: 10px;
  padding-bottom: 10px; margin-bottom: 4px; border-bottom: 1px solid var(--line); }
.card-head h2 { font-size: 15px; font-weight: 600; margin: 0; letter-spacing: 0.01em; }
.check { display: flex; gap: 10px; padding: 9px 0;
  border-bottom: 1px solid var(--line); align-items: flex-start; }
.check:last-child { border-bottom: none; }
.check .lamp { margin-top: 5px; }
.check-body { min-width: 0; flex: 1; }
.check-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.check-title { font-weight: 600; font-size: 13.5px; }
.tag { font-size: 10px; font-weight: 700; letter-spacing: 0.08em;
  text-transform: uppercase; padding: 1px 7px; border-radius: 999px; }
.tag-green  { background: var(--green-soft);  color: var(--green); }
.tag-yellow { background: var(--yellow-soft); color: var(--yellow); }
.tag-red    { background: var(--red-soft);    color: var(--red); }
.tag-blue   { background: var(--blue-soft);   color: var(--blue); }
.check-summary { font-size: 13.5px; color: var(--ink); margin-top: 1px; }
details { margin-top: 4px; }
summary { cursor: pointer; font-size: 12px; color: var(--ink-dim); }
details ul { margin: 6px 0 2px; padding-left: 18px; }
details li { font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 11.5px; color: var(--ink-dim); margin: 3px 0; overflow-wrap: anywhere; }
.narrative { margin-bottom: 22px; }
.narrative h3 { font-size: 14px; margin: 14px 0 6px; }
.narrative h4 { font-size: 13px; margin: 12px 0 4px; }
.narrative p { margin: 6px 0; max-width: 72ch; }
.narrative ul { margin: 6px 0; padding-left: 20px; max-width: 72ch; }
.narrative li { margin: 4px 0; }
.legend { display: flex; gap: 18px; flex-wrap: wrap; margin: 26px 0 0;
  font-size: 12px; color: var(--ink-dim); align-items: center; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.meta-dim { opacity: 0.75; }
@media (max-width: 640px) {
  .meta { margin-left: 0; text-align: left; }
  .stat-value { font-size: 24px; }
}
"""


def render_html(doc: dict, narrative_md: str) -> str:
    """Render the status document + narrative into a complete standalone HTML
    page (used verbatim for the emailed report and the admin pane iframe).
    Every dynamic value passes through HTML escaping."""

    def lamp(status, big=False):
        return (f'<span class="lamp lamp-{status}{" lamp-big" if big else ""}" '
                f'aria-hidden="true"></span>')

    def render_check(c):
        details = ""
        if c.get("details"):
            rows = "".join(f"<li>{_inline_md(d)}</li>" for d in c["details"])
            details = (f'<details><summary>{len(c["details"])} detail line(s)'
                       f"</summary><ul>{rows}</ul></details>")
        return (f'<div class="check">{lamp(c["status"])}'
                f'<div class="check-body">'
                f'<div class="check-head">'
                f'<span class="check-title">{_esc(c["title"])}</span>'
                f'<span class="tag tag-{c["status"]}">'
                f'{STATUS_LABEL[c["status"]]}</span></div>'
                f'<div class="check-summary">{_inline_md(c["summary"])}</div>'
                f"{details}</div></div>")

    sections_html = ""
    for s in doc.get("sections", []):
        worst = "green"
        for c in s["checks"]:
            if STATUS_RANK[c["status"]] > STATUS_RANK[worst]:
                worst = c["status"]
        checks = "\n".join(render_check(c) for c in s["checks"])
        sections_html += (f'<section class="card"><header class="card-head">'
                          f'{lamp(worst)}<h2>{_esc(s["title"])}</h2></header>'
                          f"{checks}</section>\n")

    stats_html = ""
    for t in doc.get("stats", []):
        sub = f'<div class="stat-sub">{_inline_md(t["sub"])}</div>' if t.get("sub") else ""
        stats_html += (f'<div class="stat"><div class="stat-top">{lamp(t["status"])}'
                       f'<span class="stat-label">{_esc(t["label"])}</span></div>'
                       f'<div class="stat-value">{_esc(t["value"])}</div>{sub}</div>\n')

    counts = doc.get("counts", {})
    overall = doc.get("overall", "blue")
    overall_word = {"green": "All clear", "blue": "Normal activity",
                    "yellow": "Needs a look", "red": "Action needed"}[overall]
    tally = "".join(f'<span class="tally">{lamp(k)}<span>{counts.get(k, 0)}'
                    f"</span></span>" for k in ("red", "yellow", "blue", "green"))

    narrative_html = ""
    if narrative_md.strip():
        narrative_html = (f'<section class="card narrative"><header '
                          f'class="card-head"><h2>Morning assessment</h2></header>'
                          f"{_md_to_html(narrative_md)}</section>")

    prev = doc.get("previous_run_at")
    prev_html = (f'<span class="meta-dim">diffs vs {_esc(prev)}</span>'
                 if prev else "")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hub Ops Report</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="masthead">
    <h1>{lamp(overall, big=True)} Hub Ops Report</h1>
    <span class="overall overall-{overall}">{lamp(overall)} {overall_word}</span>
    <div class="tallies">{tally}</div>
    <div class="meta"><span>{_esc(doc.get("generated_at_local", ""))}</span>{prev_html}</div>
  </header>
  <div class="stats">{stats_html}</div>
  {narrative_html}
  <div class="grid">{sections_html}</div>
  <div class="legend">
    <span>{lamp("red")} Action needed</span>
    <span>{lamp("yellow")} Watch / attention soon</span>
    <span>{lamp("blue")} Informational — checked, normal activity</span>
    <span>{lamp("green")} Checked and healthy</span>
  </div>
</div>
</body>
</html>
"""


# ------------------------------------------------------------- orchestrate

def generate_ops_report(reporter=None, hours_back: int = 24,
                        send_email: bool = True) -> dict:
    """Collect, narrate, render, store, and email the daily ops report.

    Returns a summary dict (also merged into process_stats via the caller's
    reporter.emit_data)."""
    import fyp.data_io as data_io

    def progress(pct, msg):
        if reporter is not None:
            reporter.update_progress(pct, msg)

    progress(5, "Collecting status checks...")
    doc = collect_status(hours_back=hours_back)
    new_state = doc.pop("_new_state", None)

    progress(55, "Writing the assessment...")
    narrative_md, narrative_source = build_narrative(doc)

    progress(75, "Rendering the report...")
    page = render_html(doc, narrative_md)

    progress(85, "Storing the report...")
    tz = _local_tz()
    day = _now().astimezone(tz).strftime("%Y-%m-%d")
    data_io.save_text(data=page, storage_location="cache",
                      filename=f"{REPORT_DIR}/latest.html")
    data_io.save_text(data=page, storage_location="cache",
                      filename=f"{REPORT_DIR}/report_{day}.html")
    meta = {"generated_at": doc["generated_at"],
            "generated_at_local": doc["generated_at_local"],
            "overall": doc["overall"], "counts": doc["counts"],
            "narrative_source": narrative_source, "narrative": narrative_md}
    data_io.save_json(data=meta, storage_location="cache",
                      filename=f"{REPORT_DIR}/latest.json")
    if new_state:
        data_io.save_json(data=new_state, storage_location="cache",
                          filename=f"{REPORT_DIR}/state.json")
    _prune_dated_reports(data_io)

    email_sent = False
    if send_email:
        progress(92, "Emailing the report...")
        email_sent = _email_report(page, doc)

    progress(100, f"Ops report done — overall {doc['overall']}, "
                  f"email {'sent' if email_sent else 'not sent'}.")
    return {"overall": doc["overall"], "counts": doc["counts"],
            "narrative_source": narrative_source, "email_sent": email_sent}


def _prune_dated_reports(data_io):
    try:
        dated = sorted(n for n in data_io.listdir(storage_location="cache")
                       if n.startswith(f"{REPORT_DIR}/report_")
                       and n.endswith(".html"))
        for name in dated[:-KEEP_DATED_REPORTS]:
            data_io.remove(storage_location="cache", filename=name)
    except Exception as e:
        logger.warning(f"ops_report: prune failed: {e}")


def _email_report(page: str, doc: dict) -> bool:
    from web_interface.mail_utils import _send_html_email, _site
    site = _site()
    recipient = str(site.get("ops_report_email", "")
                    or site.get("mail_sender", "") or "").strip()
    if not recipient:
        logger.warning("ops_report: no recipient configured "
                       "([site].ops_report_email / [site].mail_sender); "
                       "skipping email")
        return False
    overall_word = {"green": "all clear", "blue": "normal activity",
                    "yellow": "needs a look",
                    "red": "ACTION NEEDED"}.get(doc.get("overall"), "")
    counts = doc.get("counts", {})
    subject = (f"Hub ops report {_now().astimezone(_local_tz()).strftime('%Y-%m-%d')}"
               f" — {overall_word} ({counts.get('red', 0)} red, "
               f"{counts.get('yellow', 0)} yellow)")
    return _send_html_email(recipient, subject, page)
