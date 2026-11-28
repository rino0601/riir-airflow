import os
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from contextlib import closing
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

_MIGRATION_PATH_SETTINGS = frozenset(
    {
        "AIRFLOW__CORE__DAGS_FOLDER",
        "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN",
    }
)

_PARSE_RUNTIME_SETTINGS = frozenset(
    {
        *_MIGRATION_PATH_SETTINGS,
        "AIRFLOW__API_AUTH__JWT_SECRET",
        "AIRFLOW__CORE__EXECUTION_API_SERVER_URL",
        "AIRFLOW__CORE__EXECUTOR",
        "AIRFLOW__CORE__PARALLELISM",
        "AIRFLOW__SCHEDULER__MAX_DAGRUNS_TO_CREATE_PER_LOOP",
        "AIRFLOW__SCHEDULER__MAX_TIS_PER_QUERY",
        "AIRFLOW__SCHEDULER__NUM_RUNS",
        "AIRFLOW__SCHEDULER__ORPHANED_TASKS_CHECK_INTERVAL",
        "AIRFLOW__SCHEDULER__SCHEDULER_HEARTBEAT_SEC",
        "AIRFLOW__SCHEDULER__SCHEDULER_IDLE_SLEEP_TIME",
    }
)


class MigratedDatabaseFactory:
    def __init__(self, root: Path):
        self._root = root
        self._templates: dict[tuple[tuple[str, str], ...], Path] = {}

    def _key(self, env: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted(
                (name, value)
                for name, value in env.items()
                if name.startswith("AIRFLOW__") and name not in _MIGRATION_PATH_SETTINGS
            )
        )

    def _migrate(self, home: Path, env: Mapping[str, str]) -> Path:
        home.mkdir(parents=True, exist_ok=True)
        (home / "dags").mkdir(exist_ok=True)
        database = home / "airflow.db"
        result = subprocess.run(
            [sys.executable, "-m", "airflow", "db", "migrate"],
            env=env,
            cwd=home,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return database

    def fresh(self, home: Path, env: Mapping[str, str]) -> Path:
        case_env = dict(env)
        case_env.update(
            AIRFLOW_HOME=str(home),
            AIRFLOW__CORE__DAGS_FOLDER=str(home / "dags"),
            AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=f"sqlite:///{home / 'airflow.db'}",
        )
        return self._migrate(home, case_env)

    def template_for(self, env: Mapping[str, str]) -> Path:
        key = self._key(env)
        template = self._templates.get(key)
        if template is None:
            template = self.fresh(self._root / f"template-{len(self._templates)}", env)
            self._templates[key] = template
        return template

    def prepare(self, home: Path, env: Mapping[str, str]) -> Path:
        template = self.template_for(env)
        home.mkdir(parents=True, exist_ok=True)
        database = home / "airflow.db"
        with (
            closing(sqlite3.connect(template)) as source,
            closing(sqlite3.connect(database)) as target,
        ):
            source.backup(target)
        return database


class ParsedDagFactory:
    def __init__(self, root: Path, db_factory: MigratedDatabaseFactory):
        self._root = root
        self._db_factory = db_factory
        self._templates: dict[tuple, tuple[Path, Path]] = {}
        self.template_count = 0

    def _key(self, dag_files: Mapping[str, str], env: Mapping[str, str]) -> tuple:
        return (
            tuple(sorted(dag_files.items())),
            tuple(
                sorted(
                    (name, value)
                    for name, value in env.items()
                    if name.startswith("AIRFLOW__")
                    and name not in _PARSE_RUNTIME_SETTINGS
                )
            ),
        )

    @staticmethod
    def _write_dags(dags: Path, dag_files: Mapping[str, str]):
        dags.mkdir(parents=True, exist_ok=True)
        for name, source in dag_files.items():
            (dags / name).write_text(source)

    @staticmethod
    def _parse(home: Path, env: Mapping[str, str]):
        result = subprocess.run(
            [sys.executable, "-m", "airflow", "dag-processor", "--num-runs", "1"],
            env=env,
            cwd=home,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def prepare(
        self,
        home: Path,
        env: Mapping[str, str],
        dag_files: Mapping[str, str],
        *,
        fresh: bool = False,
    ) -> tuple[Path, dict[str, str]]:
        case_env = dict(env)
        case_env.update(
            AIRFLOW_HOME=str(home),
            AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=f"sqlite:///{home / 'airflow.db'}",
        )
        if fresh:
            dags = home / "dags"
            self._write_dags(dags, dag_files)
            case_env["AIRFLOW__CORE__DAGS_FOLDER"] = str(dags)
            database = self._db_factory.prepare(home, case_env)
            self._parse(home, case_env)
            return database, case_env

        key = self._key(dag_files, env)
        template = self._templates.get(key)
        if template is None:
            template_home = self._root / f"parsed-{len(self._templates)}"
            dags = template_home / "dags"
            self._write_dags(dags, dag_files)
            template_env = dict(case_env)
            template_env.update(
                AIRFLOW_HOME=str(template_home),
                AIRFLOW__CORE__DAGS_FOLDER=str(dags),
                AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=(
                    f"sqlite:///{template_home / 'airflow.db'}"
                ),
            )
            database = self._db_factory.prepare(template_home, template_env)
            self._parse(template_home, template_env)
            template = (database, dags)
            self._templates[key] = template
            self.template_count += 1
        database, dags = template
        home.mkdir(parents=True, exist_ok=True)
        case_database = home / "airflow.db"
        with (
            closing(sqlite3.connect(database)) as source,
            closing(sqlite3.connect(case_database)) as target,
        ):
            source.backup(target)
        case_env["AIRFLOW__CORE__DAGS_FOLDER"] = str(dags)
        return case_database, case_env


@pytest.fixture(scope="session")
def migrated_db_factory(tmp_path_factory):
    return MigratedDatabaseFactory(tmp_path_factory.mktemp("airflow-migrated"))


@pytest.fixture(scope="session")
def parsed_dag_factory(tmp_path_factory, migrated_db_factory):
    return ParsedDagFactory(
        tmp_path_factory.mktemp("airflow-parsed"), migrated_db_factory
    )


@pytest.fixture
def admission_probe(tmp_path, parsed_dag_factory):
    from ._compat import BASELINE_AIRFLOW_VERSION
    from .probes.harness import ProbeHarness

    harness = ProbeHarness(tmp_path, Path(__file__).parent, BASELINE_AIRFLOW_VERSION)

    def run(dag_names, probe, mode, **config):
        harness.run(parsed_dag_factory, dag_names, probe, mode, **config)

    return run


@pytest.fixture
def client():
    assert "AIRFLOW_HOME" in os.environ
    assert "AIRFLOW__CORE__EXECUTOR" in os.environ
    assert (
        os.environ["AIRFLOW__CORE__EXECUTOR"]
        == "riir_airflow.executors.asgi_executor.AsgiExecutor"
    )

    yield TestClient(app)
