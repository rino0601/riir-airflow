"""Compare native SQLite scheduler claims with the Airflow 3.4 baseline."""

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

from riir_airflow.scheduler import NativeSchedulerJobRunner

mode = sys.argv[1]
selections = []
claims = []
notifications = []

def observe_notification(run, msg):
    notifications.append((run.run_id, run.state, run.start_date, run.end_date, msg))
    original_notify(run, msg)

original_notify = DagRun.notify_dagrun_state_changed
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
        session.execute(
            update(DagRun).where(DagRun.run_id == "older").values(run_type=DagRunType.SCHEDULED)
        )

def runs(database):
    return database.execute(
        "SELECT run_id, state, start_date, end_date FROM dag_run "
        "WHERE dag_id = 'admission_probe' ORDER BY run_after"
    ).fetchall()

job = Job()
runner = SchedulerJobRunner(job) if mode.endswith("baseline") else NativeSchedulerJobRunner(job)
with create_session() as session:
    session.add(job)

if not mode.endswith("baseline"):
    driver = runner.native_driver
    phases = driver.phases
    prepare = phases.prepare_dag_runs
    claimed = phases.dag_run_claimed

    def observe_prepare(dtos):
        selections.append([dto["id"] for dto in dtos])
        return prepare(dtos)

    def observe_claim(dto):
        claims.append((dto["id"], dto["state"]))
        return claimed(dto)

    phases.prepare_dag_runs = observe_prepare
    phases.dag_run_claimed = observe_claim

def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)

queue = runner.executor.executor_queues[WorkloadType.EXECUTE_TASK]
with closing(sqlite3.connect("airflow.db")) as database:
    queued = runs(database)
    assert queued == [("older", "queued", None, None), ("newer", "queued", None, None)]
    older_id, newer_id = (
        row[0] for row in database.execute("SELECT id FROM dag_run ORDER BY run_after")
    )

    assert iteration() == 1
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
    assert selections == ([[older_id, newer_id]] if mode in ("native", "mixed-native") else [])
    assert claims == ([(older_id, "running")] if mode == "native" else [])
"""


@pytest.mark.parametrize(
    "mode", ["baseline", "native", "mixed-baseline", "mixed-native"]
)
def test_sqlite_scheduler_admits_only_one_queued_run(tmp_path: Path, mode: str):
    _run_probe(tmp_path, mode, _DAG, _SCHEDULER_PROBE)


_TaskDAG = """\
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

from riir_airflow.scheduler import NativeSchedulerJobRunner

mode = sys.argv[1]
selections = []
claims = []
job = Job()
runner = SchedulerJobRunner(job) if mode.endswith("baseline") else NativeSchedulerJobRunner(job)
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

# Let upstream resolve dependencies and produce the scheduled attempts.
with patch.object(runner, "_parallelism", 0):
    with create_session() as session:
        SchedulerJobRunner._do_scheduling(runner, session)

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
driver = None if mode == "baseline" else runner.native_driver

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

    if driver is not None:
        phases = driver.phases
        prepare = phases.prepare_task
        claimed = phases.task_claimed

        def observe_prepare(dto):
            result = prepare(dto)
            selections.append((dto["id"], result))
            if not result:
                print("UNSUPPORTED_DTO", dto)
            return result

        def observe_claim(dto):
            row = tasks()[dto["run_id"], dto["task_id"]]
            assert row[1] == "scheduled"
            assert row[2] is None and row[3] is None
            result = claimed(dto)
            claims.append((dto["id"], dto["state"]))
            return result

        phases.prepare_task = observe_prepare
        phases.task_claimed = observe_claim

    def iteration():
        with create_session() as session:
            return runner._do_scheduling(session)

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
        assert selections == [(older_high_id, True)], selections
        assert claims == []
        selections.clear()
        claims.clear()

    assert iteration() == 1
    assert_queued("older", "high")
    assert tasks()["older", "weighted"][1] == "scheduled"
    assert tasks()["newer", "high"][1] == "scheduled"

    admitted = tasks()["older", "high"]
    # Free pool capacity while the first workload still fills the executor.
    with create_session() as session:
        session.execute(update(Pool).where(Pool.pool == "weighted").values(include_deferred=False))
    assert iteration() == 0
    assert len(queue) == 1
    assert tasks()["older", "high"] == admitted
    assert tasks()["newer", "weighted"][1] == "scheduled"

    if mode == "fallback":
        assert selections == []
        assert claims == []
    else:
        queue.clear()
        assert iteration() == 1
        assert_queued("newer", "weighted")
        assert tasks()["older", "weighted"][1] == "scheduled"
        assert tasks()["older", "high"] == admitted

        queue.clear()
        assert iteration() == 0
        assert not queue
        if mode == "native":
            assert selections == [(older_high_id, True), (newer_weighted_id, True)], selections
            assert claims == [(older_high_id, "queued"), (newer_weighted_id, "queued")], claims
        else:
            assert selections == []
            assert claims == []
"""


@pytest.mark.parametrize("mode", ["baseline", "native", "fallback"])
def test_sqlite_scheduler_admits_task_attempts(tmp_path: Path, mode: str):
    _run_probe(
        tmp_path,
        mode,
        _TaskDAG,
        _TASK_PROBE,
        AIRFLOW__CORE__PARALLELISM="4" if mode == "native" else "1",
        AIRFLOW__SCHEDULER__MAX_TIS_PER_QUERY="1",
    )


_BOUNDARY_PROBE = """\
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

from airflow.api.common.trigger_dag import trigger_dag
from airflow.executors.workloads import WorkloadType
from airflow.jobs.job import Job
from airflow.models.dag import DagModel
from airflow.models.dagrun import DagRun
from airflow.models.taskinstance import TaskInstance
from airflow.sdk import timezone
from airflow.utils.session import create_session
from airflow.utils.types import DagRunTriggeredByType
from sqlalchemy import update

from riir_airflow.scheduler import NativeSchedulerJobRunner

_LOCKER = (
    "import sqlite3, sys\\n"
    "connection = sqlite3.connect(sys.argv[1], isolation_level=None)\\n"
    'connection.execute("PRAGMA busy_timeout=1000")\\n'
    'connection.execute("BEGIN IMMEDIATE")\\n'
    'print("locked", flush=True)\\n'
    "sys.stdin.readline()\\n"
    'connection.execute("ROLLBACK")\\n'
)

job = Job()
runner = NativeSchedulerJobRunner(job)
with create_session() as session:
    session.add(job)

trigger_dag(
    "admission_probe", run_id="probe", run_after=timezone.utcnow() - timedelta(minutes=1),
    triggered_by=DagRunTriggeredByType.CLI,
)
driver = runner.native_driver
phases = driver.phases

def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)

queue = runner.executor.executor_queues[WorkloadType.EXECUTE_TASK]
events = []
prepare_calls = []

with closing(sqlite3.connect("airflow.db")) as observer:
    def state(run, task):
        return observer.execute(
            "SELECT state, queued_dttm, queued_by_job_id FROM task_instance "
            "WHERE run_id = ? AND task_id = ? AND working_set IS TRUE", (run, task)
        ).fetchone()

    prepare_run = phases.prepare_dag_runs
    claimed_run = phases.dag_run_claimed

    def observe_prepare(dtos):
        prepare_calls.append([dto["id"] for dto in dtos])
        return prepare_run(dtos)

    def observe_run_claim(dto):
        assert dto["state"] == "running"
        assert observer.execute(
            "SELECT state FROM dag_run WHERE id = ?", (dto["id"],)
        ).fetchone() == ("queued",)
        events.append("run-dto")
        return claimed_run(dto)

    phases.prepare_dag_runs = observe_prepare
    phases.dag_run_claimed = observe_run_claim

    assert iteration() == 1
    assert observer.execute("SELECT state FROM dag_run WHERE run_id = 'probe'").fetchone() == ("running",)
    assert state("probe", "work")[0] == "queued"

    # An external write reservation skips the whole iteration without DML.
    with create_session() as session:
        session.execute(
            update(DagRun).where(DagRun.run_id == "probe").values(state="queued")
        )
    prepare_calls.clear()
    locker = subprocess.Popen(
        [sys.executable, "-c", _LOCKER, "airflow.db"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert locker.stdout.readline().strip() == "locked"
        with patch.object(phases, "before_runs", lambda: None), patch.object(
            phases, "between_phases", lambda: None
        ):
            assert iteration() == 0
        assert prepare_calls == [], prepare_calls
        assert observer.execute(
            "SELECT state FROM dag_run WHERE run_id = 'probe'"
        ).fetchone() == ("queued",)
        assert state("probe", "work")[0] == "queued"
    finally:
        locker.stdin.write("\\n")
        locker.stdin.flush()
        assert locker.wait(timeout=10) == 0
    with create_session() as session:
        session.execute(
            update(DagRun).where(DagRun.run_id == "probe").values(state="running")
        )

    # A failed handoff rolls back the guarded claim and removes the workload.
    with create_session() as session:
        session.execute(
            update(TaskInstance).where(TaskInstance.run_id == "probe")
            .values(state="scheduled", queued_dttm=None, queued_by_job_id=None)
        )
    queue.clear()
    runner.executor._task_coordinates.clear()

    class FailedHandoff(Exception):
        pass

    enqueue = runner._enqueue_task_instances_with_queued_state

    def fail_after_queue(tis, executor, session):
        enqueue(tis, executor, session=None)
        raise FailedHandoff

    with patch.object(runner, "_enqueue_task_instances_with_queued_state", fail_after_queue):
        try:
            iteration()
        except FailedHandoff:
            pass
        else:
            raise AssertionError("scheduler did not reach executor handoff")
    assert state("probe", "work") == ("scheduled", None, None)
    assert not queue
    assert not runner.executor._task_coordinates

    # A committed claim is recovered exactly once after a simulated crash.
    assert iteration() == 1
    original = state("probe", "work")
    assert original[0] == "queued"
    queue.clear()
    new_job = Job(state="running")
    replacement = NativeSchedulerJobRunner(new_job)
    with create_session() as session:
        session.execute(update(Job).where(Job.id == job.id).values(state="failed"))
        session.add(new_job)
    replacement_queue = replacement.executor.executor_queues[WorkloadType.EXECUTE_TASK]
    assert replacement.adopt_or_reset_orphaned_tasks() == 1
    assert len(replacement_queue) == 1
    assert state("probe", "work")[:2] == original[:2]
    assert state("probe", "work")[2] == new_job.id
    assert replacement.adopt_or_reset_orphaned_tasks() == 0
    assert len(replacement_queue) == 1

    # Repeated iterations cannot overwrite metadata or duplicate enqueue/notification.
    assert iteration() == 0
    assert len(replacement_queue) == 1
    assert state("probe", "work")[:2] == original[:2]

    # A running job with a stale heartbeat is failed and its queued task recovered.
    final_job = Job(state="running")
    final = NativeSchedulerJobRunner(final_job)
    with create_session() as session:
        session.execute(
            update(Job)
            .where(Job.id == new_job.id)
            .values(latest_heartbeat=timezone.utcnow() - timedelta(hours=1))
        )
        session.add(final_job)
    final_queue = final.executor.executor_queues[WorkloadType.EXECUTE_TASK]
    assert final.adopt_or_reset_orphaned_tasks() == 1
    assert len(final_queue) == 1
    assert state("probe", "work")[2] == final_job.id
    assert observer.execute(
        "SELECT state FROM job WHERE id = ?", (new_job.id,)
    ).fetchone() == ("failed",)
    assert final.adopt_or_reset_orphaned_tasks() == 0
    assert len(final_queue) == 1

    # An orphan outside the native subset is reset by Airflow and can be queued again.
    fallback_job = Job(state="running")
    fallback = NativeSchedulerJobRunner(fallback_job)
    fallback_queue = fallback.executor.executor_queues[WorkloadType.EXECUTE_TASK]
    with create_session() as session:
        session.execute(update(DagModel).where(DagModel.dag_id == "admission_probe")
                        .values(fail_fast=True))
        session.execute(
            update(Job).where(Job.id == final_job.id)
            .values(latest_heartbeat=timezone.utcnow() - timedelta(hours=1))
        )
        session.add(fallback_job)
    assert fallback.adopt_or_reset_orphaned_tasks() == 1
    assert state("probe", "work")[0] is None
    assert not fallback_queue
    assert fallback.adopt_or_reset_orphaned_tasks() == 0
    with create_session() as session:
        assert fallback._do_scheduling(session) == 1
    assert state("probe", "work")[2] == fallback_job.id
    assert len(fallback_queue) == 1
"""


def test_native_scheduler_transaction_boundaries(tmp_path: Path):
    _run_probe(tmp_path, "native", _DAG, _BOUNDARY_PROBE)


_FALLBACK_PROBE = """\
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import timedelta

from airflow.api.common.trigger_dag import trigger_dag
from airflow.jobs.job import Job
from airflow.models.dagrun import DagRun
from airflow.models.taskinstance import TaskInstance
from airflow.sdk import timezone
from airflow.utils.session import create_session
from airflow.utils.types import DagRunTriggeredByType, DagRunType
from sqlalchemy import update

from riir_airflow.scheduler import NativeSchedulerJobRunner

job = Job()
runner = NativeSchedulerJobRunner(job)
with create_session() as session:
    session.add(job)

now = timezone.utcnow() - timedelta(minutes=2)
with create_session() as session:
    session.execute(update(DagRun).values(run_type=DagRunType.MANUAL))
for run_id, logical_date in (("mixed", now - timedelta(minutes=1)), ("plain", now)):
    trigger_dag(
        "admission_probe", run_id=run_id, logical_date=logical_date, run_after=now,
        triggered_by=DagRunTriggeredByType.CLI,
    )

with create_session() as session:
    session.execute(
        update(DagRun).where(DagRun.run_id == "mixed").values(run_type=DagRunType.SCHEDULED)
    )

driver = runner.native_driver
phases = driver.phases

def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)

run_prepare = phases.prepare_dag_runs
task_prepare = phases.prepare_task
prepared = []
native_task_claims = []
phases.prepare_dag_runs = lambda dtos: (prepared.append("runs"), run_prepare(dtos))[1]

def observe_task(dto):
    native_task_claims.append(dto["id"])
    return task_prepare(dto)

phases.prepare_task = observe_task

with closing(sqlite3.connect("airflow.db")) as observer:
    def states():
        return dict(observer.execute(
            "SELECT run_id, state FROM dag_run WHERE dag_id = 'admission_probe'"
        ))

    assert iteration() == 1
    current = states()
    assert prepared == ["runs"]
    assert list(current.values()).count("running") == 1
    assert list(current.values()).count("queued") == 1
    assert native_task_claims == []

running_run = next(run_id for run_id, state in current.items() if state == "running")
queued_run = next(run_id for run_id, state in current.items() if state == "queued")
with create_session() as session:
    session.execute(
        update(DagRun).where(DagRun.run_id == queued_run).values(state="success")
    )
    session.execute(
        update(DagRun).where(DagRun.run_id == running_run).values(created_dag_version_id=None)
    )
    session.execute(
        update(TaskInstance).where(TaskInstance.run_id == running_run)
        .values(state="scheduled", queued_dttm=None, queued_by_job_id=None, dag_version_id=None)
    )
runner.executor.executor_queues.clear()
runner.executor._task_coordinates.clear()
prepared.clear()
assert iteration() == 1
assert prepared == []
assert native_task_claims == []
with closing(sqlite3.connect("airflow.db")) as observer:
    assert observer.execute(
        "SELECT state FROM dag_run WHERE run_id = ?", (running_run,)
    ).fetchone() == ("running",)
"""


def test_native_scheduler_fallback_before_write(tmp_path: Path):
    _run_probe(tmp_path, "native", _DAG, _FALLBACK_PROBE)


_LISTENER_PROBE = """\
import sqlite3
from contextlib import closing
from datetime import timedelta

from airflow.api.common.trigger_dag import trigger_dag
from airflow.jobs.job import Job
from airflow.listeners import hookimpl
from airflow.listeners.listener import get_listener_manager
from airflow.sdk import timezone
from airflow.utils.session import create_session
from airflow.utils.types import DagRunTriggeredByType

from riir_airflow.scheduler import NativeSchedulerJobRunner

notifications = []
native_prepare = []

class ProbeListener:
    @hookimpl
    def on_dag_run_running(self, dag_run, msg):
        notifications.append((dag_run.run_id, dag_run.state, msg))

get_listener_manager().add_listener(ProbeListener())

job = Job()
runner = NativeSchedulerJobRunner(job)
with create_session() as session:
    session.add(job)
trigger_dag(
    "admission_probe", run_id="probe",
    run_after=timezone.utcnow() - timedelta(minutes=1),
    triggered_by=DagRunTriggeredByType.CLI,
)
driver = runner.native_driver
assert not driver.supported()
assert "native" not in vars(driver)
phases = driver.phases
prepare = phases.prepare_dag_runs
phases.prepare_dag_runs = lambda dtos: (native_prepare.append(dtos), prepare(dtos))[1]

with create_session() as session:
    assert runner._do_scheduling(session) == 1
assert native_prepare == []
assert "native" not in vars(driver)
assert notifications == [("probe", "running", "started")]
with closing(sqlite3.connect("airflow.db")) as database:
    assert database.execute(
        "SELECT state FROM dag_run WHERE run_id = 'probe'"
    ).fetchone() == ("running",)
    assert database.execute(
        "SELECT state FROM task_instance WHERE task_id = 'work'"
    ).fetchone() == ("queued",)
"""


def test_native_scheduler_delegates_with_listeners(tmp_path: Path):
    _run_probe(tmp_path, "native", _DAG, _LISTENER_PROBE)


_RUNNER_PROBE = """\
import sqlite3
from contextlib import closing
from datetime import timedelta

from airflow.api.common.trigger_dag import trigger_dag
from airflow.jobs.job import Job
from airflow.sdk import timezone
from airflow.utils.session import create_session
from airflow.utils.types import DagRunTriggeredByType

from riir_airflow.scheduler import NativeSchedulerJobRunner

job = Job()
runner = NativeSchedulerJobRunner(job)
with create_session() as session:
    session.add(job)
trigger_dag(
    "admission_probe", run_id="probe",
    run_after=timezone.utcnow() - timedelta(minutes=1),
    triggered_by=DagRunTriggeredByType.CLI,
)
calls = []
loaded_dags = []
read_dag = runner.scheduler_dag_bag._read_dag

def observe_dag(row):
    loaded_dags.append(row.dag_id)
    return read_dag(row)

runner.scheduler_dag_bag._read_dag = observe_dag
phases = runner.native_driver.phases
for name, event in (
    ("prepare_dag_runs", "runs"),
    ("dag_run_claimed", "run-claim"),
    ("prepare_task", "task"),
    ("task_claimed", "task-claim"),
):
    original = getattr(phases, name)
    if name.endswith("runs"):
        def observe(dtos, original=original, event=event):
            calls.append(event)
            return original(dtos)
    else:
        def observe(dto, original=original, event=event):
            calls.append(event)
            return original(dto)
    setattr(phases, name, observe)

with create_session() as session:
    assert runner._do_scheduling(session) == 1
assert calls == ["runs", "run-claim", "task", "task-claim"], calls
with create_session() as session:
    assert runner._do_scheduling(session) == 0
assert calls == ["runs", "run-claim", "task", "task-claim"], calls
assert loaded_dags == ["admission_probe"], loaded_dags
with closing(sqlite3.connect("airflow.db")) as database:
    assert database.execute(
        "SELECT state FROM dag_run WHERE run_id = 'probe'"
    ).fetchone() == ("running",)
    assert database.execute(
        "SELECT state, queued_by_job_id FROM task_instance WHERE task_id = 'work'"
    ).fetchone() == ("queued", job.id)
"""


def test_scheduler_entry_point_runner_calls_native_claims(tmp_path: Path):
    dag_source = (
        _DAG
        + """
with DAG("unrelated_probe", schedule=None):
    @task
    def unrelated():
        pass

    unrelated()
"""
    )
    _run_probe(tmp_path, "native", dag_source, _RUNNER_PROBE)


_ENTRYPOINT_PROBE = """\
import os
import sqlite3
import subprocess
import sys
from datetime import timedelta

from airflow.api.common.trigger_dag import trigger_dag
from airflow.sdk import timezone
from airflow.utils.types import DagRunTriggeredByType

trigger_dag(
    "admission_probe", run_id="probe",
    run_after=timezone.utcnow() - timedelta(minutes=1),
    triggered_by=DagRunTriggeredByType.CLI,
)
result = subprocess.run(
    [sys.executable, "-m", "riir_airflow.scheduler"],
    env=os.environ.copy(),
    cwd=os.getcwd(),
    capture_output=True,
    text=True,
    timeout=90,
)
assert result.returncode == 0, result.stdout + result.stderr
with sqlite3.connect("airflow.db") as database:
    assert database.execute(
        "SELECT state FROM dag_run WHERE run_id = 'probe'"
    ).fetchone() == ("running",)
    owner = database.execute(
        "SELECT queued_dttm, queued_by_job_id FROM task_instance WHERE task_id = 'work'"
    ).fetchone()
    assert owner[0] is not None
    assert database.execute(
        "SELECT state FROM job WHERE id = ?", (owner[1],)
    ).fetchone() == ("success",)
"""


def test_python_module_scheduler_entry_point(tmp_path: Path):
    _run_probe(
        tmp_path,
        "native",
        _DAG,
        _ENTRYPOINT_PROBE,
        AIRFLOW__SCHEDULER__NUM_RUNS="1",
        AIRFLOW__CORE__UNIT_TEST_MODE="True",
    )


def test_database_path_keeps_relative_sqlite_url(monkeypatch):
    from airflow.configuration import conf
    from riir_airflow.scheduler import _database_path

    monkeypatch.setattr(conf, "get", lambda *args, **kwargs: "sqlite:///airflow.db")
    assert _database_path() == Path("airflow.db")
    monkeypatch.setattr(
        conf, "get", lambda *args, **kwargs: "sqlite:////tmp/airflow.db"
    )
    assert _database_path() == Path("/tmp/airflow.db")


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
