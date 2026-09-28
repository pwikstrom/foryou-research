# 0004. Gemini generation parameters are config-file-only

Date: 2026-07-21

## Context

The five `[machine.gemini]` generation parameters (`model`, `temperature`,
`thinking_budget`, `media_resolution`, `max_output_tokens`) could also be
overridden at runtime from an admin UI. Model id and generation parameters are
part of the annotation-version identity (the `av_` hash), so a runtime
override silently changed what the next annotation would be stamped with,
outside version control and outside the deploy that other instances run.

## Decision

The runtime-override UI was removed on 2026-07-21. The parameters are set only
in `config/config.toml` (or the `config.local.toml` overlay) and take effect
after a restart or redeploy. The only runtime switch for annotation is the
choice of backend (the `annotation_backend` admin setting, Admin → Backends).

## Consequences

- To run two model generations side by side, declare a backend variant in the
  config instead ([configuration.md](../configuration.md#pinning-or-ab-ing-annotation-model-versions-backend-variants)).
- The admin settings store holds the backend choice but deliberately none of
  the model parameters ([configuration.md](../configuration.md#admin-editable-stores-runtime-state-in-the-users-location)).
