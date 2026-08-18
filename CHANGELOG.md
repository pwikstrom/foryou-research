# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Development began in November 2025 and ran privately through August 2026, so
`0.1.0` is the first tagged release rather than a step on from an earlier
public version. Entries below describe the Hub as it stands at that release.

## [0.1.0] — 2026-08-18

### Added

**Ingestion.** Parsing of "Download Your Data" exports into a single
platform-agnostic activity table (one row per play, like, comment, share or
save): TikTok data captures, TikTok/Instagram/YouTube zipped donation
exports, Zeeschuimer browser captures, and a synthetic demo source. Adding a
platform is one ingestion class, which self-registers its upload location and
inherits the manifest, dedup, ledger and finalisation machinery. Engagement
events are folded onto the play rows they belong to, per-donor timezone
overrides are honoured, and a per-file intake report records rows read, rows
kept, and plain-language drop reasons.

**Enrichment.** Per-platform scrapers (TikTok, Instagram, YouTube) built on a
common base class with throttling, rate-limit and bot-wall detection,
circuit breakers, permanent/transient storm guards, per-platform queues and
cookie handling. Photo and carousel posts are assembled into slideshow media
with their audio track. Donated item metadata is retained as a
lowest-precedence enrichment fallback for items that cannot be scraped.

**Annotation.** Structured multimodal LLM annotation of item media across
four interchangeable backends — hosted Gemini, hosted Qwen, and local
Qwen3-Omni or MiniCPM-o — selectable as an administrative setting, plus
config-declared backend variants for pinning model versions. Batch mode is
available on Gemini. Prompt and response schema are generated from a
declarative contract rather than hand-maintained.

**Provenance.** Four declarative TOML contracts own the entire variable
schema (activity, scrape, annotation, merge-derived), each with a version
registry that stamps every row with the hash-identified contract version that
produced it. Superseded fields remain readable as "legacy". Model identity and
generation parameters are hashed into the annotation version, so changing
models forks a new version rather than mixing incomparable labels. Every study
emits a methods and provenance note recording its filters, sample sizes and
active contract versions.

**Validation.** An A/B evaluation harness that re-annotates a fixed set under
several prompt, model or backend arms and reports per-variable agreement; a
human-coding workflow with invitations, blind coding, intercoder reliability
(Cohen's kappa) against each machine arm, and blind pairwise preference votes.

**Analysis.** PCA and distance metrics, ANOVA and PERMANOVA, timeline metrics,
within-session profiling, viewing-session and binge-episode detection,
sequence-window analysis, and dense semantic embeddings clustered into
data-driven micro-genres with a 2D map, per-video typicality and niche
isolation measures, and per-collection trajectory overlays.

**Web interface.** Flask dashboard with role-based access control over data
exploration, per-video analysis, correlations with a statistical-rigor layer,
timelines, semantic space, sessions, and administration. Background work runs
as local subprocesses or Google Cloud Tasks against the same worker code, with
durable per-process run logs.

**Safety nets.** A structure sentinel that learns each platform's export
structure and per-file sanity statistics and quarantines deviating uploads for
human review; a golden regression suite that replays saved raw LLM responses
through the parsing pipeline at zero API cost; and a synthetic demo-dataset
generator so the Hub can be installed and exercised end to end without
participant data.

**Project.** MIT license, citation metadata, contributor guide, code of
conduct, security policy, ethics and data-handling documentation, continuous
integration, and a `scripts/verify.sh` gate combining lint, unit tests, the
import-cycle and schema-hash guards, the golden suite, and an app import smoke
test.

[0.1.0]: https://github.com/pwikstrom/foryou-research/releases/tag/v0.1.0
