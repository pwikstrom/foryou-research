# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Email **info@foryouresearch.net** with:

- a description of the issue and why you believe it is a security problem,
- the steps, code, or requests needed to reproduce it,
- the affected version or commit, and
- any impact you have already established.

You should get an acknowledgement within **five working days**, and an
assessment with a planned course of action within **fifteen working days**. If
you do not hear back in that window, please send a follow-up — the project is
maintained by a small academic team, and a missed message is more likely than a
deliberate silence.

Please give us a reasonable opportunity to release a fix before disclosing the
issue publicly. We are happy to credit reporters by name in the release notes;
tell us how you would like to be attributed, or if you would rather not be.

## Scope

This repository is research infrastructure for short-video data donation
studies. Reports that are especially relevant:

- authentication, session handling, or the role-based permission system,
- anything allowing one user to read another user's studies, collections, or
  media,
- unauthenticated access to data endpoints or media streaming,
- injection or deserialization issues in the ingestion and enrichment paths,
  which process untrusted participant-supplied archives,
- secrets or participant data exposed through logs, error pages, or artifacts.

Out of scope: findings that require an already-compromised administrator
account, denial of service through deliberate resource exhaustion on a
self-hosted instance, and vulnerabilities in third-party platforms this project
merely reads from.

## A note on participant data

Deployments of this software hold **personal data donated by research
participants** under ethics approval. If you believe you have found a way to
access participant data, treat it as sensitive: report it privately, do not
download or retain more than the minimum needed to demonstrate the issue, and
delete any such data once the report is acknowledged.

Operators of an instance are responsible for handling donated data under their
own ethics approval and institutional data-management plan.

Two settings decide who can reach that data, and they compose — check them
together, not one at a time:

- **Admin → Site Settings → Require approval for new user signups.** On by
  default. `/signup` is reachable without a login, so turning this off means
  anyone who finds the instance URL gets an active account.
- **Admin → Site Settings → Default role for new users**, together with each
  study's `USER_ACCESS`. A study that grants access to a *role* is readable by
  every account holding it, including accounts created later. Granting a study
  to named users keeps "who may sign up" separate from "who may read this
  corpus".
