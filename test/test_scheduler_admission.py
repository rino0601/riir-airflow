"""Compare native SQLite DAG run admission with the Airflow 3.4 baseline."""

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
import sys
from contextlib import closing
from datetime import timedelta

from airflow.api.common.trigger_dag import trigger_dag
from airflow.executors.workloads import WorkloadType
from airflow.jobs.job import Job
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
from airflow.models.dagrun import DagRun
from airflow.sdk import timezone
from airflow.utils.session import create_session
from airflow.utils.types import DagRunTriggeredByType, DagRunType
from sqlalchemy import update

from riir_airflow import _lowlevel

mode = sys.argv[1]
selections = []
claims = []
notifications = []
select_candidates = _lowlevel.manual_dag_run_candidates
claim_run = _lowlevel.start_manual_dag_run
notify = DagRun.notify_dagrun_state_changed

def observe_candidates(connection, limit):
    candidates = select_candidates(connection, limit)
    selections.append(candidates)
    return candidates

def observe_claim(connection, run_id, now):
    claimed = claim_run(connection, run_id, now)
    claims.append((run_id, claimed))
    return claimed

def observe_notification(run, msg):
    notifications.append((run.run_id, run.state, run.start_date, run.end_date, msg))
    notify(run, msg)

_lowlevel.manual_dag_run_candidates = observe_candidates
_lowlevel.start_manual_dag_run = observe_claim
DagRun.notify_dagrun_state_changed = observe_notification

# SQLite compares run_after against a clock with whole-second precision.
now = timezone.utcnow() - timedelta(minutes=1)
# Insert the newer run first so row insertion order cannot satisfy the test.
for run_id, run_after in [("newer", now), ("older", now - timedelta(minutes=1))]:
    trigger_dag(
        "admission_probe",
        run_id=run_id,
        run_after=run_after,
        triggered_by=DagRunTriggeredByType.CLI,
    )

if "mixed" in mode:
    with create_session() as session:
        session.execute(update(DagRun).where(DagRun.run_id == "older").values(run_type=DagRunType.SCHEDULED))

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
    queued = runs(database)
    assert queued == [("older", "queued", None, None), ("newer", "queued", None, None)]
    older_id, newer_id = (
        row[0] for row in database.execute("SELECT id FROM dag_run ORDER BY run_after")
    )

    if mode == "native":
        # The Rust update is visible to the Airflow session and listener,
        # but a rollback keeps it out of committed metadata.
        with create_session() as session:
            runner._start_queued_dagruns(session)
            run = session.get(DagRun, older_id)
            assert run.state == "running"
            assert run.start_date is not None
            assert run.start_date == run.updated_at
            assert runs(database) == queued
            session.rollback()
        assert runs(database) == queued
        selections.clear()
        claims.clear()
        notifications.clear()

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
    assert [(run_id, state, end, msg) for run_id, state, start, end, msg in notifications] == [
        ("older", "running", None, "started"),
    ]
    assert notifications[0][2] is not None
    assert selections == {
        "native": [[older_id, newer_id], []],
        "mixed-native": [None, []],
    }.get(mode, [])
    assert claims == ([(older_id, True), (newer_id, False)] if mode == "native" else [])
    assert [workload.ti.run_id for workload in queue.values()] == ["older"]
"""


@pytest.mark.parametrize(
    "mode", ["baseline", "native", "mixed-baseline", "mixed-native"]
)
def test_sqlite_scheduler_admits_only_one_queued_run(tmp_path: Path, mode: str):
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
        AIRFLOW__CORE__EXECUTOR=(
            "LocalExecutor"
            if mode.endswith("baseline")
            else "riir_airflow.executors.asgi_executor.AsgiExecutor"
        ),
        AIRFLOW__API_AUTH__JWT_SECRET="admission-probe-test-secret-" * 3,
        AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=f"sqlite:///{tmp_path / 'airflow.db'}",
    )
    for args in (
        ("-m", "airflow", "db", "migrate"),
        ("-m", "airflow", "dag-processor", "--num-runs", "1"),
        ("-c", _SCHEDULER_PROBE, mode),
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
