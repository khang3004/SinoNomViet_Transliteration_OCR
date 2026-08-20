"""Headless batch runner.

This exists to prove the decoupling is real: it drives the full pipeline with no
FastAPI, no uvicorn, no web layer at all. If this file ever needs a web import,
the ``core``/``api`` split has broken.

    python -m app.cli --file exports/valid_post.jsonl --limit 100
    python -m app.cli --preflight-only --file exports/valid_post.jsonl
    python -m app.cli --minio --limit 100
    python -m app.cli --health
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from app.core.batch import BatchRunner
from app.core.checkpoint import ProcessedCheckpoint
from app.core.config import Settings, describe_secrets, load_settings
from app.core.health import image_store, snapshot
from app.core.jobstore import JobStore, new_run_id
from app.core.signing import ImageUrlSigner
from app.core.sink import FileResultSink, MinioResultSink, TeeResultSink
from app.core.source import FileRecordSource, MinioRecordSource
from app.core.storage import MinioStorage
from app.core.uploads import UploadStore

log = logging.getLogger("hannom.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.cli", description="Prepare one batch of images without the web app."
    )
    parser.add_argument(
        "--file", type=Path, default=None,
        help="path to a valid_post.jsonl (default: the most recent upload)",
    )
    parser.add_argument(
        "--minio", action="store_true",
        help="read from the crawler's MinIO by_run logs instead of a file",
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
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser


def _build_source(args, settings: Settings):
    """Pick an input, mirroring what the web layer does."""
    if args.minio:
        if not settings.minio_enabled:
            raise SystemExit(
                "--minio needs MINIO_ENDPOINT and MINIO_GROUP_PREFIX set. "
                "Omit it to scan an uploaded valid_post.jsonl instead."
            )
        return MinioRecordSource(settings, storage=MinioStorage(settings.minio))

    checkpoint = ProcessedCheckpoint(settings.checkpoint_path)
    if args.file is not None:
        if not args.file.exists():
            raise SystemExit(f"no such file: {args.file}")
        return FileRecordSource(args.file, checkpoint, settings)

    upload = UploadStore(settings.uploads_dir).latest()
    if upload is None:
        raise SystemExit(
            "No upload found and no --file given. Upload a valid_post.jsonl from "
            "the dashboard, or pass --file /path/to/valid_post.jsonl."
        )
    return FileRecordSource(upload.path, checkpoint, settings)


def _build_sink(settings: Settings):
    """Local always; MinIO too when configured."""
    local = FileResultSink(settings.results_dir)
    if not settings.minio_enabled:
        return local
    return TeeResultSink(local, MinioResultSink(settings, storage=MinioStorage(settings.minio)))


async def run_batch(args) -> int:
    settings = load_settings()
    source = _build_source(args, settings)
    sink = _build_sink(settings)
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
            "source": "minio" if args.minio else "file",
            "pending_posts": len(batch.posts),
            "corpus_total": batch.corpus_total,
            "processed_total": batch.processed_total,
            "malformed_lines": batch.malformed_lines,
            "expiry": preflight_expiry(batch.posts) if batch.posts else {},
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    for directory in (
        settings.jobs_dir, settings.images_dir,
        settings.state_dir, settings.results_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    store = JobStore(settings.jobs_dir)
    job_dir, state = store.create(new_run_id(), limit=limit)
    print(f"job {state.job_id} starting (limit={limit})", file=sys.stderr)

    runner = BatchRunner(settings, source, sink, signer)
    state = await runner.run(
        job_dir, state, confirm_expired=args.confirm_expired
    )

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
    """Report configuration status. MinIO is optional, so its absence is
    reported rather than treated as a failure."""
    settings = load_settings()
    report = {
        "mode": "minio" if settings.minio_enabled else "upload",
        "secrets_present": describe_secrets(),
        "data_dir": str(settings.data_dir),
    }

    upload = UploadStore(settings.uploads_dir).latest()
    if upload is None:
        report["upload"] = {"present": False}
    else:
        checkpoint = ProcessedCheckpoint(settings.checkpoint_path)
        source = FileRecordSource(upload.path, checkpoint, settings)
        report["upload"] = {"present": True, **upload.to_json(), **source.stats()}

    report["results"] = FileResultSink(settings.results_dir).counts()

    if not settings.minio_enabled:
        report["minio"] = {
            "configured": False,
            "note": "Upload mode. Set MINIO_ENDPOINT and MINIO_GROUP_PREFIX to "
                    "also read the crawler's by_run logs directly.",
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    try:
        source = MinioRecordSource(settings, storage=MinioStorage(settings.minio))
        runs = source.list_run_ids()
        report["minio"] = {
            "configured": True, "connected": True,
            "endpoint": settings.minio.endpoint,
            "bucket": settings.minio.bucket,
            "group_prefix": settings.minio.group_prefix,
            "crawl_runs": len(runs),
            "latest_run": runs[-1] if runs else None,
        }
    except Exception as exc:  # noqa: BLE001
        report["minio"] = {
            "configured": True, "connected": False,
            "endpoint": settings.minio.endpoint,
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        print(
            "\nMinIO is unreachable. A *.svc.cluster.local address will not "
            "resolve from outside the k3s cluster — use a tailnet-reachable "
            "NodePort (30000-32767) or ingress address. Upload mode works "
            "regardless, so this is not blocking.",
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report, indent=2, ensure_ascii=False))
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
