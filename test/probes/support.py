import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from datetime import timedelta
from pathlib import Path

from airflow.api.common.trigger_dag import trigger_dag
from airflow.executors.workloads import WorkloadType
from airflow.jobs.job import Job
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
from airflow.listeners import hookimpl
from airflow.listeners.listener import get_listener_manager
from airflow.models.dagrun import DagRun
from airflow.models.pool import Pool
from airflow.sdk import timezone
from airflow.utils.session import create_session
from airflow.utils.types import DagRunTriggeredByType
from riir_airflow.scheduler import NativeSchedulerJobRunner
from sqlalchemy import update


def minutes_ago(minutes=1):
    return timezone.utcnow() - timedelta(minutes=minutes)


def trigger_run(run_id, *, dag_id="admission_probe", run_after=None, logical_date=None):
    options = {
        "run_id": run_id,
        "run_after": minutes_ago() if run_after is None else run_after,
        "triggered_by": DagRunTriggeredByType.CLI,
    }
    if logical_date is not None:
        options["logical_date"] = logical_date
    trigger_dag(dag_id, **options)


def start_runner(mode, job=None, **job_options):
    job = Job(**job_options) if job is None else job
    runner = NativeSchedulerJobRunner(job)
    with create_session() as session:
        session.add(job)
    return job, runner


def iteration(runner):
    with create_session() as session:
        return runner._do_scheduling(session)


def baseline_iteration(runner):
    with create_session() as session:
        SchedulerJobRunner._do_scheduling(runner, session)


def add_pool(name, slots, *, include_deferred=True):
    with create_session() as session:
        session.add(Pool(pool=name, slots=slots, include_deferred=include_deferred))
        session.flush()


def workload_queue(runner):
    return runner.executor.executor_queues[WorkloadType.EXECUTE_TASK]


def query(sql, parameters=()):
    with closing(sqlite3.connect("airflow.db")) as connection:
        return connection.execute(sql, parameters).fetchall()


def write(sql, parameters=()):
    with sqlite3.connect("airflow.db") as connection:
        connection.execute(sql, parameters)


def update_rows(model, values, *conditions):
    with create_session() as session:
        statement = update(model).values(**values)
        if conditions:
            statement = statement.where(*conditions)
        session.execute(statement)


def observe_notifications():
    notifications = []
    original = DagRun.notify_dagrun_state_changed

    def observe(run, msg):
        notifications.append((run.run_id, run.state, run.start_date, run.end_date, msg))
        return original(run, msg)

    DagRun.notify_dagrun_state_changed = observe
    return notifications


def notification_states(notifications):
    return [
        (run_id, state, end, msg) for run_id, state, start, end, msg in notifications
    ]


def observe_scheduled(runner, prepared):
    phases = runner.native_driver.phases
    prepare = phases.prepare_scheduled_runs

    def observe(dtos):
        prepared.append(len(dtos))
        return prepare(dtos)

    phases.prepare_scheduled_runs = observe


def observe_delegate(runner, delegate_calls):
    phases = runner.native_driver.phases
    delegate = phases.delegate_runs

    def observe(delegate_ids=None):
        delegate_calls.append(list(delegate_ids or []))
        return delegate(delegate_ids)

    phases.delegate_runs = observe


def observe_graphs(runner, graph_calls):
    phases = runner.native_driver.phases
    graphs = phases.prepare_dependency_graphs

    def observe(dtos):
        result = graphs(dtos)
        graph_calls.append(result)
        return result

    phases.prepare_dependency_graphs = observe


def observe_run_updates(update_calls):
    original = DagRun.update_state

    def observe(self, *args, **kwargs):
        update_calls.append(self.run_id)
        return original(self, *args, **kwargs)

    DagRun.update_state = observe


def observe_callbacks(runner, callback_calls):
    def observe(dag, callback=None):
        if callback:
            callback_calls.append((callback.dag_id, callback.msg))

    runner._send_dag_callbacks_to_processor = observe


def add_notification_listener(notifications):
    class ProbeListener:
        @hookimpl
        def on_dag_run_running(self, dag_run, msg):
            notifications.append((dag_run.run_id, dag_run.state, msg))

    get_listener_manager().add_listener(ProbeListener())


def holding_workload(prepare, dto):
    workload = prepare(dto)
    if workload is None:
        return None
    generation, _, payload = workload
    return generation, str(Path("holding_worker").resolve()), payload


def hold_tasks(runner):
    phases = runner.native_driver.phases
    prepare = phases.prepare_workload

    def prepare_holding(dto):
        return holding_workload(prepare, dto)

    phases.prepare_workload = prepare_holding


def release_task(runner):
    (generation,) = query(
        "SELECT external_executor_id FROM task_instance WHERE working_set IS TRUE "
        "AND state IN ('queued', 'running') AND external_executor_id IS NOT NULL"
    )[0]
    Path(f"release-{generation}").touch()
    runner.native_driver.native.wait_for_task()


def failed_program(prepare, dto):
    generation, _, payload = prepare(dto)
    return generation, "/missing/native-task-supervisor", payload


@contextmanager
def database_lock(name="airflow.db"):
    locker = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("database_locker.py")),
            name,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert locker.stdout.readline().strip() == "locked"
        yield
    finally:
        locker.stdin.write("\n")
        locker.stdin.flush()
        assert locker.wait(timeout=10) == 0


def run_command(command, *, timeout=90):
    result = subprocess.run(
        command,
        env=os.environ.copy(),
        cwd=os.getcwd(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def run_dag_processor(runs):
    run_command(
        [sys.executable, "-m", "airflow", "dag-processor", "--num-runs", str(runs)]
    )


def replace_dag(name, source, *, delay=1):
    time.sleep(delay)
    shutil.copyfile(
        Path(__file__).parent / "dags" / source,
        Path(os.environ["AIRFLOW_HOME"]) / "dags" / name,
    )
