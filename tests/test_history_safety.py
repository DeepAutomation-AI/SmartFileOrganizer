"""Regression checks for non-mutating reads and safe SQLite initialization."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from smartfileorganizer.history import History


def _event(source: str = "report.pdf") -> dict:
    return {"source": source, "destination": "Documents/report.pdf", "status": "moved"}


def _wal_history(path: Path) -> None:
    with History(path) as history:
        history.add_event(_event())
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    connection.close()
    assert path.read_bytes()[18:20] == b"\x02\x02"


def test_existing_read_only_history_creates_no_sidecars(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    with History(path) as history:
        history.add_event(_event())
        history.record_content(5, "fingerprint", tmp_path / "report.pdf")
        history.save_config({"sources": ["inbox"]})
        assert history.connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert history.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert history.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
    original = path.read_bytes()
    before = set(tmp_path.iterdir())

    with History(path, read_only=True) as history:
        assert history.recent()[0]["source"] == "report.pdf"
        assert history.find_content(5, "fingerprint") == [tmp_path / "report.pdf"]
        assert history.get_config() == {"sources": ["inbox"]}
        assert set(tmp_path.iterdir()) == before

    assert path.read_bytes() == original
    assert set(tmp_path.iterdir()) == before == {path}


@pytest.mark.parametrize("read_only", [False, True])
def test_future_schema_version_is_rejected_without_modification(
    tmp_path: Path, read_only: bool
) -> None:
    path = tmp_path / "history.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE future_data (value TEXT)")
        connection.execute("INSERT INTO future_data VALUES ('retain me')")
        connection.execute("PRAGMA user_version=99")
    connection.close()
    original = path.read_bytes()
    before = set(tmp_path.iterdir())

    with pytest.raises(RuntimeError, match="Unsupported history schema version: 99"):
        History(path, read_only=read_only)

    assert path.read_bytes() == original
    assert set(tmp_path.iterdir()) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
        assert connection.execute("SELECT value FROM future_data").fetchone()[0] == "retain me"
    connection.close()


def test_malformed_schema_initialization_closes_its_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "history.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE contents (path TEXT)")
    connection.close()
    connections = []
    original_connect = sqlite3.connect

    def capture_connection(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", capture_connection)
    with pytest.raises(sqlite3.OperationalError, match="no such column: size"):
        History(path)

    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connections[0].execute("SELECT 1")


def test_read_only_legacy_wal_history_is_rejected_before_sqlite_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "history.sqlite3"
    _wal_history(path)
    original = path.read_bytes()
    before = set(tmp_path.iterdir())

    def unexpected_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("SQLite must not be opened for read-only WAL history")

    monkeypatch.setattr(sqlite3, "connect", unexpected_open)
    with pytest.raises(RuntimeError, match="Run automatic once to migrate WAL history"):
        History(path, read_only=True)

    assert path.read_bytes() == original
    assert set(tmp_path.iterdir()) == before == {path}


def test_writer_migrates_wal_history_and_preserves_existing_events(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    _wal_history(path)

    with History(path) as history:
        assert history.connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert history.recent()[0]["source"] == "report.pdf"
        history.add_event(_event("new.pdf"))

    assert path.read_bytes()[18:20] == b"\x01\x01"
    assert set(tmp_path.iterdir()) == {path}
    with History(path, read_only=True) as history:
        assert [item["source"] for item in history.recent()] == ["new.pdf", "report.pdf"]
    assert set(tmp_path.iterdir()) == {path}
