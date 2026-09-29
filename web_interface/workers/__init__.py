"""The background workers: one ``run_<name>.py`` module per worker.

Each module's ``run_<name>(reporter, task_args)`` entry point runs as a Cloud
Task on the task-runner service, or locally as a subprocess
(``python -m web_interface.workers.run_<name>``). The worker table that wires
them up is ``web_interface/tasks/worker_registry.py``.
"""
