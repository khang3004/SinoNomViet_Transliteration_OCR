"""Command line for setup and inspection, without the web layer.

Deliberately imports nothing from ``app.api``: if this file ever needs FastAPI to
run, the core/adapter split has been broken. ``--check`` enforces that by
importing every core module.

    python -m app.cli --hash-password           # for APP_PASSWORD_HASH
    python -m app.cli --check
    python -m app.cli --ingest --jsonl gt.jsonl --xlsx gt.xlsx
    python -m app.cli --stats
    python -m app.cli --add-user mai --password '...' --display 'Mai'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def _settings():
    from app.core.config import load_settings

    return load_settings()


def cmd_hash_password(password: str) -> int:
    from app.core.users import hash_password

    if not password:
        import getpass

        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat: "):
            print("Passwords do not match.", file=sys.stderr)
            return 1
    if len(password) < 8:
        print("Use at least 8 characters.", file=sys.stderr)
        return 1
    print(hash_password(password))
    return 0


def cmd_check() -> int:
    from app.core import audit, corpus, drive, metrics, postid, sampling, users  # noqa: F401
    from app.core.config import describe_secrets

    settings = _settings()
    print("core modules import cleanly (no web framework)")
    print(f"data dir      : {settings.data_dir}")
    print(f"drive folder  : {settings.drive.folder_id or '(not set)'}")
    print(f"public base   : {settings.images.public_base_url or '(not set)'}")
    print(f"sample batch  : {settings.sampling.default_batch}")
    print(f"per-post cap  : {settings.sampling.per_post_cap}")
    print("targets       : " + ", ".join(
        f"{b.value}={w:.0%}" for b, w in
        sampling.normalize_targets(settings.sampling.targets).items()
    ))
    print("secrets       : " + ", ".join(
        f"{k}={'set' if v else 'MISSING'}" for k, v in describe_secrets().items()
    ))

    index = drive.DriveIndex(
        settings.drive_index_path, settings.drive.folder_id, settings.drive.api_key
    )
    print(f"drive index   : {len(index.load())} files")

    missing = [k for k, v in describe_secrets().items()
               if not v and k in {"AUTH_SECRET", "APP_PASSWORD_HASH"}]
    if missing:
        print(f"\nThe app will refuse to start: {', '.join(missing)} not set.")
        return 1
    return 0


def cmd_ingest(jsonl: str, xlsx: str) -> int:
    from app.core import corpus
    from app.core.audit import AuditStore

    settings = _settings()
    jsonl_path = Path(jsonl) if jsonl else settings.uploads_dir / "ground_truth.jsonl"
    xlsx_path = Path(xlsx) if xlsx else settings.uploads_dir / "ground_truth.xlsx"

    if not jsonl_path.exists() and not xlsx_path.exists():
        print(f"Neither {jsonl_path} nor {xlsx_path} exists.", file=sys.stderr)
        return 1

    records, report = corpus.build(
        jsonl_path if jsonl_path.exists() else None,
        xlsx_path if xlsx_path.exists() else None,
    )
    if not records:
        print("No reviewable records produced.", file=sys.stderr)
        print(json.dumps(report.to_json(), indent=2, ensure_ascii=False))
        return 1

    AuditStore(settings.data_dir).corpus.replace(records, report)
    print(json.dumps(report.to_json(), indent=2, ensure_ascii=False))
    return 0


def cmd_stats() -> int:
    from app.core.audit import AuditStore

    store = AuditStore(_settings().data_dir)
    print(json.dumps(store.progress(), indent=2, ensure_ascii=False))
    for row in store.per_reviewer():
        print(
            f"  {row['username']:<16} {row['reviewed']:>5} reviewed / "
            f"{row['assigned']:>5} claimed"
        )
    return 0


def cmd_add_user(name: str, password: str, display: str, role: str) -> int:
    from app.core.users import UserError, UserStore

    settings = _settings()
    store = UserStore(
        settings.users_path, super_admin=os.environ.get("APP_USERNAME", "admin")
    )
    if not password:
        import getpass

        password = getpass.getpass("Password: ")
    try:
        user = store.create(name, password, role=role, display_name=display,
                            created_by="cli")
    except UserError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"created {user.username} ({user.role})")
    return 0


def cmd_drive_index() -> int:
    from app.core.drive import DriveIndex

    settings = _settings()
    index = DriveIndex(
        settings.drive_index_path, settings.drive.folder_id, settings.drive.api_key
    )
    report = asyncio.run(index.refresh())
    if report.error:
        print(report.error, file=sys.stderr)
        return 1
    print(json.dumps(report.to_json(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify configuration")
    parser.add_argument("--hash-password", action="store_true",
                        help="print a bcrypt hash for APP_PASSWORD_HASH")
    parser.add_argument("--ingest", action="store_true", help="build the corpus")
    parser.add_argument("--drive-index", action="store_true",
                        help="list the Drive folder into the local index")
    parser.add_argument("--stats", action="store_true", help="print audit progress")
    parser.add_argument("--add-user", metavar="NAME", default="")
    parser.add_argument("--jsonl", default="", help="path to ground_truth.jsonl")
    parser.add_argument("--xlsx", default="", help="path to ground_truth.xlsx")
    parser.add_argument("--password", default="")
    parser.add_argument("--display", default="")
    parser.add_argument("--role", default="reviewer", choices=["reviewer", "admin"])
    args = parser.parse_args(argv)

    if args.hash_password:
        return cmd_hash_password(args.password)
    if args.check:
        return cmd_check()
    if args.ingest:
        return cmd_ingest(args.jsonl, args.xlsx)
    if args.drive_index:
        return cmd_drive_index()
    if args.stats:
        return cmd_stats()
    if args.add_user:
        return cmd_add_user(args.add_user, args.password, args.display, args.role)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
