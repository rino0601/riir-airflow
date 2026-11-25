"""Airflow 3.4.0 scheduler integration for manual SQLite DAG run admission."""

from __future__ import annotations

import sys
from functools import wraps
from typing import TYPE_CHECKING

from airflow import __version__
from airflow.models.dagrun import DagRun
from airflow.sdk import timezone
from airflow.utils.state import DagRunState
from airflow.utils.types import DagRunType
from sqlalchemy import func, select
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
    # Reuse Airflow's bounded, ordered SQLite candidate query within its
    # scheduler transaction. Fall back before making any changes to a batch.
    runs = list(DagRun.get_queued_dag_runs_to_set_running(session))
    if any(
        run.backfill_id is not None or run.run_type != DagRunType.MANUAL for run in runs
    ):
        return False
    if not runs:
        return True

    running = session.execute(
        select(DagRun.dag_id, func.count(DagRun.id))
        .where(
            DagRun.state == DagRunState.RUNNING,
            DagRun.backfill_id.is_(None),
            DagRun.dag_id.in_({run.dag_id for run in runs}),
        )
        .group_by(DagRun.dag_id)
    )
    budget = _lowlevel.DagRunAdmission(dict(running.tuples().all()))

    for run in runs:
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
        if budget.admit(run.dag_id, run.max_active_runs):
            run.state = DagRunState.RUNNING
            run.start_date = timezone.utcnow()
            run.notify_dagrun_state_changed(msg="started")
    return True
