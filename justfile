set dotenv-load := true
set dotenv-required := true

run: venv
    .venv/bin/python -c "print('Hello, World!')"

# make .env file use --no-dotenv to run this recipe in first time
[confirm]
@configure:
    echo SQLALCHEMY_SILENCE_UBER_WARNING=1 > .env
    echo AIRFLOW_HOME={{ absolute_path(".venv/airflow") }} >>.env
    echo AIRFLOW__LOGGING__LOGGING_LEVEL=INFO >>.env
    echo AIRFLOW__CORE__EXECUTOR=airflow.providers.asgi.executors.asgi_executor.AsgiExecutor >>.env
    echo AIRFLOW__CORE__DAGS_FOLDER={{ absolute_path("./dags") }} >>.env
    echo AIRFLOW__CORE__LOAD_EXAMPLES=False >>.env

[private]
venv:
    [ -d .venv ] || uv sync --frozen
    [ -f .git/hooks/pre-commit ] || .venv/bin/pre-commit install --install-hooks

# format code. use --check to check without modifying
format *FLAGS: venv
    .venv/bin/just --fmt --unstable
    .venv/bin/ruff format {{ FLAGS }}

# Lint code. use --fix to fix issues
lint *FLAGS: venv
    .venv/bin/ruff check {{ FLAGS }}
    .venv/bin/mkdocs build --strict

# Run tests
test *FLAGS: venv
    .venv/bin/pytest {{ FLAGS }}

docs: venv
    .venv/bin/mkdocs serve

# Clean build artifacts
clean:
    rm -rf .ruff_cache .pytest_cache .nicegui/ \
    	$(VIRTUAL_ENV)/airflow
