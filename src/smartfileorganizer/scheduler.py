"""Scheduled and optional filesystem-triggered organization."""

from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .engine import Organizer


def _run(config: Any, apply: bool, logger: logging.Logger) -> None:
    """Each trigger uses a fresh organizer, so SQLite is owned by its thread."""
    try:
        summary = Organizer(config).run(mode="automatic" if apply else "dry-run")
        logger.info("scheduled_run", extra={"summary": summary.to_dict()})
    except Exception as exc:
        logger.error("Falló la ejecución programada (%s)", type(exc).__name__)


def start_scheduler(
    config: Any, *, apply: bool = False, logger: logging.Logger | None = None
) -> None:
    """Block until interrupted. Automatic movement always requires ``apply``."""
    logger = logger or logging.getLogger("smartfileorganizer")
    settings = config.schedule or {}
    timezone = ZoneInfo(str(settings.get("timezone", "UTC")))
    scheduler = BlockingScheduler(timezone=timezone)
    options: dict[str, Any] = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 60}
    cron = settings.get("cron")
    if cron:
        if not isinstance(cron, str) or len(cron.split()) != 5:
            raise ValueError("schedule.cron debe tener cinco campos")
        trigger = CronTrigger.from_crontab(cron, timezone=timezone)
        scheduler.add_job(_run, trigger, args=[config, apply, logger], **options)
    else:
        seconds = float(settings.get("interval_seconds", 0))
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Configura schedule.interval_seconds positivo o schedule.cron")
        scheduler.add_job(
            _run, "interval", seconds=seconds, args=[config, apply, logger], **options
        )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Programación detenida")
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=True)


def start_watch(
    config: Any,
    *,
    apply: bool = False,
    logger: logging.Logger | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Debounce filesystem events, respect file age, and serialize complete scans.

    ``stop_event`` enables graceful programmatic termination and deterministic
    tests. Watchdog is an optional extra and never imported for other commands.
    """
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError as exc:
        raise RuntimeError(
            "Instala tiempo real con: pip install 'smartfileorganizer[realtime]'"
        ) from exc

    logger = logger or logging.getLogger("smartfileorganizer")
    stop_event = stop_event or threading.Event()
    changed = threading.Event()
    destination = Path(config.destination_root).resolve()
    database = Path(config.database).resolve()
    log_file = Path(config.log_file).resolve()
    internal_paths = {database, log_file}
    internal_paths.update(
        Path(f"{database}{suffix}") for suffix in ("-journal", "-wal", "-shm", ".lock")
    )
    quiet_seconds = max(1.0, float(config.min_file_age_seconds) + 0.1)

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event: Any) -> None:
            if event.is_directory or event.event_type not in {"created", "modified", "moved"}:
                return
            path = Path(getattr(event, "dest_path", None) or event.src_path).resolve()
            if (
                path == destination
                or destination in path.parents
                or path in internal_paths
                or (path.parent == log_file.parent and path.name.startswith(f"{log_file.name}."))
                or path.name.startswith(".smartfileorganizer-")
            ):
                return
            changed.set()

    observer = Observer()
    handler = Handler()
    for source in config.sources:
        observer.schedule(handler, str(source.path), recursive=source.recursive)
    started = False
    try:
        observer.start()
        started = True
        while not stop_event.is_set():
            if not changed.wait(timeout=0.5):
                continue
            changed.clear()
            deadline = time.monotonic() + quiet_seconds
            while not stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if changed.wait(timeout=min(0.5, remaining)):
                    changed.clear()
                    deadline = time.monotonic() + quiet_seconds
            if not stop_event.is_set():
                _run(config, apply, logger)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Observación detenida")
    finally:
        observer.stop()
        if started:
            observer.join()
