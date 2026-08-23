# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Development began in November 2025 and ran privately through August 2026, so
`0.1.0` is the first tagged release rather than a step on from an earlier
public version. Entries below describe the Hub as it stands at that release.

## [Unreleased]

### Added

- **Collections belong to user accounts; demographics live on the account,
  not the collection.** Each collection can be linked to one user account
  (n-to-1). The link is set at upload (a "Participant account" picker in the
  upload modal), written automatically at AIO ingest from the donation's
  participant record (matched by email to an existing account, else a
  passwordless *participant* account under that email, else a placeholder
  `p-N@<[site].participant_placeholder_domain>` account for donations with
  demographics but no email), and changed on Edit Collections (single and
  bulk; "unassigned" is remembered so a later ingest does not re-link).
- **Richer user profile.** Full name, age (number or bracket such as
  "21 - 25"), postcode, country, occupation, TikTok handle and consent to
  contact, editable on My stuff → Profile and by admins on Active users. The
  AIO demographic columns are no longer written to the collections metadata
  parquet; donation-level fields (campaign, donation type, consent) remain.
- Participant accounts have no password until an admin sets one (Reset
  Password), and signing up with that email claims the account instead of
  failing on "User already exists". Placeholder accounts can never log in.
- Active users shows account kind, collection counts and profile; deleting an
  account unassigns its collections by default and can optionally delete
  them (runs the existing collection-delete task); placeholder accounts left
  with no collections are flagged with a one-click cleanup.
- `scripts/migrate_collection_accounts.py` — one-off, idempotent migration
  of existing collections (dry run by default; refuses to apply unless the
  configured storage is GCS, snapshots first, writes a report).

### Security

- **New user signups now require admin approval by default.** A fresh install
  previously shipped `new_user_admin_approval_required = false`, so on any
  hosted instance a stranger who found the URL could self-register into an
  immediately active account — and on an instance whose studies grant access
  to the default role, that account could read donated participant data. New
  signups now land unapproved and the oldest admin is notified. This changes
  only the fallback: an instance that already stores an explicit value keeps
  it, and the first admin is still created at first boot with a one-time
  console password rather than through the signup route, so no install can be
  locked out. Operators who want open registration can turn the setting off
  under Admin → Site Settings.
- `docs/installation.md` and `SECURITY.md` now describe how signup approval,
  the default new-user role and a study's `USER_ACCESS` compose to decide who
  can read a corpus — the three have to be checked together.

### Fixed

- The Admin → New Users page pointed at a "General" sub-page that no longer
  exists under that name; it now names **Site Settings**.

### Internal

- `.gitignore` now ignores all of `.claude/` rather than only
  `.claude/launch.json`. Both files in that directory can carry live API keys,
  and `settings.local.json` was previously covered only by a machine-global
  ignore, which does not travel with the repository.

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

**Project.** MIT license, citation metadata, contributor guide, security
policy, ethics and data-handling documentation, continuous
integration, and a `scripts/verify.sh` gate combining lint, unit tests, the
import-cycle and schema-hash guards, the golden suite, and an app import smoke
test.

[0.1.0]: https://github.com/pwikstrom/foryou-research/releases/tag/v0.1.0
