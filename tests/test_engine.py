"""End-to-end safety and organization checks using real temporary files."""

from __future__ import annotations

import errno
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from smartfileorganizer import engine
from smartfileorganizer.config import AppConfig, Rule, SourceConfig
from smartfileorganizer.engine import Organizer
from smartfileorganizer.history import History
from smartfileorganizer.models import FileInfo


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    source = tmp_path / "inbox"
    source.mkdir()
    return AppConfig(
        sources=[SourceConfig(path=source, recursive=True)],
        destination_root=tmp_path / "organized",
        rules=[Rule(name="pdfs", destination="Documents", match={"extensions": [".pdf"]})],
        database=tmp_path / "state" / "history.sqlite3",
        log_file=tmp_path / "logs" / "organizer.jsonl",
        duplicate_policy="skip",
        duplicate_directory="Duplicates",
        exclude=[],
        min_file_age_seconds=0,
        notifications={},
        schedule={},
    )


def put(config: AppConfig, name: str, content: bytes = b"document") -> Path:
    path = config.sources[0].path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_dry_run_plans_without_creating_state_or_outputs(config: AppConfig) -> None:
    document = put(config, "invoice.pdf")
    unmatched = put(config, "notes.txt")

    summary = Organizer(config).run(mode="dry-run")

    assert (summary.scanned, summary.planned, summary.moved, summary.skipped, summary.errors) == (
        2,
        1,
        0,
        1,
        0,
    )
    assert document.read_bytes() == b"document"
    assert unmatched.exists()
    assert not config.destination_root.exists()
    assert not config.database.parent.exists()
    assert not config.log_file.parent.exists()
    assert summary.to_dict()["planned"] == 1
    assert len(summary.results) == 2


def test_automatic_move_preserves_content_and_records_history(config: AppConfig) -> None:
    source = put(config, "report.pdf", b"a complete report\x00\xff")

    summary = Organizer(config).run(mode="automatic")

    destination = config.destination_root / "Documents" / "report.pdf"
    assert summary.moved == 1
    assert summary.errors == 0
    assert not source.exists()
    assert destination.read_bytes() == b"a complete report\x00\xff"
    with History(config.database, read_only=True) as history:
        events = history.recent()
        assert any(
            event["source"] == str(source) and event["status"] == "moved" for event in events
        )
        assert any(event["destination"] == str(destination) for event in events)
        assert history.get_config() is not None


def test_interactive_decline_leaves_source_and_destination_unchanged(config: AppConfig) -> None:
    source = put(config, "report.pdf")
    asked: list[tuple[Path, Path]] = []

    def decline(src: Path, dst: Path) -> bool:
        asked.append((src, dst))
        return False

    summary = Organizer(config).run(mode="interactive", confirm=decline)

    assert len(asked) == 1
    assert asked[0][0] == source
    assert (summary.moved, summary.skipped, summary.errors) == (0, 1, 0)
    assert source.exists()
    assert not (config.destination_root / "Documents" / source.name).exists()


def test_interactive_accept_moves_file(config: AppConfig) -> None:
    source = put(config, "approved.pdf")
    summary = Organizer(config).run(mode="interactive", confirm=lambda _src, _dst: True)
    assert summary.moved == 1
    assert not source.exists()


def test_non_recursive_scan_leaves_nested_files(config: AppConfig) -> None:
    config = replace(config, sources=[replace(config.sources[0], recursive=False)])
    direct = put(config, "direct.pdf")
    nested = put(config, "nested/deep.pdf")

    summary = Organizer(config).run(mode="automatic")

    assert summary.scanned == 1
    assert summary.moved == 1
    assert not direct.exists()
    assert nested.exists()


def test_recursive_scan_moves_nested_files(config: AppConfig) -> None:
    nested = put(config, "nested/deeper/report.pdf")
    summary = Organizer(config).run(mode="automatic")
    assert summary.scanned == 1
    assert summary.moved == 1
    assert not nested.exists()


def test_destination_subtree_is_not_rescanned(config: AppConfig) -> None:
    config = replace(config, destination_root=config.sources[0].path / "organized")
    existing = config.destination_root / "Documents" / "old.pdf"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"old")
    put(config, "new.pdf", b"new")

    first = Organizer(config).run(mode="automatic")
    second = Organizer(config).run(mode="automatic")

    assert first.scanned == 1
    assert first.moved == 1
    assert second.scanned == 0
    assert existing.read_bytes() == b"old"


def test_symlink_files_and_directories_are_not_followed(config: AppConfig, tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    real = external / "secret.pdf"
    real.write_bytes(b"external")
    linked_file = config.sources[0].path / "linked.pdf"
    linked_dir = config.sources[0].path / "linked-directory"
    try:
        linked_file.symlink_to(real)
        linked_dir.symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable on this platform")

    summary = Organizer(config).run(mode="automatic")

    assert summary.moved == 0
    assert linked_file.is_symlink()
    assert real.read_bytes() == b"external"
    assert not list(config.destination_root.rglob("*.pdf"))


def test_first_matching_rule_wins(config: AppConfig) -> None:
    config = replace(
        config,
        rules=[
            Rule(name="priority", destination="Priority", match={"extensions": [".pdf"]}),
            Rule(name="fallback", destination="Other", match={"extensions": [".pdf"]}),
        ],
    )
    put(config, "report.pdf")
    summary = Organizer(config).run(mode="automatic")
    assert summary.moved == 1
    assert (config.destination_root / "Priority" / "report.pdf").exists()
    assert not (config.destination_root / "Other").exists()


def test_disabled_rule_does_not_move_matching_files(config: AppConfig) -> None:
    config = replace(config, rules=[replace(config.rules[0], enabled=False)])
    source = put(config, "report.pdf")
    summary = Organizer(config).run(mode="automatic")
    assert summary.moved == 0
    assert summary.skipped == 1
    assert source.exists()


def test_exclude_patterns_protect_files_and_subdirectories(config: AppConfig) -> None:
    config = replace(config, exclude=["ignored.pdf", "private/*"])
    ignored = put(config, "ignored.pdf")
    private = put(config, "private/personal.pdf")
    selected = put(config, "public.pdf")
    summary = Organizer(config).run(mode="automatic")
    assert summary.moved == 1
    assert ignored.exists()
    assert private.exists()
    assert not selected.exists()


def test_recent_file_waits_until_stable(config: AppConfig) -> None:
    config = replace(config, min_file_age_seconds=60)
    recent = put(config, "download.pdf")

    summary = Organizer(config).run(mode="automatic")

    assert (summary.moved, summary.skipped, summary.errors) == (0, 1, 0)
    assert recent.exists()
    old = time.time() - 120
    os.utime(recent, (old, old))
    assert Organizer(config).run(mode="automatic").moved == 1


def test_same_content_in_one_run_is_skipped_without_deletion(config: AppConfig) -> None:
    first = put(config, "one.pdf", b"identical")
    second = put(config, "two.pdf", b"identical")

    summary = Organizer(config).run(mode="automatic")

    assert (summary.moved, summary.duplicates, summary.errors) == (1, 1, 0)
    assert sum(path.exists() for path in (first, second)) == 1
    assert len(list(config.destination_root.rglob("*.pdf"))) == 1
    assert next(path for path in (first, second) if path.exists()).read_bytes() == b"identical"


def test_duplicate_detected_against_previous_move(config: AppConfig) -> None:
    put(config, "first.pdf", b"same content")
    assert Organizer(config).run(mode="automatic").moved == 1
    duplicate = put(config, "second.pdf", b"same content")

    summary = Organizer(config).run(mode="automatic")

    assert summary.duplicates == 1
    assert summary.moved == 0
    assert duplicate.exists()


def test_removed_historical_destination_does_not_block_new_file(config: AppConfig) -> None:
    put(config, "first.pdf", b"same content")
    assert Organizer(config).run(mode="automatic").moved == 1
    (config.destination_root / "Documents" / "first.pdf").unlink()
    second = put(config, "second.pdf", b"same content")

    summary = Organizer(config).run(mode="automatic")

    assert summary.moved == 1
    assert summary.duplicates == 0
    assert not second.exists()


def test_same_name_different_content_never_overwrites(config: AppConfig) -> None:
    put(config, "report.pdf", b"old contents")
    assert Organizer(config).run(mode="automatic").moved == 1
    put(config, "report.pdf", b"new contents")

    summary = Organizer(config).run(mode="automatic")

    assert summary.moved == 1
    assert summary.duplicates == 0
    destination = config.destination_root / "Documents"
    assert (destination / "report.pdf").read_bytes() == b"old contents"
    assert (destination / "report (1).pdf").read_bytes() == b"new contents"


@pytest.mark.parametrize("policy", ["quarantine", "keep"])
def test_duplicate_retention_policies(config: AppConfig, policy: str) -> None:
    config = replace(config, duplicate_policy=policy)
    put(config, "first.pdf", b"same")
    put(config, "second.pdf", b"same")

    summary = Organizer(config).run(mode="automatic")

    assert summary.moved == 2
    assert summary.duplicates == 1
    assert summary.errors == 0
    all_files = list(config.destination_root.rglob("*.pdf"))
    assert len(all_files) == 2
    assert all(path.read_bytes() == b"same" for path in all_files)
    if policy == "quarantine":
        assert len(list((config.destination_root / "Duplicates").rglob("*.pdf"))) == 1


def test_missing_source_reports_error_without_crashing(config: AppConfig) -> None:
    config = replace(config, sources=[SourceConfig(path=config.sources[0].path / "missing")])
    summary = Organizer(config).run(mode="automatic")
    assert summary.errors >= 1
    assert summary.moved == 0


def test_failed_move_keeps_original_and_continues_other_files(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = put(config, "blocked.pdf", b"locked")
    movable = put(config, "movable.pdf", b"available")
    real_move = engine.safe_move

    def fail_one(source: Path, *args: object, **kwargs: object) -> Path:
        if source == blocked:
            raise PermissionError("file is locked")
        return real_move(source, *args, **kwargs)

    monkeypatch.setattr(engine, "safe_move", fail_one)
    summary = Organizer(config).run(mode="automatic")

    assert summary.errors == 1
    assert summary.moved == 1
    assert blocked.read_bytes() == b"locked"
    assert not movable.exists()
    assert not (config.destination_root / "Documents" / "blocked.pdf").exists()


def test_unreadable_file_does_not_stop_the_run(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = put(config, "blocked.pdf")

    def unreadable(_path: Path) -> str:
        raise PermissionError("cannot read")

    monkeypatch.setattr(engine, "sha256_file", unreadable)
    summary = Organizer(config).run(mode="automatic")
    assert summary.errors == 1
    assert summary.moved == 0
    assert blocked.exists()


def test_dry_run_detects_duplicates_without_persisting(config: AppConfig) -> None:
    first = put(config, "one.pdf", b"identical")
    second = put(config, "two.pdf", b"identical")
    summary = Organizer(config).run(mode="dry-run")
    assert summary.duplicates == 1
    assert summary.moved == 0
    assert first.exists() and second.exists()
    assert not config.database.exists()


def test_destination_symlink_cannot_escape_root(config: AppConfig, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    config.destination_root.mkdir()
    (config.destination_root / "Documents").symlink_to(outside, target_is_directory=True)
    source = put(config, "secret.pdf")

    summary = Organizer(config).run(mode="automatic")

    assert summary.errors >= 1
    assert summary.moved == 0
    assert source.exists()
    assert not (outside / "secret.pdf").exists()


def test_invalid_destination_cannot_traverse_above_root(config: AppConfig, tmp_path: Path) -> None:
    config = replace(config, rules=[replace(config.rules[0], destination="../escaped")])
    source = put(config, "secret.pdf")
    summary = Organizer(config).run(mode="automatic")
    assert summary.moved == 0
    assert summary.errors >= 1
    assert source.exists()
    assert not (tmp_path / "escaped" / "secret.pdf").exists()


def test_unknown_mode_is_rejected_without_side_effects(config: AppConfig) -> None:
    source = put(config, "report.pdf")
    with pytest.raises(ValueError):
        Organizer(config).run(mode="accidental")
    assert source.exists()
    assert not config.database.exists()


def test_safe_move_rejects_corrupt_copy_without_removing_original(config: AppConfig) -> None:
    source = put(config, "report.pdf", b"verified contents")
    destination = config.destination_root / "Documents" / "report.pdf"
    info = FileInfo.from_path(source)

    with pytest.raises((OSError, ValueError, RuntimeError)):
        engine.safe_move(source, destination, info, "0" * 64)

    assert source.read_bytes() == b"verified contents"
    assert not destination.exists()
    assert not list(destination.parent.iterdir())


def test_safe_move_fsync_failure_keeps_original_and_removes_partial_output(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = put(config, "report.pdf", b"durable contents")
    destination = config.destination_root / "Documents" / "report.pdf"
    info = FileInfo.from_path(source)
    digest = engine.sha256_file(source)

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("storage device unavailable")

    monkeypatch.setattr(engine.os, "fsync", fail_fsync)
    with pytest.raises(OSError):
        engine.safe_move(source, destination, info, digest)

    assert source.read_bytes() == b"durable contents"
    assert not destination.exists()
    assert not list(destination.parent.iterdir())


def test_safe_move_rejects_file_changed_after_scan(config: AppConfig) -> None:
    source = put(config, "report.pdf", b"old")
    info = FileInfo.from_path(source)
    digest = engine.sha256_file(source)
    source.write_bytes(b"new bytes arrived while copying")
    destination = config.destination_root / "Documents" / "report.pdf"

    with pytest.raises((OSError, ValueError, RuntimeError)):
        engine.safe_move(source, destination, info, digest)

    assert source.read_bytes() == b"new bytes arrived while copying"
    assert not destination.exists()


def test_safe_move_works_on_filesystem_without_hardlinks(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = put(config, "report.pdf", b"portable move")
    destination = config.destination_root / "Documents" / "report.pdf"
    info = FileInfo.from_path(source)
    digest = engine.sha256_file(source)

    def unsupported_link(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.ENOTSUP, "hard links are unsupported")

    monkeypatch.setattr(engine.os, "link", unsupported_link)
    result = engine.safe_move(source, destination, info, digest)

    assert result == destination
    assert destination.read_bytes() == b"portable move"
    assert not source.exists()
    assert list(destination.parent.iterdir()) == [destination]


def test_fallback_copy_failure_removes_only_own_partial_output(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = put(config, "report.pdf", b"portable move")
    destination = config.destination_root / "Documents" / "report.pdf"
    info = FileInfo.from_path(source)
    digest = engine.sha256_file(source)

    def unsupported_link(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.ENOTSUP, "hard links are unsupported")

    def incomplete_copy(_source: object, output: object, _size: int) -> None:
        output.write(b"partial")
        raise OSError("disk is full")

    monkeypatch.setattr(engine.os, "link", unsupported_link)
    monkeypatch.setattr(engine.shutil, "copyfileobj", incomplete_copy)
    with pytest.raises(OSError, match="disk is full"):
        engine.safe_move(source, destination, info, digest)

    assert source.read_bytes() == b"portable move"
    assert not destination.exists()
    assert not list(destination.parent.iterdir())


def test_source_unlink_failure_rolls_back_published_copy(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = put(config, "report.pdf", b"keep original")
    destination = config.destination_root / "Documents" / "report.pdf"
    info = FileInfo.from_path(source)
    digest = engine.sha256_file(source)
    original_unlink = Path.unlink

    def fail_source_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == source:
            raise PermissionError("cannot remove original")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_source_unlink)
    with pytest.raises(PermissionError, match="cannot remove original"):
        engine.safe_move(source, destination, info, digest)

    assert source.read_bytes() == b"keep original"
    assert not destination.exists()
    assert not list(destination.parent.iterdir())


def test_database_lock_prevents_simultaneous_moves(config: AppConfig) -> None:
    source = put(config, "report.pdf")
    with engine.RunLock(config.database):
        summary = Organizer(config).run(mode="automatic")
        assert summary.errors == 1
        assert summary.moved == 0
        assert source.exists()
        assert not config.database.exists()
    assert Organizer(config).run(mode="automatic").moved == 1


def test_dry_run_can_preview_while_real_run_lock_is_held(config: AppConfig) -> None:
    source = put(config, "report.pdf")
    with engine.RunLock(config.database):
        summary = Organizer(config).run(mode="dry-run")
    assert summary.errors == 0
    assert summary.planned == 1
    assert source.exists()
    assert not config.database.exists()


def test_file_mutation_during_hash_is_skipped_safely(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = put(config, "report.pdf", b"original")
    actual_hash = engine.sha256_file

    def mutation(path: Path) -> str:
        digest = actual_hash(path)
        if path == source:
            path.write_bytes(b"download still in progress")
        return digest

    monkeypatch.setattr(engine, "sha256_file", mutation)
    summary = Organizer(config).run(mode="automatic")
    assert (summary.moved, summary.skipped, summary.errors) == (0, 1, 0)
    assert source.read_bytes() == b"download still in progress"
