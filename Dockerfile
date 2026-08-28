# Thin application layer on top of the base image built from Dockerfile.base
# (Python 3.12, gcc, Rust, and all pinned pip dependencies).
#
# Build the base image first, then this one:
#   docker build -f Dockerfile.base -t foryou-hub-base:latest .
#   docker build -t foryou-hub:latest .
#
# To pull the base from a registry instead, override the ARG:
#   docker build --build-arg BASE_IMAGE=<registry>/<project>/foryou-hub-base:latest -t foryou-hub:latest .
ARG BASE_IMAGE=foryou-hub-base:latest
FROM ${BASE_IMAGE}

WORKDIR /app

# Copy application code only
COPY . .

# Pre-compile the application's own modules to bytecode. pip already compiles
# site-packages at install time, but code arriving via COPY does not, so without
# this every cold start pays to compile ~200 modules on first import before the
# app can serve its first request. Scoped to the two imported packages so a
# stray non-importable file elsewhere in the tree cannot fail the build.
RUN python -m compileall -q fyp web_interface

# The Launch Command
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 web_interface.fyp_data_hub:app
