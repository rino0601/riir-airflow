"""Verify ASGI executor task retries against Airflow 3.4 SQLite state transitions."""

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path

import airflow
import httpx
import pytest

from ._compat import BASELINE_AIRFLOW_VERSION

pytestmark = pytest.mark.differential

_RETRY_DAG = """\
import multiprocessing
import os
from pathlib import Path

from airflow.sdk import DAG, get_current_context, task

with DAG("retry_probe", schedule=None) as dag:
    @task(retries=1, retry_delay=0)
    def retry_once():
        attempt = get_current_context()["ti"].try_number
        with Path(os.environ["RETRY_PROBE_ATTEMPTS"]).open("a") as attempts:
            attempts.write(f"{attempt}\\n")
        if attempt == 1:
            raise RuntimeError("Intentional first-attempt failure")

    retry_once()

if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")
    dag.test(use_executor=True)
"""

_EXECUTION_API = """\
import sys

import uvicorn
from airflow.api_fastapi.app import create_app

uvicorn.run(create_app(apps="execution"), fd=int(sys.argv[1]))
"""


@contextmanager
def _process(args, log, *, env, cwd, pass_fds=()):
    with (
        log.open("w") as output,
        subprocess.Popen(
            [sys.executable, *args],
            stdout=output,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=cwd,
            start_new_session=True,
            pass_fds=pass_fds,
        ) as process,
    ):
        try:
            yield process
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def _wait_for_api(process, port, log):
    deadline = time.monotonic() + 60
    with httpx.Client(timeout=1, trust_env=False) as client:
        while process.poll() is None and time.monotonic() < deadline:
            try:
                if client.get(f"http://127.0.0.1:{port}/openapi.json").is_success:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.1)
    pytest.fail(f"Execution API did not start:\n{log.read_text()}")


def _run_retry_dag(home: Path, executor: str, db_factory):
    dags = home / "dags"
    dags.mkdir(parents=True)
    dag = dags / "retry_probe.py"
    dag.write_text(_RETRY_DAG)
    database = home / "airflow.db"
    attempts = home / "attempts.txt"
    # The run starts with settings and schema independent of local Airflow config.
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AIRFLOW")
    }
    env.update(
        AIRFLOW_HOME=str(home),
        AIRFLOW__CORE__DAGS_FOLDER=str(dags),
        AIRFLOW__CORE__LOAD_EXAMPLES="False",
        AIRFLOW__CORE__EXECUTOR=executor,
        AIRFLOW__CORE__PARALLELISM="1",
        AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=f"sqlite:///{database}",
        AIRFLOW__API_AUTH__JWT_SECRET="retry-probe-test-secret-" * 3,
        RETRY_PROBE_ATTEMPTS=str(attempts),
    )
    database = db_factory.prepare(home, env)

    # Record even a zero-delay retry's transient state without polling SQLite.
    with closing(sqlite3.connect(database)) as db:
        db.executescript("""
            CREATE TABLE retry_probe_states (attempt INTEGER, state TEXT);
            CREATE TRIGGER observe_retry_probe AFTER UPDATE OF state ON task_instance
            WHEN NEW.dag_id = 'retry_probe'
                AND OLD.state IS NOT NEW.state
                AND NEW.state IN ('running', 'up_for_retry', 'success', 'failed')
            BEGIN
                INSERT INTO retry_probe_states VALUES (NEW.try_number, NEW.state);
            END;
        """)

    api_log = home / "api.log"
    dag_log = home / "dag.log"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        env["AIRFLOW__CORE__EXECUTION_API_SERVER_URL"] = (
            f"http://127.0.0.1:{port}/execution/"
        )
        with _process(
            ["-c", _EXECUTION_API, str(listener.fileno())],
            api_log,
            env=env,
            cwd=home,
            pass_fds=(listener.fileno(),),
        ) as api:
            _wait_for_api(api, port, api_log)
            with _process([str(dag)], dag_log, env=env, cwd=home) as run:
                try:
                    returncode = run.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    pytest.fail(
                        f"DAG did not finish:\n{dag_log.read_text()}\n{api_log.read_text()}"
                    )
                assert returncode == 0, dag_log.read_text() + api_log.read_text()

    with closing(sqlite3.connect(database)) as db:
        states = db.execute(
            "SELECT attempt, state FROM retry_probe_states ORDER BY rowid"
        ).fetchall()
        dag_states = db.execute(
            "SELECT state FROM dag_run WHERE dag_id = 'retry_probe'"
        ).fetchall()
    executions = attempts.read_text().splitlines() if attempts.exists() else []
    return states, dag_states, executions


def test_asgi_executor_retry_keeps_airflow_states(tmp_path, migrated_db_factory):
    assert airflow.__version__ == BASELINE_AIRFLOW_VERSION
    states = _run_retry_dag(
        tmp_path / "asgi",
        "riir_airflow.executors.asgi_executor.AsgiExecutor",
        migrated_db_factory,
    )
    expected = (
        [
            (1, "running"),
            (1, "up_for_retry"),
            (1, "failed"),
            (2, "running"),
            (2, "success"),
        ],
        [("success",)],
        ["1", "2"],
    )
    assert states == expected
