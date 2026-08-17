"""Han-scan core — pure pipeline logic with no web framework dependency.

This package must never import FastAPI, uvicorn, or anything from ``app.api``.
That constraint is what lets the crawl pipeline drive this stage headlessly
(see ``app/cli.py``) and swap the MinIO I/O for something else.
"""
