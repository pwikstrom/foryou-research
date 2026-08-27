# Ethics and data handling

How the Hub handles participant data, and the ethical position of its scraping
and annotation components. This document describes what the *software* does
and enforces; each deployment additionally operates under its own
institutional ethics approval and data-management plan.

> **Deployment note:** research conducted with the author's deployment of
> The For You Data Hub is covered by an institutional human-research ethics
> approval. Other teams deploying the Hub are responsible for their own
> approvals.

## No participant data in this repository

This repository contains **code and configuration only**. No donation
exports, activity data, scraped media, annotations, or participant
identifiers ship with it. Test fixtures are synthetic or structural
(e.g. the golden annotation suite replays saved model *responses*, not
participant data).

## Consent-gated intake

Donations arrive in three ways, all consent-first:

- **External donation store**: where a study operates its own donation-intake
  service, the automated fetch (`fyp/analysis/donations.py`) retrieves only
  records carrying an affirmative `consentProvided` flag, so a donation
  without that flag is never downloaded into the Hub. This route depends on
  an intake service that a particular deployment happens to run; a standard
  installation has none, and uses researcher upload instead.
- **Researcher upload**: zipped "Download Your Data" exports are uploaded
  manually by an authenticated researcher through the Data Pipeline tab,
  after whatever consent procedure the study's ethics protocol requires.
- **Participant self-serve upload**: a participant with an account uploads
  their own export through My stuff → My Collections. The route is
  authenticated, and available only after the participant has accepted the
  terms of use at signup (`terms_accepted_at` is stamped on the account).

Participants obtain their exports themselves through each platform's own
data-access mechanism (GDPR/CCPA-mandated "Download Your Data" flows), so
donation is inherently participant-initiated: the participant sees exactly
what the export contains before sharing it.

## What is collected and stored

- **Activity data** (from donations): one row per feed event (play, like,
  comment, ...), keyed by an internal collection identifier per donor —
  not by platform account name. Donor timezone may be recorded to
  interpret timestamps.
- **Item enrichment** (from scraping): public metadata and media for the
  *items* participants watched (video description, author handle,
  popularity counts, the media file). This describes public content, not
  participants.
- **Annotations** (from the LLM): structured content labels for each item,
  again describing the public item, not the participant.

Free-text fields that may contain participant-authored content (e.g. a
donated comment's text carried in `extra_data`) stay within the Hub's
access-controlled storage and are not exported by any built-in report.

For participant self-serve uploads, minimisation starts **before** the data
reaches the Hub: the participant reviews and prunes their export in the
browser prior to upload, and nothing leaves their machine during review —
the parsing and rebuilding run entirely client-side. TikTok DMs, settings,
ads data and profile sections are stripped in the browser by default, and
login-history (IP address) rows are shown as a reviewable section the
participant can delete.

## Access control and storage

- The web dashboard sits behind authentication (Flask-Login) with
  role-based permissions per tab and sub-page; destructive and
  data-management operations additionally require an admin role. Signup
  approval is **on by default**: new accounts land inactive until an admin
  approves them (an admin setting can open registration deliberately).
- One deliberate broadening of access: an admin can name a site-wide
  **default study** (Admin → Site Settings), which is then readable by
  *every* logged-in user regardless of the study's own `USER_ACCESS` list —
  and each participant's auto-managed "Everyone & Me" study composes that
  default study with their own data. Operators should choose the default
  study knowing it widens who can read the participant-derived data inside
  it.
- All storage goes through a single I/O abstraction
  (`fyp/core/data_io.py`): a local filesystem in local mode, or a private
  Google Cloud Storage bucket in cloud mode. Nothing is served publicly;
  media streaming to the dashboard requires an authenticated session.
- Secrets (API keys, cookies for authenticated scraping) are supplied via
  environment variables or a dedicated secrets location — never committed.

## Scraping position

The Hub's scrapers exist to *enrich consented donations*, not to collect data
at scale: the scrape queue is derived from item ids that appear in
participants' donated histories, and each item is fetched once. Rate
limiting, throttling, and circuit breakers are built in — not only for
robustness, but so that enrichment stays low-intensity toward the
platforms. Media downloads respect configurable duration caps. Researchers
deploying the Hub should satisfy themselves that this enrichment is compatible
with their jurisdiction's rules and their institution's ethics framework;
the Hub makes the practice transparent (per-row provenance stamps
record what was fetched, when, and under which schema version).

## LLM annotation

Downloaded media is sent to a multimodal model for structured content
annotation. Only the *item's* media and metadata are sent — never
participant identities or activity histories. The prompt and response
schema are declarative and versioned (`config/annotation_contract.toml`),
and every annotation row is stamped with the schema version that produced
it, so the exact instructions under which any label was generated remain
auditable.

The model itself is a configurable backend (Admin → Backends): a hosted
API (Google Gemini, or Qwen via DashScope) or an open-weight model running
locally on the operator's own hardware. Text embeddings for the semantic
map are chosen the same way, independently of the annotation backend. With
both set to a local backend, no participant-derived content leaves the
machine — the option to take for corpora whose approval conditions rule
out third-party processing. Model identity and generation parameters are
hashed into the annotation version, so labels produced by different models
are never silently pooled.

## Deletion and participant withdrawal

The deletable unit is the **collection** — one donor's ingested data.
Deleting a collection (Data Pipeline interface, admin-only, runs as a
background task) is precise about participant-linked data but is **not a
full purge on its own**. Participants also have a self-service path: a
participant can withdraw their own processed collection from My stuff → My
Collections (behind a typed collection-id confirmation), which runs the
same collection-delete worker described below and emails the oldest admin;
the participant can restore the withdrawal themselves within the restore
window. Operators handling a withdrawal request should
know exactly what each step does:

**Removed immediately** (participant-linked rows):

- the collection's activity rows (the feed-event table),
- its collection-metadata row (persona/participant descriptors) and tags,
- its membership in every study definition, and the affected studies'
  cached datasets (each study is re-refreshed afterwards).

**Retained by design — the original donation.** The raw upload files are
*moved to an archive location*, not destroyed, so an accidental deletion
is recoverable; the deletion dialog says so explicitly. The archive's
expiry depends on who initiated the deletion: an **admin-initiated**
delete leaves the archived files indefinitely, while a
**participant-initiated withdrawal self-purges** — the archived raw file
is deleted for good 30 days after the withdrawal
(`WITHDRAWAL_RETENTION_DAYS` in
`web_interface/services/my_collections_service.py`), with restore
possible within that window. **For a genuine withdrawal handled by an
operator, the operator must also
delete the archived files** (and, where Google Cloud Storage object
versioning or soft-delete is enabled on the bucket, purge noncurrent
object generations — the application neither enables nor manages bucket
versioning, so retention there is a deployment setting, invisible to the
code).

**Retained because it describes public content, not participants.** The
scraped item metadata, downloaded media, machine annotations, and
embedding vectors are keyed by `(platform, item id)` with no link back to
any collection — they describe the public videos, are shared across all
donors who watched the same items, and are therefore kept. This includes
items only the withdrawn participant happened to watch, and the small
"enrichment seed" rows (item caption/author) extracted from the donation
itself; none of these records who watched what. Deployments whose
approval conditions treat *any* donation-derived record as in scope
should note this and handle it in their data-management plan.

**Cleared on the next refresh, not immediately.** Session-level artifacts
(the Sessions tab's session index, binge episodes, and play cache) and
per-study sequence caches do carry collection identifiers; the delete
task does not rewrite them. They drop the deleted collection the next
time a sessions refresh / dataset assembly runs — **run one as part of a
withdrawal** rather than waiting for the next routine refresh.

**Operational residue.** Bookkeeping stores (the ingestion ledger,
structure-sentinel baselines, process logs, the admin activity log)
retain the collection id and raw filenames as audit metadata; they
contain no feed content.

**The participant's account.** Demographic and contact details a
participant submits with a donation (name, email, age, postcode, country,
TikTok handle, consent to contact) are **not stored on the collection**:
the collection is linked to a *user account* (n-to-1) and those details
live on that account's profile, visible only to the person and to
administrators. Deleting a collection leaves the account in place; a
placeholder account (a participant who left demographics but no email,
held under a fake `p-N@…` address that can never log in) that no longer
owns any collection is flagged on Admin → Active users with a one-click
"Remove orphan accounts" action. Deleting a *user account* (Admin →
Active users) unassigns the account's collections by default — the
collections themselves stay — and offers to delete them as well; that
cascade runs the same collection-delete task described above, so the
rest of this section applies to it unchanged.

There is no per-item or per-file deletion path — the structure-review
"reject" and ledger controls exclude files from ingestion but leave them
on disk. A complete withdrawal is therefore: delete the collection (or
the account, with the cascade), then delete its archived raw files, then
run a sessions refresh and dataset assembly, and — if the study's protocol
requires content-level erasure — remove the orphaned item-keyed artifacts
manually.
