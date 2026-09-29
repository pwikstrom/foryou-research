"""Background-task runtime: how workers are registered, launched and tracked.

``worker_registry`` is the one worker table; ``process_manager`` launches a
worker (a local subprocess, or a Cloud Task on Cloud Run); ``task_status``,
``run_logs`` and ``task_failures`` record what it did; ``worker_runner`` is the
shared worker skeleton; ``drain_lease`` arbitrates local vs cloud scraper
drains. The workers themselves live in ``web_interface/workers/``.
"""
