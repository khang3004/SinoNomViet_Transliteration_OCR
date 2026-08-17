"""Headless batch runner.

This exists to prove the decoupling is real: it drives the full pipeline with no
FastAPI, no uvicorn, no web layer at all. If this file ever needs a web import,
the ``core``/``api`` split has broken.

    python -m app.cli --limit 100
    python -m app.cli --preflight-only
    python -m app.cli --health
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from app.core.batch import BatchRunner
from app.core.config import describe_secrets, load_settings
from app.core.health import image_store, snapshot
from app.core.jobstore import JobStore, new_run_id
from app.core.signing import ImageUrlSigner
from app.core.sink import MinioResultSink
from app.core.source import MinioRecordSource
from app.core.storage import MinioStorage

log = logging.getLogger("hannom.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.cli", description="Run one Han-scan batch without the web app."
    )
    parser.add_argument("--limit", type=int, default=None, help="max posts (default BATCH_SIZE)")
    parser.add_argument(
        "--confirm-expired", action="store_true",
        help="proceed even when many source URLs have already expired",
    )
    parser.add_argument(
        "--preflight-only", action="store_true",
        help="report the expiry audit and pending counts, then exit",
    )
    parser.add_argument("--health", action="store_true", help="print host telemetry and exit")
    parser.add_argument("--check", action="store_true", help="verify MinIO connectivity and exit")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


async def run_batch(args) -> int:
    settings = load_settings()
    storage = MinioStorage(settings.minio)
    source = MinioRecordSource(settings, storage=storage)
    sink = MinioResultSink(settings, storage=storage)
    signer = ImageUrlSigner(
        base_url=settings.images.public_base_url,
        secret=settings.images.signing_secret,
        ttl_days=settings.images.ttl_days,
    )

    limit = args.limit or settings.batch_size

    if args.preflight_only:
        from app.core.parser import preflight_expiry

        batch = await asyncio.to_thread(source.iter_pending, limit)
        report = {
            "pending_posts": len(batch.posts),
            "corpus_total": batch.corpus_total,
            "processed_total": batch.processed_total,
            "crawl_runs": batch.runs_total,
            "malformed_lines": batch.malformed_lines,
            "expiry": preflight_expiry(batch.posts) if batch.posts else {},
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    settings.images_dir.mkdir(parents=True, exist_ok=True)

    store = JobStore(settings.jobs_dir)
    job_dir, state = store.create(new_run_id(), limit=limit)
    print(f"job {state.job_id} starting (limit={limit})", file=sys.stderr)

    runner = BatchRunner(settings, source, sink, signer)
    state = await runner.run(job_dir, state, confirm_expired=args.confirm_expired)

    summary = {
        "job_id": state.job_id,
        "phase": state.phase.value,
        "counts": state.counts.__dict__,
        "preflight": state.preflight,
        "error": state.error,
        "awaiting_confirmation": state.awaiting_confirmation,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if state.awaiting_confirmation:
        print(
            "\nPaused: too many source URLs have already expired. "
            "Re-crawl, or re-run with --confirm-expired to scan anyway.",
            file=sys.stderr,
        )
        return 2
    return 0 if state.phase.value == "done" else 1


def check_connectivity() -> int:
    settings = load_settings()
    if not settings.minio.endpoint:
        print("MINIO_ENDPOINT is not set.", file=sys.stderr)
        return 1

    storage = MinioStorage(settings.minio)
    source = MinioRecordSource(settings, storage=storage)
    try:
        runs = source.list_run_ids()
        total = source.corpus_total()
    except Exception as exc:  # noqa: BLE001
        print(f"MinIO unreachable: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "\nIf the endpoint is a *.svc.cluster.local address, it will not "
            "resolve from outside the k3s cluster — use the tailnet-reachable "
            "NodePort or ingress address instead.",
            file=sys.stderr,
        )
        return 1

    print(json.dumps({
        "endpoint": settings.minio.endpoint,
        "bucket": settings.minio.bucket,
        "group_prefix": settings.minio.group_prefix,
        "crawl_runs": len(runs),
        "latest_run": runs[-1] if runs else None,
        "corpus_total": total,
        "secrets_present": describe_secrets(),
    }, indent=2))
    return 0


def print_health() -> int:
    settings = load_settings()
    data = snapshot(settings.data_dir, settings.images_dir)
    data["images"] = image_store(settings.images_dir)
    print(json.dumps(data, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )

    if args.health:
        return print_health()
    if args.check:
        return check_connectivity()
    return asyncio.run(run_batch(args))


if __name__ == "__main__":
    raise SystemExit(main())
