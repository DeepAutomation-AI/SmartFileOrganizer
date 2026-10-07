"""Transport, CLI and background-service tests run without network or long loops."""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from click.testing import CliRunner

from smartfileorganizer import cli, notifications, scheduler
from smartfileorganizer.config import ConfigError
from smartfileorganizer.notifications import Notifier


@pytest.fixture
def service_config(tmp_path):
    source = tmp_path / "inbox"
    source.mkdir()
    return SimpleNamespace(
        sources=[SimpleNamespace(path=source, recursive=True)],
        destination_root=tmp_path / "organized",
        database=tmp_path / "history.sqlite3",
        log_file=tmp_path / "events.jsonl",
        min_file_age_seconds=0,
        schedule={"interval_seconds": 30, "timezone": "UTC"},
    )


@pytest.fixture
def cli_setup(monkeypatch, service_config):
    loader = Mock(return_value=service_config)
    monkeypatch.setattr(cli, "load_config", loader)
    monkeypatch.setattr(cli, "load_dotenv", Mock())
    monkeypatch.setattr(cli, "config_to_dict", lambda config: {"database": str(config.database)})
    return CliRunner(), loader


def test_cli_help_does_not_load_config(cli_setup):
    runner, loader = cli_setup
    result = runner.invoke(cli.main, ["--help"])
    assert result.exit_code == 0
    assert all(
        command in result.output
        for command in ("organize", "schedule", "watch", "history", "validate")
    )
    loader.assert_not_called()


def test_cli_default_dry_run_and_configuration_env(cli_setup, monkeypatch):
    runner, loader = cli_setup
    monkeypatch.setenv("SFO_CONFIG", "custom.yaml")
    summary = SimpleNamespace(to_dict=lambda: {"scanned": 1, "planned": 1, "errors": 0})
    organizer = Mock()
    organizer.run.return_value = summary
    monkeypatch.setattr(cli, "Organizer", Mock(return_value=organizer))
    result = runner.invoke(cli.main, ["organize"])
    assert result.exit_code == 0
    assert json.loads(result.output)["planned"] == 1
    loader.assert_called_once_with(Path("custom.yaml"))
    organizer.run.assert_called_once_with(mode="dry-run", confirm=None)
    cli.load_dotenv.assert_called_once_with(dotenv_path=Path(".env"), override=False)


def test_cli_explicit_configuration_and_validation(cli_setup):
    runner, loader = cli_setup
    result = runner.invoke(cli.main, ["--config", "selected.json", "validate"])
    assert result.exit_code == 0
    assert "database" in json.loads(result.output)
    loader.assert_called_once_with(Path("selected.json"))


@pytest.mark.parametrize("answer, expected", [("y\n", True), ("n\n", False)])
def test_cli_interactive_confirmation(cli_setup, monkeypatch, answer, expected):
    runner, _ = cli_setup
    accepted = []

    def run(**kwargs):
        assert kwargs["mode"] == "interactive"
        accepted.append(kwargs["confirm"](Path("a.pdf"), Path("docs/a.pdf")))
        return SimpleNamespace(to_dict=lambda: {"errors": 0})

    monkeypatch.setattr(cli, "Organizer", Mock(return_value=SimpleNamespace(run=run)))
    result = runner.invoke(cli.main, ["organize", "--mode", "interactive"], input=answer)
    assert result.exit_code == 0
    assert accepted == [expected]
    assert "¿Mover" in result.output


def test_cli_automatic_and_error_exit(cli_setup, monkeypatch):
    runner, _ = cli_setup
    organizer = Mock()
    organizer.run.return_value = SimpleNamespace(to_dict=lambda: {"counts": {"errors": 2}})
    monkeypatch.setattr(cli, "Organizer", Mock(return_value=organizer))
    result = runner.invoke(cli.main, ["organize", "--mode", "automatic"])
    assert result.exit_code == 1
    assert "terminó con errores" in result.output
    organizer.run.assert_called_once_with(mode="automatic", confirm=None)


@pytest.mark.parametrize("command", ["schedule", "watch"])
@pytest.mark.parametrize("apply", [True, False])
def test_cli_background_requires_explicit_apply(cli_setup, monkeypatch, command, apply):
    runner, _ = cli_setup
    start = Mock()
    monkeypatch.setattr(cli, "start_" + ("scheduler" if command == "schedule" else "watch"), start)
    result = runner.invoke(cli.main, [command] + (["--apply"] if apply else []))
    assert result.exit_code == (0 if apply else 1)
    if apply:
        assert start.call_args.kwargs["apply"] is True
    else:
        start.assert_not_called()
        assert "requiere --apply" in result.output


def test_cli_history_missing_does_not_create_database(cli_setup, service_config, monkeypatch):
    runner, _ = cli_setup
    history = Mock()
    monkeypatch.setattr(cli, "History", history)
    result = runner.invoke(cli.main, ["history"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []
    assert not service_config.database.exists()
    history.assert_not_called()


def test_cli_history_read_only_and_limit(cli_setup, service_config, monkeypatch):
    runner, _ = cli_setup
    service_config.database.touch()
    database = MagicMock()
    database.__enter__.return_value.recent.return_value = [{"source": "a.pdf"}]
    history = Mock(return_value=database)
    monkeypatch.setattr(cli, "History", history)
    result = runner.invoke(cli.main, ["history", "--limit", "3"])
    assert result.exit_code == 0
    history.assert_called_once_with(service_config.database, read_only=True)
    database.__enter__.return_value.recent.assert_called_once_with(limit=3)


@pytest.mark.parametrize(
    "command, exception",
    [
        ("validate", ConfigError("configuración inválida")),
        ("validate", PermissionError("sin permiso")),
    ],
)
def test_cli_configuration_errors(cli_setup, command, exception):
    runner, loader = cli_setup
    loader.side_effect = exception
    result = runner.invoke(cli.main, [command])
    assert result.exit_code == 1
    assert "Error:" in result.output


def test_cli_operational_errors(cli_setup, service_config, monkeypatch):
    runner, _ = cli_setup
    service_config.database.touch()
    monkeypatch.setattr(cli, "History", Mock(side_effect=sqlite3.DatabaseError("corrupta")))
    assert runner.invoke(cli.main, ["history"]).exit_code == 1
    monkeypatch.setattr(cli, "Organizer", Mock(side_effect=OSError("fallo de lectura")))
    assert runner.invoke(cli.main, ["organize"]).exit_code == 1
    monkeypatch.setattr(cli, "start_scheduler", Mock(side_effect=ValueError("cron inválido")))
    assert runner.invoke(cli.main, ["schedule", "--apply"]).exit_code == 1
    monkeypatch.setattr(cli, "start_watch", Mock(side_effect=RuntimeError("instala watchdog")))
    assert runner.invoke(cli.main, ["watch", "--apply"]).exit_code == 1
    monkeypatch.setattr(cli, "load_dotenv", Mock(side_effect=OSError("ilegible")))
    assert runner.invoke(cli.main, ["validate"]).exit_code == 1


def test_scheduler_interval_non_overlapping_and_interrupt(service_config, monkeypatch):
    instance = MagicMock()
    instance.start.side_effect = KeyboardInterrupt
    instance.running = True
    factory = Mock(return_value=instance)
    monkeypatch.setattr(scheduler, "BlockingScheduler", factory)
    scheduler.start_scheduler(service_config, apply=False)
    args, kwargs = instance.add_job.call_args
    assert args[1] == "interval"
    assert kwargs["seconds"] == 30
    assert kwargs["max_instances"] == 1
    assert kwargs["coalesce"] is True
    assert kwargs["args"][1] is False
    instance.shutdown.assert_called_once_with(wait=True)


def test_scheduler_cron_and_apply(service_config, monkeypatch):
    service_config.schedule = {"cron": "0 8 * * *", "timezone": "UTC"}
    instance = MagicMock(running=False)
    monkeypatch.setattr(scheduler, "BlockingScheduler", Mock(return_value=instance))
    scheduler.start_scheduler(service_config, apply=True)
    trigger = instance.add_job.call_args.args[1]
    assert str(trigger).startswith("cron[")
    assert instance.add_job.call_args.kwargs["args"][1] is True
    instance.shutdown.assert_not_called()


@pytest.mark.parametrize(
    "settings",
    [
        {"interval_seconds": 0},
        {"interval_seconds": -2},
        {"interval_seconds": "nan"},
        {"cron": "0 8 *"},
        {"cron": 12},
        {"cron": "not a valid cron expression"},
    ],
)
def test_scheduler_rejects_invalid_settings(service_config, settings):
    service_config.schedule = settings
    with pytest.raises(ValueError):
        scheduler.start_scheduler(service_config)


@pytest.mark.parametrize("apply, mode", [(False, "dry-run"), (True, "automatic")])
def test_scheduled_job_modes(service_config, monkeypatch, apply, mode):
    organizer = Mock()
    organizer.run.return_value = SimpleNamespace(to_dict=lambda: {"errors": 0})
    monkeypatch.setattr(scheduler, "Organizer", Mock(return_value=organizer))
    scheduler._run(service_config, apply, logging.getLogger("test.scheduler"))
    organizer.run.assert_called_once_with(mode=mode)


def test_scheduled_job_failure_isolated(service_config, monkeypatch, caplog):
    monkeypatch.setattr(scheduler, "Organizer", Mock(side_effect=OSError("sensitive detail")))
    scheduler._run(service_config, True, logging.getLogger("test.scheduler"))
    assert "Falló la ejecución" in caplog.text
    assert "sensitive detail" not in caplog.text


def test_watch_missing_dependency(service_config, monkeypatch):
    monkeypatch.setitem(sys.modules, "watchdog.events", None)
    with pytest.raises(RuntimeError, match=r"smartfileorganizer\[realtime\]"):
        scheduler.start_watch(service_config)


def test_watch_graceful_stop_and_ignored_events(service_config, monkeypatch):
    fake_observer = MagicMock()
    monkeypatch.setitem(
        sys.modules, "watchdog.events", SimpleNamespace(FileSystemEventHandler=object)
    )
    monkeypatch.setitem(
        sys.modules, "watchdog.observers", SimpleNamespace(Observer=lambda: fake_observer)
    )
    stopped = threading.Event()
    stopped.set()
    scheduler.start_watch(service_config, stop_event=stopped)
    fake_observer.start.assert_called_once()
    fake_observer.stop.assert_called_once()
    fake_observer.join.assert_called_once()
    handler = fake_observer.schedule.call_args.args[0]
    for path, event_type, is_directory in [
        (service_config.database, "modified", False),
        (service_config.destination_root / "out.pdf", "created", False),
        (service_config.sources[0].path, "created", True),
        (service_config.sources[0].path / "old.pdf", "deleted", False),
        (service_config.sources[0].path / "new.pdf", "created", False),
    ]:
        handler.on_any_event(
            SimpleNamespace(src_path=str(path), event_type=event_type, is_directory=is_directory)
        )
    assert fake_observer.schedule.call_args.kwargs["recursive"] is True


def test_watch_debounces_events_and_runs_once(service_config, monkeypatch):
    fake_observer = MagicMock()
    monkeypatch.setitem(
        sys.modules, "watchdog.events", SimpleNamespace(FileSystemEventHandler=object)
    )
    monkeypatch.setitem(
        sys.modules, "watchdog.observers", SimpleNamespace(Observer=lambda: fake_observer)
    )
    stopped = threading.Event()
    changed = Mock()
    changed.wait.side_effect = [False, True, True, False]
    monkeypatch.setattr(scheduler.threading, "Event", lambda: changed)
    clock = iter([0, 0, 0.1, 0.2, 2])
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: next(clock))
    execute = Mock(side_effect=lambda *args: stopped.set())
    monkeypatch.setattr(scheduler, "_run", execute)
    scheduler.start_watch(service_config, apply=True, stop_event=stopped)
    execute.assert_called_once_with(service_config, True, logging.getLogger("smartfileorganizer"))
    assert changed.clear.call_count == 2


def test_watch_ignores_own_state_and_rotated_logs_inside_source(service_config, monkeypatch):
    source = service_config.sources[0].path
    service_config.database = source / "state" / "history.sqlite3"
    service_config.log_file = source / "logs" / "events.jsonl"
    fake_observer = MagicMock()
    monkeypatch.setitem(
        sys.modules, "watchdog.events", SimpleNamespace(FileSystemEventHandler=object)
    )
    monkeypatch.setitem(
        sys.modules, "watchdog.observers", SimpleNamespace(Observer=lambda: fake_observer)
    )
    stopped = threading.Event()
    stopped.set()
    changed = Mock()
    monkeypatch.setattr(scheduler.threading, "Event", lambda: changed)
    scheduler.start_watch(service_config, stop_event=stopped)
    handler = fake_observer.schedule.call_args.args[0]
    internal = [
        service_config.database,
        *(
            Path(f"{service_config.database}{suffix}")
            for suffix in ("-journal", "-wal", "-shm", ".lock")
        ),
        service_config.log_file,
        Path(f"{service_config.log_file}.1"),
        Path(f"{service_config.log_file}.2"),
        source / ".smartfileorganizer-staging",
    ]
    for path in internal:
        handler.on_any_event(
            SimpleNamespace(src_path=str(path), event_type="modified", is_directory=False)
        )
    handler.on_any_event(
        SimpleNamespace(
            src_path=str(service_config.log_file),
            dest_path=f"{service_config.log_file}.1",
            event_type="moved",
            is_directory=False,
        )
    )
    changed.set.assert_not_called()
    for path in (source / "report.csv", source / "another" / "events.jsonl.1"):
        handler.on_any_event(
            SimpleNamespace(src_path=str(path), event_type="created", is_directory=False)
        )
    assert changed.set.call_count == 2


def test_watch_keyboard_interrupt(service_config, monkeypatch):
    fake_observer = MagicMock()
    fake_observer.start.side_effect = KeyboardInterrupt
    monkeypatch.setitem(
        sys.modules, "watchdog.events", SimpleNamespace(FileSystemEventHandler=object)
    )
    monkeypatch.setitem(
        sys.modules, "watchdog.observers", SimpleNamespace(Observer=lambda: fake_observer)
    )
    scheduler.start_watch(service_config)
    fake_observer.stop.assert_called_once()
    fake_observer.join.assert_not_called()


def test_watch_start_failure_preserves_original_error(service_config, monkeypatch):
    fake_observer = MagicMock()
    fake_observer.start.side_effect = FileNotFoundError("fuente inexistente")
    fake_observer.join.side_effect = RuntimeError("cannot join before start")
    monkeypatch.setitem(
        sys.modules, "watchdog.events", SimpleNamespace(FileSystemEventHandler=object)
    )
    monkeypatch.setitem(
        sys.modules, "watchdog.observers", SimpleNamespace(Observer=lambda: fake_observer)
    )
    with pytest.raises(FileNotFoundError, match="fuente inexistente"):
        scheduler.start_watch(service_config)
    fake_observer.stop.assert_called_once()
    fake_observer.join.assert_not_called()


def test_notification_summary_disabled_and_unknown_channel(caplog):
    assert "4 movidos" in Notifier._message({"counts": {"moved": 4}})
    assert "0 movidos" in Notifier._message({"counts": 3, "moved": "invalid"})
    notifier = Notifier({"enabled": False, "channels": ["email"]})
    notifier._email = Mock()
    notifier.send({})
    notifier._email.assert_not_called()
    notifier = Notifier({"channels": ["email"]})
    notifier._email = Mock()
    notifier.send({"mode": "dry-run"})
    notifier._email.assert_not_called()
    Notifier({"channels": ["unknown"]}).send({})
    assert "desconocido" in caplog.text


def test_notification_channel_failure_redacted_and_other_channel_continues(caplog):
    notifier = Notifier({"channels": ["telegram", "email"]})
    notifier._telegram = Mock(side_effect=RuntimeError("secret-token https://secret"))
    notifier._email = Mock()
    notifier.send({"scanned": 3})
    notifier._email.assert_called_once()
    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text


def test_desktop_notification(monkeypatch):
    transport = Mock()
    monkeypatch.setitem(sys.modules, "plyer", SimpleNamespace(notification=transport))
    Notifier({"channels": "desktop"}).send({"moved": 2})
    assert "2 movidos" in transport.notify.call_args.kwargs["message"]


def _smtp_environment(monkeypatch):
    for name, value in {
        "SMTP_HOST": "smtp.example.org",
        "SMTP_FROM": "source@example.org",
        "SMTP_TO": "one@example.org, two@example.org",
        "SMTP_USERNAME": "username",
        "SMTP_PASSWORD": "password",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SMTP_PORT", raising=False)


@pytest.mark.parametrize("use_ssl", [False, True])
def test_email_verified_tls(monkeypatch, use_ssl):
    _smtp_environment(monkeypatch)
    transport = MagicMock()
    factory = Mock(return_value=transport)
    monkeypatch.setattr(notifications.smtplib, "SMTP_SSL" if use_ssl else "SMTP", factory)
    Notifier({"channels": ["email"], "email": {"ssl": use_ssl}}).send({"moved": 2})
    assert factory.call_args.args == ("smtp.example.org", 465 if use_ssl else 587)
    client = transport.__enter__.return_value
    client.login.assert_called_once_with("username", "password")
    client.send_message.assert_called_once()
    assert client.send_message.call_args.kwargs["to_addrs"] == [
        "one@example.org",
        "two@example.org",
    ]
    if use_ssl:
        assert factory.call_args.kwargs["context"].check_hostname is True
    else:
        assert client.starttls.call_args.kwargs["context"].check_hostname is True
        assert client.ehlo.call_count == 2


def test_email_missing_credentials_and_invalid_port(monkeypatch, caplog):
    for name in ("SMTP_HOST", "SMTP_FROM", "SMTP_TO"):
        monkeypatch.delenv(name, raising=False)
    Notifier({"channels": ["email"]}).send({})
    assert "SMTP_HOST" in caplog.text
    _smtp_environment(monkeypatch)
    monkeypatch.delenv("SMTP_PASSWORD")
    Notifier({"channels": ["email"]}).send({})
    assert "incompletas" in caplog.text
    monkeypatch.setenv("SMTP_PASSWORD", "password")
    monkeypatch.setenv("SMTP_PORT", "0")
    Notifier({"channels": ["email"]}).send({})
    assert "ValueError" in caplog.text
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_TO", " , ")
    Notifier({"channels": ["email"]}).send({})


def test_telegram_transport_verifies_tls_and_payload(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "12345:secret_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987")
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"ok":true}'
    transport = Mock(return_value=response)
    monkeypatch.setattr(notifications.urllib.request, "urlopen", transport)
    Notifier({"channels": ["telegram"]}).send({"moved": 5})
    request = transport.call_args.args[0]
    assert request.full_url.startswith("https://api.telegram.org/")
    assert json.loads(request.data)["chat_id"] == "987"
    assert "5 movidos" in json.loads(request.data)["text"]
    assert transport.call_args.kwargs["context"].check_hostname is True


def test_telegram_missing_invalid_and_rejected_credentials(monkeypatch, caplog):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    Notifier({"channels": ["telegram"]}).send({})
    assert "TELEGRAM_BOT_TOKEN" in caplog.text
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    Notifier({"channels": ["telegram"]}).send({})
    assert "secret-token" not in caplog.text
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"ok":false}'
    monkeypatch.setattr(notifications.urllib.request, "urlopen", Mock(return_value=response))
    Notifier({"channels": ["telegram"]}).send({})
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("timeout", [0, -1, 61, "invalid"])
def test_notification_rejects_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        Notifier({"timeout_seconds": timeout})._timeout()
