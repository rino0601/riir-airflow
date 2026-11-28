import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

from airflow.jobs.job import Job
from airflow.models.dag import DagModel
from airflow.models.dagrun import DagRun
from airflow.models.pool import Pool
from airflow.models.taskinstance import TaskInstance
from airflow.sdk import timezone
from airflow.utils.types import DagRunType
from support import (
    add_notification_listener,
    add_pool,
    baseline_iteration,
    database_lock,
    failed_program,
    hold_tasks,
    holding_workload,
    iteration,
    minutes_ago,
    observe_callbacks,
    observe_delegate,
    observe_graphs,
    observe_run_updates,
    observe_scheduled,
    query,
    release_task,
    replace_dag,
    run_command,
    run_dag_processor,
    start_runner,
    trigger_run,
    update_rows,
    workload_queue,
    write,
)


def scheduler_admission(mode):
    now = minutes_ago()
    for run_id, run_after in [
        ("newer", now),
        ("older", now - timedelta(minutes=1)),
    ]:
        trigger_run(run_id, run_after=run_after)
    if "mixed" in mode:
        unsupported = "newer" if "mixed-newer" in mode else "older"
        update_rows(
            DagRun,
            {"run_type": DagRunType.SCHEDULED},
            DagRun.run_id == unsupported,
        )

    def runs():
        return query(
            "SELECT run_id, state, start_date, end_date FROM dag_run "
            "WHERE dag_id = 'admission_probe' ORDER BY run_after"
        )

    _, runner = start_runner(mode)
    hold_tasks(runner)
    queue = workload_queue(runner)
    assert runs() == [
        ("older", "queued", None, None),
        ("newer", "queued", None, None),
    ]

    assert iteration(runner) == 1
    admitted = runs()
    older, newer = admitted
    assert older[:2] == ("older", "running")
    assert older[2] is not None
    assert older[3] is None
    assert newer == ("newer", "queued", None, None)
    if mode == "native":
        assert not queue
        assert query(
            "SELECT run_id FROM task_instance WHERE working_set IS TRUE "
            "AND state = 'queued' AND external_executor_id IS NOT NULL"
        ) == [("older",)]
    else:
        assert [workload.ti.run_id for workload in queue.values()] == ["older"]

    assert iteration(runner) == 0
    assert runs() == admitted
    if mode == "native":
        release_task(runner)


def scheduled_runs(mode):
    scheduled_preparations = []
    _, runner = start_runner(mode)
    hold_tasks(runner)
    observe_scheduled(runner, scheduled_preparations)
    queue = workload_queue(runner)

    def runs():
        return query(
            "SELECT run_id, logical_date, data_interval_start, data_interval_end, "
            "run_after, state, run_type, triggered_by FROM dag_run ORDER BY run_id"
        )

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
    assert iteration(runner) == 1
    assert runs() == [expected_run]
    assert query(
        "SELECT next_dagrun, next_dagrun_create_after, exceeds_max_non_backfill FROM dag"
    ) == [("2026-01-02 00:00:00.000000", "2026-01-02 00:00:00.000000", 1)]
    assert query("SELECT run_id, task_id, state, try_number FROM task_instance") == [
        (expected_run[0], "work", "queued", 1)
    ]
    assert scheduled_preparations == [1]
    assert not queue
    assert query(
        "SELECT run_id FROM task_instance WHERE working_set IS TRUE "
        "AND state = 'queued' AND external_executor_id IS NOT NULL"
    ) == [(expected_run[0],)]

    for _ in range(2):
        assert iteration(runner) == 0
    assert runs() == [expected_run]
    release_task(runner)


def unsupported_schedule_creation(mode):
    _, runner = start_runner(mode)
    update_rows(
        DagModel,
        {"timetable_type": "CustomTimetable"},
        DagModel.dag_id == "schedule_probe",
    )
    prepared = []
    observe_scheduled(runner, prepared)
    queue = workload_queue(runner)
    assert iteration(runner) == 1
    assert prepared == [1]
    assert query(
        "SELECT run_id, state FROM dag_run WHERE dag_id = 'schedule_probe'"
    ) == [("scheduled__2026-01-01T00:00:00+00:00", "running")]
    assert query(
        "SELECT next_dagrun, next_dagrun_create_after FROM dag "
        "WHERE dag_id = 'schedule_probe'"
    ) == [("2026-01-02 00:00:00.000000", "2026-01-02 00:00:00.000000")]
    assert [workload.ti.run_id for workload in queue.values()] == [
        "scheduled__2026-01-01T00:00:00+00:00"
    ]


def schedule_executor_tasks(mode):
    _, runner = start_runner(mode)
    prepared = []
    observe_scheduled(runner, prepared)
    assert iteration(runner) == 1
    assert prepared == [1]
    assert query(
        "SELECT run_id, state FROM dag_run WHERE dag_id = 'schedule_probe'"
    ) == [("scheduled__2026-01-01T00:00:00+00:00", "running")]
    assert query("SELECT executor FROM task_instance") == [
        ("riir_airflow.executors.asgi_executor.AsgiExecutor",)
    ]


def disabled_bundle_versioning(mode):
    _, runner = start_runner(mode)
    update_rows(
        DagModel,
        {"bundle_version": "pinned"},
        DagModel.dag_id == "schedule_probe",
    )
    prepared = []
    if mode == "native":
        observe_scheduled(runner, prepared)
    assert iteration(runner) == 1
    assert query(
        "SELECT run_id, bundle_version FROM dag_run WHERE dag_id = 'schedule_probe'"
    ) == [("scheduled__2026-01-01T00:00:00+00:00", None)]
    assert prepared == ([1] if mode == "native" else [])


def capacity_blocked_dags(mode):
    _, runner = start_runner(mode)
    update_rows(
        DagModel,
        {
            "next_dagrun": datetime(2025, 1, 1, tzinfo=dt_timezone.utc),
            "next_dagrun_create_after": datetime(2025, 1, 1, tzinfo=dt_timezone.utc),
            "exceeds_max_non_backfill": True,
        },
        DagModel.dag_id == "blocked_probe",
    )
    prepared = []
    if mode == "native":
        hold_tasks(runner)
        observe_scheduled(runner, prepared)
    queue = workload_queue(runner)
    assert iteration(runner) == 1
    assert query("SELECT dag_id, run_id FROM dag_run ORDER BY dag_id") == [
        ("schedule_probe", "scheduled__2026-01-01T00:00:00+00:00")
    ]
    if mode == "native":
        assert prepared == [1]
        assert not queue
    else:
        assert prepared == []
        assert [workload.ti.dag_id for workload in queue.values()] == ["schedule_probe"]
    if mode == "native":
        release_task(runner)


def pending_asset_work(mode):
    _, runner = start_runner(mode)
    write(
        'INSERT INTO asset (id, name, uri, "group", extra, created_at, updated_at) '
        "VALUES (1, 'probe_asset', 's3://probe', '', '{}', "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )
    write(
        "INSERT INTO asset_event (id, asset_id, extra, timestamp) "
        "VALUES (1, 1, '{}', CURRENT_TIMESTAMP)"
    )
    write(
        "INSERT INTO asset_dag_run_queue (target_dag_id, asset_event_id, asset_id, created_at) "
        "VALUES ('schedule_probe', 1, 1, CURRENT_TIMESTAMP)"
    )
    phases = runner.native_driver.phases
    before_runs = phases.before_runs
    delegated = []

    def observe_before():
        delegated.append(True)
        return before_runs()

    phases.before_runs = observe_before
    prepared = []
    observe_scheduled(runner, prepared)
    assert iteration(runner) == 1
    assert delegated == [True]
    assert prepared == []
    assert (
        query("SELECT count(*) FROM dag_run WHERE dag_id = 'schedule_probe'")[0][0] >= 1
    )


def task_attempts(mode):
    selections = []
    job, runner = start_runner(mode)
    add_pool("weighted", 2)
    job_id = job.id
    now = minutes_ago(2)
    for run_id, logical_date in [
        ("newer", now),
        ("older", now - timedelta(minutes=1)),
    ]:
        trigger_run(run_id, logical_date=logical_date, run_after=now)
    with patch.object(runner, "_parallelism", 0):
        baseline_iteration(runner)
    update_rows(
        TaskInstance,
        {"state": "deferred"},
        TaskInstance.task_id == "holder",
        TaskInstance.run_id == "older",
    )
    update_rows(
        TaskInstance,
        {"state": "success"},
        TaskInstance.task_id == "holder",
        TaskInstance.run_id == "newer",
    )
    if mode == "fallback":
        update_rows(
            TaskInstance,
            {"executor": runner.executor.name.module_path},
            TaskInstance.task_id == "high",
            TaskInstance.run_id == "older",
        )

    queue = workload_queue(runner)
    driver = runner.native_driver

    def tasks():
        return {
            (run, task): (row_id, state, queued, owner, updated)
            for run, task, row_id, state, queued, owner, updated in query(
                "SELECT run_id, task_id, id, state, queued_dttm, queued_by_job_id, "
                "updated_at FROM task_instance WHERE working_set IS TRUE"
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

    def assert_queued(run, task):
        task_id, state, queued, owner, updated = tasks()[run, task]
        assert state == "queued"
        assert queued and queued <= updated
        assert owner == job_id
        if mode == "native":
            assert not queue
            assert query(
                "SELECT id, queued_by_job_id FROM task_instance WHERE working_set IS TRUE "
                "AND state = 'queued' AND external_executor_id IS NOT NULL"
            ) == [(task_id, job_id)]
        else:
            [(key, workload)] = queue.items()
            assert key == runner.executor.get_workload_key(workload)
            assert (
                workload.ti.run_id,
                workload.ti.task_id,
                workload.ti.id.hex,
            ) == (run, task, task_id)

    assert iteration(runner) == 1
    assert_queued("older", "high")
    assert tasks()["older", "weighted"][1] == "scheduled"
    assert tasks()["newer", "high"][1] == "scheduled"

    admitted = tasks()["older", "high"]
    update_rows(Pool, {"include_deferred": False}, Pool.pool == "weighted")
    assert iteration(runner) == 0
    assert len(queue) == (0 if mode == "native" else 1)
    assert tasks()["older", "high"] == admitted
    assert tasks()["newer", "weighted"][1] == "scheduled"

    if mode == "fallback":
        assert selections == []
    else:
        if mode == "native":
            update_rows(
                TaskInstance,
                {"state": "running"},
                TaskInstance.run_id == "older",
                TaskInstance.task_id == "high",
            )
            release_task(runner)
        else:
            queue.clear()
        assert iteration(runner) == 1
        assert_queued("newer", "weighted")
        assert tasks()["older", "weighted"][1] == "scheduled"
        assert tasks()["older", "high"][2:4] == admitted[2:4]

        if mode == "native":
            assert iteration(runner) == 0
            assert selections == [
                (older_high_id, True),
                (newer_weighted_id, True),
            ], selections
            release_task(runner)
        assert not queue


def transaction_boundaries(mode):
    job, runner = start_runner(mode)
    trigger_run("probe")
    hold_tasks(runner)
    phases = runner.native_driver.phases
    queue = workload_queue(runner)

    def state(run, task):
        return query(
            "SELECT state, queued_dttm, queued_by_job_id FROM task_instance "
            "WHERE run_id = ? AND task_id = ? AND working_set IS TRUE",
            (run, task),
        )[0]

    assert iteration(runner) == 1
    assert query("SELECT state FROM dag_run WHERE run_id = 'probe'") == [("running",)]
    assert state("probe", "work")[0] == "queued"

    update_rows(DagRun, {"state": "queued"}, DagRun.run_id == "probe")
    with database_lock():
        with (
            patch.object(phases, "before_runs", lambda: None),
            patch.object(phases, "delegate_runs", lambda *_: None),
        ):
            assert iteration(runner) == 0
        assert query("SELECT state FROM dag_run WHERE run_id = 'probe'") == [
            ("queued",)
        ]
        assert state("probe", "work")[0] == "queued"
    update_rows(DagRun, {"state": "running"}, DagRun.run_id == "probe")

    release_task(runner)
    prepare = phases.prepare_workload

    def unavailable_program(dto):
        return failed_program(prepare, dto)

    with patch.object(phases, "prepare_workload", unavailable_program):
        assert iteration(runner) == 0
    assert state("probe", "work") == ("scheduled", None, None)
    assert not queue
    assert query(
        "SELECT count(*) FROM task_instance WHERE working_set IS TRUE "
        "AND state = 'queued' AND external_executor_id IS NOT NULL"
    ) == [(0,)]

    update_rows(
        TaskInstance,
        {
            "state": "queued",
            "queued_dttm": timezone.utcnow(),
            "queued_by_job_id": job.id,
        },
        TaskInstance.run_id == "probe",
    )
    update_rows(Job, {"state": "failed"}, Job.id == job.id)
    original = state("probe", "work")
    new_job, replacement = start_runner(mode, state="running")
    hold_tasks(replacement)
    assert replacement.adopt_or_reset_orphaned_tasks() == 1
    assert state("probe", "work")[:2] == original[:2]
    assert state("probe", "work")[2] == new_job.id
    assert replacement.adopt_or_reset_orphaned_tasks() == 0
    assert iteration(runner) == 0
    assert state("probe", "work")[:2] == original[:2]
    release_task(replacement)

    update_rows(
        TaskInstance,
        {"queued_by_job_id": new_job.id},
        TaskInstance.run_id == "probe",
    )
    update_rows(
        Job,
        {"latest_heartbeat": timezone.utcnow() - timedelta(hours=1)},
        Job.id == new_job.id,
    )
    final_job, final = start_runner(mode, state="running")
    hold_tasks(final)
    assert final.adopt_or_reset_orphaned_tasks() == 1
    assert state("probe", "work")[2] == final_job.id
    assert query("SELECT state FROM job WHERE id = ?", (new_job.id,)) == [("failed",)]
    assert final.adopt_or_reset_orphaned_tasks() == 0
    release_task(final)

    fallback_job, fallback = start_runner(mode, state="running")
    fallback_queue = workload_queue(fallback)
    update_rows(DagModel, {"fail_fast": True}, DagModel.dag_id == "admission_probe")
    update_rows(
        TaskInstance,
        {"queued_by_job_id": final_job.id},
        TaskInstance.run_id == "probe",
    )
    update_rows(
        Job,
        {"latest_heartbeat": timezone.utcnow() - timedelta(hours=1)},
        Job.id == final_job.id,
    )
    assert fallback.adopt_or_reset_orphaned_tasks() == 1
    assert state("probe", "work")[0] is None
    assert not fallback_queue
    assert fallback.adopt_or_reset_orphaned_tasks() == 0
    assert iteration(fallback) == 1
    assert state("probe", "work")[2] == fallback_job.id
    assert len(fallback_queue) == 1


def fallback_before_write(mode):
    _, runner = start_runner(mode)
    now = minutes_ago(2)
    update_rows(DagRun, {"run_type": DagRunType.MANUAL})
    for run_id, logical_date in (
        ("mixed", now - timedelta(minutes=1)),
        ("plain", now),
    ):
        trigger_run(run_id, logical_date=logical_date, run_after=now)
    update_rows(
        DagRun,
        {"run_type": DagRunType.SCHEDULED},
        DagRun.run_id == "mixed",
    )
    phases = runner.native_driver.phases
    task_prepare = phases.prepare_workload
    native_task_claims = []

    def observe_task(dto):
        native_task_claims.append(dto["id"])
        return task_prepare(dto)

    phases.prepare_workload = observe_task

    def states():
        return dict(
            query("SELECT run_id, state FROM dag_run WHERE dag_id = 'admission_probe'")
        )

    assert iteration(runner) == 1
    current = states()
    assert list(current.values()).count("running") == 1
    assert list(current.values()).count("queued") == 1
    assert native_task_claims == []

    running_run = next(
        run_id for run_id, state in current.items() if state == "running"
    )
    queued_run = next(run_id for run_id, state in current.items() if state == "queued")
    update_rows(DagRun, {"state": "success"}, DagRun.run_id == queued_run)
    update_rows(
        DagRun,
        {"created_dag_version_id": None},
        DagRun.run_id == running_run,
    )
    update_rows(
        TaskInstance,
        {
            "state": "scheduled",
            "queued_dttm": None,
            "queued_by_job_id": None,
            "dag_version_id": None,
        },
        TaskInstance.run_id == running_run,
    )
    runner.executor.executor_queues.clear()
    runner.executor._task_coordinates.clear()
    assert iteration(runner) == 1
    assert native_task_claims == []
    assert query("SELECT state FROM dag_run WHERE run_id = ?", (running_run,)) == [
        ("running",)
    ]


def listeners_delegation(mode):
    notifications = []
    add_notification_listener(notifications)
    _, runner = start_runner(mode)
    trigger_run("probe")
    driver = runner.native_driver
    assert not driver.supported()
    assert "native" not in vars(driver)
    assert iteration(runner) == 1
    assert "native" not in vars(driver)
    assert notifications == [("probe", "running", "started")]
    assert query("SELECT state FROM dag_run WHERE run_id = 'probe'") == [("running",)]
    assert query("SELECT state FROM task_instance WHERE task_id = 'work'") == [
        ("queued",)
    ]


def runner_native_claims(mode):
    job, runner = start_runner(mode)
    trigger_run("probe")
    loaded_dags = []
    read_dag = runner.scheduler_dag_bag._read_dag

    def observe_dag(row):
        loaded_dags.append(row.dag_id)
        return read_dag(row)

    runner.scheduler_dag_bag._read_dag = observe_dag
    hold_tasks(runner)
    assert iteration(runner) == 1
    assert iteration(runner) == 0
    assert loaded_dags == ["admission_probe"], loaded_dags
    assert query("SELECT state FROM dag_run WHERE run_id = 'probe'") == [("running",)]
    assert query(
        "SELECT state, queued_by_job_id FROM task_instance WHERE task_id = 'work'"
    ) == [("queued", job.id)]
    release_task(runner)


def python_module_entry_point(mode):
    trigger_run("probe")
    run_command([sys.executable, "-m", "riir_airflow.scheduler"])
    assert query("SELECT state FROM dag_run WHERE run_id = 'probe'") == [("running",)]
    assert query(
        "SELECT count(*) FROM log WHERE event = 'queued' "
        "AND json_extract(extra, '$.executor_id') IS NOT NULL"
    ) == [(1,)]
    owner = query(
        "SELECT json_extract(extra, '$.job_id') FROM log WHERE event = 'queued' "
        "AND json_extract(extra, '$.executor_id') IS NOT NULL"
    )[0]
    assert query("SELECT state FROM job WHERE id = ?", owner) == [("success",)]
    assert query(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'riir_attempt'"
    ) == [(0,)]


def dependency_evaluation(mode):
    _, runner = start_runner(mode)
    trigger_run("probe")
    delegate_calls = []
    graph_calls = []
    if mode.endswith("native"):
        observe_delegate(runner, delegate_calls)
        observe_graphs(runner, graph_calls)

    def states():
        return dict(
            query("SELECT task_id, state FROM task_instance WHERE run_id = 'probe'")
        )

    def mark_upstream_success():
        write(
            "UPDATE task_instance SET state = 'success' "
            "WHERE run_id = 'probe' AND task_id = 'upstream'"
        )

    with patch.object(runner, "_parallelism", 0):
        iteration(runner)
        assert states() == {"upstream": "scheduled", "downstream": None}, states()
        mark_upstream_success()
        iteration(runner)
        assert states() == {"upstream": "success", "downstream": "scheduled"}, states()
        iteration(runner)
        assert states() == {"upstream": "success", "downstream": "scheduled"}, states()

    if mode.endswith("native"):
        run_pk = query("SELECT id FROM dag_run WHERE run_id = 'probe'")[0][0]
        assert len(graph_calls) == 3
        if mode.startswith("fallback"):
            assert len(delegate_calls) == 3
            assert graph_calls == [[None], [None], [None]]
            assert delegate_calls == [[run_pk], [run_pk], [run_pk]]
        else:
            assert [call[0]["id"] for call in graph_calls] == [run_pk] * 3
            assert delegate_calls == []


def future_logical_dates(mode):
    _, runner = start_runner(mode)
    delegate_calls = []
    graph_calls = []
    if mode.endswith("native"):
        observe_delegate(runner, delegate_calls)
        observe_graphs(runner, graph_calls)
    trigger_run(
        "probe",
        logical_date=timezone.utcnow() + timedelta(hours=1),
    )
    with patch.object(runner, "_parallelism", 0):
        iteration(runner)
        assert dict(
            query("SELECT task_id, state FROM task_instance WHERE run_id = 'probe'")
        ) == {"upstream": None, "downstream": None}

    if mode.endswith("native"):
        run_pk = query("SELECT id FROM dag_run WHERE run_id = 'probe'")[0][0]
        assert graph_calls == [[None]]
        assert delegate_calls == [[run_pk]]


def changed_unpinned_runs(mode):
    _, runner = start_runner(mode)
    delegate_calls = []
    graph_calls = []
    if mode.endswith("native"):
        observe_delegate(runner, delegate_calls)
        observe_graphs(runner, graph_calls)
    trigger_run("probe")
    with closing(sqlite3.connect("airflow.db")) as observer:
        original_version = observer.execute(
            "SELECT created_dag_version_id FROM dag_run WHERE run_id = 'probe'"
        ).fetchone()[0]
        with patch.object(runner, "_parallelism", 0):
            iteration(runner)
            assert dict(
                observer.execute(
                    "SELECT task_id, state FROM task_instance WHERE run_id = 'probe'"
                )
            ) == {"upstream": "scheduled", "downstream": None}

        replace_dag("dependency_probe.py", "version_change_probe.py")
        run_dag_processor(2)

        with patch.object(runner, "_parallelism", 0):
            iteration(runner)
        latest_version = observer.execute(
            "SELECT id FROM dag_version ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
        assert latest_version != original_version, (latest_version, original_version)
        assert dict(
            observer.execute(
                "SELECT task_id, state FROM task_instance WHERE run_id = 'probe'"
            )
        ) == {"upstream": "scheduled", "downstream": None, "extra": None}
        assert set(
            observer.execute(
                "SELECT DISTINCT dag_version_id FROM task_instance WHERE run_id = 'probe'"
            )
        ) == {(latest_version,)}

        if mode.endswith("native"):
            run_pk = observer.execute(
                "SELECT id FROM dag_run WHERE run_id = 'probe'"
            ).fetchone()[0]
            assert len(graph_calls) == 2
            assert graph_calls[0][0]["id"] == run_pk
            assert graph_calls[1] == [None]
            assert delegate_calls == [[run_pk]]


def dagrun_timeouts(mode):
    _, runner = start_runner(mode)
    delegate_calls = []
    graph_calls = []
    if mode.endswith("native"):
        observe_delegate(runner, delegate_calls)
        observe_graphs(runner, graph_calls)
    trigger_run("probe")
    with patch.object(runner, "_parallelism", 0):
        iteration(runner)
        assert dict(
            query("SELECT task_id, state FROM task_instance WHERE run_id = 'probe'")
        ) == {"upstream": "scheduled", "downstream": None}
        time.sleep(1.2)
        iteration(runner)
        assert query("SELECT state FROM dag_run WHERE run_id = 'probe'") == [
            ("failed",)
        ]
        assert dict(
            query("SELECT task_id, state FROM task_instance WHERE run_id = 'probe'")
        ) == {"upstream": "skipped", "downstream": "skipped"}

    if mode.endswith("native"):
        run_pk = query("SELECT id FROM dag_run WHERE run_id = 'probe'")[0][0]
        assert graph_calls == [[None], [None]]
        assert delegate_calls == [[run_pk], [run_pk]]


def retry_period(mode):
    _, runner = start_runner(mode)
    trigger_run("probe")
    delegate_calls = []
    graph_calls = []
    if mode.endswith("native"):
        observe_delegate(runner, delegate_calls)
        observe_graphs(runner, graph_calls)

    def states():
        return dict(
            query("SELECT task_id, state FROM task_instance WHERE run_id = 'probe'")
        )

    def set_retry(retry_at):
        write(
            "UPDATE task_instance SET state = 'up_for_retry', retry_at = ? "
            "WHERE run_id = 'probe' AND task_id = 'upstream'",
            (retry_at,),
        )

    future = (timezone.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.%f")
    past = (timezone.utcnow() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S.%f")
    with patch.object(runner, "_parallelism", 0):
        iteration(runner)
        assert states() == {"upstream": "scheduled", "downstream": None}, states()
        set_retry(future)
        iteration(runner)
        assert states() == {"upstream": "up_for_retry", "downstream": None}, states()
        set_retry(past)
        iteration(runner)
        assert states() == {"upstream": "scheduled", "downstream": None}, states()

    if mode.endswith("native"):
        run_pk = query("SELECT id FROM dag_run WHERE run_id = 'probe'")[0][0]
        assert len(graph_calls) == 3
        assert len(delegate_calls) == 1
        assert [call[0]["id"] if call[0] else None for call in graph_calls] == [
            run_pk,
            run_pk,
            None,
        ]
        assert delegate_calls == [[run_pk]]


def finalized_task_outcomes(mode):
    case = mode.split("-", 1)[0]
    native = mode.endswith("native")
    _, runner = start_runner(mode)
    trigger_run("probe")
    delegate_calls = []
    update_calls = []
    callback_calls = []
    if native:
        observe_delegate(runner, delegate_calls)
    observe_run_updates(update_calls)
    observe_callbacks(runner, callback_calls)

    run_pk = query("SELECT id FROM dag_run WHERE run_id = 'probe'")[0][0]

    def run_state():
        return query("SELECT state, end_date FROM dag_run WHERE id = ?", (run_pk,))[0]

    def mark(task, state, retry_at=None):
        write(
            "UPDATE task_instance SET state = ?, retry_at = ? "
            "WHERE run_id = 'probe' AND task_id = ?",
            (state, retry_at, task),
        )

    if case == "retry":
        mark(
            "upstream",
            "up_for_retry",
            (timezone.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.%f"),
        )
    else:
        mark("upstream", "success")
        if case != "unfinished":
            mark("downstream", "failed" if case == "failure" else "success")
    write(
        "UPDATE dag SET exceeds_max_non_backfill = 1 WHERE dag_id = 'admission_probe'"
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
        iteration(runner)
        current = run_state()
        assert current[0] == expected, (case, mode, current)
        assert (current[1] is not None) == (expected in ("success", "failed"))
        if first is None:
            first = current
        else:
            assert current == first
        assert query(
            "SELECT last_scheduling_decision IS NOT NULL FROM dag_run WHERE id = ?",
            (run_pk,),
        ) == [(1,)]

    exceeds = query(
        "SELECT exceeds_max_non_backfill FROM dag WHERE dag_id = 'admission_probe'"
    )[0][0]
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


def phase_handoff(mode):
    native = mode.endswith("native")
    _, runner = start_runner(mode)
    run_after = minutes_ago()
    trigger_run("native_run", run_after=run_after)
    trigger_run("delegated_run", run_after=run_after, dag_id="callback_probe")
    delegate_calls = []
    update_calls = []
    callback_calls = []
    if native:
        phases = runner.native_driver.phases
        assert not hasattr(phases, "between_phases")
        observe_delegate(runner, delegate_calls)
    observe_run_updates(update_calls)
    observe_callbacks(runner, callback_calls)

    for run_id in ("native_run", "delegated_run"):
        write("UPDATE task_instance SET state = 'success' WHERE run_id = ?", (run_id,))
    expected = {"native_run": "success", "delegated_run": "success"}
    with patch.object(runner, "_parallelism", 0):
        iteration(runner)
        iteration(runner)
    assert (
        dict(
            query(
                "SELECT run_id, state FROM dag_run WHERE run_id IN "
                "('native_run', 'delegated_run') ORDER BY run_id"
            )
        )
        == expected
    )
    assert callback_calls == [("callback_probe", "success")]
    if native:
        run_pk = query("SELECT id FROM dag_run WHERE run_id = 'delegated_run'")[0][0]
        assert delegate_calls == [[run_pk]]
        assert update_calls == ["delegated_run"]
    else:
        assert set(update_calls) == {"native_run", "delegated_run"}


PROBES = {
    "scheduler_admission": scheduler_admission,
    "scheduled_runs": scheduled_runs,
    "unsupported_schedule_creation": unsupported_schedule_creation,
    "schedule_executor_tasks": schedule_executor_tasks,
    "disabled_bundle_versioning": disabled_bundle_versioning,
    "capacity_blocked_dags": capacity_blocked_dags,
    "pending_asset_work": pending_asset_work,
    "task_attempts": task_attempts,
    "transaction_boundaries": transaction_boundaries,
    "fallback_before_write": fallback_before_write,
    "listeners_delegation": listeners_delegation,
    "runner_native_claims": runner_native_claims,
    "python_module_entry_point": python_module_entry_point,
    "dependency_evaluation": dependency_evaluation,
    "future_logical_dates": future_logical_dates,
    "changed_unpinned_runs": changed_unpinned_runs,
    "dagrun_timeouts": dagrun_timeouts,
    "retry_period": retry_period,
    "finalized_task_outcomes": finalized_task_outcomes,
    "phase_handoff": phase_handoff,
}


def main():
    probe, mode = sys.argv[1:3]
    PROBES[probe](mode)


if __name__ == "__main__":
    main()
