"""Airflow 3.4.0 scheduler integration for SQLite scheduler admission."""

from __future__ import annotations

import sqlite3
import sys
from functools import wraps
from typing import TYPE_CHECKING
from uuid import UUID

from airflow import __version__
from airflow.models.dag_version import DagVersion
from airflow.models.dagrun import DagRun
from airflow.models.taskinstance import TaskInstance
from airflow.sdk import timezone
from airflow.utils.state import TaskInstanceState
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import joinedload, make_transient, selectinload

from riir_airflow import _lowlevel

if TYPE_CHECKING:
    from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
    from sqlalchemy.orm import Session


def install_admission(executor_type: type) -> None:
    # Airflow has no executor hook for admitting DAG runs. Only wrap the
    # scheduler implementation this adapter was developed and tested against.
    if __version__ != "3.4.0":
        return

    # The configured executor is constructed by the runner. Avoid importing
    # scheduler machinery when the executor is used on its own.
    scheduler = sys.modules.get("airflow.jobs.scheduler_job_runner")
    runner_type = getattr(scheduler, "SchedulerJobRunner", None)
    if runner_type is None:
        return

    _install_task_admission(runner_type, executor_type)

    upstream = runner_type._start_queued_dagruns
    if getattr(upstream, "_riir_admission", False):
        return

    @wraps(upstream)
    def start(runner: SchedulerJobRunner, session: Session) -> None:
        if (
            isinstance(runner.executor, executor_type)
            and not runner._multi_team
            and session.get_bind(mapper=DagRun).dialect.name == "sqlite"
            and _admit_manual_runs(runner, session)
        ):
            return
        upstream(runner, session)

    start._riir_admission = True
    runner_type._start_queued_dagruns = start


def _admission_connection(session: Session, mapper: type) -> sqlite3.Connection:
    connection = session.connection(bind_arguments={"mapper": mapper})
    # SQLite's legacy driver mode does not start a transaction for a SELECT.
    # Keep the candidate snapshot and claims under the same write reservation.
    # If Airflow already started a transaction, use it as-is.
    driver = connection.connection.driver_connection
    if not driver.in_transaction:
        driver.execute("BEGIN IMMEDIATE")
    session.flush()
    return driver


def _admit_manual_runs(runner: SchedulerJobRunner, session: Session) -> bool:
    connection = _admission_connection(session, DagRun)
    candidates = _lowlevel.manual_dag_run_candidates(
        connection, DagRun.DEFAULT_DAGRUNS_TO_EXAMINE
    )
    if candidates is None:
        return False

    for run_id in candidates:
        run = session.get(DagRun, run_id)
        try:
            run.dag = runner.scheduler_dag_bag.get_dag_for_run(run, session=session)
        except DBAPIError:
            raise
        except Exception:
            runner.log.exception(
                "Failed to load Dag %s for queued run %s, skipping it",
                run.dag_id,
                run.run_id,
            )
            continue
        if not run.dag:
            runner.log.error("DAG '%s' not found in serialized_dag table", run.dag_id)
            continue
        now = timezone.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
        if _lowlevel.start_manual_dag_run(connection, run.id, now):
            session.refresh(run, ["_state", "start_date", "end_date", "updated_at"])
            run.notify_dagrun_state_changed(msg="started")
    return True


def _install_task_admission(runner_type: type, executor_type: type) -> None:
    upstream = runner_type._critical_section_enqueue_task_instances
    if getattr(upstream, "_riir_admission", False):
        return

    @wraps(upstream)
    def enqueue(runner: SchedulerJobRunner, session: Session) -> int:
        executor = runner.executor
        if (
            not runner._multi_team
            and type(executor) is executor_type
            and runner.executors == [executor]
            and executor.parallelism == 1
            and not executor.pre_assigns_external_executor_id
            and sqlite3.sqlite_version_info >= (3, 38)
            and session.get_bind(mapper=TaskInstance).dialect.name == "sqlite"
        ):
            count = _admit_task(runner, session)
            if count is not None:
                return count
        return upstream(runner, session)

    enqueue._riir_admission = True
    runner_type._critical_section_enqueue_task_instances = enqueue


def _admit_task(runner: SchedulerJobRunner, session: Session) -> int | None:
    executor = runner.executor
    scheduler_slots = runner._parallelism - executor.slots_occupied
    max_tis = min(runner.job.max_tis_per_query or scheduler_slots, scheduler_slots)
    if max_tis <= 0 or executor.slots_available <= 0:
        return 0

    connection = _admission_connection(session, TaskInstance)
    supported, task_id = _lowlevel.select_sqlite_task(connection, max_tis)
    if not supported:
        return None
    if task_id is None:
        return 0

    # Workload construction reads the bundle name and pinned version after detachment.
    ti = session.scalars(
        select(TaskInstance)
        .where(TaskInstance.id == UUID(hex=task_id))
        .options(
            selectinload(TaskInstance.dag_model),
            joinedload(TaskInstance.dag_run)
            .selectinload(DagRun.created_dag_version)
            .load_only(DagVersion.version_data),
        )
    ).one()
    if executor.has_task(ti):
        return 0

    now = timezone.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
    if not _lowlevel.queue_sqlite_task(connection, task_id, runner.job.id, now):
        return 0

    # Keep the same transient ORM snapshot as Airflow's bulk queue update.
    ti.emit_state_change_metric(TaskInstanceState.QUEUED)
    make_transient(ti)
    runner._enqueue_task_instances_with_queued_state([ti], executor, session=session)
    return 1
