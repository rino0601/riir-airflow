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
export AIRFLOW__CORE__DAGS_FOLDER=$(VIRTUAL_ENV)/dags
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__WEBSERVER__EXPOSE_CONFIG=True

# Show help message(Default target; first present)
.PHONY: help
help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  help                  Show this help message"
	@echo "  setup                 setup dependencies using Uv"
	@echo "  run                   Run the application (airflow standalone)"
	@echo "  test                  Run tests using pytest"
	@echo "  format,format-check   Format code"
	@echo "  lint,lint-fix         Lint code"
	@echo "  clean,clean-hard      Clean build artifacts"

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
	riir-airflow

# Clean build artifacts
.PHONY: clean clean-hard
clean:
	rm -rf .ruff_cache .pytest_cache .nicegui/ \
		$(VIRTUAL_ENV)/airflow
clean-hard: clean
	rye sync -f

