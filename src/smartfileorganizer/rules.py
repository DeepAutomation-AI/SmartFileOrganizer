"""Deterministic rule matching and confined destination rendering."""

from __future__ import annotations

from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath, PureWindowsPath
from string import Formatter

from .models import FileInfo, Rule

TEMPLATE_FIELDS = frozenset({"year", "año", "month", "day", "extension", "stem", "name"})


def validate_relative_destination(destination: str, *, templates: bool = True) -> None:
    """Reject absolute paths, traversal and executable formatting expressions.

    Backslashes and Windows drive syntax are rejected on every OS so that a
    configuration cannot change meaning when deployed on another platform.
    """
    if not isinstance(destination, str) or not destination.strip():
        raise ValueError("Destination must be a nonempty relative directory")
    posix = PurePosixPath(destination)
    windows = PureWindowsPath(destination)
    if (
        posix.is_absolute()
        or windows.drive
        or windows.root
        or "\\" in destination
        or "\x00" in destination
        or any(part in {".", ".."} for part in destination.split("/"))
    ):
        raise ValueError("Destination must be relative and cannot contain traversal")
    if templates:
        for _, field_name, format_spec, conversion in Formatter().parse(destination):
            if field_name is not None and (
                field_name not in TEMPLATE_FIELDS or format_spec or conversion
            ):
                raise ValueError("Destination contains an unsupported template field")


def _items(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else value


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def matching_rule(info: FileInfo, rules: list[Rule], now: datetime | None = None) -> Rule | None:
    """Return the first enabled rule whose conditions all match.

    Lists within a condition mean "any". Size boundaries are inclusive; age
    boundaries are strict. ``older_than_days`` refers to modification time.
    """
    reference = _utc(now or datetime.now(timezone.utc))
    modified_age = (reference - _utc(info.modified)).total_seconds() / 86400
    created_age = (reference - _utc(info.created)).total_seconds() / 86400
    for rule in rules:
        if not rule.enabled:
            continue
        match = rule.match
        if "patterns" in match and not any(
            fnmatchcase(info.name, pattern) for pattern in _items(match["patterns"])
        ):
            continue
        if "extensions" in match and info.extension not in {
            extension.lstrip(".").lower() for extension in _items(match["extensions"])
        }:
            continue
        if "mime" in match and not any(
            fnmatchcase(info.mime.lower(), pattern.lower()) for pattern in _items(match["mime"])
        ):
            continue
        if "keywords" in match and not any(
            word.casefold() in info.name.casefold() for word in _items(match["keywords"])
        ):
            continue
        if "min_size" in match and info.size < match["min_size"]:
            continue
        if "max_size" in match and info.size > match["max_size"]:
            continue
        if "older_than_days" in match and modified_age <= match["older_than_days"]:
            continue
        if (
            "modified_older_than_days" in match
            and modified_age <= match["modified_older_than_days"]
        ):
            continue
        if "created_older_than_days" in match and created_age <= match["created_older_than_days"]:
            continue
        if "newer_than_days" in match and modified_age >= match["newer_than_days"]:
            continue
        return rule
    return None


def resolve_destination(info: FileInfo, rule: Rule, root: Path) -> Path:
    """Render a rule's directory, append the filename and enforce root confinement."""
    validate_relative_destination(rule.destination)
    modified = _utc(info.modified)
    rendered = rule.destination.format_map(
        {
            "year": f"{modified.year:04d}",
            "año": f"{modified.year:04d}",
            "month": f"{modified.month:02d}",
            "day": f"{modified.day:02d}",
            "extension": info.extension,
            "stem": info.path.stem,
            "name": info.name,
        }
    )
    validate_relative_destination(rendered, templates=False)
    if info.name in {"", ".", ".."} or "/" in info.name or "\\" in info.name:
        raise ValueError("Source filename is unsafe for a destination")
    canonical_root = Path(root).resolve()
    destination = (canonical_root / rendered / info.name).resolve()
    if not destination.is_relative_to(canonical_root):
        raise ValueError("Destination escapes destination_root, possibly through a symlink")
    return destination
