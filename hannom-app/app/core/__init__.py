"""Review core — pure logic with no web framework dependency.

This package must never import FastAPI, uvicorn, or anything from ``app.api``.
That constraint is what lets the same code be driven headlessly (see
``app/cli.py``) and keeps the sampling and accuracy rules testable without a
server.
"""
