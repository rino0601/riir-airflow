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
    _run_probe(tmp_path, mode, _DAG, _SCHEDULER_PROBE)


def _run_probe(tmp_path: Path, mode: str, dag_source: str, probe: str, **config: str):
    assert airflow.__version__ == BASELINE_AIRFLOW_VERSION
    dags = tmp_path / "dags"
    dags.mkdir()
    (dags / "admission_probe.py").write_text(dag_source)
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
    env.update(config)
    for args in (
        ("-m", "airflow", "db", "migrate"),
        ("-m", "airflow", "dag-processor", "--num-runs", "1"),
        ("-c", probe, mode),
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


_TASK_DAG = """\
from airflow.sdk import DAG, task

with DAG("admission_probe", schedule=None, max_active_runs=2, max_active_tasks=1):
    @task
    def work():
        pass

    work.override(task_id="low", priority_weight=10)()
    work.override(task_id="high", priority_weight=50)()
    work.override(task_id="weighted", priority_weight=100, pool="weighted", pool_slots=2)()
    work.override(task_id="holder", priority_weight=1, pool="weighted")()
"""

_TASK_PROBE = """\
import sqlite3
import sys
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

from airflow.api.common.trigger_dag import trigger_dag
from airflow.executors.workloads import WorkloadType
from airflow.jobs.job import Job
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
from airflow.models.pool import Pool
from airflow.models.taskinstance import TaskInstance
from airflow.sdk import timezone
from airflow.utils.session import create_session
from airflow.utils.types import DagRunTriggeredByType
from sqlalchemy import update

from riir_airflow import _lowlevel

mode = sys.argv[1]
selections = []
claims = []
select_task = _lowlevel.select_sqlite_task
queue_task = _lowlevel.queue_sqlite_task

def observe_selection(connection, limit):
    result = select_task(connection, limit)
    selections.append((limit, result))
    return result

def observe_claim(connection, task_id, job_id, now):
    claimed = queue_task(connection, task_id, job_id, now)
    claims.append((task_id, claimed))
    if claimed:
        assert not queue_task(connection, task_id, job_id, "2000-01-01 00:00:00.000000")
    return claimed

_lowlevel.select_sqlite_task = observe_selection
_lowlevel.queue_sqlite_task = observe_claim

job = Job()
runner = SchedulerJobRunner(job)
with create_session() as session:
    session.add(job)
    session.add(Pool(pool="weighted", slots=2, include_deferred=True))
    session.flush()
    job_id = job.id

now = timezone.utcnow() - timedelta(minutes=2)
for run_id, logical_date in [("newer", now), ("older", now - timedelta(minutes=1))]:
    trigger_dag(
        "admission_probe", run_id=run_id, logical_date=logical_date,
        run_after=now, triggered_by=DagRunTriggeredByType.CLI,
    )

def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)

# First let Airflow resolve dependencies and produce the scheduled attempts.
with patch.object(runner, "_parallelism", 0):
    assert iteration() == 0

with create_session() as session:
    session.execute(
        update(TaskInstance).where(TaskInstance.task_id == "holder", TaskInstance.run_id == "older")
        .values(state="deferred")
    )
    session.execute(
        update(TaskInstance).where(TaskInstance.task_id == "holder", TaskInstance.run_id == "newer")
        .values(state="success")
    )
    if mode == "fallback":
        session.execute(
            update(TaskInstance).where(TaskInstance.task_id == "high", TaskInstance.run_id == "older")
            .values(executor=runner.executor.name.module_path)
        )

queue = runner.executor.executor_queues[WorkloadType.EXECUTE_TASK]

with closing(sqlite3.connect("airflow.db")) as database:
    def tasks():
        return {
            (run, task): (row_id, state, queued, owner, updated)
            for run, task, row_id, state, queued, owner, updated in database.execute(
                "SELECT run_id, task_id, id, state, queued_dttm, queued_by_job_id, updated_at "
                "FROM task_instance WHERE working_set IS TRUE"
            )
        }

    older_high_id = tasks()["older", "high"][0]
    newer_weighted_id = tasks()["newer", "weighted"][0]

    def assert_queued(run, task):
        task_id, state, queued, owner, updated = tasks()[run, task]
        assert state == "queued"
        assert queued and queued <= updated
        assert owner == job_id
        [(key, workload)] = queue.items()
        assert key == runner.executor.get_workload_key(workload)
        assert (workload.ti.run_id, workload.ti.task_id, workload.ti.id.hex) == (run, task, task_id)

    if mode == "native":
        class FailedHandoff(Exception):
            pass

        def fail_before_handoff(tis, executor, session):
            assert [ti.id.hex for ti in tis] == [older_high_id]
            state, owner = session.connection().exec_driver_sql(
                "SELECT state, queued_by_job_id FROM task_instance WHERE id = ?", (older_high_id,)
            ).one()
            assert (state, owner) == ("queued", job_id)
            assert tasks()["older", "high"][1:4] == ("scheduled", None, None)
            raise FailedHandoff

        with patch.object(runner, "_enqueue_task_instances_with_queued_state", fail_before_handoff):
            try:
                iteration()
            except FailedHandoff:
                pass
            else:
                raise AssertionError("scheduler did not reach executor handoff")
        assert tasks()["older", "high"][1:4] == ("scheduled", None, None)
        assert not queue
        assert selections == [(1, (True, older_high_id))]
        assert claims == [(older_high_id, True)]
        selections.clear()
        claims.clear()

    assert iteration() == 1
    assert_queued("older", "high")
    assert tasks()["older", "weighted"][1] == "scheduled"
    assert tasks()["newer", "high"][1] == "scheduled"

    # Full executor prevents another claim. No workload is duplicated and the
    # committed state and metadata of the existing claim are unchanged.
    admitted = tasks()["older", "high"]
    assert iteration() == 0
    assert len(queue) == 1
    assert tasks()["older", "high"] == admitted

    if mode == "fallback":
        assert selections == [(1, (False, None))]
        assert claims == []
    else:
        # Free executor capacity without changing persisted ownership. The
        # older run remains saturated, and the newly unblocked pool can now
        # take the newer run's two-slot task.
        queue.clear()
        with create_session() as session:
            session.execute(update(Pool).where(Pool.pool == "weighted").values(include_deferred=False))
        assert iteration() == 1
        assert_queued("newer", "weighted")
        assert tasks()["older", "weighted"][1] == "scheduled"
        assert tasks()["older", "high"] == admitted

        queue.clear()
        assert iteration() == 0
        assert not queue
        assert selections == (
            [(1, (True, older_high_id)), (1, (True, newer_weighted_id)), (1, (True, None))]
            if mode == "native" else []
        )
        assert claims == (
            [(older_high_id, True), (newer_weighted_id, True)] if mode == "native" else []
        )
"""


@pytest.mark.parametrize("mode", ["baseline", "native", "fallback"])
def test_sqlite_scheduler_admits_task_attempts(tmp_path: Path, mode: str):
    _run_probe(
        tmp_path,
        mode,
        _TASK_DAG,
        _TASK_PROBE,
        AIRFLOW__CORE__PARALLELISM="1",
        AIRFLOW__SCHEDULER__MAX_TIS_PER_QUERY="1",
    )
