"""Append-only JSONL logs, fsynced.

Assignments and reviews are recorded as events rather than as mutable rows. That
choice buys three things at once: a reviewer's edit never destroys what they
said before, two writers cannot interleave into a corrupted record, and a crash
loses at most the line being written.

Reading folds the log down to current state — last write per key wins. At the
volumes here (tens of thousands of lines) that fold is milliseconds, and the
result is cached until the file changes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Iterator

log = logging.getLogger(__name__)


class JsonlLog:
    """One append-only file. Safe for concurrent readers in one process."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cache: list[dict[str, Any]] | None = None
        self._signature: tuple[float, int] | None = None

    # --- writing ---------------------------------------------------------

    def append(self, row: dict[str, Any]) -> None:
        self.extend([row])

    def extend(self, rows: list[dict[str, Any]]) -> None:
        """Write rows and fsync before returning.

        The fsync is the point: without it an OS-level crash can lose an
        acknowledged review, and a reviewer who saw "saved" would have to be
        told to do the work again.
        """
        if not rows:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._cache = None
            self._signature = None

    # --- reading ---------------------------------------------------------

    def _stat_signature(self) -> tuple[float, int] | None:
        try:
            info = self.path.stat()
        except OSError:
            return None
        return (info.st_mtime, info.st_size)

    def rows(self) -> list[dict[str, Any]]:
        """Every row, in write order. Cached until the file changes."""
        signature = self._stat_signature()
        if signature is None:
            self._cache, self._signature = [], None
            return []
        if self._cache is not None and self._signature == signature:
            return self._cache

        rows: list[dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    # A torn final line from a crash mid-write. Every complete
                    # line before it is still good, so keep going.
                    log.warning("skipping malformed line in %s", self.path.name)
                    continue
                if isinstance(data, dict):
                    rows.append(data)

        self._cache, self._signature = rows, signature
        return rows

    def fold(self, key: Callable[[dict[str, Any]], str]) -> dict[str, dict[str, Any]]:
        """Latest row per key — the current state of the log."""
        state: dict[str, dict[str, Any]] = {}
        for row in self.rows():
            try:
                state[key(row)] = row
            except (KeyError, TypeError):
                continue
        return state

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.rows())

    def __len__(self) -> int:
        return len(self.rows())


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Replace a file wholesale, atomically.

    Used for snapshots (the normalized corpus, exports) rather than logs. The
    temp-then-rename means a reader never sees a half-written file, and an
    interrupted write leaves the previous version intact.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default
