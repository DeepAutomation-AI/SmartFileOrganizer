"""Regression tests for durable publication and stale or damaged state."""

from __future__ import annotations

import errno
import sqlite3
from pathlib import Path

import pytest

from smartfileorganizer import engine
from smartfileorganizer.config import AppConfig, Rule, SourceConfig
from smartfileorganizer.models import FileInfo


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    return AppConfig(
        sources=[SourceConfig(inbox)],
        destination_root=tmp_path / "organized",
        rules=[Rule("documents", "Documents", {"extensions": ["pdf"]})],
        database=tmp_path / "state" / "history.sqlite3",
        log_file=tmp_path / "logs" / "organizer.jsonl",
        min_file_age_seconds=0,
    )


def source_file(config: AppConfig, name: str = "report.pdf", data: bytes = b"verified") -> Path:
    source = config.sources[0].path / name
    source.write_bytes(data)
    return source


def test_destination_directory_is_synced_before_unlinking_original(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_file(config)
    destination = config.destination_root / "Documents" / source.name
    destination.parent.mkdir(parents=True)
    info = FileInfo.from_path(source)
    digest = engine.sha256_file(source)
    observed: list[tuple[Path, bool, bool]] = []

    def record_sync(path: Path) -> None:
        observed.append((path, source.exists(), destination.exists()))

    monkeypatch.setattr(engine, "_sync_directory", record_sync)
    actual = engine.safe_move(source, destination, info, digest)

    publication = (destination.parent, True, True)
    deletion = (source.parent, False, True)
    assert publication in observed
    assert deletion in observed
    assert observed.index(publication) < observed.index(deletion)
    assert actual.read_bytes() == b"verified"


def test_destination_directory_sync_failure_retains_original(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_file(config)
    destination = config.destination_root / "Documents" / source.name
    destination.parent.mkdir(parents=True)
    info = FileInfo.from_path(source)
    digest = engine.sha256_file(source)

    def fail_publication_sync(path: Path) -> None:
        if path == destination.parent and destination.exists():
            raise OSError("destination directory cannot be synced")

    monkeypatch.setattr(engine, "_sync_directory", fail_publication_sync)
    with pytest.raises(OSError, match="destination directory cannot be synced"):
        engine.safe_move(source, destination, info, digest)

    assert source.read_bytes() == b"verified"
    assert not destination.exists()
    assert not list(destination.parent.iterdir())


def test_source_directory_sync_failure_never_removes_only_remaining_copy(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_file(config)
    destination = config.destination_root / "Documents" / source.name
    info = FileInfo.from_path(source)
    digest = engine.sha256_file(source)

    def fail_after_unlink(path: Path) -> None:
        if path == source.parent and not source.exists():
            raise OSError("source directory cannot be synced")

    monkeypatch.setattr(engine, "_sync_directory", fail_after_unlink)
    with pytest.raises(OSError, match="source directory cannot be synced"):
        engine.safe_move(source, destination, info, digest)

    assert not source.exists()
    assert destination.read_bytes() == b"verified"
    assert list(destination.parent.iterdir()) == [destination]


def test_run_reports_post_unlink_sync_failure_and_preserves_destination(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_file(config)
    destination = config.destination_root / "Documents" / source.name

    def fail_after_unlink(path: Path) -> None:
        if path == source.parent and not source.exists():
            raise OSError("source directory cannot be synced")

    monkeypatch.setattr(engine, "_sync_directory", fail_after_unlink)
    summary = engine.Organizer(config).run(mode="automatic")

    assert summary.errors == 1
    assert not source.exists()
    assert destination.read_bytes() == b"verified"
    assert any(result["status"] == "error" for result in summary.results)


def test_new_destination_hierarchy_is_durable_before_original_is_unlinked(
    config: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_file(config)
    destination = config.destination_root / "Documents" / "2026" / source.name
    info = FileInfo.from_path(source)
    digest = engine.sha256_file(source)
    synced_before_unlink: set[Path] = set()
    real_unlink = Path.unlink

    def record_sync(path: Path) -> None:
        if source.exists():
            synced_before_unlink.add(path)

    def verify_then_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == source:
            expected = {
                tmp_path,
                config.destination_root,
                destination.parent.parent,
                destination.parent,
            }
            assert expected <= synced_before_unlink
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(engine, "_sync_directory", record_sync)
    monkeypatch.setattr(Path, "unlink", verify_then_unlink)
    actual = engine.safe_move(source, destination, info, digest)

    assert actual.read_bytes() == b"verified"
    assert not source.exists()


@pytest.mark.parametrize("change", ["removed", "modified"])
def test_duplicate_cache_is_revalidated_after_previous_destination_changes(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    source_file(config, "a.pdf", b"same")
    second = source_file(config, "b.pdf", b"same")
    first_destination = config.destination_root / "Documents" / "a.pdf"
    real_hash = engine.sha256_file
    changed = False

    def mutate_previous_destination(path: Path) -> str:
        nonlocal changed
        digest = real_hash(path)
        if path == second and not changed:
            assert first_destination.exists()
            if change == "removed":
                first_destination.unlink()
            else:
                # Equal length ensures size alone cannot establish equality.
                first_destination.write_bytes(b"diff")
            changed = True
        return digest

    monkeypatch.setattr(engine, "sha256_file", mutate_previous_destination)
    summary = engine.Organizer(config).run(mode="automatic")

    assert changed
    assert summary.errors == 0
    assert summary.moved == 2
    assert summary.duplicates == 0
    assert not second.exists()
    assert (config.destination_root / "Documents" / "b.pdf").read_bytes() == b"same"
    if change == "modified":
        assert first_destination.read_bytes() == b"diff"


@pytest.mark.parametrize("mode", ["automatic", "dry-run"])
def test_incompatible_sqlite_schema_returns_error_and_keeps_source(
    config: AppConfig, mode: str
) -> None:
    source = source_file(config)
    config.database.parent.mkdir(parents=True)
    with sqlite3.connect(config.database) as database:
        database.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")

    summary = engine.Organizer(config).run(mode=mode)

    assert summary.errors >= 1
    assert summary.moved == 0
    assert source.read_bytes() == b"verified"
    assert not (config.destination_root / "Documents" / source.name).exists()
    assert any(result["status"] == "error" for result in summary.results)


def test_non_sqlite_state_file_returns_error_instead_of_traceback(config: AppConfig) -> None:
    source = source_file(config)
    config.database.parent.mkdir(parents=True)
    config.database.write_bytes(b"this is not a SQLite database")

    summary = engine.Organizer(config).run(mode="automatic")

    assert summary.errors == 1
    assert summary.moved == 0
    assert source.exists()


def test_destination_walk_permission_error_is_recorded_and_scan_continues(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_file(config)
    config.destination_root.mkdir()
    denied = config.destination_root / "private"
    denied.mkdir()
    real_walk = engine.os.walk

    def permission_error(top: Path, *args: object, **kwargs: object) -> object:
        if Path(top) == config.destination_root:
            callback = kwargs.get("onerror")
            assert callable(callback)
            callback(PermissionError(errno.EACCES, "Permission denied", str(denied)))
            return iter(())
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(engine.os, "walk", permission_error)
    summary = engine.Organizer(config).run(mode="dry-run")

    assert summary.errors == 1
    assert summary.planned == 1
    assert source.exists()
    assert any(
        result["status"] == "error" and result["source"] == str(denied)
        for result in summary.results
    )
    assert not config.database.exists()
