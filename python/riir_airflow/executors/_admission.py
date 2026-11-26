"""Airflow 3.4.0 scheduler integration for manual SQLite DAG run admission."""

from __future__ import annotations

import sys
from functools import wraps
from typing import TYPE_CHECKING

from airflow import __version__
from airflow.models.dagrun import DagRun
from airflow.sdk import timezone
from sqlalchemy.exc import DBAPIError

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


def _admit_manual_runs(runner: SchedulerJobRunner, session: Session) -> bool:
    connection = session.connection(bind_arguments={"mapper": DagRun})
    # SQLite's legacy driver mode does not start a transaction for a SELECT.
    # Keep the candidate snapshot and claims under the same write reservation.
    # If Airflow already started a transaction, use it as-is.
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    session.flush()
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
