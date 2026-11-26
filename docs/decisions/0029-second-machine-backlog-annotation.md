# 0029. A second machine annotates the backlog, and gives way to the hosted annotator

Date: 2026-11-26

## Context

A site can hold far more scraped videos than its budget for a hosted
annotation model covers. A computer with a local model (a Mac running local
Qwen) can annotate the remainder at no cost per video, over months, while
the hosted Hub keeps annotating small batches with the backend an admin
selected. The Hub was built for one annotator: one shared setting chose the
backend, the annotation queue had no notion of who was working on what, the
most recent successful annotation of a video won whichever model produced
it, and the Hub noticed new result files only when its own workers had
produced them.

## Decision

- **No second Hub.** The second machine runs one command-line program
  (`web_interface/workers/backlog_annotator.py`) that uses the Hub's code
  against the same storage. It serves no pages and never consolidates.
- **Separate lists.** The hosted annotator keeps `to_annotate.json`. The
  second machine reads a backlog (`annotate_backlog.json`) with three lanes
  taken in order: urgent, coverage, rest.
- **Claims, not removal.** The machine takes 50 videos at a time by writing a
  claim that it renews every 60 seconds and that counts as abandoned after 15
  minutes. A claim removes nothing from the backlog, so a crash costs at most
  the videos in hand. Fifty keeps that loss, and the wait before a change of
  priority takes effect, to about ten minutes of work.
- **The backlog gives way.** A claim passes over videos queued for or in
  flight with the hosted annotator, and drops videos that have been annotated
  since they were listed.
- **The hosted annotation wins.** Annotations from the backends in
  `[machine] yielding_backends` never hide another annotator's successful
  annotation of the same video, whichever is preferred or more recent. A
  video annotated by both is therefore read from the hosted model, which
  keeps the set of videos annotated by the hosted model stable for studies
  built on it.
- **The annotator is chosen per machine.** `FYP_ANNOTATION_BACKEND` selects
  the annotator for one process before the shared admin setting, and
  `FYP_ANNOTATION_MACHINE` tags that process's raw result files.
- **One consolidation a night.** The enrichment supervisor starts a
  Consolidate & Refresh between 01:00 and 05:00 when the second machine has
  written results since the last consolidation.
- **Off by default, behind one admin setting.** `second_machine_annotates`
  gates the controls, the status line, the report check, the nightly
  consolidation and the yielding rule.

## Consequences

- A Hub on one computer is unchanged, including one that selects local Qwen
  as its only annotator: with the setting off, no backend yields.
- The view rule's name in the consolidation ledger gains a
  `|yielding:<versions>` suffix, so the first consolidation after a yielding
  version is registered rebuilds every video's chosen annotation once.
- Switching the setting off later stops the yielding rule, which is again a
  rule change and again one full rebuild.
- The backlog annotator is not a registered worker and must not be given a
  `run_` prefix (`tests/unit/test_worker_registry.py`).
- Nothing fills the coverage lane yet; an admin adds videos to the lanes.
- The mechanism is described in
  [pipeline.md](../pipeline.md#a-second-machine-on-the-backlog) and the
  commands in DEVELOPING.md.
