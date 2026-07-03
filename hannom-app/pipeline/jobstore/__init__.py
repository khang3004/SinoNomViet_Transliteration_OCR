"""Job store package — SQLite-backed job queue decoupling app ⇄ worker.

The clean ``JobStore`` API here is deliberately self-contained so that an
external scheduler (e.g. a future Airflow DAG, AGENTS.md §9) can enqueue jobs
the worker already consumes — Airflow would orchestrate, the worker still
executes. No Airflow code in this task.
"""

from pipeline.jobstore.store import Job, JobStore, JobStatus

__all__ = ["Job", "JobStore", "JobStatus", "get_store"]


def get_store(config):
    """Return the job store for the current config.

    ``DATABASE_URL`` set → Postgres (``PostgresJobStore``, multi-worker safe);
    otherwise the local SQLite ``JobStore`` (dev / tests, no ``psycopg`` needed).
    Both satisfy the same interface, so app/worker code is identical either way.
    """
    dsn = getattr(config, "database_url", "")
    if dsn:
        from pipeline.db.postgres_store import PostgresJobStore

        return PostgresJobStore(dsn)
    return JobStore(config.jobs_db)
