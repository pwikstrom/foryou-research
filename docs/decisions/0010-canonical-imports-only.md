# 0010. First-party code imports canonical subpackage paths only

Date: 2026-08-16

## Context

After the subpackage restructure ([decision 0002](0002-fyp-subpackage-restructure.md))
every flat path (`fyp/data_io.py`, `fyp/pca.py`, ...) remained a
`sys.modules` alias shim. Two threads resolving cold shims concurrently can
receive a partially-initialized module (CPython's per-module-lock deadlock
breaker), and in production this silently dropped collections from
Timelines refresh batches.

Measured with a barrier probe on a cold interpreter:

| pool body imports | threads failing |
|---|---|
| one cold shim, alone | 0 of 9 |
| one cold shim after another cold import | 0–2 of 9 (flaky) |
| two cold shims | 11 of 12 |
| two cold canonical paths | 0 of 12 |

The hazard is the shim's `sys.modules` swap window, not concurrency as such,
and two threads are enough to trip it. An audit of every thread pool found
the annotation pools warm only by accident (an earlier import of the same
shim on the calling path), plus two offenders in `qwen_local`'s shared
helpers.

## Decision

Code in this repository imports only the canonical
`fyp.<subpackage>.<module>` paths. The flat shims stay for code outside the
repository.

## Consequences

- `tests/unit/test_pool_import_race.py` sweeps every `ThreadPoolExecutor`
  body in the tree, following same-module calls, plus a registry of pool
  bodies reached through an interface (e.g. `backend.annotate_one`).
- On 2026-09-28 the rule became a lint error: ruff's banned-api rule
  (`TID251`, listed in `pyproject.toml`) rejects the flat paths.
- The rule is CONTRIBUTING.md invariant 6 and is summarized in
  [architecture.md](../architecture.md#package-layout).
