"""Admin system endpoints: System Information, System Health (and acknowledging
task failures), and the daily ops report.
"""

import os
import platform

from flask import jsonify, request
from flask_login import current_user

import fyp.core.data_io as data_io
from fyp.core.fyp_config import fyp_cf
from fyp.scrape import scraper_alerts
from web_interface.auth.permissions import admin_required, permission_required
from web_interface.services import system_health
from web_interface.tasks import task_failures, worker_registry

from ._blueprint import explorer_bp


@explorer_bp.route("/api/system-info")
@permission_required("tab.admin.system_info")
def system_info():
    """Return basic system information for the Information panel."""

    # Detect Google Cloud Run via its injected environment variables
    k_service = os.environ.get("K_SERVICE")
    is_cloud_run = k_service is not None

    if is_cloud_run:
        environment = f"Google Cloud Run ({k_service})"
        revision = os.environ.get("K_REVISION", "unknown")
    else:
        environment = "Local"
        revision = None

    # Storage locations: Local or Remote based on the use_gcs_for_* flags
    data_io_cf = fyp_cf.get("data_io", {})
    data_location = "Remote" if data_io_cf.get("use_gcs_for_data") else "Local"
    media_location = "Remote" if data_io_cf.get("use_gcs_for_media") else "Local"
    cache_location = "Remote" if data_io_cf.get("use_gcs_for_cache") else "Local"

    info = {
        "os": f"{platform.system()} {platform.release()}",
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "environment": environment,
        "revision": revision,
        "data_location": data_location,
        "media_location": media_location,
        "cache_location": cache_location,
    }

    return jsonify(info)


@explorer_bp.route("/api/system-health")
@permission_required("tab.admin.system_info")
def get_system_health():
    """Return the current system-health document for the Information panel.

    Includes the active per-platform scraper alerts (raised by the scrape
    worker on systematic failures such as a permanent-failure storm) so the
    panel can flag "scraper needs revision" conditions alongside the checks,
    plus the recent background-task failure ledger (the dead-letter record for
    the Cloud Tasks queue, which has no native dead-letter topic).
    """
    doc = system_health.get_health()
    doc["scraper_alerts"] = scraper_alerts.load_alerts()
    doc["task_failures"] = task_failures.unacknowledged_dead()
    return jsonify(doc)


@explorer_bp.route("/api/system-health/task-failures/ack", methods=["POST"])
@admin_required
def ack_task_failures():
    """Acknowledge one ledger entry (``{"id": ...}``) or all of them."""
    data = request.json or {}
    changed = task_failures.acknowledge(str(data.get("id") or ""))
    return jsonify({"status": "success", "acknowledged": changed})


@explorer_bp.route("/api/system-health/run", methods=["POST"])
@permission_required("tab.admin.system_info")
def run_system_health():
    """Kick off a manual health-check run; 409 when one is already running."""
    if not system_health.start_health_check(trigger="manual"):
        return jsonify({"started": False, "reason": "already_running"}), 409
    return jsonify({"started": True})


@explorer_bp.route("/api/admin/ops-report")
@permission_required("tab.admin.ops_report")
def ops_report_meta():
    """Metadata for the latest daily ops report (Admin → System pane)."""
    meta = data_io.load_json(storage_location="cache", filename="ops_report/latest.json")
    if not meta:
        return jsonify({"available": False})
    meta.pop("narrative", None)  # the pane iframes the full HTML instead
    meta["available"] = True
    return jsonify(meta)


@explorer_bp.route("/api/admin/ops-report/html")
@permission_required("tab.admin.ops_report")
def ops_report_html():
    """Serve the latest rendered ops report for the pane's iframe."""
    from flask import Response

    page = data_io.load_text(storage_location="cache", filename="ops_report/latest.html")
    if not page:
        return "No ops report has been generated yet.", 404
    return Response(page, mimetype="text/html")


@explorer_bp.route("/api/admin/ops-report/run", methods=["POST"])
@permission_required("tab.admin.ops_report")
def ops_report_run():
    """Generate a fresh ops report now (runs on the task-runner via the
    normal background-task dispatch)."""
    from web_interface.tasks.process_manager import start_process

    success, msg = start_process(
        "ops_report",
        worker_registry.worker_module("ops_report"),
        [],
        started_by=getattr(current_user, "username", ""),
    )
    return jsonify({"started": bool(success), "message": msg}), (200 if success else 409)
