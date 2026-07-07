# Thin app layer on top of pre-built base image.
# Base image has Python 3.12, Rust, gcc, and all pip dependencies.
# To rebuild the base image (only needed when requirements312.txt changes):
#   gcloud builds submit --config=cloudbuild-base.yaml --project=<gcp-project> --region=australia-southeast1
FROM australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-base:latest

WORKDIR /app

# Copy application code only
COPY . .

# The Launch Command. gunicorn.conf.py adds max_requests=1 on the task-runner
# only (recycles the worker per request so native memory can't accumulate
# across chained Cloud Tasks); the web server keeps its long-lived worker.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 -c gunicorn.conf.py web_interface.fyp_data_hub:app
