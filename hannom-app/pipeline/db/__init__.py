"""Postgres data layer (self-hosted, multi-user).

Holds the Postgres-backed job queue (``PostgresJobStore``), the records
repository, and the connection/schema helpers. All of it is OPTIONAL: the
package is only imported when ``DATABASE_URL`` is set, so the SQLite/JSONL path
keeps working (and unit tests keep passing) without ``psycopg`` installed.
"""
