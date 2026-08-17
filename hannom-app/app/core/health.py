"""Host telemetry for the monitoring UI.

The VPS is 4 vCPU / 8 GB, and RAM is the binding constraint on a multi-hour OCR
run — so these numbers are not decoration, they are how you see an OOM coming.
Lives in core so the CLI can print them too.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any


def _psutil():
    try:
        import psutil

        return psutil
    except ModuleNotFoundError:
        return None


def memory() -> dict[str, Any]:
    ps = _psutil()
    if ps is None:
        return {"available": False}
    vm = ps.virtual_memory()
    return {
        "available": True,
        "total_mb": round(vm.total / 1048576),
        "used_mb": round((vm.total - vm.available) / 1048576),
        "free_mb": round(vm.available / 1048576),
        "percent": vm.percent,
    }


def cpu() -> dict[str, Any]:
    ps = _psutil()
    if ps is None:
        return {"available": False, "count": os.cpu_count() or 0}
    return {
        "available": True,
        "count": ps.cpu_count() or os.cpu_count() or 0,
        # interval=None -> non-blocking, comparing against the previous call.
        # A blocking sample would stall the event loop on every poll.
        "percent": ps.cpu_percent(interval=None),
        "per_cpu": ps.cpu_percent(interval=None, percpu=True),
        "load_avg": list(getattr(os, "getloadavg", lambda: (0, 0, 0))()),
    }


def disk(path: Path) -> dict[str, Any]:
    target = path if path.exists() else path.parent
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return {"available": False}
    return {
        "available": True,
        "total_gb": round(usage.total / 1073741824, 1),
        "used_gb": round(usage.used / 1073741824, 1),
        "free_gb": round(usage.free / 1073741824, 1),
        "percent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
    }


def process_tree() -> dict[str, Any]:
    """This process plus its OCR workers, with per-worker RSS.

    Per-worker RSS is the number that predicts failure: 3 workers at ~1 GB each
    on an 8 GB box is fine, 3 at 2 GB is not.
    """
    ps = _psutil()
    if ps is None:
        return {"available": False, "workers": []}

    try:
        me = ps.Process()
    except Exception:  # noqa: BLE001
        return {"available": False, "workers": []}

    workers = []
    total = 0.0
    for child in me.children(recursive=True):
        try:
            rss = child.memory_info().rss / 1048576
        except Exception:  # noqa: BLE001 - worker exited mid-sample
            continue
        total += rss
        workers.append({"pid": child.pid, "rss_mb": round(rss, 1)})

    try:
        own = me.memory_info().rss / 1048576
    except Exception:  # noqa: BLE001
        own = 0.0

    return {
        "available": True,
        "main_rss_mb": round(own, 1),
        "workers": workers,
        "worker_count": len(workers),
        "worker_rss_mb": round(total, 1),
        "total_rss_mb": round(own + total, 1),
    }


def image_store(images_dir: Path) -> dict[str, Any]:
    """Count and size of downloaded images awaiting the Gemini stage."""
    if not images_dir.exists():
        return {"count": 0, "bytes": 0, "mb": 0.0}
    count = 0
    total = 0
    for entry in images_dir.rglob("*"):
        if entry.is_file() and not entry.name.endswith(".part"):
            count += 1
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return {"count": count, "bytes": total, "mb": round(total / 1048576, 1)}


def snapshot(data_dir: Path, images_dir: Path) -> dict[str, Any]:
    return {
        "ts": time.time(),
        "memory": memory(),
        "cpu": cpu(),
        "disk": disk(data_dir),
        "processes": process_tree(),
    }
