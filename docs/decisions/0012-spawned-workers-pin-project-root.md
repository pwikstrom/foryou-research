# 0012. Spawned workers inherit the server's project root

Date: 2026-08-31

## Context

In subprocess mode, `process_manager.start_process` spawned each worker
without telling it which configuration the server had loaded, so the child
rediscovered its own project root: `fyp.core.paths` walks up from the working
directory for `__proj__.py`, and `import fyp` can be answered by the venv's
editable install, which points at whichever checkout pip was given. Either
route can land the child on a different `config.toml` — and so a different
gitignored `config.local.toml` overlay and a different data store — than its
parent. On 2026-08-28 workers spawned from a git worktree during a local test
read and pruned the production scrape queue while the server itself was on
the local store.

## Decision

`process_manager.worker_env()` builds the child environment in one place:
`FYP_CONFIG_PATH` names the config TOML the server actually loaded (both
discovery paths honour it ahead of the directory walk), and `PROJECT_ROOT` is
prepended to `PYTHONPATH`, which lets Python resolve `fyp` from the server's
checkout before the editable-install finder does. Both are no-ops wherever
parent and child already agree, which is every deployed configuration.

## Consequences

- Guarded by `tests/unit/test_worker_spawn_env.py`.
- The job framework is described in
  [web_interface.md](../web_interface.md#background-workers).
