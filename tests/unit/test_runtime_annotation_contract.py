"""Unit tests for the runtime-editable annotation contract.

Pins the storage-backed contract snapshot in ``fyp/annotation_contract.py`` and
its cache/version interactions:

  * resolution order — absent → baked, present+valid → runtime, invalid → baked
    (with an error recorded), and the ``FYP_BAKED_CONTRACTS_ONLY`` bypass;
  * a content change moves the etag;
  * a metadata-only edit (display_name / role / scale / [recode.drop]) leaves the
    ``av_`` version unchanged, while a prompt/schema-affecting edit (desc / enum)
    changes it — this is the whole point of lifting the contract to storage;
  * ``current_version_descriptor`` busts its cache when the snapshot swaps.

The runtime file is simulated by monkeypatching the ``fyp.data_io`` primitives
the snapshot loader calls, so nothing touches real local/GCS storage.

Usage:
    python tests/unit/test_runtime_annotation_contract.py
Exit 0 iff all checks pass.
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Import (and boot) with the REAL data_io, then patch below so boot is unaffected.
import fyp.fyp_config as cfg
import fyp.data_io as data_io
from fyp import annotation_contract as ac
from fyp import annotation_schema as sch
from fyp import annotation_versioning as av

PARAMS = {k: None for k in av._VERSION_GEN_PARAM_KEYS}
PARAMS["use_structured_output"] = True


class _Store:
    """In-memory stand-in for the runtime contract file."""

    def __init__(self) -> None:
        self.text: str | None = None
        self.mtime: float | None = None
        self.meta: dict = {}

    def set(self, text: str, mtime: float) -> None:
        self.text = text
        self.mtime = mtime

    def clear(self) -> None:
        self.text = None
        self.mtime = None
        self.meta = {}


STORE = _Store()
_orig_getmtime = data_io.getmtime
_orig_load_text = data_io.load_text
_orig_load_json = data_io.load_json


def _is_runtime_file(loc: str, fname: str) -> bool:
    return loc == ac.RUNTIME_LOCATION and fname == ac.RUNTIME_FILENAME


def _fake_getmtime(storage_location="cache", filename="", verbose=False):
    if _is_runtime_file(storage_location, filename):
        if STORE.mtime is None:
            raise FileNotFoundError(filename)
        return STORE.mtime
    return _orig_getmtime(storage_location, filename, verbose)


def _fake_load_text(storage_location="cache", filename="", verbose=False):
    if _is_runtime_file(storage_location, filename):
        return STORE.text
    return _orig_load_text(storage_location, filename, verbose)


def _fake_load_json(storage_location="cache", filename="", verbose=False):
    if storage_location == ac.RUNTIME_LOCATION and filename == ac.RUNTIME_META_FILENAME:
        return dict(STORE.meta)
    return _orig_load_json(storage_location, filename, verbose)


def _install_fakes() -> None:
    data_io.getmtime = _fake_getmtime
    data_io.load_text = _fake_load_text
    data_io.load_json = _fake_load_json


def _uninstall_fakes() -> None:
    data_io.getmtime = _orig_getmtime
    data_io.load_text = _orig_load_text
    data_io.load_json = _orig_load_json


# Under pytest, patch data_io only for the duration of THIS module's tests —
# module-level patching leaked into the rest of the suite and made every later
# runtime-contract round-trip read a phantom (in-memory) file.
import pytest


@pytest.fixture(autouse=True, scope="module")
def _patched_data_io():
    _install_fakes()
    yield
    _uninstall_fakes()
    _reset_to_baked()


def _reset_to_baked() -> None:
    """Clear the simulated runtime file and every dependent cache; reload baked."""
    STORE.clear()
    os.environ.pop("FYP_BAKED_CONTRACTS_ONLY", None)
    ac._SNAPSHOT.clear()
    ac._SNAPSHOT["loaded"] = False
    sch._SPECS_CACHE.clear()
    av._DESCRIPTOR_CACHE.clear()
    ac.refresh_runtime_contract()


def _desc_for(contract: dict) -> dict:
    """Build a version descriptor the way the upload dry-run does."""
    return av.build_version_descriptor(
        "m", sch.build_prompt(contract), sch.get_annotation_json_schema(contract), PARAMS
    )


# ---------------------------------------------------------------------------
# resolution order
# ---------------------------------------------------------------------------

def test_absent_is_baked() -> None:
    _reset_to_baked()
    st = ac.contract_status()
    assert st["source"] == "baked", st
    assert st["error"] is None, st
    assert ac.contract_etag().startswith("baked:")


def test_present_valid_is_runtime() -> None:
    _reset_to_baked()
    STORE.set(ac._read_baked_text(), 1000.0)
    STORE.meta = {"updated_at": "2026-07-08T00:00:00", "updated_by": "tester"}
    ac.refresh_runtime_contract()
    st = ac.contract_status()
    assert st["source"] == "runtime", st
    assert st["error"] is None, st
    assert ac.contract_etag().startswith("runtime:")
    assert st["updated_by"] == "tester", st


def test_invalid_toml_falls_back_to_baked() -> None:
    _reset_to_baked()
    STORE.set("this is = = not valid toml [[[", 1001.0)
    ac.refresh_runtime_contract()
    st = ac.contract_status()
    assert st["source"] == "baked", st
    assert st["error"], "expected a recorded parse error"


def test_valid_toml_failing_validation_falls_back() -> None:
    _reset_to_baked()
    # Parses as TOML but has no [[fields]] and no [prompt].header → validation fails.
    STORE.set('title = "not a contract"\n', 1002.0)
    ac.refresh_runtime_contract()
    st = ac.contract_status()
    assert st["source"] == "baked", st
    assert st["error"], "expected a recorded validation error"


def test_baked_only_env_ignores_runtime() -> None:
    _reset_to_baked()
    STORE.set(ac._read_baked_text(), 1003.0)
    os.environ["FYP_BAKED_CONTRACTS_ONLY"] = "1"
    ac._SNAPSHOT.clear()
    ac._SNAPSHOT["loaded"] = False
    ac.refresh_runtime_contract()
    assert ac.contract_status()["source"] == "baked"
    os.environ.pop("FYP_BAKED_CONTRACTS_ONLY", None)


# ---------------------------------------------------------------------------
# etag movement
# ---------------------------------------------------------------------------

def test_etag_moves_on_content_change() -> None:
    _reset_to_baked()
    baked_etag = ac.contract_etag()
    edited = ac._read_baked_text() + "\n# a trailing comment\n"
    STORE.set(edited, 1004.0)
    changed = ac.refresh_runtime_contract()
    assert changed is True
    assert ac.contract_etag() != baked_etag


# ---------------------------------------------------------------------------
# av_ sensitivity: metadata-only vs prompt/schema-affecting
# ---------------------------------------------------------------------------

def test_metadata_only_edits_keep_av() -> None:
    _reset_to_baked()
    baked = ac.load_contract()
    base_av = _desc_for(baked)["annotation_version"]

    for mutate in (
        lambda c: c["fields"][0].__setitem__("display_name", "Edited Display Name"),
        lambda c: c["fields"][0].__setitem__("role", "skip"),
        lambda c: c["fields"][0].__setitem__("scale", "nominal"),
        lambda c: c.setdefault("recode", {}).setdefault("drop", {}).__setitem__(
            "some_col", ["ignore", "me"]
        ),
    ):
        edited = copy.deepcopy(baked)
        mutate(edited)
        assert _desc_for(edited)["annotation_version"] == base_av, mutate


def test_desc_edit_changes_av() -> None:
    _reset_to_baked()
    baked = ac.load_contract()
    base_av = _desc_for(baked)["annotation_version"]
    edited = copy.deepcopy(baked)
    field = edited["fields"][0]
    field["desc"] = (field.get("desc") or "") + " EXTRA PROMPT WORDS"
    assert _desc_for(edited)["annotation_version"] != base_av


def test_enum_edit_changes_av() -> None:
    _reset_to_baked()
    baked = ac.load_contract()
    base_av = _desc_for(baked)["annotation_version"]
    edited = copy.deepcopy(baked)
    enums = edited.get("enums", {})
    # Pick any enum a field references so the change reaches prompt + schema.
    enum_field = next((f for f in edited["fields"] if f.get("enum")), None)
    assert enum_field is not None, "contract has no enum field to test"
    ref = enum_field["enum"]
    target = enums[ref]
    if isinstance(target, dict):
        target["__extra_value__"] = "a new option"
    else:
        target.append("__extra_value__")
    assert _desc_for(edited)["annotation_version"] != base_av


# ---------------------------------------------------------------------------
# descriptor cache busts when the live snapshot swaps (hook 1)
# ---------------------------------------------------------------------------

def test_descriptor_cache_busts_on_snapshot_swap() -> None:
    machine = cfg.fyp_cf["machine"]
    saved = dict(machine)
    machine["use_generated_prompt"] = True
    machine["use_structured_output"] = True
    try:
        _reset_to_baked()
        v1 = av.current_annotation_version()

        # Simulate an uploaded contract whose prompt differs, then swap the live
        # snapshot the way refresh_runtime_contract would (new contract + etag).
        edited = copy.deepcopy(ac.load_contract())
        f = edited["fields"][0]
        f["desc"] = (f.get("desc") or "") + " SWAPPED PROMPT"
        ac._SNAPSHOT["contract"] = edited
        ac._SNAPSHOT["etag"] = "runtime:swaptest0000"
        sch._SPECS_CACHE.clear()

        v2 = av.current_annotation_version()
        assert v1 != v2, (v1, v2)
    finally:
        machine.clear()
        machine.update(saved)
        _reset_to_baked()


def _main() -> int:
    _install_fakes()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {t.__name__}: {exc}")
        except Exception:
            failures += 1
            import traceback

            print(f"ERROR {t.__name__}")
            traceback.print_exc()
    # Leave data_io and the snapshot in a clean state for any later in-process use.
    _uninstall_fakes()
    _reset_to_baked()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
