"""SQLite audit history, configuration snapshots and content fingerprints."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class History:
    """A process-local connection. Read-only access never creates state."""

    def __init__(self, path: Path, read_only: bool = False):
        self.path = Path(path).expanduser().resolve()
        self.read_only = read_only
        self.connection: sqlite3.Connection | None = None
        if read_only and not self.path.exists():
            return
        if read_only:
            # SQLite's mode=ro can still create WAL/SHM sidecars. Inspect the
            # journal flag before connecting so previews never create state.
            with self.path.open("rb") as stream:
                header = stream.read(20)
            if header[18:20] == b"\x02\x02":
                raise RuntimeError(
                    "Run automatic once to migrate WAL history; "
                    "read-only access cannot create sidecars"
                )
        try:
            if read_only:
                self.connection = sqlite3.connect(
                    f"{self.path.as_uri()}?mode=ro", uri=True, timeout=10
                )
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.connection = sqlite3.connect(self.path, timeout=10)
            self.connection.row_factory = sqlite3.Row
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError(f"Unsupported history schema version: {version}")
            self.connection.execute("PRAGMA busy_timeout=10000")
            if not read_only:
                # Real organization is already serialized by RunLock. DELETE
                # also keeps reads free of SQLite-created WAL/SHM sidecars.
                self.connection.execute("PRAGMA journal_mode=DELETE")
                self.connection.execute("PRAGMA synchronous=FULL")
                self.connection.executescript(
                    """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    source TEXT NOT NULL,
                    destination TEXT,
                    status TEXT NOT NULL,
                    rule TEXT,
                    size INTEGER,
                    sha256 TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS contents (
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    path TEXT NOT NULL,
                    PRIMARY KEY (size, sha256, path)
                );
                CREATE INDEX IF NOT EXISTS contents_by_size ON contents(size);
                CREATE TABLE IF NOT EXISTS configuration (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                PRAGMA user_version=1;
                    """
                )
                self.connection.commit()
        except BaseException:
            # __enter__ has not run yet; an ExitStack cannot close this failed
            # constructor's connection for us.
            self.close()
            raise

    def __enter__(self) -> History:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _writer(self) -> sqlite3.Connection:
        if self.read_only or self.connection is None:
            raise RuntimeError("History is read-only or closed")
        return self.connection

    def add_event(self, event: dict[str, Any]) -> int:
        conn = self._writer()
        fields = ("source", "destination", "status", "rule", "size", "sha256", "error")
        values = [
            str(event[key]) if isinstance(event.get(key), Path) else event.get(key)
            for key in fields
        ]
        with conn:
            cursor = conn.execute(
                "INSERT INTO events "
                "(timestamp, source, destination, status, rule, size, sha256, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [datetime.now(timezone.utc).isoformat(), *values],
            )
        return int(cursor.lastrowid)

    def update_event(self, event_id: int, event: dict[str, Any]) -> None:
        conn = self._writer()
        fields = ("destination", "status", "error")
        with conn:
            conn.execute(
                "UPDATE events SET destination=?, status=?, error=? WHERE id=?",
                [
                    *(
                        str(event[k]) if isinstance(event.get(k), Path) else event.get(k)
                        for k in fields
                    ),
                    event_id,
                ],
            )

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if self.connection is None:
            return []
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            )
        ]

    def save_config(self, config: dict[str, Any]) -> None:
        conn = self._writer()
        with conn:
            conn.execute(
                "INSERT INTO configuration (id, payload, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (
                    json.dumps(config, ensure_ascii=False, sort_keys=True),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def get_config(self) -> dict[str, Any] | None:
        if self.connection is None:
            return None
        row = self.connection.execute("SELECT payload FROM configuration WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def record_content(self, size: int, sha256: str, path: Path) -> None:
        conn = self._writer()
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO contents (size, sha256, path) VALUES (?, ?, ?)",
                (size, sha256, str(Path(path).absolute())),
            )

    def find_content(self, size: int, sha256: str) -> list[Path]:
        if self.connection is None:
            return []
        return [
            Path(row[0])
            for row in self.connection.execute(
                "SELECT path FROM contents WHERE size=? AND sha256=? ORDER BY path",
                (size, sha256),
            )
        ]

    def recent_size(self, size: int) -> list[Path]:
        if self.connection is None:
            return []
        return [
            Path(row[0])
            for row in self.connection.execute(
                "SELECT DISTINCT path FROM contents WHERE size=? ORDER BY path", (size,)
            )
        ]

    def is_known_path(self, path: Path) -> bool:
        if self.connection is None:
            return False
        return (
            self.connection.execute(
                "SELECT 1 FROM contents WHERE path=? LIMIT 1", (str(Path(path).absolute()),)
            ).fetchone()
            is not None
        )
