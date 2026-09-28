# 0007. A study with no `USER_ACCESS` is shared with nobody

Date: 2026-07-31

## Context

Per-study sharing is the study definition's `USER_ACCESS` list (role names,
usernames, or `'all'`). An empty or missing list meant *shared with everyone*.
With the read-only `student` teaching role added the same day, that default
would have exposed every unlisted study — including studies over donated
participant data — to any new role or account.

## Decision

An empty or missing `USER_ACCESS` means **shared with nobody**, on every
surface. A boot-time migration
(`fyp.analysis.studies.migrate_user_access_defaults`, run by serving
processes only) backfilled explicit grants into the studies created before
the change, so nothing that was visible became hidden.

## Consequences

- Sharing a study is always an explicit grant. The access model is described
  in [web_interface.md](../web_interface.md#auth--permissions).
- The admin "default study" setting is a separate, wider grant: it makes that
  study readable by every logged-in user regardless of `USER_ACCESS`.
