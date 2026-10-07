"""File organization with verified copies, audit history and collision safety."""

from __future__ import annotations

import errno
import fnmatch
import hashlib
import logging
import os
import shutil
import sqlite3
import stat
import tempfile
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import AppConfig, config_to_dict
from .history import History
from .logging_setup import configure_logging
from .models import FileInfo
from .notifications import Notifier
from .rules import matching_rule, resolve_destination

CHUNK_SIZE = 1024 * 1024


class FileChangedError(OSError):
    """The source changed while it was being inspected or copied."""


def _signature(info: FileInfo) -> tuple[int, int, int, int]:
    return info.device, info.inode, info.size, info.mtime_ns


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _open_regular(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode) or path.is_symlink():
        os.close(fd)
        raise OSError(f"Not a regular file: {path}")
    return fd


def sha256_file(path: Path) -> str:
    """Hash a stable regular file in bounded memory, without following links."""
    digest = hashlib.sha256()
    with os.fdopen(_open_regular(path), "rb") as stream:
        initial = os.fstat(stream.fileno())
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
        if _stat_signature(initial) != _stat_signature(os.fstat(stream.fileno())):
            raise FileChangedError(f"File changed while hashing: {path}")
    return digest.hexdigest()


def unique_destination(destination: Path, reserved: set[Path] | None = None) -> Path:
    """Find a free name; publication still uses an exclusive operation."""
    reserved = reserved or set()
    candidate = destination
    counter = 1
    while candidate.exists() or candidate.is_symlink() or candidate in reserved:
        candidate = destination.with_name(f"{destination.stem} ({counter}){destination.suffix}")
        counter += 1
    return candidate


def _exclusive_publish(staging: Path, destination: Path) -> None:
    try:
        os.link(staging, destination, follow_symlinks=False)
    except OSError as exc:
        # Some filesystems do not support hard links. O_EXCL still guarantees
        # that an existing name is never overwritten by the fallback copy.
        if exc.errno not in {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}:
            raise
        created = False
        try:
            with destination.open("xb") as output:
                created = True
                with staging.open("rb") as source:
                    shutil.copyfileobj(source, output, CHUNK_SIZE)
                output.flush()
                os.fsync(output.fileno())
            shutil.copystat(staging, destination, follow_symlinks=False)
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise


def _sync_directory(path: Path) -> None:
    """Persist directory entries on POSIX before removing the original."""
    if os.name == "nt":
        return  # Windows does not provide a portable fsync for directories.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def safe_move(source: Path, destination: Path, info: FileInfo, expected_hash: str) -> Path:
    """Copy, verify, publish exclusively, then unlink the unchanged source.

    The staging file lives on the target filesystem, including cross-device
    moves. Failures retain the source and remove only output owned by this call.
    """
    if _stat_signature(source.lstat()) != _signature(info) or source.is_symlink():
        raise FileChangedError(f"Source changed before copying: {source}")
    expected_parent = destination.parent.resolve()
    directories_to_sync = [destination.parent]
    ancestor = destination.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
        directories_to_sync.append(ancestor)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.parent.resolve() != expected_parent:
        raise ValueError("Destination parent changed while creating directories")
    fd, staging_name = tempfile.mkstemp(prefix=".smartfileorganizer-", dir=destination.parent)
    staging = Path(staging_name)
    published: Path | None = None
    source_removed = False
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as output, os.fdopen(_open_regular(source), "rb") as input_file:
            before = os.fstat(input_file.fileno())
            if _stat_signature(before) != _signature(info):
                raise FileChangedError(f"Source changed before copying: {source}")
            while chunk := input_file.read(CHUNK_SIZE):
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
            if _stat_signature(os.fstat(input_file.fileno())) != _signature(info):
                raise FileChangedError(f"Source changed while copying: {source}")
        if digest.hexdigest() != expected_hash:
            raise FileChangedError(f"Source contents changed while copying: {source}")
        os.chmod(staging, stat.S_IMODE(before.st_mode))
        os.utime(staging, ns=(before.st_atime_ns, before.st_mtime_ns))
        # Metadata changes also need to reach storage before publication.
        with staging.open("rb") as synced:
            os.fsync(synced.fileno())
        candidate = unique_destination(destination)
        while True:
            try:
                _exclusive_publish(staging, candidate)
                published = candidate
                break
            except FileExistsError:
                candidate = unique_destination(destination)
        if sha256_file(published) != expected_hash:
            raise OSError("Published copy failed SHA-256 verification; source retained")
        for directory in directories_to_sync:
            _sync_directory(directory)
        if _stat_signature(source.lstat()) != _signature(info) or source.is_symlink():
            raise FileChangedError(f"Source changed before unlinking: {source}")
        source.unlink()
        source_removed = True
        _sync_directory(source.parent)
        return published
    except BaseException:
        # Once unlink succeeds the published copy is the only copy we own.
        # A source-directory fsync failure must never remove that destination.
        if published is not None and not source_removed:
            # SIGINT (or a filesystem error) can arrive after unlink completed
            # but before Python assigned source_removed. Confirm the original
            # is still present before deleting any published data.
            try:
                original_present = not source.is_symlink() and _stat_signature(
                    source.lstat()
                ) == _signature(info)
            except OSError:
                original_present = False
            if original_present:
                published.unlink(missing_ok=True)
        raise
    finally:
        staging.unlink(missing_ok=True)


class RunLock:
    """An OS-managed lock prevents simultaneous real runs with the same DB."""

    def __init__(self, database: Path):
        self.path = Path(f"{database}.lock")
        self.fd: int | None = None

    def __enter__(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(self.fd).st_size == 0:
                    os.write(self.fd, b"0")
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError("Another organizer run holds the database lock") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        # Keep the inode: deleting a lock file lets another process lock a
        # different inode while the first one is still in use.


@dataclass
class RunSummary:
    mode: str = "dry-run"
    scanned: int = 0
    moved: int = 0
    duplicates: int = 0
    skipped: int = 0
    errors: int = 0
    planned: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "scanned": self.scanned,
            "moved": self.moved,
            "duplicates": self.duplicates,
            "skipped": self.skipped,
            "errors": self.errors,
            "planned": self.planned,
            "results": self.results,
        }


class Organizer:
    def __init__(self, config: AppConfig):
        self.config = config
        self.root = config.destination_root.resolve()
        self.summary = RunSummary()
        self.history: History | None = None
        self.logger: logging.Logger | None = None
        self._seen: dict[tuple[int, str], Path] = {}
        self._reserved: set[Path] = set()
        self._candidates: dict[int, set[Path]] = defaultdict(set)
        self._hashes: dict[Path, tuple[tuple[int, int, int, int], str]] = {}

    def run(
        self, mode: str = "dry-run", confirm: Callable[[Path, Path], bool] | None = None
    ) -> RunSummary:
        if mode not in {"dry-run", "interactive", "automatic"}:
            raise ValueError("mode must be dry-run, interactive or automatic")
        if mode == "interactive" and confirm is None:
            raise ValueError("interactive mode requires a confirmation callback")
        self.summary = RunSummary(mode=mode)
        self._seen.clear()
        self._reserved.clear()
        self._candidates.clear()
        self._hashes.clear()
        self.logger = None
        try:
            with ExitStack() as stack:
                if mode != "dry-run":
                    stack.enter_context(RunLock(self.config.database))
                self.history = stack.enter_context(
                    History(self.config.database, read_only=mode == "dry-run")
                )
                if mode != "dry-run":
                    self.logger = configure_logging(self.config.log_file)
                    self.history.save_config(config_to_dict(self.config))
                self._index_destination()
                for path in self._scan():
                    self.summary.scanned += 1
                    self._process(path, mode, confirm)
                if mode != "dry-run":
                    Notifier(self.config.notifications, self.logger).send(self.summary.to_dict())
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            self._error(self.config.database, exc, persist=False)
        finally:
            self.history = None
        return self.summary

    def _internal(self, path: Path) -> bool:
        absolute = path.absolute()
        # Do not call resolve on source file links: they must be skipped.
        database = self.config.database.absolute()
        log = self.config.log_file.absolute()
        if absolute in {
            database,
            log,
            Path(f"{database}.lock"),
            Path(f"{database}-wal"),
            Path(f"{database}-shm"),
            Path(f"{database}-journal"),
        }:
            return True
        if absolute.parent == log.parent and absolute.name.startswith(f"{log.name}."):
            return True
        return absolute.name.startswith(".smartfileorganizer-")

    def _excluded(self, path: Path, source: Path) -> bool:
        relative = path.relative_to(source).as_posix()
        return any(
            fnmatch.fnmatchcase(path.name, pattern)
            or fnmatch.fnmatchcase(relative, pattern)
            or path.match(pattern)
            for pattern in self.config.exclude
        )

    def _scan(self) -> Iterator[Path]:
        yielded: set[Path] = set()
        for source_config in self.config.sources:
            source = source_config.path.absolute()
            if source.is_symlink() or not source.is_dir():
                self._error(
                    source, OSError("Source is missing, is a symlink or is not a directory")
                )
                continue
            source = source.resolve()
            if source == self.root or source.is_relative_to(self.root):
                self._error(source, ValueError("Source cannot be inside the destination root"))
                continue

            def walk_error(exc: OSError) -> None:
                self._error(Path(exc.filename or source), exc)

            for current, directories, filenames in os.walk(
                source, followlinks=False, onerror=walk_error
            ):
                current_path = Path(current)
                directories[:] = (
                    sorted(
                        name
                        for name in directories
                        if not (current_path / name).is_symlink()
                        and not (current_path / name).resolve().is_relative_to(self.root)
                        and not self._excluded(current_path / name, source)
                    )
                    if source_config.recursive
                    else []
                )
                for name in sorted(filenames):
                    path = current_path / name
                    if path in yielded or self._internal(path):
                        continue
                    yielded.add(path)
                    if self._excluded(path, source):
                        self.summary.scanned += 1
                        self._skip(path, "Excluded by configuration")
                        continue
                    yield path

    def _index_destination(self) -> None:
        if not self.root.is_dir():
            return

        def walk_error(exc: OSError) -> None:
            self._error(Path(exc.filename or self.root), exc)

        for current, directories, names in os.walk(
            self.root, followlinks=False, onerror=walk_error
        ):
            parent = Path(current)
            directories[:] = [name for name in directories if not (parent / name).is_symlink()]
            for name in names:
                path = parent / name
                if path.is_symlink() or self._internal(path):
                    continue
                try:
                    value = path.stat()
                    if stat.S_ISREG(value.st_mode):
                        self._candidates[value.st_size].add(path)
                except OSError as exc:
                    self._error(path, exc)

    def _duplicate(self, path: Path, info: FileInfo, digest: str) -> Path | None:
        key = (info.size, digest)
        if key in self._seen:
            previous = self._seen[key]
            try:
                value = previous.stat()
                if (
                    not previous.is_symlink()
                    and value.st_size == info.size
                    and stat.S_ISREG(value.st_mode)
                    and sha256_file(previous) == digest
                ):
                    return previous
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._error(previous, exc)
            del self._seen[key]
        candidates = self._candidates.get(info.size, set()).copy()
        if self.history is not None:
            candidates.update(self.history.find_content(info.size, digest))
        for candidate in sorted(candidates):
            if candidate == path or candidate.is_symlink():
                continue
            try:
                value = candidate.stat()
                if value.st_size != info.size or not stat.S_ISREG(value.st_mode):
                    continue
                signature = _stat_signature(value)
                cached = self._hashes.get(candidate)
                fingerprint = (
                    cached[1] if cached and cached[0] == signature else sha256_file(candidate)
                )
                self._hashes[candidate] = (signature, fingerprint)
                if fingerprint == digest:
                    self._seen[key] = candidate
                    return candidate
            except FileNotFoundError:
                continue  # A stale history entry cannot establish a duplicate.
            except OSError as exc:
                self._error(candidate, exc)
        return None

    def _process(self, path: Path, mode: str, confirm: Callable[[Path, Path], bool] | None) -> None:
        event: dict[str, Any] = {
            "source": str(path),
            "destination": None,
            "rule": None,
            "size": None,
            "sha256": None,
            "error": None,
            "duplicate": False,
        }
        event_id: int | None = None
        try:
            if path.is_symlink():
                self._skip(path, "Symlinks are not followed")
                return
            info = FileInfo.from_path(path)
            now = datetime.now(timezone.utc)
            if (now - info.modified).total_seconds() < self.config.min_file_age_seconds:
                self._skip(path, "File is too recent; retry after it becomes stable")
                return
            rule = matching_rule(info, self.config.rules, now=now)
            if rule is None:
                self._skip(path, "No matching rule")
                return
            event.update(rule=rule.name, size=info.size)
            digest = sha256_file(path)
            if _stat_signature(path.lstat()) != _signature(info):
                raise FileChangedError(f"File changed after inspection: {path}")
            event["sha256"] = digest
            duplicate = self._duplicate(path, info, digest)
            if duplicate is not None:
                self.summary.duplicates += 1
                event["duplicate"] = True
                if self.config.duplicate_policy == "skip":
                    event.update(status="duplicate", destination=str(duplicate))
                    self._record(event)
                    return
            destination = resolve_destination(info, rule, self.root)
            if duplicate is not None and self.config.duplicate_policy == "quarantine":
                destination = self.root / self.config.duplicate_directory / path.name
            destination = unique_destination(destination, self._reserved)
            self._check_destination(destination)
            event["destination"] = str(destination)
            if mode == "interactive" and not confirm(path, destination):
                self._skip(path, "Declined by user")
                return
            if mode == "dry-run":
                self._reserved.add(destination)
                self._seen[(info.size, digest)] = path
                event["status"] = "planned"
                self.summary.planned += 1
                self._record(event)
                return
            # Audit intent is durable before touching the source. A process
            # crash leaves 'moving' for operator inspection instead of silence.
            event["status"] = "moving"
            event_id = self.history.add_event(event)
            actual_destination = safe_move(path, destination, info, digest)
            self.summary.moved += 1
            event.update(destination=str(actual_destination), status="moved")
            self.history.update_event(event_id, event)
            self.history.record_content(info.size, digest, actual_destination)
            self._seen[(info.size, digest)] = actual_destination
            self._candidates[info.size].add(actual_destination)
            self._record(event, persist=False)
        except FileChangedError as exc:
            event.update(status="skipped", error=str(exc))
            self.summary.skipped += 1
            if event_id is not None:
                self.history.update_event(event_id, event)
            self._record(event, persist=event_id is None)
        except (OSError, ValueError, RuntimeError) as exc:
            self.summary.errors += 1
            event.update(status="error", error=f"{type(exc).__name__}: {exc}")
            if event_id is not None:
                self.history.update_event(event_id, event)
            self._record(event, persist=event_id is None)

    def _check_destination(self, destination: Path) -> None:
        if not destination.parent.resolve().is_relative_to(self.root):
            raise ValueError("Destination escapes the configured root")
        if destination.is_symlink():
            raise ValueError("Destination is a symlink")

    def _skip(self, path: Path, reason: str) -> None:
        self.summary.skipped += 1
        self._record(
            {
                "source": str(path),
                "destination": None,
                "status": "skipped",
                "rule": None,
                "size": None,
                "sha256": None,
                "error": reason,
            }
        )

    def _error(self, path: Path, exc: Exception, persist: bool = True) -> None:
        self.summary.errors += 1
        self._record(
            {
                "source": str(path),
                "destination": None,
                "status": "error",
                "rule": None,
                "size": None,
                "sha256": None,
                "error": f"{type(exc).__name__}: {exc}",
            },
            persist=persist,
        )

    def _record(self, event: dict[str, Any], persist: bool = True) -> None:
        self.summary.results.append(event.copy())
        if self.logger is not None:
            self.logger.log(
                logging.ERROR if event["status"] == "error" else logging.INFO,
                "file_event",
                extra={"event": event},
            )
        if persist and self.summary.mode != "dry-run" and self.history is not None:
            self.history.add_event(event)
