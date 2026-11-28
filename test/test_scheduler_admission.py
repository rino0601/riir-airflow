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

_HOLDING_WORKER = """\
import json
import os
import sys
import time
from pathlib import Path

json.load(sys.stdin)
generation = sys.argv[-2]
parent = os.getppid()
for _ in range(500):
    if os.getppid() != parent or Path(f"release-{generation}").exists():
        break
    time.sleep(0.02)
"""

_HOLDING_PROBE = """\
from pathlib import Path

def holding_workload(prepare, dto):
    workload = prepare(dto)
    if workload is None:
        return None
    generation, _, payload = workload
    return generation, str(Path("holding_worker").resolve()), payload

def hold_tasks(runner):
    phases = runner.native_driver.phases
    prepare = phases.prepare_workload
    phases.prepare_workload = lambda dto: holding_workload(prepare, dto)

def release_task(database, runner):
    generation, = database.execute(
        "SELECT external_executor_id FROM task_instance WHERE working_set IS TRUE "
        "AND state IN ('queued', 'running') AND external_executor_id IS NOT NULL"
    ).fetchone()
    Path(f"release-{generation}").touch()
    runner.native_driver.native.wait_for_task()
"""

_SCHEDULER_PROBE = (
    _HOLDING_PROBE
    + """\
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
notifications = []

def observe_notification(run, msg):
    notifications.append((run.run_id, run.state, run.start_date, run.end_date, msg))
    original_notify(run, msg)

if mode.endswith("baseline"):
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
    unsupported = "newer" if "mixed-newer" in mode else "older"
    with create_session() as session:
        session.execute(
            update(DagRun).where(DagRun.run_id == unsupported).values(run_type=DagRunType.SCHEDULED)
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
    hold_tasks(runner)

def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)

queue = runner.executor.executor_queues[WorkloadType.EXECUTE_TASK]
with closing(sqlite3.connect("airflow.db")) as database:
    queued = runs(database)
    assert queued == [("older", "queued", None, None), ("newer", "queued", None, None)]

    assert iteration() == 1
    admitted = runs(database)
    older, newer = admitted
    assert older[:2] == ("older", "running")
    assert older[2] is not None
    assert older[3] is None
    assert newer == ("newer", "queued", None, None)
    if mode == "native":
        assert not queue
        assert database.execute(
            "SELECT run_id FROM task_instance WHERE working_set IS TRUE "
            "AND state = 'queued' AND external_executor_id IS NOT NULL"
        ).fetchone() == ("older",)
    else:
        assert [workload.ti.run_id for workload in queue.values()] == ["older"]

    with create_session() as session:
        assert runner._do_scheduling(session) == 0

    assert runs(database) == admitted
    if mode.endswith("baseline"):
        assert [(run_id, state, end, msg) for run_id, state, start, end, msg in notifications] == [
            ("older", "running", None, "started"),
        ]
        assert notifications[0][2] is not None
    else:
        assert notifications == []
    if mode == "native":
        release_task(database, runner)
"""
)


@pytest.mark.parametrize(
    "mode",
    [
        "baseline",
        "native",
        "mixed-baseline",
        "mixed-native",
        "mixed-newer-baseline",
        "mixed-newer-native",
    ],
)
def test_sqlite_scheduler_admits_only_one_queued_run(
    tmp_path: Path, mode: str, parsed_dag_factory
):
    _run_probe(tmp_path, mode, parsed_dag_factory, _DAG, _SCHEDULER_PROBE)


_SCHEDULE_DAG = """\
from datetime import datetime

from airflow.sdk import DAG, task

with DAG(
    "schedule_probe",
    schedule="@daily",
    start_date=datetime(2026, 1, 1),
    catchup=True,
    max_active_runs=1,
) as dag:
    @task
    def work():
        pass

    work()
"""

_SCHEDULE_PROBE = (
    _HOLDING_PROBE
    + """\
import sqlite3
import sys
from contextlib import closing
from datetime import timedelta

from airflow.executors.workloads import WorkloadType
from airflow.jobs.job import Job
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
from airflow.models.dagrun import DagRun
from airflow.sdk import timezone
from airflow.utils.session import create_session

from riir_airflow.scheduler import NativeSchedulerJobRunner

mode = sys.argv[1]
notifications = []
scheduled_preparations = []

def observe_notification(run, msg):
    notifications.append((run.run_id, run.state, run.start_date, run.end_date, msg))
    original_notify(run, msg)

if mode == "baseline":
    original_notify = DagRun.notify_dagrun_state_changed
    DagRun.notify_dagrun_state_changed = observe_notification

job = Job()
runner = SchedulerJobRunner(job) if mode == "baseline" else NativeSchedulerJobRunner(job)
with create_session() as session:
    session.add(job)

if mode == "native":
    hold_tasks(runner)
    phases = runner.native_driver.phases
    prepare_scheduled = phases.prepare_scheduled_runs

    def observe_scheduled(dtos):
        scheduled_preparations.append(len(dtos))
        return prepare_scheduled(dtos)

    phases.prepare_scheduled_runs = observe_scheduled

queue = runner.executor.executor_queues[WorkloadType.EXECUTE_TASK]

def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)

with closing(sqlite3.connect("airflow.db")) as database:
    def runs():
        return database.execute(
            "SELECT run_id, logical_date, data_interval_start, data_interval_end, "
            "run_after, state, run_type, triggered_by FROM dag_run ORDER BY run_id"
        ).fetchall()

    expected_run = (
        "scheduled__2026-01-01T00:00:00+00:00",
        "2026-01-01 00:00:00.000000",
        "2026-01-01 00:00:00.000000",
        "2026-01-01 00:00:00.000000",
        "2026-01-01 00:00:00.000000",
        "running",
        "scheduled",
        "TIMETABLE",
    )
    assert runs() == []
    assert iteration() == 1
    assert runs() == [expected_run]
    assert database.execute(
        "SELECT next_dagrun, next_dagrun_create_after, exceeds_max_non_backfill FROM dag"
    ).fetchone() == ("2026-01-02 00:00:00.000000", "2026-01-02 00:00:00.000000", 1)
    assert database.execute(
        "SELECT run_id, task_id, state, try_number FROM task_instance"
    ).fetchall() == [(expected_run[0], "work", "queued", 1)]
    if mode == "baseline":
        assert [(run_id, state, end, msg) for run_id, state, start, end, msg in notifications] == [
            (expected_run[0], "running", None, "started"),
        ]
        assert notifications[0][2] is not None
    else:
        assert notifications == []
    if mode == "native":
        assert scheduled_preparations == [1]
        assert not queue
        assert database.execute(
            "SELECT run_id FROM task_instance WHERE working_set IS TRUE "
            "AND state = 'queued' AND external_executor_id IS NOT NULL"
        ).fetchone() == (expected_run[0],)
    else:
        assert [workload.ti.run_id for workload in queue.values()] == [expected_run[0]]

    for _ in range(2):
        with create_session() as session:
            assert runner._do_scheduling(session) == 0
    assert runs() == [expected_run]
    assert len(notifications) == (1 if mode == "baseline" else 0)

    if mode == "native":
        release_task(database, runner)
"""
)


@pytest.mark.parametrize("mode", ["baseline", "native"])
def test_sqlite_scheduler_creates_and_starts_scheduled_run(
    tmp_path: Path, mode: str, parsed_dag_factory
):
    _run_probe(tmp_path, mode, parsed_dag_factory, _SCHEDULE_DAG, _SCHEDULE_PROBE)


_UNSUPPORTED_SCHEDULE_PROBE = """\
import sqlite3
import sys
from contextlib import closing

from airflow.executors.workloads import WorkloadType
from airflow.jobs.job import Job
from airflow.models.dag import DagModel
from airflow.utils.session import create_session
from sqlalchemy import update

from riir_airflow.scheduler import NativeSchedulerJobRunner

job = Job()
runner = NativeSchedulerJobRunner(job)
with create_session() as session:
    session.add(job)
    session.execute(
        update(DagModel).where(DagModel.dag_id == "schedule_probe")
        .values(timetable_type="CustomTimetable")
    )

phases = runner.native_driver.phases
prepare_scheduled = phases.prepare_scheduled_runs
prepared = []

def observe_scheduled(dtos):
    prepared.append(len(dtos))
    return prepare_scheduled(dtos)

phases.prepare_scheduled_runs = observe_scheduled

def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)

queue = runner.executor.executor_queues[WorkloadType.EXECUTE_TASK]
with closing(sqlite3.connect("airflow.db")) as database:
    assert iteration() == 1
    assert prepared == [1]
    assert database.execute(
        "SELECT run_id, state FROM dag_run WHERE dag_id = 'schedule_probe'"
    ).fetchall() == [("scheduled__2026-01-01T00:00:00+00:00", "running")]
    assert database.execute(
        "SELECT next_dagrun, next_dagrun_create_after FROM dag "
        "WHERE dag_id = 'schedule_probe'"
    ).fetchone() == ("2026-01-02 00:00:00.000000", "2026-01-02 00:00:00.000000")
    assert [workload.ti.run_id for workload in queue.values()] == [
        "scheduled__2026-01-01T00:00:00+00:00"
    ]
"""


def test_native_scheduler_delegates_unsupported_scheduled_creation(
    tmp_path: Path, parsed_dag_factory
):
    _run_probe(
        tmp_path,
        "native",
        parsed_dag_factory,
        _SCHEDULE_DAG,
        _UNSUPPORTED_SCHEDULE_PROBE,
    )


_SCHEDULE_EXECUTOR_DAG = """\
from datetime import datetime

from airflow.sdk import DAG, task

with DAG(
    "schedule_probe",
    schedule="@daily",
    start_date=datetime(2026, 1, 1),
    catchup=True,
    max_active_runs=1,
) as dag:
    @task(executor="riir_airflow.executors.asgi_executor.AsgiExecutor")
    def work():
        pass

    work()
"""

_SCHEDULE_EXECUTOR_PROBE = """\
import sqlite3
from contextlib import closing

from airflow.jobs.job import Job
from airflow.utils.session import create_session

from riir_airflow.scheduler import NativeSchedulerJobRunner

job = Job()
runner = NativeSchedulerJobRunner(job)
with create_session() as session:
    session.add(job)

phases = runner.native_driver.phases
prepare_scheduled = phases.prepare_scheduled_runs
prepared = []

def observe_scheduled(dtos):
    prepared.append(len(dtos))
    return prepare_scheduled(dtos)

phases.prepare_scheduled_runs = observe_scheduled

def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)

with closing(sqlite3.connect("airflow.db")) as database:
    assert iteration() == 1
    assert prepared == [1]
    assert database.execute(
        "SELECT run_id, state FROM dag_run WHERE dag_id = 'schedule_probe'"
    ).fetchall() == [("scheduled__2026-01-01T00:00:00+00:00", "running")]
    assert database.execute("SELECT executor FROM task_instance").fetchall() == [
        ("riir_airflow.executors.asgi_executor.AsgiExecutor",)
    ]
"""


def test_native_scheduler_delegates_explicit_executor_tasks(
    tmp_path: Path, parsed_dag_factory
):
    _run_probe(
        tmp_path,
        "native",
        parsed_dag_factory,
        _SCHEDULE_EXECUTOR_DAG,
        _SCHEDULE_EXECUTOR_PROBE,
    )


_SCHEDULE_DISABLED_VERSION_DAG = """\
from datetime import datetime

from airflow.sdk import DAG, task

with DAG(
    "schedule_probe",
    schedule="@daily",
    start_date=datetime(2026, 1, 1),
    catchup=True,
    max_active_runs=1,
    disable_bundle_versioning=True,
) as dag:
    @task
    def work():
        pass

    work()
"""

_SCHEDULE_DISABLED_VERSION_PROBE = """\
import sqlite3
import sys
from contextlib import closing

from airflow.jobs.job import Job
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
from airflow.models.dag import DagModel
from airflow.utils.session import create_session
from sqlalchemy import update

from riir_airflow.scheduler import NativeSchedulerJobRunner

mode = sys.argv[1]
job = Job()
runner = SchedulerJobRunner(job) if mode == "baseline" else NativeSchedulerJobRunner(job)
with create_session() as session:
    session.add(job)
    session.execute(
        update(DagModel).where(DagModel.dag_id == "schedule_probe").values(
            bundle_version="pinned"
        )
    )

prepared = []
if mode == "native":
    phases = runner.native_driver.phases
    prepare_scheduled = phases.prepare_scheduled_runs

    def observe_scheduled(dtos):
        prepared.append(len(dtos))
        return prepare_scheduled(dtos)

    phases.prepare_scheduled_runs = observe_scheduled

def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)

with closing(sqlite3.connect("airflow.db")) as database:
    assert iteration() == 1
    assert database.execute(
        "SELECT run_id, bundle_version FROM dag_run WHERE dag_id = 'schedule_probe'"
    ).fetchall() == [("scheduled__2026-01-01T00:00:00+00:00", None)]
    assert prepared == ([1] if mode == "native" else [])
"""


@pytest.mark.parametrize("mode", ["baseline", "native"])
def test_scheduled_run_honors_disabled_bundle_versioning(
    tmp_path: Path, mode: str, parsed_dag_factory
):
    _run_probe(
        tmp_path,
        mode,
        parsed_dag_factory,
        _SCHEDULE_DISABLED_VERSION_DAG,
        _SCHEDULE_DISABLED_VERSION_PROBE,
    )


_CAPACITY_DAGS = (
    _SCHEDULE_DAG
    + """\
with DAG(
    "blocked_probe",
    schedule="@daily",
    start_date=datetime(2025, 1, 1),
    catchup=True,
    max_active_runs=1,
) as blocked:
    @task
    def blocked_work():
        pass

    blocked_work()
"""
)

_CAPACITY_PROBE = (
    _HOLDING_PROBE
    + """\
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone as dt_timezone

from airflow.executors.workloads import WorkloadType
from airflow.jobs.job import Job
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
from airflow.models.dag import DagModel
from airflow.utils.session import create_session
from sqlalchemy import update

from riir_airflow.scheduler import NativeSchedulerJobRunner

mode = sys.argv[1]
job = Job()
runner = SchedulerJobRunner(job) if mode == "baseline" else NativeSchedulerJobRunner(job)
with create_session() as session:
    session.add(job)
    session.execute(
        update(DagModel).where(DagModel.dag_id == "blocked_probe").values(
            next_dagrun=datetime(2025, 1, 1, tzinfo=dt_timezone.utc),
            next_dagrun_create_after=datetime(2025, 1, 1, tzinfo=dt_timezone.utc),
            exceeds_max_non_backfill=True,
        )
    )

prepared = []
if mode == "native":
    hold_tasks(runner)
    phases = runner.native_driver.phases
    prepare_scheduled = phases.prepare_scheduled_runs

    def observe_scheduled(dtos):
        prepared.append(len(dtos))
        return prepare_scheduled(dtos)

    phases.prepare_scheduled_runs = observe_scheduled

queue = runner.executor.executor_queues[WorkloadType.EXECUTE_TASK]

def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)

with closing(sqlite3.connect("airflow.db")) as database:
    assert iteration() == 1
    assert database.execute(
        "SELECT dag_id, run_id FROM dag_run ORDER BY dag_id"
    ).fetchall() == [("schedule_probe", "scheduled__2026-01-01T00:00:00+00:00")]
    if mode == "native":
        assert prepared == [1]
        assert not queue
    else:
        assert prepared == []
        assert [workload.ti.dag_id for workload in queue.values()] == ["schedule_probe"]
    if mode == "native":
        release_task(database, runner)
"""
)


@pytest.mark.parametrize("mode", ["baseline", "native"])
def test_sqlite_scheduler_skips_capacity_blocked_dags_before_limit(
    tmp_path: Path, mode: str, parsed_dag_factory
):
    _run_probe(
        tmp_path,
        mode,
        parsed_dag_factory,
        _CAPACITY_DAGS,
        _CAPACITY_PROBE,
        AIRFLOW__SCHEDULER__MAX_DAGRUNS_TO_CREATE_PER_LOOP="1",
    )


_ASSET_FALLBACK_PROBE = """\
import sqlite3
import sys
from contextlib import closing

from airflow.jobs.job import Job
from airflow.utils.session import create_session

from riir_airflow.scheduler import NativeSchedulerJobRunner

job = Job()
runner = NativeSchedulerJobRunner(job)
with create_session() as session:
    session.add(job)

with closing(sqlite3.connect("airflow.db")) as database:
    database.execute(
        'INSERT INTO asset (id, name, uri, "group", extra, created_at, updated_at) '
        "VALUES (1, 'probe_asset', 's3://probe', '', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )
    database.execute(
        "INSERT INTO asset_event (id, asset_id, extra, timestamp) "
        "VALUES (1, 1, '{}', CURRENT_TIMESTAMP)"
    )
    database.execute(
        "INSERT INTO asset_dag_run_queue (target_dag_id, asset_event_id, asset_id, created_at) "
        "VALUES ('schedule_probe', 1, 1, CURRENT_TIMESTAMP)"
    )
    database.commit()

phases = runner.native_driver.phases
before_runs = phases.before_runs
prepare_scheduled = phases.prepare_scheduled_runs
delegated = []
prepared = []

def observe_before():
    delegated.append(True)
    return before_runs()

def observe_scheduled(dtos):
    prepared.append(len(dtos))
    return prepare_scheduled(dtos)

phases.before_runs = observe_before
phases.prepare_scheduled_runs = observe_scheduled

with create_session() as session:
    runner._do_scheduling(session)
assert delegated == [True]
assert prepared == []
with closing(sqlite3.connect("airflow.db")) as database:
    assert database.execute(
        "SELECT count(*) FROM dag_run WHERE dag_id = 'schedule_probe'"
    ).fetchone()[0] >= 1
"""


def test_native_scheduler_delegates_pending_asset_work(
    tmp_path: Path, parsed_dag_factory
):
    _run_probe(
        tmp_path, "native", parsed_dag_factory, _SCHEDULE_DAG, _ASSET_FALLBACK_PROBE
    )


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

_TASK_PROBE = (
    _HOLDING_PROBE
    + """\
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
        prepare = phases.prepare_workload

        def observe_prepare(dto):
            row = tasks()[dto["run_id"], dto["task_id"]]
            assert row[1:4] == ("scheduled", None, None)
            result = holding_workload(prepare, dto)
            selections.append((dto["id"], result is not None))
            return result

        phases.prepare_workload = observe_prepare

    def iteration():
        with create_session() as session:
            return runner._do_scheduling(session)

    def assert_queued(run, task):
        task_id, state, queued, owner, updated = tasks()[run, task]
        assert state == "queued"
        assert queued and queued <= updated
        assert owner == job_id
        if mode == "native":
            assert not queue
            assert database.execute(
                "SELECT id, queued_by_job_id FROM task_instance WHERE working_set IS TRUE "
                "AND state = 'queued' AND external_executor_id IS NOT NULL"
            ).fetchone() == (task_id, job_id)
        else:
            [(key, workload)] = queue.items()
            assert key == runner.executor.get_workload_key(workload)
            assert (workload.ti.run_id, workload.ti.task_id, workload.ti.id.hex) == (run, task, task_id)

    assert iteration() == 1
    assert_queued("older", "high")
    assert tasks()["older", "weighted"][1] == "scheduled"
    assert tasks()["newer", "high"][1] == "scheduled"

    admitted = tasks()["older", "high"]
    # Free pool capacity while the first workload still fills the executor.
    with create_session() as session:
        session.execute(update(Pool).where(Pool.pool == "weighted").values(include_deferred=False))
    assert iteration() == 0
    assert len(queue) == (0 if mode == "native" else 1)
    assert tasks()["older", "high"] == admitted
    assert tasks()["newer", "weighted"][1] == "scheduled"

    if mode == "fallback":
        assert selections == []
    else:
        if mode == "native":
            # Simulate the SDK's start transition before freeing the process slot.
            with create_session() as session:
                session.execute(update(TaskInstance).where(TaskInstance.run_id == "older", TaskInstance.task_id == "high")
                                .values(state="running"))
            release_task(database, runner)
        else:
            queue.clear()
        assert iteration() == 1
        assert_queued("newer", "weighted")
        assert tasks()["older", "weighted"][1] == "scheduled"
        assert tasks()["older", "high"][2:4] == admitted[2:4]

        if mode == "native":
            assert iteration() == 0
            assert selections == [(older_high_id, True), (newer_weighted_id, True)], selections
            release_task(database, runner)
        assert not queue or mode == "baseline"
"""
)


@pytest.mark.parametrize("mode", ["baseline", "native", "fallback"])
def test_sqlite_scheduler_admits_task_attempts(
    tmp_path: Path, mode: str, parsed_dag_factory
):
    _run_probe(
        tmp_path,
        mode,
        parsed_dag_factory,
        _TaskDAG,
        _TASK_PROBE,
        AIRFLOW__CORE__PARALLELISM="4" if mode == "native" else "1",
        AIRFLOW__SCHEDULER__MAX_TIS_PER_QUERY="1",
    )


_BOUNDARY_PROBE = (
    _HOLDING_PROBE
    + """\
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
hold_tasks(runner)
phases = driver.phases

def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)

queue = runner.executor.executor_queues[WorkloadType.EXECUTE_TASK]

with closing(sqlite3.connect("airflow.db")) as observer:
    def state(run, task):
        return observer.execute(
            "SELECT state, queued_dttm, queued_by_job_id FROM task_instance "
            "WHERE run_id = ? AND task_id = ? AND working_set IS TRUE", (run, task)
        ).fetchone()

    assert iteration() == 1
    assert observer.execute("SELECT state FROM dag_run WHERE run_id = 'probe'").fetchone() == ("running",)
    assert state("probe", "work")[0] == "queued"

    # An external write reservation skips the whole iteration without DML.
    with create_session() as session:
        session.execute(
            update(DagRun).where(DagRun.run_id == "probe").values(state="queued")
        )
    locker = subprocess.Popen(
        [sys.executable, "-c", _LOCKER, "airflow.db"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert locker.stdout.readline().strip() == "locked"
        with patch.object(phases, "before_runs", lambda: None), patch.object(
            phases, "delegate_runs", lambda *_: None
        ):
            assert iteration() == 0
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

    # A spawn failure compensates the committed claim without adding a queue entry.
    release_task(observer, runner)
    prepare = phases.prepare_workload

    def unavailable_program(dto):
        generation, _, payload = prepare(dto)
        return generation, "/missing/native-task-supervisor", payload

    with patch.object(phases, "prepare_workload", unavailable_program):
        assert iteration() == 0
    assert state("probe", "work") == ("scheduled", None, None)
    assert not queue
    assert observer.execute(
        "SELECT count(*) FROM task_instance WHERE working_set IS TRUE "
        "AND state = 'queued' AND external_executor_id IS NOT NULL"
    ).fetchone() == (0,)

    # Recover a #78-format queued attempt with a failed owner and no launch intent.
    with create_session() as session:
        session.execute(update(TaskInstance).where(TaskInstance.run_id == "probe")
                        .values(state="queued", queued_dttm=timezone.utcnow(), queued_by_job_id=job.id))
        session.execute(update(Job).where(Job.id == job.id).values(state="failed"))
    original = state("probe", "work")
    new_job = Job(state="running")
    replacement = NativeSchedulerJobRunner(new_job)
    hold_tasks(replacement)
    with create_session() as session:
        session.add(new_job)
    assert replacement.adopt_or_reset_orphaned_tasks() == 1
    assert state("probe", "work")[:2] == original[:2]
    assert state("probe", "work")[2] == new_job.id
    assert replacement.adopt_or_reset_orphaned_tasks() == 0
    assert iteration() == 0
    assert state("probe", "work")[:2] == original[:2]
    release_task(observer, replacement)

    # Recover another legacy queued attempt from an owner with a stale heartbeat.
    with create_session() as session:
        session.execute(update(TaskInstance).where(TaskInstance.run_id == "probe")
                        .values(queued_by_job_id=new_job.id))
        session.execute(update(Job).where(Job.id == new_job.id)
                        .values(latest_heartbeat=timezone.utcnow() - timedelta(hours=1)))
    final_job = Job(state="running")
    final = NativeSchedulerJobRunner(final_job)
    hold_tasks(final)
    with create_session() as session:
        session.add(final_job)
    assert final.adopt_or_reset_orphaned_tasks() == 1
    assert state("probe", "work")[2] == final_job.id
    assert observer.execute(
        "SELECT state FROM job WHERE id = ?", (new_job.id,)
    ).fetchone() == ("failed",)
    assert final.adopt_or_reset_orphaned_tasks() == 0
    release_task(observer, final)

    # An orphan outside the native subset is reset by Airflow and can be queued again.
    fallback_job = Job(state="running")
    fallback = NativeSchedulerJobRunner(fallback_job)
    fallback_queue = fallback.executor.executor_queues[WorkloadType.EXECUTE_TASK]
    with create_session() as session:
        session.execute(update(DagModel).where(DagModel.dag_id == "admission_probe")
                        .values(fail_fast=True))
        session.execute(update(TaskInstance).where(TaskInstance.run_id == "probe")
                        .values(queued_by_job_id=final_job.id))
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
)


def test_native_scheduler_transaction_boundaries(tmp_path: Path, parsed_dag_factory):
    _run_probe(tmp_path, "native", parsed_dag_factory, _DAG, _BOUNDARY_PROBE)


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

task_prepare = phases.prepare_workload
native_task_claims = []

def observe_task(dto):
    native_task_claims.append(dto["id"])
    return task_prepare(dto)

phases.prepare_workload = observe_task

with closing(sqlite3.connect("airflow.db")) as observer:
    def states():
        return dict(observer.execute(
            "SELECT run_id, state FROM dag_run WHERE dag_id = 'admission_probe'"
        ))

    assert iteration() == 1
    current = states()
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
assert iteration() == 1
assert native_task_claims == []
with closing(sqlite3.connect("airflow.db")) as observer:
    assert observer.execute(
        "SELECT state FROM dag_run WHERE run_id = ?", (running_run,)
    ).fetchone() == ("running",)
"""


def test_native_scheduler_fallback_before_write(tmp_path: Path, parsed_dag_factory):
    _run_probe(tmp_path, "native", parsed_dag_factory, _DAG, _FALLBACK_PROBE)


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

with create_session() as session:
    assert runner._do_scheduling(session) == 1
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


def test_native_scheduler_delegates_with_listeners(tmp_path: Path, parsed_dag_factory):
    _run_probe(tmp_path, "native", parsed_dag_factory, _DAG, _LISTENER_PROBE)


_RUNNER_PROBE = (
    _HOLDING_PROBE
    + """\
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
loaded_dags = []
read_dag = runner.scheduler_dag_bag._read_dag

def observe_dag(row):
    loaded_dags.append(row.dag_id)
    return read_dag(row)

runner.scheduler_dag_bag._read_dag = observe_dag
hold_tasks(runner)

with create_session() as session:
    assert runner._do_scheduling(session) == 1
with create_session() as session:
    assert runner._do_scheduling(session) == 0
assert loaded_dags == ["admission_probe"], loaded_dags
with closing(sqlite3.connect("airflow.db")) as database:
    assert database.execute(
        "SELECT state FROM dag_run WHERE run_id = 'probe'"
    ).fetchone() == ("running",)
    assert database.execute(
        "SELECT state, queued_by_job_id FROM task_instance WHERE task_id = 'work'"
    ).fetchone() == ("queued", job.id)
    release_task(database, runner)
"""
)


def test_scheduler_entry_point_runner_calls_native_claims(
    tmp_path: Path, parsed_dag_factory
):
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
    _run_probe(tmp_path, "native", parsed_dag_factory, dag_source, _RUNNER_PROBE)


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
    assert database.execute(
        "SELECT count(*) FROM log WHERE event = 'queued' "
        "AND json_extract(extra, '$.executor_id') IS NOT NULL"
    ).fetchone() == (1,)
    owner = database.execute(
        "SELECT json_extract(extra, '$.job_id') FROM log WHERE event = 'queued' "
        "AND json_extract(extra, '$.executor_id') IS NOT NULL"
    ).fetchone()
    assert database.execute(
        "SELECT state FROM job WHERE id = ?", (owner[0],)
    ).fetchone() == ("success",)
    assert database.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'riir_attempt'"
    ).fetchone() == (0,)
"""


def test_python_module_scheduler_entry_point(tmp_path: Path, parsed_dag_factory):
    _run_probe(
        tmp_path,
        "native",
        parsed_dag_factory,
        _DAG,
        _ENTRYPOINT_PROBE,
        AIRFLOW__SCHEDULER__NUM_RUNS="1",
        AIRFLOW__CORE__UNIT_TEST_MODE="True",
    )


_DEPENDENCY_DAG = """\
from airflow.sdk import DAG, task

with DAG("admission_probe", schedule=None, max_active_runs=1) as dag:
    @task
    def upstream():
        pass

    @task
    def downstream():
        pass

    upstream() >> downstream()
"""

_DEPENDENCY_FALLBACK_DAG = """\
from airflow.sdk import DAG, task

with DAG("admission_probe", schedule=None, max_active_runs=1) as dag:
    @task
    def upstream():
        pass

    @task(trigger_rule="all_done")
    def downstream():
        pass

    upstream() >> downstream()
"""

_DEPENDENCY_PROBE = """\
import sqlite3
import sys
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

from airflow.api.common.trigger_dag import trigger_dag
from airflow.jobs.job import Job
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
from airflow.sdk import timezone
from airflow.utils.session import create_session
from airflow.utils.types import DagRunTriggeredByType

from riir_airflow.scheduler import NativeSchedulerJobRunner

mode = sys.argv[1]
job = Job()
runner = SchedulerJobRunner(job) if mode.endswith("baseline") else NativeSchedulerJobRunner(job)
with create_session() as session:
    session.add(job)

trigger_dag(
    "admission_probe", run_id="probe",
    run_after=timezone.utcnow() - timedelta(minutes=1),
    triggered_by=DagRunTriggeredByType.CLI,
)

delegate_calls = []
graph_calls = []
if mode.endswith("native"):
    phases = runner.native_driver.phases
    delegate = phases.delegate_runs
    graphs = phases.prepare_dependency_graphs

    def observe_delegate(delegate_ids=None):
        delegate_calls.append(list(delegate_ids or []))
        return delegate(delegate_ids)

    def observe_graphs(dtos):
        result = graphs(dtos)
        graph_calls.append(result)
        return result

    phases.delegate_runs = observe_delegate
    phases.prepare_dependency_graphs = observe_graphs


def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)


with closing(sqlite3.connect("airflow.db")) as observer:
    def states():
        return dict(observer.execute(
            "SELECT task_id, state FROM task_instance WHERE run_id = 'probe'"
        ))

    def mark_upstream_success():
        with sqlite3.connect("airflow.db") as writable:
            writable.execute(
                "UPDATE task_instance SET state = 'success' "
                "WHERE run_id = 'probe' AND task_id = 'upstream'"
            )

    with patch.object(runner, "_parallelism", 0):
        iteration()
        assert states() == {"upstream": "scheduled", "downstream": None}, states()
        mark_upstream_success()
        iteration()
        assert states() == {"upstream": "success", "downstream": "scheduled"}, states()
        iteration()
        assert states() == {"upstream": "success", "downstream": "scheduled"}, states()

    if mode.endswith("native"):
        run_pk = observer.execute(
            "SELECT id FROM dag_run WHERE run_id = 'probe'"
        ).fetchone()[0]
        assert len(graph_calls) == 3
        if mode.startswith("fallback"):
            assert len(delegate_calls) == 3
            assert graph_calls == [[None], [None], [None]]
            assert delegate_calls == [[run_pk], [run_pk], [run_pk]]
        else:
            assert [call[0]["id"] for call in graph_calls] == [run_pk] * 3
            assert delegate_calls == []
"""


@pytest.mark.parametrize(
    "mode",
    [
        "supported-baseline",
        "supported-native",
        "fallback-baseline",
        "fallback-native",
    ],
)
def test_native_scheduler_evaluates_task_dependencies(
    tmp_path: Path, mode: str, parsed_dag_factory
):
    dag_source = (
        _DEPENDENCY_FALLBACK_DAG if mode.startswith("fallback") else _DEPENDENCY_DAG
    )
    _run_probe(tmp_path, mode, parsed_dag_factory, dag_source, _DEPENDENCY_PROBE)


_FUTURE_LOGICAL_DATE_PROBE = """\
import sqlite3
import sys
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

from airflow.api.common.trigger_dag import trigger_dag
from airflow.jobs.job import Job
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
from airflow.sdk import timezone
from airflow.utils.session import create_session
from airflow.utils.types import DagRunTriggeredByType

from riir_airflow.scheduler import NativeSchedulerJobRunner

mode = sys.argv[1]
job = Job()
runner = SchedulerJobRunner(job) if mode.endswith("baseline") else NativeSchedulerJobRunner(job)
with create_session() as session:
    session.add(job)

delegate_calls = []
graph_calls = []
if mode.endswith("native"):
    phases = runner.native_driver.phases
    delegate = phases.delegate_runs
    graphs = phases.prepare_dependency_graphs

    def observe_delegate(delegate_ids=None):
        delegate_calls.append(list(delegate_ids or []))
        return delegate(delegate_ids)

    def observe_graphs(dtos):
        result = graphs(dtos)
        graph_calls.append(result)
        return result

    phases.delegate_runs = observe_delegate
    phases.prepare_dependency_graphs = observe_graphs

trigger_dag(
    "admission_probe", run_id="probe", logical_date=timezone.utcnow() + timedelta(hours=1),
    run_after=timezone.utcnow() - timedelta(minutes=1),
    triggered_by=DagRunTriggeredByType.CLI,
)


def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)


with closing(sqlite3.connect("airflow.db")) as observer:
    with patch.object(runner, "_parallelism", 0):
        iteration()
        assert dict(observer.execute(
            "SELECT task_id, state FROM task_instance WHERE run_id = 'probe'"
        )) == {"upstream": None, "downstream": None}

    if mode.endswith("native"):
        run_pk = observer.execute(
            "SELECT id FROM dag_run WHERE run_id = 'probe'"
        ).fetchone()[0]
        assert graph_calls == [[None]]
        assert delegate_calls == [[run_pk]]
"""


@pytest.mark.parametrize("mode", ["future-baseline", "future-native"])
def test_native_scheduler_defers_future_logical_dates(
    tmp_path: Path, mode: str, parsed_dag_factory
):
    _run_probe(
        tmp_path, mode, parsed_dag_factory, _DEPENDENCY_DAG, _FUTURE_LOGICAL_DATE_PROBE
    )


_VERSION_CHANGE_PROBE = """\
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from airflow.api.common.trigger_dag import trigger_dag
from airflow.jobs.job import Job
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
from airflow.sdk import timezone
from airflow.utils.session import create_session
from airflow.utils.types import DagRunTriggeredByType

from riir_airflow.scheduler import NativeSchedulerJobRunner

mode = sys.argv[1]
job = Job()
runner = SchedulerJobRunner(job) if mode.endswith("baseline") else NativeSchedulerJobRunner(job)
with create_session() as session:
    session.add(job)

delegate_calls = []
graph_calls = []
if mode.endswith("native"):
    phases = runner.native_driver.phases
    delegate = phases.delegate_runs
    graphs = phases.prepare_dependency_graphs

    def observe_delegate(delegate_ids=None):
        delegate_calls.append(list(delegate_ids or []))
        return delegate(delegate_ids)

    def observe_graphs(dtos):
        result = graphs(dtos)
        graph_calls.append(result)
        return result

    phases.delegate_runs = observe_delegate
    phases.prepare_dependency_graphs = observe_graphs

trigger_dag(
    "admission_probe", run_id="probe",
    run_after=timezone.utcnow() - timedelta(minutes=1),
    triggered_by=DagRunTriggeredByType.CLI,
)


def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)


with closing(sqlite3.connect("airflow.db")) as observer:
    original_version = observer.execute(
        "SELECT created_dag_version_id FROM dag_run WHERE run_id = 'probe'"
    ).fetchone()[0]
    with patch.object(runner, "_parallelism", 0):
        iteration()
        assert dict(observer.execute(
            "SELECT task_id, state FROM task_instance WHERE run_id = 'probe'"
        )) == {"upstream": "scheduled", "downstream": None}

time.sleep(1)
(Path(os.environ["AIRFLOW_HOME"]) / "dags" / "admission_probe.py").write_text('''
from airflow.sdk import DAG, task

with DAG("admission_probe", schedule=None, max_active_runs=1) as dag:
    @task
    def upstream():
        pass

    @task
    def downstream():
        pass

    @task
    def extra():
        pass

    upstream() >> downstream() >> extra()
''')
processed = subprocess.run(
    [sys.executable, "-m", "airflow", "dag-processor", "--num-runs", "2"],
    env=os.environ.copy(), cwd=os.getcwd(), capture_output=True, text=True,
    timeout=90,
)
assert processed.returncode == 0, processed.stdout + processed.stderr

with closing(sqlite3.connect("airflow.db")) as observer:
    with patch.object(runner, "_parallelism", 0):
        iteration()

    latest_version = observer.execute(
        "SELECT id FROM dag_version ORDER BY created_at DESC LIMIT 1"
    ).fetchone()[0]
    assert latest_version != original_version, (latest_version, original_version)
    assert dict(observer.execute(
        "SELECT task_id, state FROM task_instance WHERE run_id = 'probe'"
    )) == {"upstream": "scheduled", "downstream": None, "extra": None}
    assert set(observer.execute(
        "SELECT DISTINCT dag_version_id FROM task_instance WHERE run_id = 'probe'"
    )) == {(latest_version,)}

    if mode.endswith("native"):
        run_pk = observer.execute(
            "SELECT id FROM dag_run WHERE run_id = 'probe'"
        ).fetchone()[0]
        assert len(graph_calls) == 2
        assert graph_calls[0][0]["id"] == run_pk
        assert graph_calls[1] == [None]
        assert delegate_calls == [[run_pk]]
"""


@pytest.mark.parametrize("mode", ["version-baseline", "version-native"])
def test_native_scheduler_delegates_changed_unpinned_runs(
    tmp_path: Path, mode: str, parsed_dag_factory
):
    _run_probe(
        tmp_path,
        mode,
        parsed_dag_factory,
        _DEPENDENCY_DAG,
        _VERSION_CHANGE_PROBE,
        fresh_parse=True,
    )


_TIMEOUT_DAG = """\
from datetime import timedelta

from airflow.sdk import DAG, task

with DAG(
    "admission_probe", schedule=None, max_active_runs=1, dagrun_timeout=timedelta(seconds=1)
) as dag:
    @task
    def upstream():
        pass

    @task
    def downstream():
        pass

    upstream() >> downstream()
"""

_TIMEOUT_PROBE = """\
import sqlite3
import sys
import time
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

from airflow.api.common.trigger_dag import trigger_dag
from airflow.jobs.job import Job
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
from airflow.sdk import timezone
from airflow.utils.session import create_session
from airflow.utils.types import DagRunTriggeredByType

from riir_airflow.scheduler import NativeSchedulerJobRunner

mode = sys.argv[1]
job = Job()
runner = SchedulerJobRunner(job) if mode.endswith("baseline") else NativeSchedulerJobRunner(job)
with create_session() as session:
    session.add(job)

delegate_calls = []
graph_calls = []
if mode.endswith("native"):
    phases = runner.native_driver.phases
    delegate = phases.delegate_runs
    graphs = phases.prepare_dependency_graphs

    def observe_delegate(delegate_ids=None):
        delegate_calls.append(list(delegate_ids or []))
        return delegate(delegate_ids)

    def observe_graphs(dtos):
        result = graphs(dtos)
        graph_calls.append(result)
        return result

    phases.delegate_runs = observe_delegate
    phases.prepare_dependency_graphs = observe_graphs

trigger_dag(
    "admission_probe", run_id="probe",
    run_after=timezone.utcnow() - timedelta(minutes=1),
    triggered_by=DagRunTriggeredByType.CLI,
)


def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)


with closing(sqlite3.connect("airflow.db")) as observer:
    with patch.object(runner, "_parallelism", 0):
        iteration()
        assert dict(observer.execute(
            "SELECT task_id, state FROM task_instance WHERE run_id = 'probe'"
        )) == {"upstream": "scheduled", "downstream": None}
        time.sleep(1.2)
        iteration()
        assert observer.execute(
            "SELECT state FROM dag_run WHERE run_id = 'probe'"
        ).fetchone() == ("failed",)
        assert dict(observer.execute(
            "SELECT task_id, state FROM task_instance WHERE run_id = 'probe'"
        )) == {"upstream": "skipped", "downstream": "skipped"}

    if mode.endswith("native"):
        run_pk = observer.execute(
            "SELECT id FROM dag_run WHERE run_id = 'probe'"
        ).fetchone()[0]
        assert graph_calls == [[None], [None]]
        assert delegate_calls == [[run_pk], [run_pk]]
"""


@pytest.mark.parametrize("mode", ["timeout-baseline", "timeout-native"])
def test_native_scheduler_delegates_dagrun_timeouts(
    tmp_path: Path, mode: str, parsed_dag_factory
):
    _run_probe(tmp_path, mode, parsed_dag_factory, _TIMEOUT_DAG, _TIMEOUT_PROBE)


_RETRY_PROBE = """\
import sqlite3
import sys
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

from airflow.api.common.trigger_dag import trigger_dag
from airflow.jobs.job import Job
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
from airflow.sdk import timezone
from airflow.utils.session import create_session
from airflow.utils.types import DagRunTriggeredByType

from riir_airflow.scheduler import NativeSchedulerJobRunner

mode = sys.argv[1]
job = Job()
runner = SchedulerJobRunner(job) if mode.endswith("baseline") else NativeSchedulerJobRunner(job)
with create_session() as session:
    session.add(job)

trigger_dag(
    "admission_probe", run_id="probe",
    run_after=timezone.utcnow() - timedelta(minutes=1),
    triggered_by=DagRunTriggeredByType.CLI,
)

delegate_calls = []
graph_calls = []
if mode.endswith("native"):
    phases = runner.native_driver.phases
    delegate = phases.delegate_runs
    graphs = phases.prepare_dependency_graphs

    def observe_delegate(delegate_ids=None):
        delegate_calls.append(list(delegate_ids or []))
        return delegate(delegate_ids)

    def observe_graphs(dtos):
        result = graphs(dtos)
        graph_calls.append(result)
        return result

    phases.delegate_runs = observe_delegate
    phases.prepare_dependency_graphs = observe_graphs


def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)


with closing(sqlite3.connect("airflow.db")) as observer:
    def states():
        return dict(observer.execute(
            "SELECT task_id, state FROM task_instance WHERE run_id = 'probe'"
        ))

    def set_retry(retry_at):
        with sqlite3.connect("airflow.db") as writable:
            writable.execute(
                "UPDATE task_instance SET state = 'up_for_retry', retry_at = ? "
                "WHERE run_id = 'probe' AND task_id = 'upstream'",
                (retry_at,),
            )

    future = (timezone.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.%f")
    past = (timezone.utcnow() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S.%f")

    with patch.object(runner, "_parallelism", 0):
        iteration()
        assert states() == {"upstream": "scheduled", "downstream": None}, states()
        set_retry(future)
        iteration()
        assert states() == {"upstream": "up_for_retry", "downstream": None}, states()
        set_retry(past)
        iteration()
        assert states() == {"upstream": "scheduled", "downstream": None}, states()

    if mode.endswith("native"):
        run_pk = observer.execute(
            "SELECT id FROM dag_run WHERE run_id = 'probe'"
        ).fetchone()[0]
        assert len(graph_calls) == 3
        assert len(delegate_calls) == 1
        assert [call[0]["id"] if call[0] else None for call in graph_calls] == [
            run_pk,
            run_pk,
            None,
        ]
        assert delegate_calls == [[run_pk]]
"""


@pytest.mark.parametrize("mode", ["retry-baseline", "retry-native"])
def test_native_scheduler_honors_retry_period(
    tmp_path: Path, mode: str, parsed_dag_factory
):
    _run_probe(tmp_path, mode, parsed_dag_factory, _DEPENDENCY_DAG, _RETRY_PROBE)


_CALLBACK_DAG = """\
from pathlib import Path

from airflow.sdk import DAG, task


def note_success(context):
    Path("dag_callback").write_text(context["dag_run"].run_id)


with DAG(
    "admission_probe",
    schedule=None,
    max_active_runs=1,
    on_success_callback=note_success,
) as dag:
    @task
    def upstream():
        pass

    @task
    def downstream():
        pass

    upstream() >> downstream()
"""

_TERMINAL_PROBE = """\
import sqlite3
import sys
from contextlib import closing
from datetime import timedelta

from airflow.api.common.trigger_dag import trigger_dag
from airflow.jobs.job import Job
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
from airflow.models.dagrun import DagRun
from airflow.sdk import timezone
from airflow.utils.session import create_session
from airflow.utils.types import DagRunTriggeredByType

from riir_airflow.scheduler import NativeSchedulerJobRunner

mode = sys.argv[1]
case = mode.split("-", 1)[0]
native = mode.endswith("native")
job = Job()
runner = NativeSchedulerJobRunner(job) if native else SchedulerJobRunner(job)
with create_session() as session:
    session.add(job)

trigger_dag(
    "admission_probe", run_id="probe",
    run_after=timezone.utcnow() - timedelta(minutes=1),
    triggered_by=DagRunTriggeredByType.CLI,
)

delegate_calls = []
update_calls = []
callback_calls = []
if native:
    phases = runner.native_driver.phases
    delegate = phases.delegate_runs

    def observe_delegate(delegate_ids=None):
        delegate_calls.append(list(delegate_ids or []))
        return delegate(delegate_ids)

    phases.delegate_runs = observe_delegate

original_update_state = DagRun.update_state


def observe_update_state(self, *args, **kwargs):
    update_calls.append(self.run_id)
    return original_update_state(self, *args, **kwargs)


DagRun.update_state = observe_update_state


def observe_callbacks(dag, callback=None):
    if callback:
        callback_calls.append((callback.dag_id, callback.msg))


runner._send_dag_callbacks_to_processor = observe_callbacks


def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)


with closing(sqlite3.connect("airflow.db")) as observer:
    run_pk = observer.execute(
        "SELECT id FROM dag_run WHERE run_id = 'probe'"
    ).fetchone()[0]

    def run_state():
        return observer.execute(
            "SELECT state, end_date FROM dag_run WHERE id = ?", (run_pk,)
        ).fetchone()

    def mark(task, state, retry_at=None):
        with sqlite3.connect("airflow.db") as writable:
            writable.execute(
                "UPDATE task_instance SET state = ?, retry_at = ? "
                "WHERE run_id = 'probe' AND task_id = ?",
                (state, retry_at, task),
            )

    if case == "retry":
        mark("upstream", "up_for_retry", (
            timezone.utcnow() + timedelta(hours=1)
        ).strftime("%Y-%m-%d %H:%M:%S.%f"))
    else:
        mark("upstream", "success")
        if case != "unfinished":
            mark("downstream", "failed" if case == "failure" else "success")

    with sqlite3.connect("airflow.db") as writable:
        writable.execute(
            "UPDATE dag SET exceeds_max_non_backfill = 1 "
            "WHERE dag_id = 'admission_probe'"
        )

    expected = {
        "success": "success",
        "failure": "failed",
        "unfinished": "running",
        "retry": "running",
        "callback": "success",
    }[case]
    first = None
    for _ in range(2):
        iteration()
        current = run_state()
        assert current[0] == expected, (case, mode, current)
        assert (current[1] is not None) == (expected in ("success", "failed"))
        if first is None:
            first = current
        else:
            assert current == first
        assert observer.execute(
            "SELECT last_scheduling_decision IS NOT NULL FROM dag_run WHERE id = ?",
            (run_pk,),
        ).fetchone() == (1,)

    exceeds = observer.execute(
        "SELECT exceeds_max_non_backfill FROM dag WHERE dag_id = 'admission_probe'"
    ).fetchone()[0]
    assert exceeds == (1 if expected == "running" else 0), (case, expected, exceeds)
    if case == "callback":
        assert callback_calls == [("admission_probe", "success")], (
            callback_calls,
            update_calls,
            delegate_calls,
        )
        assert len(update_calls) == 1
        if native:
            assert delegate_calls == [[run_pk]]
    else:
        assert callback_calls == []
        if native:
            assert update_calls == []
            assert delegate_calls == []
"""


@pytest.mark.parametrize(
    "mode",
    [
        f"{case}-{scheduler}"
        for case in ("success", "failure", "unfinished", "retry", "callback")
        for scheduler in ("baseline", "native")
    ],
)
def test_native_scheduler_finalizes_task_outcomes(
    tmp_path: Path, mode: str, parsed_dag_factory
):
    dag_source = _CALLBACK_DAG if mode.startswith("callback") else _DEPENDENCY_DAG
    _run_probe(tmp_path, mode, parsed_dag_factory, dag_source, _TERMINAL_PROBE)


_PHASE_HANDOFF_DAG = """\
from airflow.sdk import DAG, task

with DAG("admission_probe", schedule=None, max_active_runs=1) as dag:
    @task
    def upstream():
        pass

    @task
    def downstream():
        pass

    upstream() >> downstream()


def note_success(context):
    pass


with DAG(
    "callback_probe",
    schedule=None,
    max_active_runs=1,
    on_success_callback=note_success,
) as callback_dag:
    @task
    def work():
        pass

    work()
"""

_PHASE_HANDOFF_PROBE = """\
import sqlite3
import sys
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

from airflow.api.common.trigger_dag import trigger_dag
from airflow.jobs.job import Job
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
from airflow.models.dagrun import DagRun
from airflow.sdk import timezone
from airflow.utils.session import create_session
from airflow.utils.types import DagRunTriggeredByType

from riir_airflow.scheduler import NativeSchedulerJobRunner

mode = sys.argv[1]
case, scheduler = mode.rsplit("-", 1)
native = scheduler == "native"
job = Job()
runner = NativeSchedulerJobRunner(job) if native else SchedulerJobRunner(job)
with create_session() as session:
    session.add(job)

run_after = timezone.utcnow() - timedelta(minutes=1)
trigger_dag(
    "admission_probe", run_id="native_run", run_after=run_after,
    triggered_by=DagRunTriggeredByType.CLI,
)
if case == "mixed":
    trigger_dag(
        "callback_probe", run_id="delegated_run", run_after=run_after,
        triggered_by=DagRunTriggeredByType.CLI,
    )

delegate_calls = []
update_calls = []
callback_calls = []
if native:
    phases = runner.native_driver.phases
    assert not hasattr(phases, "between_phases")
    delegate = phases.delegate_runs

    def observe_delegate(delegate_ids=None):
        delegate_calls.append(list(delegate_ids or []))
        return delegate(delegate_ids)

    phases.delegate_runs = observe_delegate
    if case == "native-only":
        def forbidden(*args, **kwargs):
            raise AssertionError("native-only run entered Airflow run scheduling")

        SchedulerJobRunner._schedule_all_dag_runs = forbidden

original_update_state = DagRun.update_state

def observe_update_state(self, *args, **kwargs):
    update_calls.append(self.run_id)
    return original_update_state(self, *args, **kwargs)

DagRun.update_state = observe_update_state

def observe_callbacks(dag, callback=None):
    if callback:
        callback_calls.append((callback.dag_id, callback.msg))

runner._send_dag_callbacks_to_processor = observe_callbacks

def iteration():
    with create_session() as session:
        return runner._do_scheduling(session)

with closing(sqlite3.connect("airflow.db")) as observer:
    with sqlite3.connect("airflow.db") as writable:
        writable.executemany(
            "UPDATE task_instance SET state = 'success' WHERE run_id = ?",
            [("native_run",)] + ([("delegated_run",)] if case == "mixed" else []),
        )
    expected = {"native_run": "success"}
    if case == "mixed":
        expected["delegated_run"] = "success"
    with patch.object(runner, "_parallelism", 0):
        iteration()
        iteration()
    assert dict(observer.execute(
        "SELECT run_id, state FROM dag_run WHERE run_id IN "
        "('native_run', 'delegated_run') ORDER BY run_id"
    )) == expected
    if case == "mixed":
        assert callback_calls == [("callback_probe", "success")]
        if native:
            run_pk = observer.execute(
                "SELECT id FROM dag_run WHERE run_id = 'delegated_run'"
            ).fetchone()[0]
            assert delegate_calls == [[run_pk]]
            assert update_calls == ["delegated_run"]
        else:
            assert set(update_calls) == {"native_run", "delegated_run"}
    else:
        assert callback_calls == []
        assert update_calls == ([] if native else ["native_run"])
        if native:
            assert delegate_calls == []
"""


@pytest.mark.parametrize(
    "mode",
    [
        f"{case}-{scheduler}"
        for case in ("native-only", "mixed")
        for scheduler in ("baseline", "native")
    ],
)
def test_native_scheduler_phase_handoff_is_explicit(
    tmp_path: Path, mode: str, parsed_dag_factory
):
    _run_probe(
        tmp_path, mode, parsed_dag_factory, _PHASE_HANDOFF_DAG, _PHASE_HANDOFF_PROBE
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


def _run_probe(
    tmp_path: Path,
    mode: str,
    parsed_dag_factory,
    dag_source: str,
    probe: str,
    *,
    fresh_parse: bool = False,
    **config: str,
):
    assert airflow.__version__ == BASELINE_AIRFLOW_VERSION
    holding_worker = tmp_path / "holding_worker"
    holding_worker.write_text(f"#!{sys.executable}\n{_HOLDING_WORKER}")
    holding_worker.chmod(0o700)
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AIRFLOW")
    }
    env.update(
        AIRFLOW_HOME=str(tmp_path),
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
    _, env = parsed_dag_factory.prepare(
        tmp_path,
        env,
        {"admission_probe.py": dag_source},
        fresh=fresh_parse,
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, mode],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
