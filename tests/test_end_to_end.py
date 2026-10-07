"""Exercise the installed CLI and real scheduler/observer with temporary data."""

from __future__ import annotations

import errno
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from apscheduler.schedulers.blocking import BlockingScheduler

from smartfileorganizer import engine, scheduler
from smartfileorganizer.config import AppConfig, Rule, SourceConfig
from smartfileorganizer.history import History
from smartfileorganizer.models import FileInfo


def config_for(tmp_path: Path) -> AppConfig:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    return AppConfig(
        sources=[SourceConfig(inbox)],
        destination_root=tmp_path / "output",
        rules=[Rule("PDF", "PDF/{year}", {"extensions": ["pdf"]})],
        database=tmp_path / "state" / "history.sqlite3",
        log_file=tmp_path / "logs" / "events.jsonl",
        min_file_age_seconds=0,
        schedule={"interval_seconds": 0.1, "timezone": "UTC"},
    )


def wait_until(predicate, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("The service did not produce the expected result before the deadline")


def test_cli_module_round_trip_and_nonmutating_preview(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    source = config.sources[0].path / "report.pdf"
    source.write_bytes(b"A real report")
    duplicate = config.sources[0].path / "copy.pdf"
    duplicate.write_bytes(source.read_bytes())
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "sources": [str(config.sources[0].path)],
                "destination_root": str(config.destination_root),
                "database": str(config.database),
                "log_file": str(config.log_file),
                "rules": [{"name": "PDF", "match": {"extensions": ["pdf"]}, "destination": "PDFs"}],
                "min_file_age_seconds": 0,
            }
        )
    )

    def cli(*args: str) -> object:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "smartfileorganizer",
                "--config",
                str(config_path),
                "--env-file",
                str(tmp_path / "absent.env"),
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return json.loads(result.stdout)

    before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    assert cli("validate")["duplicate_policy"] == "skip"
    plan = cli("organize")
    assert (plan["planned"], plan["duplicates"], plan["errors"]) == (1, 1, 0)
    assert before == {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    actual = cli("organize", "--mode", "automatic")
    assert (actual["moved"], actual["duplicates"], actual["errors"]) == (1, 1, 0)
    assert len(list(config.destination_root.rglob("*.pdf"))) == 1
    assert len(list(config.sources[0].path.glob("*.pdf"))) == 1
    snapshot = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert any(event["status"] == "moved" for event in cli("history"))
    assert cli("organize")["duplicates"] == 1
    assert snapshot == {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }


def test_real_apscheduler_moves_then_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config_for(tmp_path)
    source = config.sources[0].path / "scheduled.pdf"
    source.write_bytes(b"Scheduled report")
    real_scheduler = BlockingScheduler(timezone="UTC")
    monkeypatch.setattr(scheduler, "BlockingScheduler", lambda **kwargs: real_scheduler)
    thread = threading.Thread(
        target=scheduler.start_scheduler, args=(config,), kwargs={"apply": True}, daemon=True
    )
    thread.start()
    try:

        def persisted_move() -> bool:
            if source.exists() or not config.database.exists():
                return False
            with History(config.database, read_only=True) as history:
                return any(event["status"] == "moved" for event in history.recent())

        wait_until(persisted_move)
        with History(config.database, read_only=True) as history:
            assert any(event["status"] == "moved" for event in history.recent())
        assert not source.exists()
    finally:
        if real_scheduler.running:
            real_scheduler.shutdown(wait=True)
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_real_watchdog_detects_new_file_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observers = pytest.importorskip("watchdog.observers")
    config = config_for(tmp_path)
    ready, stop = threading.Event(), threading.Event()
    original_observer = observers.Observer

    class ReadyObserver(original_observer):
        def start(self) -> None:
            super().start()
            ready.set()

    monkeypatch.setattr(observers, "Observer", ReadyObserver)
    thread = threading.Thread(
        target=scheduler.start_watch,
        args=(config,),
        kwargs={"apply": True, "stop_event": stop},
        daemon=True,
    )
    thread.start()
    try:
        assert ready.wait(timeout=3)
        source = config.sources[0].path / "watched.pdf"
        source.write_bytes(b"Watchdog report")
        wait_until(
            lambda: not source.exists() and any(config.destination_root.rglob("watched.pdf"))
        )
        assert not source.exists()
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_corrupt_fallback_copy_cannot_remove_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"A complete report that must survive")
    info = FileInfo.from_path(source)
    digest = engine.sha256_file(source)
    destination = tmp_path / "output" / source.name

    def no_links(*args, **kwargs):
        raise OSError(errno.ENOTSUP, "Hard links unavailable")

    def bad_copy(input_file, output, length):
        output.write(input_file.read(2))  # A successful return must not hide data loss.

    monkeypatch.setattr(engine.os, "link", no_links)
    monkeypatch.setattr(engine.shutil, "copyfileobj", bad_copy)
    with pytest.raises(OSError, match="failed SHA-256 verification"):
        engine.safe_move(source, destination, info, digest)
    assert source.read_bytes() == b"A complete report that must survive"
    assert not destination.exists()
    assert not list(destination.parent.glob(".smartfileorganizer-*"))


def test_interrupt_after_source_unlink_preserves_published_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(b"This must survive Ctrl-C")
    info = FileInfo.from_path(source)
    digest = engine.sha256_file(source)
    destination = tmp_path / "output" / source.name
    original_unlink = Path.unlink

    def interrupt_after_unlink(path: Path, *args, **kwargs):
        original_unlink(path, *args, **kwargs)
        if path == source:
            raise KeyboardInterrupt

    monkeypatch.setattr(Path, "unlink", interrupt_after_unlink)
    with pytest.raises(KeyboardInterrupt):
        engine.safe_move(source, destination, info, digest)
    assert not source.exists()
    assert destination.read_bytes() == b"This must survive Ctrl-C"
