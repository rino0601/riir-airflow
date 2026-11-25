"""Airflow 3.4 SQLite reference for admitting an existing queued DAG run."""

import os
import subprocess
import sys
from pathlib import Path

import airflow
import pytest

from ._compat import BASELINE_AIRFLOW_VERSION

pytestmark = pytest.mark.differential

_DAG = """\
from airflow.sdk import DAG, task

with DAG("admission_probe", schedule=None, max_active_runs=1) as dag:
    @task
    def work():
        pass

    work()
"""

_SCHEDULER_PROBE = """\
import sqlite3
from contextlib import closing
from datetime import timedelta

from airflow.api.common.trigger_dag import trigger_dag
from airflow.executors.workloads import WorkloadType
from airflow.jobs.job import Job
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
from airflow.sdk import timezone
from airflow.utils.session import create_session
from airflow.utils.types import DagRunTriggeredByType

now = timezone.utcnow()
# Insert the newer run first so row insertion order cannot satisfy the test.
for run_id, run_after in [("newer", now), ("older", now - timedelta(minutes=1))]:
    trigger_dag(
        "admission_probe",
        run_id=run_id,
        run_after=run_after,
        triggered_by=DagRunTriggeredByType.CLI,
    )

def runs(database):
    return database.execute(
        "SELECT run_id, state, start_date, end_date FROM dag_run "
        "WHERE dag_id = 'admission_probe' ORDER BY run_after"
    ).fetchall()

# Run the scheduler decisions without the executor heartbeat that launches workers.
job = Job()
runner = SchedulerJobRunner(job)
with create_session() as session:
    session.add(job)

queue = runner.executor.executor_queues[WorkloadType.EXECUTE_TASK]
with closing(sqlite3.connect("airflow.db")) as database:
    assert runs(database) == [
        ("older", "queued", None, None),
        ("newer", "queued", None, None),
    ]

    with create_session() as session:
        assert runner._do_scheduling(session) == 1

    admitted = runs(database)
    older, newer = admitted
    assert older[:2] == ("older", "running")
    assert older[2] is not None
    assert older[3] is None
    assert newer == ("newer", "queued", None, None)
    assert [workload.ti.run_id for workload in queue.values()] == ["older"]

    with create_session() as session:
        assert runner._do_scheduling(session) == 0

    assert runs(database) == admitted
    assert [workload.ti.run_id for workload in queue.values()] == ["older"]
"""


def test_sqlite_scheduler_admits_only_one_queued_run(tmp_path: Path):
    assert airflow.__version__ == BASELINE_AIRFLOW_VERSION
    dags = tmp_path / "dags"
    dags.mkdir()
    (dags / "admission_probe.py").write_text(_DAG)
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AIRFLOW")
    }
    env.update(
        AIRFLOW_HOME=str(tmp_path),
        AIRFLOW__CORE__DAGS_FOLDER=str(dags),
        AIRFLOW__CORE__LOAD_EXAMPLES="False",
        AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION="False",
        AIRFLOW__CORE__EXECUTOR="LocalExecutor",
        AIRFLOW__API_AUTH__JWT_SECRET="admission-probe-test-secret-" * 3,
        AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=f"sqlite:///{tmp_path / 'airflow.db'}",
    )
    for args in (
        ("-m", "airflow", "db", "migrate"),
        ("-m", "airflow", "dag-processor", "--num-runs", "1"),
        ("-c", _SCHEDULER_PROBE),
    ):
        result = subprocess.run(
            [sys.executable, *args],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stdout + result.stderr
