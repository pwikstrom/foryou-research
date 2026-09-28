# 0011. GCP identifiers are supplied at build time, never committed

Date: 2026-08-18

## Context

The repository became public in August 2026. The Dockerfiles and deploy
commands carried the production project id and registry paths. Commit
`a958a609` (2026-08-18) parameterized the GCP identifiers and made the
Dockerfiles buildable by anyone.

On 2026-09-17 local Docker was dropped from the deploy workflow. A
`gcloud builds submit --tag` one-liner cannot pass `-f Dockerfile.base` or
`--build-arg`, so building the base and app images in Cloud Build needs a
config file per image.

## Decision

- No registry path, project id or bucket name is committed. The deploy
  documentation uses placeholders (`<gcp-project>`, `<prod-bucket>`, ...).
- `cloudbuild-base.yaml` and `cloudbuild-app.yaml` (repo root) are generic
  Cloud Build configs that select the Dockerfile and wire the base image in;
  both image paths are supplied per invocation via `--substitutions`.
  Hardcoding a registry path in either would undo `a958a609`.

## Consequences

- Every build runs in Cloud Build; a local `docker build` (see the comments
  atop `Dockerfile` / `Dockerfile.base`) still works but is optional.
- Commands: [DEVELOPING.md](../../DEVELOPING.md#cloud-run-deployment).
