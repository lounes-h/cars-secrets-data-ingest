"""File-system side of the pipeline: which files to pick up, when they are complete, where to."""

from __future__ import annotations

import gzip
import hashlib
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

# Browsers and download tools write to these first and rename when done.
PARTIAL_SUFFIXES = (".part", ".partial", ".tmp", ".crdownload", ".download")


def is_candidate(path: Path) -> bool:
    name = path.name.lower()
    return (
        not name.startswith(".")
        and not name.endswith(PARTIAL_SUFFIXES)
        and name.endswith(".csv")
    )


def provider_of(path: Path, inbox: Path) -> str | None:
    """Files must sit directly in inbox/<provider>/; anything else has no provider."""
    try:
        rel = path.relative_to(inbox)
    except ValueError:
        return None
    return rel.parts[0] if len(rel.parts) == 2 else None


def signature(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return st.st_size, st.st_mtime_ns


def wait_until_stable(
    path: Path, stable_seconds: float, *, poll: float = 0.5, timeout: float = 600,
    clock=time.monotonic, sleep=time.sleep,
) -> bool:
    """True once size and mtime stop changing for `stable_seconds`; False if the file vanished
    or never settled within `timeout` (still being downloaded)."""
    last = signature(path)
    if last is None:
        return False
    stable_since = clock()
    deadline = stable_since + timeout
    while clock() - stable_since < stable_seconds:
        if clock() > deadline:
            return False
        sleep(poll)
        current = signature(path)
        if current is None:
            return False
        if current != last or current[0] == 0:
            last, stable_since = current, clock()
    return True


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def archive(path: Path, archive_dir: Path, source: str, file_id: int) -> Path:
    """Gzip into archive/<source>/YYYY/MM/DD/<id>_<name>.gz and delete the original."""
    day = datetime.now(UTC)
    target_dir = archive_dir / source / f"{day:%Y/%m/%d}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{file_id}_{path.name}.gz"
    tmp = target.with_suffix(".gz.tmp")
    with open(path, "rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
    tmp.replace(target)
    path.unlink()
    return target


def move_to(path: Path, directory: Path, source: str, file_id: int) -> Path:
    target_dir = directory / source
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{file_id}_{path.name}"
    shutil.move(str(path), target)
    return target


def purge_archive(archive_dir: Path, retention_days: int, *, now: float | None = None) -> int:
    """Delete archived files older than the retention period. Returns how many were deleted."""
    cutoff = (now if now is not None else time.time()) - retention_days * 86400
    deleted = 0
    for f in archive_dir.rglob("*.gz"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            deleted += 1
    return deleted
