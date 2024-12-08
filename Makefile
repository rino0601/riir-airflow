# Makefile for managing a Python project with Rye
# Ensure virtual environment is activated
ifeq ($(VIRTUAL_ENV),)
$(error VIRTUAL_ENV is not set, please source .venv/bin/activate)
endif

# ENV
export SQLALCHEMY_SILENCE_UBER_WARNING=1
export AIRFLOW_HOME=$(VIRTUAL_ENV)/airflow
export AIRFLOW__LOGGING__LOGGING_LEVEL=INFO
export AIRFLOW__CORE__EXECUTOR=riir_airflow.executors.asgi_executor.AsgiExecutor
export AIRFLOW__CORE__DAGS_FOLDER=$(shell realpath $(VIRTUAL_ENV)/..)/dags
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__WEBSERVER__EXPOSE_CONFIG=True

# Default target
.PHONY: all
all: help

# setup dependencies
.PHONY: setup update-all
configure:
	@echo SQLALCHEMY_SILENCE_UBER_WARNING=$(SQLALCHEMY_SILENCE_UBER_WARNING) > .env
	@echo AIRFLOW_HOME=$(AIRFLOW_HOME) >>.env
	@echo AIRFLOW__LOGGING__LOGGING_LEVEL=$(AIRFLOW__LOGGING__LOGGING_LEVEL) >>.env
	@echo AIRFLOW__CORE__EXECUTOR=$(AIRFLOW__CORE__EXECUTOR) >>.env
	@echo AIRFLOW__CORE__DAGS_FOLDER=$(AIRFLOW__CORE__DAGS_FOLDER) >>.env
	@echo AIRFLOW__CORE__LOAD_EXAMPLES=$(AIRFLOW__CORE__LOAD_EXAMPLES) >>.env
	@echo AIRFLOW__CORE__LOAD_EXAMPLES=$(AIRFLOW__CORE__LOAD_EXAMPLES) >>.env
	
setup: configure
	uv sync --frozen
	uv run pre-commit install

# Format code
.PHONY: format format-check
format:
	ruff format 
format-check:
	ruff format --check

# Lint code
.PHONY: lint lint-fix
lint:
	ruff check
lint-fix:
	ruff check --fix

# Check types with pyright
.PHONY: type-check
type-check: ensure-node
	pyright

# Run tests
.PHONY: test
test: setup format-check lint
	pytest

.PHONY: docs docs-check
docs:
	mkdocs serve
docs-check:
	mkdocs build --strict

# Run the application
.PHONY: run
run: setup
	riir-airflow standalone

# Clean build artifacts
.PHONY: clean clean-hard
clean:
	rm -rf .ruff_cache .pytest_cache \
		$(VIRTUAL_ENV)/airflow
clean-hard: clean
	rye sync -f

# Show help message
.PHONY: help
help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  setup                 setup dependencies using Uv"
	@echo "  run                   Run the application (airflow standalone)"
	@echo "  test                  Run tests using pytest"
	@echo "  format,format-check   Format code"
	@echo "  lint,lint-fix         Lint code"
	@echo "  type-check            Check types using pyright"
	@echo "  clean,clean-hard      Clean build artifacts"
	@echo "  help                  Show this help message"
