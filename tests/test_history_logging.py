"""SQLite durability, read-only behavior and structured rotating log checks."""

from __future__ import annotations

import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from smartfileorganizer.history import History
from smartfileorganizer.logging_setup import configure_logging


def event(number: int = 0) -> dict:
    return {
        "source": f"/inbox/{number}.pdf",
        "destination": f"/documents/{number}.pdf",
        "status": "moved",
        "rule": "pdfs",
        "size": 123,
        "sha256": hashlib.sha256(str(number).encode()).hexdigest(),
        "error": None,
    }


def test_read_only_missing_database_does_not_create_files(tmp_path: Path) -> None:
    path = tmp_path / "missing-parent" / "history.sqlite3"
    with History(path, read_only=True) as history:
        assert history.recent() == []
        assert history.get_config() is None
        assert history.find_content(3, "unknown") == []
        assert history.recent_size(3) == []
        assert history.is_known_path(tmp_path / "file.pdf") is False
    assert not path.parent.exists()


def test_events_survive_close_and_are_newest_first(tmp_path: Path) -> None:
    path = tmp_path / "state" / "history.sqlite3"
    with History(path) as history:
        history.add_event(event(1))
        history.add_event(event(2))
        history.add_event(event(3))

    with History(path, read_only=True) as history:
        recent = history.recent(limit=2)
        assert [item["source"] for item in recent] == ["/inbox/3.pdf", "/inbox/2.pdf"]
        assert all(item["timestamp"] for item in recent)
        assert all(isinstance(item["id"], int) for item in recent)
        assert recent[0]["sha256"] == event(3)["sha256"]


def test_configuration_roundtrips_as_json_and_can_be_replaced(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    config = {"sources": ["/inbox"], "rules": [{"name": "pdfs", "match": {"extensions": [".pdf"]}}]}
    with History(path) as history:
        assert history.get_config() is None
        history.save_config(config)
        assert history.get_config() == config
        history.save_config({"sources": ["/other"], "rules": []})
    with History(path, read_only=True) as history:
        assert history.get_config() == {"sources": ["/other"], "rules": []}


def test_content_index_requires_size_and_hash_and_keeps_distinct_paths(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    first, second = tmp_path / "one.pdf", tmp_path / "two.pdf"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    digest = hashlib.sha256(b"same").hexdigest()
    with History(path) as history:
        history.record_content(4, digest, first)
        history.record_content(4, digest, second)
        assert set(history.find_content(4, digest)) == {first, second}
        assert history.find_content(5, digest) == []
        assert history.find_content(4, "different") == []
        assert set(history.recent_size(4)) == {first, second}
        assert history.recent_size(5) == []
        assert history.is_known_path(first)
        assert not history.is_known_path(tmp_path / "unseen.pdf")


def test_recording_same_fingerprint_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    destination = tmp_path / "document.pdf"
    destination.write_bytes(b"a")
    first_digest = hashlib.sha256(b"a").hexdigest()
    with History(path) as history:
        history.record_content(1, first_digest, destination)
        history.record_content(1, first_digest, destination)
        assert history.find_content(1, first_digest) == [destination]


def test_two_history_connections_can_write_without_losing_events(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    with History(path) as one, History(path) as two:
        one.add_event(event(1))
        two.add_event(event(2))
        one.add_event(event(3))
    with History(path, read_only=True) as history:
        assert len(history.recent()) == 3


def test_read_only_history_rejects_writes(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    with History(path) as history:
        history.add_event(event())
    with History(path, read_only=True) as history:
        with pytest.raises(RuntimeError):
            history.add_event(event(1))
        with pytest.raises(RuntimeError):
            history.save_config({"sources": []})
        with pytest.raises(RuntimeError):
            history.record_content(1, "hash", tmp_path / "file.pdf")
        assert len(history.recent()) == 1


def test_history_validates_limit_and_rejects_use_after_close(tmp_path: Path) -> None:
    history = History(tmp_path / "history.sqlite3")
    with pytest.raises(ValueError):
        history.recent(limit=0)
    history.close()
    with pytest.raises(RuntimeError):
        history.add_event(event())
    history.close()


def test_sql_parameters_handle_quotes_in_paths_and_rules(tmp_path: Path) -> None:
    item = event()
    item["source"] = "/inbox/it's a file.pdf"
    item["rule"] = "quoted'; DROP TABLE events; --"
    with History(tmp_path / "history.sqlite3") as history:
        history.add_event(item)
        history.add_event(event(1))
        results = history.recent()
        assert len(results) == 2
        assert results[1]["source"] == item["source"]
        assert results[1]["rule"] == item["rule"]


def close_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.flush()
        handler.close()
        logger.removeHandler(handler)


def test_logging_writes_structured_json_with_rotation(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "organizer.jsonl"
    logger = configure_logging(path)
    try:
        logger.info("file_event", extra={"event": event(7)})
        for handler in logger.handlers:
            handler.flush()
        record = json.loads(path.read_text().splitlines()[-1])
        assert record["message"] == "file_event"
        assert record["event"]["source"] == "/inbox/7.pdf"
        rotating = [
            handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler)
        ]
        assert len(rotating) == 1
        assert rotating[0].maxBytes == 5 * 1024 * 1024
        assert rotating[0].backupCount == 3
    finally:
        close_handlers(logger)


def test_reconfiguring_logging_does_not_duplicate_records(tmp_path: Path) -> None:
    path = tmp_path / "organizer.jsonl"
    configure_logging(path)
    logger = configure_logging(path)
    try:
        logger.info("once", extra={"event": {"status": "moved"}})
        for handler in logger.handlers:
            handler.flush()
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        assert sum(item["message"] == "once" for item in lines) == 1
    finally:
        close_handlers(logger)


def test_logger_serializes_path_values_in_extra_event(tmp_path: Path) -> None:
    path = tmp_path / "organizer.jsonl"
    logger = configure_logging(path)
    try:
        logger.warning(
            "path-event", extra={"event": {"source": tmp_path / "café.pdf", "error": "permission"}}
        )
        for handler in logger.handlers:
            handler.flush()
        record = json.loads(path.read_text().splitlines()[-1])
        assert record["event"]["source"] == str(tmp_path / "café.pdf")
    finally:
        close_handlers(logger)
