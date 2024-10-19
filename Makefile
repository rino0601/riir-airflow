# Makefile for managing a Python project with Rye
# Ensure virtual environment is activated
ifeq ($(VIRTUAL_ENV),)
$(error VIRTUAL_ENV is not set, please source .venv/bin/activate)
endif

# ENV
export SQLALCHEMY_SILENCE_UBER_WARNING=1
export AIRFLOW_HOME=$(VIRTUAL_ENV)/airflow
export AIRFLOW__LOGGING__LOGGING_LEVEL=INFO
# export AIRFLOW__CORE__EXECUTOR=riir_airflow.executors.asgi_executor.AsgiExecutor
export AIRFLOW__CORE__DAGS_FOLDER=$(shell realpath $(VIRTUAL_ENV)/..)/dags
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__WEBSERVER__EXPOSE_CONFIG=True

# Default target (1st)
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

# setup dependencies
.PHONY: setup ensure-node
setup:
	uv sync --frozen
	uv run pre-commit install
update-all:
	uv sync --all-extras --upgrade

$(VIRTUAL_ENV)/include/node: |setup
	nodeenv -p
ensure-node: $(VIRTUAL_ENV)/include/node

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
test: setup
	pytest

.PHONY: docs docs-check
docs: setup
	mkdocs serve
docs-check: setup
	mkdocs build --strict

.PHONY: build
build: setup
	uv build .

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
	rm -rf $(VIRTUAL_ENV)