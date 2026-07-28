"""Read-only Data Contracts endpoints (/api/manage/data-contracts/*).

Surfaces the three code-coupled contracts (scrape / activity / derived) for
inspection: parsed field tables, the raw TOML, and — for the two contracts
with a version registry — the version history. Deliberately read-only: these
contracts only meaningfully change together with the code that emits their
fields, so they are edited in the repo and deployed, never uploaded at
runtime (unlike the annotation contract).
"""

from pathlib import Path

from flask import Response, jsonify
from flask_login import login_required

from fyp import activity_contract, activity_versioning, derived_contract, scrape_contract, scrape_versioning
from fyp.fyp_config import PROJECT_ROOT

from ...permissions import permission_required
from ._blueprint import management_bp


# kind -> (contract module, versioning module or None, version-id key or None)
_KINDS = {
    "scrape": (scrape_contract, scrape_versioning, "scrape_contract_version"),
    "activity": (activity_contract, activity_versioning, "activity_contract_version"),
    "derived": (derived_contract, None, None),
}





def _contract_rel_path(module) -> str:
    """Return the contract's repo-relative path for display."""
    path = Path(module.default_contract_path())
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)





def _active_version(kind: str) -> dict | None:
    """Return {version, label} for the contract the running code would stamp."""
    _, versioning, id_key = _KINDS[kind]
    if versioning is None:
        return None
    descriptor = versioning.active_version_descriptor()
    return {
        "version": descriptor.get(id_key),
        "label": descriptor.get("label"),
    }





@management_bp.route('/api/manage/data-contracts/<kind>', methods=['GET'])
@permission_required('tab.admin.data_contracts')
@login_required
def get_data_contract(kind):
    """Parsed contract payload: meta, full field list, active version."""
    if kind not in _KINDS:
        return jsonify({"error": f"unknown contract kind: {kind}"}), 404
    module, versioning, _ = _KINDS[kind]
    try:
        try:
            contract = module.load_contract()
            validation_errors: list[str] = []
        except (ValueError, FileNotFoundError) as e:
            # load_contract validates internally; surface the failure instead
            # of a bare 500 so the page can show what is wrong with the file.
            return jsonify({
                "kind": kind,
                "path": _contract_rel_path(module),
                "fields": [],
                "validation_errors": [str(e)],
                "active_version": None,
            })

        meta = contract.get("meta", {}) or {}
        payload = {
            "kind": kind,
            "path": _contract_rel_path(module),
            "meta_version": str(meta.get("version", "")),
            "fields": list(contract.get("fields", []) or []),
            "validation_errors": validation_errors,
            "active_version": _active_version(kind),
        }
        if hasattr(module, "platforms"):
            payload["platforms"] = module.platforms(contract)
        if hasattr(module, "default_platform"):
            payload["default_platform"] = module.default_platform(contract)
        if versioning is not None:
            payload["preferred_version"] = versioning.get_preferred_version()
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500





@management_bp.route('/api/manage/data-contracts/<kind>/raw', methods=['GET'])
@permission_required('tab.admin.data_contracts')
@login_required
def get_data_contract_raw(kind):
    """The contract's raw TOML text, for the in-page viewer."""
    if kind not in _KINDS:
        return jsonify({"error": f"unknown contract kind: {kind}"}), 404
    module, _, _ = _KINDS[kind]
    try:
        text = Path(module.default_contract_path()).read_text(encoding="utf-8")
        return jsonify({"toml": text, "path": _contract_rel_path(module)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500





@management_bp.route('/api/manage/data-contracts/<kind>/download', methods=['GET'])
@permission_required('tab.admin.data_contracts')
@login_required
def download_data_contract(kind):
    """Download the contract TOML as an attachment."""
    if kind not in _KINDS:
        return jsonify({"error": f"unknown contract kind: {kind}"}), 404
    module, _, _ = _KINDS[kind]
    try:
        path = Path(module.default_contract_path())
        text = path.read_text(encoding="utf-8")
        return Response(
            text,
            mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500





@management_bp.route('/api/manage/data-contracts/<kind>/versions', methods=['GET'])
@permission_required('tab.admin.data_contracts')
@login_required
def list_data_contract_versions(kind):
    """Version-history summaries for a registry-backed contract."""
    if kind not in _KINDS:
        return jsonify({"error": f"unknown contract kind: {kind}"}), 404
    _, versioning, id_key = _KINDS[kind]
    if versioning is None:
        return jsonify({"error": f"the {kind} contract has no version registry"}), 404
    try:
        versions = []
        for summary in versioning.list_versions():
            summary = dict(summary)
            summary["version"] = summary.pop(id_key, None)
            versions.append(summary)
        versions.sort(key=lambda v: v.get("created_at") or "", reverse=True)
        active = _active_version(kind)
        return jsonify({
            "versions": versions,
            "preferred": versioning.get_preferred_version(),
            "active": active.get("version") if active else None,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500





@management_bp.route('/api/manage/data-contracts/<kind>/versions/<version>', methods=['GET'])
@permission_required('tab.admin.data_contracts')
@login_required
def get_data_contract_version(kind, version):
    """Full registry record for one version (incl. the field-digest snapshot)."""
    if kind not in _KINDS:
        return jsonify({"error": f"unknown contract kind: {kind}"}), 404
    _, versioning, id_key = _KINDS[kind]
    if versioning is None:
        return jsonify({"error": f"the {kind} contract has no version registry"}), 404
    try:
        registry = versioning.load_registry()
        record = registry.get("versions", {}).get(version)
        if record is None:
            return jsonify({"error": f"unknown version: {version}"}), 404
        record = dict(record)
        record["version"] = record.pop(id_key, version)
        record["preferred"] = version == registry.get("preferred")
        return jsonify(record)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
