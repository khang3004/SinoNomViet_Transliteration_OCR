"""Job store package — SQLite-backed job queue decoupling app ⇄ worker.

The clean ``JobStore`` API here is deliberately self-contained so that an
external scheduler (e.g. a future Airflow DAG, AGENTS.md §9) can enqueue jobs
the worker already consumes — Airflow would orchestrate, the worker still
executes. No Airflow code in this task.
"""

from pipeline.jobstore.store import Job, JobStore, JobStatus

__all__ = ["Job", "JobStore", "JobStatus"]
