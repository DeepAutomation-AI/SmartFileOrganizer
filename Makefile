PYTHON ?= python3
UV ?= uv
VENV ?= .venv
CONFIG ?= config/default.yaml
ENV_FILE ?= .env
APPLY ?=
VENV_PYTHON := $(VENV)/bin/python
CLI := $(VENV)/bin/smartfileorganizer --config "$(CONFIG)" --env-file "$(ENV_FILE)"

.PHONY: sync sync-realtime install install-realtime test lint format build validate run dry-run schedule watch

sync:
	UV_PROJECT_ENVIRONMENT="$(VENV)" $(UV) sync --locked --extra dev

sync-realtime:
	UV_PROJECT_ENVIRONMENT="$(VENV)" $(UV) sync --locked --extra dev --extra realtime

$(VENV_PYTHON):
	$(PYTHON) -m venv "$(VENV)"

install: $(VENV_PYTHON)
	$(VENV_PYTHON) -m pip install -e '.[dev]'

install-realtime: $(VENV_PYTHON)
	$(VENV_PYTHON) -m pip install -e '.[dev,realtime]'

test:
	$(VENV_PYTHON) -m pytest

lint:
	$(VENV_PYTHON) -m ruff check src tests

format:
	$(VENV_PYTHON) -m ruff format src tests

build:
	$(VENV_PYTHON) -m build

validate:
	$(CLI) validate

dry-run:
	$(CLI) organize --mode dry-run

run:
	$(CLI) organize --mode interactive

schedule:
	$(CLI) schedule $(APPLY)

watch:
	$(CLI) watch $(APPLY)
