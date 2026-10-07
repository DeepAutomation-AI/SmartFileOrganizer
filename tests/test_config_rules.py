"""Configuration contracts, condition semantics and destination confinement."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from smartfileorganizer.config import ConfigError, config_to_dict, load_config
from smartfileorganizer.models import FileInfo, Rule, from_path
from smartfileorganizer.rules import matching_rule, resolve_destination

NOW = datetime(2026, 10, 7, 12, 30, tzinfo=timezone.utc)


@pytest.fixture
def configuration() -> dict:
    return {
        "sources": [{"path": "incoming", "recursive": True}],
        "destination_root": "organized",
        "rules": [
            {
                "name": "PDF",
                "destination": "Documents/PDFs/{año}",
                "match": {"extensions": [".PDF"]},
            }
        ],
    }


@pytest.fixture
def info(tmp_path: Path) -> FileInfo:
    return FileInfo(
        path=tmp_path / "Invoice_REPORT.PDF",
        size=1024,
        modified=NOW - timedelta(days=100),
        created=NOW - timedelta(days=150),
        creation_is_fallback=False,
        mime="application/pdf",
        mtime_ns=123,
        device=1,
        inode=2,
    )


def write_config(tmp_path: Path, data: dict, suffix: str = ".yaml") -> Path:
    path = tmp_path / f"config{suffix}"
    path.write_text(
        json.dumps(data) if suffix == ".json" else yaml.safe_dump(data), encoding="utf-8"
    )
    return path


@pytest.mark.parametrize("suffix", [".yaml", ".yml", ".json"])
def test_configuration_resolves_paths_without_creating_state(tmp_path, configuration, suffix):
    path = write_config(tmp_path, configuration, suffix)
    config = load_config(path)
    assert config.sources[0].path == tmp_path / "incoming"
    assert config.destination_root == tmp_path / "organized"
    assert config.database == tmp_path / ".smartfileorganizer/history.sqlite3"
    assert config.log_file == tmp_path / ".smartfileorganizer/organizer.log"
    assert config.exclude == ["*.part", "*.crdownload", "*.tmp"]
    assert config.rules[0].match == {"extensions": ["pdf"]}
    assert config.min_file_age_seconds == 2
    assert set(tmp_path.iterdir()) == {path}


def test_configuration_expands_environment_home_and_shorthand_sources(
    tmp_path, monkeypatch, configuration
):
    monkeypatch.setenv("SFO_TEST_ROOT", str(tmp_path / "chosen"))
    configuration.update(
        sources=["${SFO_TEST_ROOT}/incoming", {"path": "~/Desktop", "recursive": False}],
        destination_root="$SFO_TEST_ROOT/out",
        database="state.sqlite",
        log_file="organizer.log",
        duplicate_policy="keep",
        exclude=[],
        min_file_age_seconds=0,
        notifications={"channels": ["desktop", "email", "telegram"], "email": {"ssl": True}},
        schedule={"cron": "*/5 * * * *", "timezone": "America/Lima"},
    )
    config = load_config(write_config(tmp_path, configuration))
    assert config.sources[0].path == tmp_path / "chosen/incoming"
    assert config.sources[1].path == Path.home() / "Desktop"
    assert not config.sources[1].recursive
    assert config.destination_root == tmp_path / "chosen/out"
    assert config.database == tmp_path / "state.sqlite"
    serialized = config_to_dict(config)
    assert serialized["sources"][0]["path"] == str(config.sources[0].path)
    assert serialized["notifications"]["email"]["ssl"] is True
    serialized["rules"][0]["match"]["extensions"].append("txt")
    assert config.rules[0].match["extensions"] == ["pdf"]
    roundtrip = load_config(write_config(tmp_path, config_to_dict(config), ".json"))
    assert roundtrip == config


@pytest.mark.parametrize(
    "field,value",
    [
        ("unknown", True),
        ("sources", []),
        ("sources", "incoming"),
        ("sources", [None]),
        ("sources", [{"recursive": True}]),
        ("sources", [{"path": " "}]),
        ("sources", [{"path": "in", "recursive": "yes"}]),
        ("sources", [{"path": "in", "recusive": True}]),
        ("destination_root", ""),
        ("destination_root", "$SFO_MISSING_ENV/out"),
        ("rules", {}),
        ("rules", [None]),
        ("rules", [{"name": "x", "destination": "out"}]),
        ("rules", [{"name": "x", "destination": "out", "match": {}, "enabeld": True}]),
        ("rules", [{"name": "x", "destination": "out", "match": {}, "enabled": 1}]),
        ("duplicate_policy", "delete"),
        ("duplicate_policy", []),
        ("duplicate_directory", "../bad"),
        ("exclude", "*.tmp"),
        ("exclude", [""]),
        ("min_file_age_seconds", -1),
        ("min_file_age_seconds", True),
        ("min_file_age_seconds", float("inf")),
        ("min_file_age_seconds", "2"),
    ],
)
def test_invalid_config_fields_are_rejected(tmp_path, configuration, field, value, monkeypatch):
    monkeypatch.delenv("SFO_MISSING_ENV", raising=False)
    configuration[field] = value
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, configuration))


@pytest.mark.parametrize("field", ["sources", "destination_root", "rules"])
def test_required_config_fields_are_reported(tmp_path, configuration, field):
    del configuration[field]
    with pytest.raises(ConfigError, match="required"):
        load_config(write_config(tmp_path, configuration))


@pytest.mark.parametrize(
    "condition,value",
    [
        ("extension", "pdf"),
        ("patterns", []),
        ("mime", 4),
        ("keywords", [""]),
        ("extensions", ["."]),
        ("extensions", ["*.pdf"]),
        ("min_size", 1.5),
        ("max_size", -1),
        ("older_than_days", False),
        ("newer_than_days", float("nan")),
    ],
)
def test_invalid_rule_conditions_are_rejected(tmp_path, configuration, condition, value):
    configuration["rules"][0]["match"] = {condition: value}
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, configuration))


def test_inverted_size_range_and_duplicate_rule_names_are_rejected(tmp_path, configuration):
    configuration["rules"][0]["match"] = {"min_size": 10, "max_size": 9}
    with pytest.raises(ConfigError, match="cannot exceed"):
        load_config(write_config(tmp_path, configuration))
    configuration["rules"][0]["match"] = {}
    configuration["rules"].append(dict(configuration["rules"][0]))
    with pytest.raises(ConfigError, match="unique"):
        load_config(write_config(tmp_path, configuration))


@pytest.mark.parametrize(
    "destination",
    [
        "../escape",
        "out/../escape",
        "/absolute",
        "C:/absolute",
        "C:relative",
        r"..\escape",
        r"\\server\share",
        "./out",
        "out/./nested",
        "",
        "\x00",
        "{unknown}",
        "{name.__class__}",
        "{name[0]}",
        "{year!r}",
        "{year:04d}",
        "{year",
    ],
)
def test_unsafe_destinations_rejected_at_load_and_render(
    tmp_path, configuration, info, destination
):
    configuration["rules"][0]["destination"] = destination
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, configuration))
    with pytest.raises(ValueError):
        resolve_destination(info, Rule("unsafe", destination, {}), tmp_path / "out")


@pytest.mark.parametrize(
    "notifications",
    [
        [],
        {"channels": "desktop"},
        {"channels": ["sms"]},
        {"channels": [None]},
        {"channels": ["desktop", "desktop"]},
        {"enabled": "yes"},
        {"timeout_seconds": 0},
        {"timeout_seconds": 61},
        {"email": []},
        {"email": {"password": "never-save-this"}},
        {"email": {"ssl": "true"}},
        {"token": "never-save-this"},
    ],
)
def test_invalid_notification_settings_are_rejected(tmp_path, configuration, notifications):
    configuration["notifications"] = notifications
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, configuration))


@pytest.mark.parametrize(
    "schedule",
    [
        [],
        {"interval": 5},
        {"interval_seconds": 0},
        {"interval_seconds": -5},
        {"interval_seconds": 2, "cron": "* * * * *"},
        {"cron": "bad cron"},
        {"cron": "60 * * * *"},
        {"timezone": "Mars/Olympus"},
        {"timezone": ""},
    ],
)
def test_invalid_schedule_is_rejected(tmp_path, configuration, schedule):
    configuration["schedule"] = schedule
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, configuration))


def test_valid_interval_notifications_and_disabled_rule(tmp_path, configuration):
    configuration["schedule"] = {"interval_seconds": 2.5}
    configuration["notifications"] = {"enabled": False, "timeout_seconds": 30}
    configuration["rules"][0]["enabled"] = False
    config = load_config(write_config(tmp_path, configuration))
    assert config.schedule["interval_seconds"] == 2.5
    assert config.notifications["enabled"] is False
    assert not config.rules[0].enabled


@pytest.mark.parametrize(
    "suffix,content",
    [
        (".yaml", "sources: []\nsources: []\n"),
        (".json", '{"sources": [], "sources": []}'),
        (".yaml", "!!python/object/apply:os.system ['echo unsafe']"),
        (".yaml", "key: [broken"),
        (".json", "{"),
        (".yaml", "[]"),
        (".txt", "{}"),
        (".yaml", "? [list, key]\n: bad"),
    ],
)
def test_malformed_ambiguous_or_unsafe_configuration_rejected(tmp_path, suffix, content):
    path = tmp_path / f"config{suffix}"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_oversized_and_invalid_utf8_configs_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    with pytest.raises(ConfigError):
        load_config(path)
    path.write_bytes(b"#" * (1_048_576 + 1))
    with pytest.raises(ConfigError, match="size limit"):
        load_config(path)
    path.write_bytes(b"\xff")
    with pytest.raises(ConfigError):
        load_config(path)


def test_empty_expanded_path_rejected(tmp_path, monkeypatch, configuration):
    monkeypatch.setenv("SFO_EMPTY_ROOT", "")
    configuration["destination_root"] = "${SFO_EMPTY_ROOT}"
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, configuration))


@pytest.mark.parametrize(
    "conditions",
    [
        {"patterns": ["*.PDF", "*.txt"]},
        {"extensions": ".pdf"},
        {"mime": ["image/*", "application/*"]},
        {"keywords": ["missing", "report"]},
        {"min_size": 1024, "max_size": 1024},
        {"older_than_days": 90},
        {"modified_older_than_days": 90},
        {"created_older_than_days": 140},
        {"newer_than_days": 101},
        {},
    ],
)
def test_each_condition_can_match(info, conditions):
    rule = Rule("match", "Documents", conditions)
    assert matching_rule(info, [rule], NOW) is rule


@pytest.mark.parametrize(
    "conditions",
    [
        {"patterns": "*.txt"},
        {"extensions": ["txt"]},
        {"mime": "image/*"},
        {"keywords": "absent"},
        {"min_size": 1025},
        {"max_size": 1023},
        {"older_than_days": 100},
        {"modified_older_than_days": 100},
        {"created_older_than_days": 150},
        {"newer_than_days": 100},
    ],
)
def test_each_condition_can_reject_and_age_boundaries_are_strict(info, conditions):
    assert matching_rule(info, [Rule("reject", "Documents", conditions)], NOW) is None


def test_rule_order_disabled_rules_and_conditions_and(info):
    disabled = Rule("disabled", "Disabled", {}, enabled=False)
    mismatch = Rule("all must match", "Mismatch", {"extensions": "pdf", "min_size": 1025})
    first = Rule("first", "First", {"keywords": "INVOICE"})
    second = Rule("second", "Second", {})
    assert matching_rule(info, [disabled, mismatch, first, second], NOW) is first
    assert matching_rule(info, [], NOW) is None
    assert matching_rule(info, [Rule("now", "Now", {"older_than_days": 0})]) is not None
    naive = replace(info, modified=info.modified.replace(tzinfo=None))
    assert matching_rule(naive, [first], NOW.replace(tzinfo=None)) is first


def test_all_supported_destination_templates(info, tmp_path):
    rule = Rule("templates", "{year}/{año}/{month}/{day}/{extension}/{stem}/{name}", {})
    result = resolve_destination(info, rule, tmp_path / "output")
    assert (
        result
        == tmp_path
        / "output/2026/2026/06/29/pdf/Invoice_REPORT/Invoice_REPORT.PDF/Invoice_REPORT.PDF"
    )


def test_destination_rejects_symlink_escape(info, tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "Documents").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        resolve_destination(info, Rule("outside", "Documents", {}), root)


def test_rendered_filename_cannot_introduce_traversal_or_windows_paths(info, tmp_path):
    unsafe = replace(info, path=tmp_path / "..\\escape.pdf")
    with pytest.raises(ValueError):
        resolve_destination(unsafe, Rule("unsafe", "{stem}", {}), tmp_path / "root")
    with pytest.raises(ValueError):
        resolve_destination(unsafe, Rule("unsafe", "Documents", {}), tmp_path / "root")


def test_file_info_regular_file_mime_and_explicit_creation_fallback(tmp_path):
    path = tmp_path / "sample.PDF"
    path.write_bytes(b"example")
    os.utime(path, (1_000_000, 1_000_000))
    result = from_path(path)
    assert result.size == 7
    assert result.extension == "pdf"
    assert result.name == path.name
    assert result.mime == "application/pdf"
    assert result.modified == datetime.fromtimestamp(1_000_000, timezone.utc)
    assert result.device == path.stat().st_dev
    assert result.inode == path.stat().st_ino
    assert result.mtime_ns == path.stat().st_mtime_ns
    if not hasattr(path.stat(), "st_birthtime"):
        assert result.creation_is_fallback
        assert result.created == result.modified


def test_file_info_uses_birth_time_when_available(tmp_path, monkeypatch):
    path = tmp_path / "sample.unknown_extension"
    path.write_bytes(b"x")
    metadata = path.stat()
    replacement = SimpleNamespace(
        st_mode=metadata.st_mode,
        st_size=metadata.st_size,
        st_mtime=metadata.st_mtime,
        st_mtime_ns=metadata.st_mtime_ns,
        st_dev=metadata.st_dev,
        st_ino=metadata.st_ino,
        st_birthtime=10,
    )
    monkeypatch.setattr(Path, "lstat", lambda self: replacement)
    result = FileInfo.from_path(path)
    assert not result.creation_is_fallback
    assert result.created == datetime.fromtimestamp(10, timezone.utc)
    assert result.mime == "application/octet-stream"


def test_file_info_rejects_symlinks_directories_and_missing_paths(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("example", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        FileInfo.from_path(link)
    with pytest.raises(ValueError):
        FileInfo.from_path(tmp_path)
    with pytest.raises(FileNotFoundError):
        FileInfo.from_path(tmp_path / "missing")
