"""Strict YAML/JSON configuration loading without filesystem mutations."""

from __future__ import annotations

import json
import math
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from apscheduler.triggers.cron import CronTrigger

from .models import AppConfig, Rule, SourceConfig
from .rules import validate_relative_destination


class ConfigError(ValueError):
    """A configuration file contains an invalid or unsupported value."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader with duplicate-key rejection instead of silent overrides."""


def _yaml_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    result: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in result:
                raise ConfigError("Duplicate YAML mapping key")
            result[key] = loader.construct_object(value_node, deep=deep)
        except TypeError as exc:
            raise ConfigError("Configuration mapping keys must be strings") from exc
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _yaml_mapping)

_TOP_LEVEL = frozenset(
    {
        "sources",
        "destination_root",
        "rules",
        "database",
        "log_file",
        "duplicate_policy",
        "duplicate_directory",
        "exclude",
        "min_file_age_seconds",
        "notifications",
        "schedule",
    }
)
_CONDITIONS = frozenset(
    {
        "patterns",
        "extensions",
        "mime",
        "keywords",
        "min_size",
        "max_size",
        "older_than_days",
        "modified_older_than_days",
        "created_older_than_days",
        "newer_than_days",
    }
)
_ENV_VARIABLE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ConfigError(f"{label} must be a mapping with string keys")
    return value


def _unknown(mapping: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    extra = mapping.keys() - allowed
    if extra:
        raise ConfigError(f"Unknown {label} keys: {', '.join(sorted(extra))}")


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigError(f"{label} must be a nonempty string")
    return value


def _strings(value: Any, label: str) -> list[str]:
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values:
        raise ConfigError(f"{label} must be a string or a nonempty list of strings")
    return [_string(item, label) for item in values]


def _bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{label} must be a boolean")
    return value


def _number(value: Any, label: str, *, integer: bool = False) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (integer and not isinstance(value, int))
        or (isinstance(value, float) and not math.isfinite(value))
        or value < 0
    ):
        kind = "integer" if integer else "number"
        raise ConfigError(f"{label} must be a finite nonnegative {kind}")
    return value


def _path(value: Any, label: str, directory: Path) -> Path:
    text = _string(value, label)
    for variable in _ENV_VARIABLE.finditer(text):
        name = variable.group(1) or variable.group(2)
        if name not in os.environ:
            raise ConfigError(f"{label} refers to an unset environment variable: {name}")
    expanded = os.path.expandvars(text)
    _string(expanded, label)
    try:
        path = Path(expanded).expanduser()
        return (path if path.is_absolute() else directory / path).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError(f"Invalid path in {label}") from exc


def _json_values(value: Any, label: str, seen: frozenset[int] = frozenset()) -> Any:
    """Ensure persisted configuration is representable as ordinary JSON."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if id(value) in seen:
        raise ConfigError(f"{label} cannot contain recursive aliases")
    nested_seen = seen | {id(value)}
    if isinstance(value, list):
        return [_json_values(item, label, nested_seen) for item in value]
    if isinstance(value, dict):
        return {
            key: _json_values(item, label, nested_seen)
            for key, item in _mapping(value, label).items()
        }
    raise ConfigError(f"{label} must contain only JSON-compatible values")


def _rule(value: Any, index: int) -> Rule:
    label = f"rules[{index}]"
    data = _mapping(value, label)
    _unknown(data, frozenset({"name", "destination", "match", "enabled"}), label)
    for required in ("name", "destination", "match"):
        if required not in data:
            raise ConfigError(f"{label}.{required} is required")
    destination = _string(data["destination"], f"{label}.destination")
    try:
        validate_relative_destination(destination)
    except ValueError as exc:
        raise ConfigError(f"{label}.destination: {exc}") from exc
    conditions = deepcopy(_mapping(data["match"], f"{label}.match"))
    _unknown(conditions, _CONDITIONS, f"{label}.match")
    for key, value in conditions.items():
        if key in {"patterns", "extensions", "mime", "keywords"}:
            conditions[key] = _strings(value, f"{label}.match.{key}")
            if key == "extensions":
                conditions[key] = [item.lstrip(".").lower() for item in conditions[key]]
                if any(
                    not item or any(char in item for char in "/\\*?[]") for item in conditions[key]
                ):
                    raise ConfigError(
                        f"{label}.match.extensions must contain plain file extensions"
                    )
        else:
            conditions[key] = _number(
                value, f"{label}.match.{key}", integer=key in {"min_size", "max_size"}
            )
    if conditions.get("min_size", 0) > conditions.get("max_size", math.inf):
        raise ConfigError(f"{label}.match.min_size cannot exceed max_size")
    return Rule(
        name=_string(data["name"], f"{label}.name"),
        destination=destination,
        match=conditions,
        enabled=_bool(data.get("enabled", True), f"{label}.enabled"),
    )


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in pairs:
        if key in data:
            raise ConfigError("Duplicate JSON mapping key")
        data[key] = value
    return data


def _notifications(value: Any) -> dict[str, Any]:
    data = deepcopy(_mapping(value, "notifications"))
    _unknown(data, frozenset({"channels", "enabled", "timeout_seconds", "email"}), "notifications")
    channels = data.get("channels", [])
    if not isinstance(channels, list) or any(
        not isinstance(channel, str) or channel not in {"desktop", "email", "telegram"}
        for channel in channels
    ):
        raise ConfigError("notifications.channels must list desktop, email or telegram")
    if len(set(channels)) != len(channels):
        raise ConfigError("notifications.channels cannot contain duplicates")
    if "enabled" in data:
        _bool(data["enabled"], "notifications.enabled")
    if "timeout_seconds" in data:
        timeout = _number(data["timeout_seconds"], "notifications.timeout_seconds")
        if timeout == 0 or timeout > 60:
            raise ConfigError("notifications.timeout_seconds must be greater than 0 and at most 60")
    if "email" in data:
        email = _mapping(data["email"], "notifications.email")
        _unknown(email, frozenset({"ssl"}), "notifications.email")
        if "ssl" in email:
            _bool(email["ssl"], "notifications.email.ssl")
    return _json_values(data, "notifications")


def _schedule(value: Any) -> dict[str, Any]:
    data = deepcopy(_mapping(value, "schedule"))
    _unknown(data, frozenset({"cron", "interval_seconds", "timezone"}), "schedule")
    if "cron" in data and "interval_seconds" in data:
        raise ConfigError("schedule must choose cron or interval_seconds, not both")
    timezone_name = _string(data.get("timezone", "UTC"), "schedule.timezone")
    try:
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError("schedule.timezone must name a valid IANA timezone") from exc
    if "cron" in data:
        cron = _string(data["cron"], "schedule.cron")
        try:
            CronTrigger.from_crontab(cron, timezone=tz)
        except ValueError as exc:
            raise ConfigError(f"Invalid schedule.cron: {exc}") from exc
    if "interval_seconds" in data:
        if _number(data["interval_seconds"], "schedule.interval_seconds") == 0:
            raise ConfigError("schedule.interval_seconds must be greater than zero")
    return _json_values(data, "schedule")


def load_config(path: str | Path) -> AppConfig:
    """Read a YAML or JSON file; resolve local paths relative to that file.

    Loading creates no directories, databases, log files or application state.
    Environment variables are expanded only in filesystem paths, never saved
    as credentials in notification settings.
    """
    try:
        path = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError("Invalid configuration file path") from exc
    try:
        if path.stat().st_size > 1_048_576:
            raise ConfigError("Configuration exceeds the 1 MiB size limit")
        content = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            data = json.loads(content, object_pairs_hook=_json_pairs)
        elif path.suffix.lower() in {".yaml", ".yml"}:
            data = yaml.load(content, Loader=_UniqueKeyLoader)
        else:
            raise ConfigError("Configuration file must use .yaml, .yml or .json")
    except (OSError, UnicodeError, yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Unable to read configuration: {exc}") from exc
    data = _mapping(data, "Configuration")
    _unknown(data, _TOP_LEVEL, "configuration")
    for required in ("sources", "destination_root", "rules"):
        if required not in data:
            raise ConfigError(f"{required} is required")
    if not isinstance(data["sources"], list) or not data["sources"]:
        raise ConfigError("sources must be a nonempty list")
    sources = []
    for index, value in enumerate(data["sources"]):
        label = f"sources[{index}]"
        source = {"path": value} if isinstance(value, str) else _mapping(value, label)
        _unknown(source, frozenset({"path", "recursive"}), label)
        if "path" not in source:
            raise ConfigError(f"{label}.path is required")
        sources.append(
            SourceConfig(
                path=_path(source["path"], f"{label}.path", path.parent),
                recursive=_bool(source.get("recursive", True), f"{label}.recursive"),
            )
        )
    if not isinstance(data["rules"], list):
        raise ConfigError("rules must be a list")
    rules = [_rule(value, index) for index, value in enumerate(data["rules"])]
    if len({rule.name for rule in rules}) != len(rules):
        raise ConfigError("Rule names must be unique")
    duplicate_policy = _string(data.get("duplicate_policy", "skip"), "duplicate_policy")
    if duplicate_policy not in {"skip", "keep", "quarantine"}:
        raise ConfigError("duplicate_policy must be skip, keep or quarantine")
    duplicate_directory = _string(
        data.get("duplicate_directory", "Duplicates"), "duplicate_directory"
    )
    try:
        validate_relative_destination(duplicate_directory, templates=False)
    except ValueError as exc:
        raise ConfigError(f"duplicate_directory: {exc}") from exc
    exclude = data.get("exclude", ["*.part", "*.crdownload", "*.tmp"])
    if not isinstance(exclude, list):
        raise ConfigError("exclude must be a list of filename patterns")
    exclude = [_string(value, "exclude") for value in exclude]
    return AppConfig(
        sources=sources,
        destination_root=_path(data["destination_root"], "destination_root", path.parent),
        rules=rules,
        database=_path(
            data.get("database", ".smartfileorganizer/history.sqlite3"), "database", path.parent
        ),
        log_file=_path(
            data.get("log_file", ".smartfileorganizer/organizer.log"), "log_file", path.parent
        ),
        duplicate_policy=duplicate_policy,
        duplicate_directory=duplicate_directory,
        exclude=exclude,
        min_file_age_seconds=float(
            _number(data.get("min_file_age_seconds", 2.0), "min_file_age_seconds")
        ),
        notifications=_notifications(data.get("notifications", {})),
        schedule=_schedule(data.get("schedule", {})),
    )


def config_to_dict(config: AppConfig) -> dict[str, Any]:
    """Serialize configuration for history without referring to secret values."""
    return {
        "sources": [
            {"path": str(source.path), "recursive": source.recursive} for source in config.sources
        ],
        "destination_root": str(config.destination_root),
        "rules": [
            {
                "name": rule.name,
                "destination": rule.destination,
                "match": deepcopy(rule.match),
                "enabled": rule.enabled,
            }
            for rule in config.rules
        ],
        "database": str(config.database),
        "log_file": str(config.log_file),
        "duplicate_policy": config.duplicate_policy,
        "duplicate_directory": config.duplicate_directory,
        "exclude": list(config.exclude),
        "min_file_age_seconds": config.min_file_age_seconds,
        "notifications": deepcopy(config.notifications),
        "schedule": deepcopy(config.schedule),
    }
