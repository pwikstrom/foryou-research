#!/usr/bin/env bash
# Configure the Cloud Tasks queue that drives the background workers.
#
# The queue ran with --max-attempts=1 until 2026-07 because the app acked every
# delivery with HTTP 200 (failures included), so retries would have been both
# invisible and unsafe: the scrapers/annotators claim work by pruning their
# queue, and a blind re-delivery means double spend or a lost batch.
#
# web_interface/routes/process_routes.py now decides per task:
#   * QUEUE_RETRY_SAFE tasks (pure recomputations) return 503 on failure, so
#     Cloud Tasks retries them with backoff, bounded by MAX_APP_RETRIES.
#   * every other task returns 200 on failure — terminal by design.
# Failures land in the app-level ledger (cache/task_failures.json) either way,
# which is the dead-letter record: Cloud Tasks HTTP queues have no native one.
#
# ORDER MATTERS: deploy BOTH services (fyp-data-hub and fyp-task-runner) with
# the retry code before running this. Until that code is live, every failure is
# acked 200 and raising max-attempts changes nothing.
#
# Keep --max-attempts >= process_routes.MAX_APP_RETRIES; the smaller of the two
# bounds is what actually applies.

set -euo pipefail

PROJECT="${FYP_GCP_PROJECT:?set FYP_GCP_PROJECT to your GCP project id}"
LOCATION="${FYP_TASKS_LOCATION:-australia-southeast1}"
QUEUE="${FYP_TASKS_QUEUE:-fyp-background-tasks}"

echo "Configuring queue '${QUEUE}' (${PROJECT}/${LOCATION})..."

gcloud tasks queues update "${QUEUE}" \
  --location="${LOCATION}" \
  --project="${PROJECT}" \
  --max-attempts=4 \
  --min-backoff=60s \
  --max-backoff=600s \
  --max-retry-duration=3600s

echo
echo "Current configuration:"
gcloud tasks queues describe "${QUEUE}" \
  --location="${LOCATION}" \
  --project="${PROJECT}" \
  --format="yaml(name, rateLimits, retryConfig)"
