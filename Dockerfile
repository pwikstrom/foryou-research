# Thin app layer on top of pre-built base image.
# Base image has Python 3.12, Rust, gcc, and all pip dependencies.
# To rebuild the base image (only needed when requirements312.txt changes):
#   gcloud builds submit --tag australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-base:latest -f Dockerfile.base .
FROM australia-southeast1-docker.pkg.dev/<gcp-project>/cloud-run-source-deploy/fyp-base:latest

WORKDIR /app

# Copy application code only
COPY . .

# The Launch Command
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 web_interface.fyp_data_hub:app
