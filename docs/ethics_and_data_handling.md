# Ethics and data handling

How the Hub handles participant data, and the ethical position of its scraping
and annotation components. This document describes what the *software* does
and enforces; each deployment additionally operates under its own
institutional ethics approval and data-management plan.

> **Deployment note:** research conducted with the author's deployment of
> The For You Data Hub is covered by an institutional human-research ethics approval
> (approval number: `<ETHICS-APPROVAL-NUMBER — fill in>`). Other teams
> deploying the Hub are responsible for their own approvals.

## No participant data in this repository

This repository contains **code and configuration only**. No donation
exports, activity data, scraped media, annotations, or participant
identifiers ship with it. Test fixtures are synthetic or structural
(e.g. the golden annotation suite replays saved model *responses*, not
participant data).

## Consent-gated intake

Donations arrive in two ways, both consent-first:

- **External donation store**: where a study operates its own donation-intake
  service, the automated fetch (`fyp/analysis/donations.py`) retrieves only
  records carrying an affirmative `consentProvided` flag, so a donation
  without that flag is never downloaded into the Hub. This route depends on
  an intake service that a particular deployment happens to run; a standard
  installation has none, and uses researcher upload instead.
- **Researcher upload**: zipped "Download Your Data" exports are uploaded
  manually by an authenticated researcher through the Data Management tab,
  after whatever consent procedure the study's ethics protocol requires.

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

## Access control and storage

- The web dashboard sits behind authentication (Flask-Login) with
  role-based permissions per tab and sub-page; destructive and
  data-management operations additionally require an admin role. Signup
  can be gated by an admin setting.
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

## Deletion

Collections (a donor's ingested data) can be deleted from the Hub via
the Data Management interface, which removes the collection's rows from
the recoded and metadata stores.
