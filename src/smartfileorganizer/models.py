"""Typed configuration and file metadata shared by the application."""

from __future__ import annotations

import mimetypes
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceConfig:
    path: Path
    recursive: bool = True


@dataclass(frozen=True)
class Rule:
    name: str
    destination: str
    match: dict[str, Any]
    enabled: bool = True


@dataclass(frozen=True)
class AppConfig:
    sources: list[SourceConfig]
    destination_root: Path
    rules: list[Rule]
    database: Path
    log_file: Path
    duplicate_policy: str = "skip"
    duplicate_directory: str = "Duplicates"
    exclude: list[str] = field(default_factory=lambda: ["*.part", "*.crdownload", "*.tmp"])
    min_file_age_seconds: float = 2.0
    notifications: dict[str, Any] = field(default_factory=dict)
    schedule: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FileInfo:
    path: Path
    size: int
    modified: datetime
    created: datetime
    creation_is_fallback: bool
    mime: str
    mtime_ns: int
    device: int
    inode: int

    @property
    def extension(self) -> str:
        """The final extension without its dot, normalized to lowercase."""
        return self.path.suffix.removeprefix(".").lower()

    @property
    def name(self) -> str:
        return self.path.name

    @classmethod
    def from_path(cls, path: Path) -> FileInfo:
        """Read metadata without following symlinks or treating ctime as creation.

        Filesystems without birth time use modification time as a documented
        fallback. MIME detection uses the filename's extension, not file content.
        """
        path = Path(path)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Only regular files are eligible; symlinks are excluded")
        birth_time = getattr(metadata, "st_birthtime", None)
        return cls(
            path=path,
            size=metadata.st_size,
            modified=datetime.fromtimestamp(metadata.st_mtime, timezone.utc),
            created=datetime.fromtimestamp(
                metadata.st_mtime if birth_time is None else birth_time,
                timezone.utc,
            ),
            creation_is_fallback=birth_time is None,
            mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            mtime_ns=metadata.st_mtime_ns,
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )


def from_path(path: Path) -> FileInfo:
    """Convenience alias for :meth:`FileInfo.from_path`."""
    return FileInfo.from_path(path)
