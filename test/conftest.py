import os
from pathlib import Path

import pytest
from riir_airflow.asgi import app
from starlette.testclient import TestClient

# `uv run pytest`로 바로 실행해도 mise run configure와 같은 Airflow 설정을 사용한다.
_project_root = Path(__file__).resolve().parents[1]
_venv = _project_root / ".venv"
os.environ.setdefault("SQLALCHEMY_SILENCE_UBER_WARNING", "1")
os.environ.setdefault("AIRFLOW_HOME", str(_venv / "airflow"))
os.environ.setdefault("AIRFLOW__LOGGING__LOGGING_LEVEL", "INFO")
os.environ.setdefault(
    "AIRFLOW__CORE__EXECUTOR",
    "riir_airflow.executors.asgi_executor.AsgiExecutor",
)
os.environ.setdefault("AIRFLOW__CORE__DAGS_FOLDER", str(_venv / "dags"))
os.environ.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "False")


@pytest.fixture
def client():
    assert "AIRFLOW_HOME" in os.environ
    assert "AIRFLOW__CORE__EXECUTOR" in os.environ
    assert (
        os.environ["AIRFLOW__CORE__EXECUTOR"]
        == "riir_airflow.executors.asgi_executor.AsgiExecutor"
    )

    yield TestClient(app)
