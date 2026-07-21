"""Refuse to run tests against GCS-backed storage.

2026-07-14 incident: the test suite was run on a machine whose storage
resolved to the production GCS bucket (a leftover ``FYP_FORCE_GCS=1`` shell or
a ``config.local.toml`` with ``use_gcs_*`` enabled). Test fixtures overwrote
the production recoded annotation parquets, the annotation version registry
and the annotation queue. This guard makes that class of accident impossible:
every test entry point calls :func:`assert_local_storage` before any test
code touches ``data_io``.
"""

import os


def assert_local_storage() -> None:
    """Abort the test run unless all storage resolves to the local filesystem.

    Raises:
        SystemExit: When ``FYP_FORCE_GCS`` is set or any ``[data_io]``
            ``use_gcs_*`` flag is enabled — running tests would read and
            WRITE fixtures against the live bucket.
    """
    if os.environ.get("FYP_FORCE_GCS"):
        raise SystemExit(
            "REFUSING to run tests: FYP_FORCE_GCS is set, so all storage "
            "(including test fixture writes) would hit the live GCS bucket. "
            "Unset it (close the drain shell) and re-run."
        )
    from fyp.fyp_config import get_config

    data_io_cf = get_config().get("data_io", {})
    gcs_flags = sorted(k for k, v in data_io_cf.items()
                       if k.startswith("use_gcs") and v)
    if gcs_flags:
        raise SystemExit(
            f"REFUSING to run tests: config resolves storage to GCS "
            f"({', '.join(gcs_flags)} enabled — check config.local.toml). "
            "Tests write fixtures through data_io and would overwrite live "
            "bucket objects. Run tests only with local storage."
        )
