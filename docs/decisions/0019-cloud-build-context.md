# 0019. `.gcloudignore` is the build-context filter for deployed images

Date: 2026-09-09

## Context

A build submitted from a git worktree baked a local filesystem path into the
production image. A worktree's `.git` is a *file* holding a `gitdir:`
pointer, not a directory, so the long-standing `.git/` ignore pattern never
matched it; `.pytest_cache/` was listed in neither ignore file and rode along
the same way.

Investigating showed which file matters: `.gcloudignore` excludes
`.dockerignore` itself, so `.dockerignore` never reaches Cloud Build and the
docker step runs with no ignore file at all.

## Decision

- `.gcloudignore` is the only filter that shapes a deployed image;
  `.dockerignore` matters only to a local `docker build`. The two are kept in
  step.
- Both list bare `.git` and `.pytest_cache/` (`.ruff_cache/` was already
  covered).

## Consequences

- After the change the uploaded source differed from `git archive HEAD` only
  by the ignore files, `.gitattributes`, and the documentation and test
  directories, and the built image carried no `.git`, `.pytest_cache` or
  `.ruff_cache`.
- Deploy commands: [DEVELOPING.md](../../DEVELOPING.md#cloud-run-deployment).
