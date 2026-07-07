"""Gunicorn config — recycle the task-runner worker after every request.

The background task-runner (``K_SERVICE == "fyp-task-runner"``) runs each Cloud
Task synchronously in the gunicorn worker. yt-dlp/ffmpeg (and other native
libraries) accumulate memory across the many per-item invocations in a single
long-lived process, so a worker that survives across chained Cloud Tasks would
carry that growth from one batch into the next and eventually get OOM-killed.

Setting ``max_requests = 1`` on the task-runner makes gunicorn gracefully
recycle the worker after each request, so every Cloud Task — including each
self-chained scrape batch — starts in a fresh process with reset memory. The
re-import cost (a few seconds) is negligible against multi-minute task
durations. The web server (``fyp-data-hub``) is unaffected: it keeps its
long-lived worker.

Only ``max_requests`` is set here; ``bind`` / ``workers`` / ``threads`` /
``timeout`` continue to come from the CLI flags in the Dockerfile CMD.
"""

import os

if os.environ.get("K_SERVICE") == "fyp-task-runner":
    max_requests = 1
    max_requests_jitter = 0
