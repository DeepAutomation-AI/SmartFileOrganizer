"""Click command line interface. File movement is always an explicit choice."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import click
from dotenv import load_dotenv

from .config import ConfigError, config_to_dict, load_config
from .engine import Organizer
from .history import History
from .scheduler import start_scheduler, start_watch


def _json(value: Any) -> None:
    click.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _load(context: click.Context) -> Any:
    try:
        return load_config(context.obj["config_path"])
    except (ConfigError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Configuración YAML/JSON (SFO_CONFIG o config/default.yaml).",
)
@click.option(
    "--env-file",
    type=click.Path(path_type=Path),
    default=Path(".env"),
    show_default=True,
    help="Variables locales; respeta las variables ya exportadas.",
)
@click.pass_context
def main(context: click.Context, config_path: Path | None, env_file: Path) -> None:
    """SmartFileOrganizer: organiza archivos usando reglas seguras y auditables."""
    try:
        load_dotenv(dotenv_path=env_file, override=False)
    except OSError as exc:
        raise click.ClickException(f"No se pudo leer el archivo .env: {exc}") from exc
    context.ensure_object(dict)
    context.obj["config_path"] = config_path or Path(
        os.environ.get("SFO_CONFIG", "config/default.yaml")
    )


@main.command()
@click.option(
    "--mode",
    type=click.Choice(["dry-run", "interactive", "automatic"]),
    default="dry-run",
    show_default=True,
)
@click.pass_context
def organize(context: click.Context, mode: str) -> None:
    """Escanea las fuentes y organiza (por defecto solo muestra el plan)."""
    config = _load(context)

    def confirm(source: Path, destination: Path) -> bool:
        return click.confirm(f"¿Mover {source} → {destination}?", default=False)

    try:
        summary = Organizer(config).run(
            mode=mode, confirm=confirm if mode == "interactive" else None
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(f"No se pudo organizar: {exc}") from exc
    result = summary.to_dict()
    _json(result)
    if result.get("errors", result.get("counts", {}).get("errors", 0)):
        raise click.ClickException(
            "La ejecución terminó con errores; consulta el resumen y, en modo real, el historial"
        )


@main.command()
@click.pass_context
def validate(context: click.Context) -> None:
    """Valida y muestra la configuración efectiva sin mover archivos."""
    _json(config_to_dict(_load(context)))


@main.command()
@click.option("--limit", type=click.IntRange(1, 10000), default=20, show_default=True)
@click.pass_context
def history(context: click.Context, limit: int) -> None:
    """Muestra movimientos recientes; no crea una base de datos inexistente."""
    config = _load(context)
    if not Path(config.database).exists():
        _json([])
        return
    try:
        with History(config.database, read_only=True) as database:
            _json(database.recent(limit=limit))
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        raise click.ClickException(f"No se pudo leer el historial: {exc}") from exc


@main.command()
@click.option("--apply", is_flag=True, help="Autoriza movimientos automáticos en cada ejecución.")
@click.pass_context
def schedule(context: click.Context, apply: bool) -> None:
    """Ejecuta la programación hasta Ctrl-C; requiere --apply."""
    if not apply:
        raise click.ClickException(
            "schedule requiere --apply para autorizar movimientos automáticos"
        )
    config = _load(context)
    logger = logging.getLogger("smartfileorganizer")
    click.echo("Programación automática iniciada; Ctrl-C para salir.")
    try:
        start_scheduler(config, apply=apply, logger=logger)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        raise click.ClickException(f"No se pudo iniciar la programación: {exc}") from exc


@main.command()
@click.option("--apply", is_flag=True, help="Autoriza movimientos al detectar cambios.")
@click.pass_context
def watch(context: click.Context, apply: bool) -> None:
    """Observa cambios con watchdog; requiere --apply."""
    if not apply:
        raise click.ClickException("watch requiere --apply para autorizar movimientos automáticos")
    config = _load(context)
    logger = logging.getLogger("smartfileorganizer")
    click.echo("Observación automática iniciada; Ctrl-C para salir.")
    try:
        start_watch(config, apply=apply, logger=logger)
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(f"No se pudo observar: {exc}") from exc


if __name__ == "__main__":
    main()
