"""Explicit native scheduler driver for the bounded Airflow 3.4 SQLite subset."""

from __future__ import annotations

import json
import pickle
import sqlite3
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from functools import cached_property, lru_cache, partial
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from airflow import __version__
from airflow.configuration import conf
from airflow.executors.base_executor import TaskInstanceUuid
from airflow.executors.workloads import WorkloadType
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner, prohibit_commit
from airflow.listeners.listener import get_listener_manager
from airflow.models.dag import DagModel
from airflow.models.dag_version import DagVersion
from airflow.models.dagrun import DagRun
from airflow.models.taskinstance import TaskInstance
from airflow.sdk import timezone
from airflow.utils.log.task_log_address import SENTINEL_REGION_ID
from airflow.utils.session import NEW_SESSION, create_session
from airflow.utils.state import TaskInstanceState
from sqlalchemy.engine import make_url
from sqlalchemy.orm import make_transient

from riir_airflow import _lowlevel
from riir_airflow.executors.asgi_executor import AsgiExecutor

if TYPE_CHECKING:
    from airflow.models.dagbag import DBDagBag


def _datetime(value: str | None):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)


def _json(value: str | None):
    return json.loads(value) if value else None


def _uuid(value: str | None) -> UUID | None:
    return UUID(hex=value) if value else None


def _database_path() -> Path:
    url = make_url(conf.get("database", "sql_alchemy_conn"))
    if (
        url.get_backend_name() != "sqlite"
        or not url.database
        or url.database == ":memory:"
        or str(url.query.get("uri", "")).lower() in {"true", "yes", "on", "1"}
    ):
        raise ValueError("native scheduler requires a file-backed SQLite database")
    return Path(url.database)


class NativeSchedulerDriver:
    """Run one Airflow scheduling iteration with Rust-owned SQLite claims."""

    def __init__(self, runner: SchedulerJobRunner):
        self.runner = runner
        self.phases = SchedulerPhases(runner)

    @cached_property
    def native(self):
        return _lowlevel.NativeScheduler(str(_database_path()))

    def supported(self) -> bool:
        if __version__ != "3.4.0":
            return False
        if self.runner._multi_team:
            return False
        try:
            _database_path()
        except ValueError:
            return False
        executor = self.runner.executor
        if (
            type(executor) is not AsgiExecutor
            or self.runner.executors != [executor]
            or executor.parallelism != 1
            or executor.pre_assigns_external_executor_id
        ):
            return False
        if sqlite3.sqlite_version_info < (3, 38):
            return False
        return not get_listener_manager().has_listeners

    def run_iteration(self) -> int:
        if not self.supported():
            return self.phases.delegate_iteration()
        executor = self.runner.executor
        slots = min(
            executor.slots_available,
            self.runner._parallelism - executor.slots_occupied,
        )
        max_tis = min(self.runner.job.max_tis_per_query or slots, slots)
        now = timezone.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
        return self.native.run_iteration(
            self.phases,
            DagRun.DEFAULT_DAGRUNS_TO_EXAMINE,
            max_tis,
            self.runner.job.id,
            now,
        )

    def recover_queued_workloads(self) -> int | None:
        if not self.supported():
            return None
        threshold = conf.getint("scheduler", "scheduler_health_check_threshold")
        stale_before = (timezone.utcnow() - timedelta(seconds=threshold)).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )
        return self.native.recover_queued_workloads(
            self.phases,
            self.runner.job.id,
            self.runner.executor.slots_available,
            stale_before,
        )


class SchedulerPhases:
    """Airflow phases and DTO callbacks called by the Rust transaction owner."""

    def __init__(self, runner: SchedulerJobRunner):
        self.runner = runner
        self._runs: dict[int, DagRun] = {}
        self._pending_ti: TaskInstance | None = None
        self._pending_before: set[object] | None = None

    @property
    def dag_bag(self) -> DBDagBag:
        return self.runner.scheduler_dag_bag

    def before_runs(self) -> None:
        with create_session() as session:
            with prohibit_commit(session) as guard:
                if self.runner._scheduler_use_job_schedule:
                    self.runner._create_dagruns_for_dags(guard, session)
                guard.commit()

    def prepare_dag_runs(self, dtos: list[dict]) -> bool:
        if any(
            dto["run_type"] != "manual" or dto["backfill_id"] is not None
            for dto in dtos
        ):
            return False
        if get_listener_manager().has_listeners:
            return False
        self._runs.clear()
        for dto in dtos:
            version = _uuid(dto["created_dag_version_id"])
            dag = self._load_dag(dto["dag_id"], version)
            if dag is None:
                return False
            run = DagRun(
                dag_id=dto["dag_id"],
                run_id=dto["run_id"],
                logical_date=_datetime(dto["logical_date"]),
                run_after=_datetime(dto["run_after"]),
                start_date=_datetime(dto["start_date"]),
                conf=_json(dto["conf"]),
                run_type=dto["run_type"],
                bundle_version=dto["bundle_version"],
            )
            run.id = dto["id"]
            run._state = dto["state"]
            run.start_date = _datetime(dto["start_date"])
            run.end_date = _datetime(dto["end_date"])
            run.updated_at = _datetime(dto["updated_at"])
            run.clear_number = dto["clear_number"] or 0
            run.log_template_id = dto["log_template_id"]
            run.context_carrier = _json(dto["context_carrier"])
            run.created_dag_version_id = version
            run.dag = dag
            make_transient(run)
            self._runs[dto["id"]] = run
        return True

    def dag_run_claimed(self, dto: dict) -> bool:
        run = self._runs.get(dto["id"])
        if run is None:
            return False
        run._state = dto["state"]
        run.start_date = _datetime(dto["start_date"])
        run.end_date = _datetime(dto["end_date"])
        run.updated_at = _datetime(dto["updated_at"])
        run.notify_dagrun_state_changed(msg="started")
        return True

    def between_phases(self) -> None:
        with create_session() as session:
            with prohibit_commit(session) as guard:
                dag_runs = list(
                    DagRun.get_running_dag_runs_to_examine(
                        session=session,
                        eagerly_load_dag_tags=self.runner._dag_tags_in_metrics,
                    )
                )
                self.runner._stamp_team_names(dag_runs, session)
                callback_tuples = self.runner._schedule_all_dag_runs(
                    guard, dag_runs, session
                )
                guard.commit()

            cached_get_dag = lru_cache()(
                partial(self.dag_bag.get_dag_for_run, session=session)
            )
            for dag_run, callback_to_run in callback_tuples:
                dag = cached_get_dag(dag_run)
                if dag:
                    self.runner._send_dag_callbacks_to_processor(dag, callback_to_run)
                    session.commit()

    def delegate_iteration(self) -> int:
        with create_session() as session:
            return SchedulerJobRunner._do_scheduling(self.runner, session)

    def delegate_after_run_creation(self) -> int:
        with create_session() as session:
            with prohibit_commit(session) as guard:
                self.runner._start_queued_dagruns(session)
                guard.commit()
        self.between_phases()
        if self.runner.executor.slots_available <= 0:
            return 0
        return self.delegate_task_phase()

    def delegate_task_phase(self) -> int:
        with create_session() as session:
            return self.runner._critical_section_enqueue_task_instances(session=session)

    def prepare_task(self, dto: dict) -> bool:
        self._pending_ti = None
        self._pending_before = None
        if (
            dto["fail_fast"]
            or dto["has_task_concurrency_limits"]
            or dto["next_method"]
            or _json(dto["next_kwargs"])
            or dto["trigger_id"]
        ):
            return False
        region = _uuid(dto["region_id"]) or SENTINEL_REGION_ID
        if region != SENTINEL_REGION_ID:
            return False
        version = _uuid(dto["dag_version_id"])
        run_version = _uuid(dto["created_dag_version_id"])
        dag = self._load_dag(dto["dag_id"], version or run_version)
        if dag is None:
            return False
        try:
            task = dag.get_task(dto["task_id"])
        except Exception:
            return False
        if task is None:
            return False

        ti = TaskInstance(
            task,
            version,
            run_id=dto["run_id"],
            state=dto["state"],
            map_index=dto["region_index"] if dto["region_index"] is not None else -1,
            region_id=region,
            region_index=dto["region_index"],
        )
        ti.id = _uuid(dto["id"])
        ti.try_number = dto["try_number"] or 1
        ti.hostname = dto["hostname"]
        ti.context_carrier = _json(dto["context_carrier"])
        ti.queue = dto["queue"] or "default"
        ti.pool_slots = dto["pool_slots"] or 1
        ti.priority_weight = dto["priority_weight"] or 0
        ti.external_executor_id = dto["external_executor_id"]
        ti.executor_config = (
            pickle.loads(dto["executor_config"]) if dto["executor_config"] else {}
        )
        ti.queued_dttm = _datetime(dto["queued_dttm"])
        ti.queued_by_job_id = dto["queued_by_job_id"]
        ti.updated_at = _datetime(dto["updated_at"])
        ti.dag_model = DagModel(
            dag_id=dto["dag_id"],
            bundle_name=dto["bundle_name"],
            relative_fileloc=dto["relative_fileloc"],
        )
        run = DagRun(
            dag_id=dto["dag_id"],
            run_id=dto["run_id"],
            logical_date=_datetime(dto["logical_date"]),
            run_after=_datetime(dto["run_after"]),
            bundle_version=dto["bundle_version"],
        )
        run.state = dto["run_state"]
        run.data_interval_start = _datetime(dto["data_interval_start"])
        run.data_interval_end = _datetime(dto["data_interval_end"])
        run.created_dag_version_id = run_version
        run.created_dag_version = DagVersion(
            id=run_version,
            bundle_version=dto["version_bundle_version"],
            version_data=_json(dto["version_data"]),
        )
        ti.dag_run = run
        make_transient(ti)
        make_transient(run)
        if self.runner.executor.has_task(ti):
            return False
        self._pending_ti = ti
        return True

    def task_claimed(self, dto: dict) -> bool:
        ti = self._pending_ti
        if ti is None:
            return False
        queue = self.runner.executor.executor_queues[WorkloadType.EXECUTE_TASK]
        self._pending_before = set(queue)
        ti.state = dto["state"]
        ti.queued_dttm = _datetime(dto["queued_dttm"])
        ti.queued_by_job_id = dto["queued_by_job_id"]
        ti.updated_at = _datetime(dto["updated_at"])
        ti.emit_state_change_metric(TaskInstanceState.QUEUED)
        self.runner._enqueue_task_instances_with_queued_state(
            [ti], self.runner.executor, session=None
        )
        return len(set(queue) - self._pending_before) == 1

    def discard_pending_workload(self) -> None:
        if self._pending_before is not None and self._pending_ti is not None:
            executor = self.runner.executor
            queue = executor.executor_queues[WorkloadType.EXECUTE_TASK]
            for key in set(queue) - self._pending_before:
                queue.pop(key)
            executor._task_coordinates.pop(TaskInstanceUuid(self._pending_ti.id), None)
        self._pending_before = None
        self._pending_ti = None

    def _load_dag(self, dag_id: str, version: UUID | None):
        with create_session(scoped=False) as session:
            if version is None:
                latest = DagVersion.get_latest_version(dag_id, session=session)
                if latest is None:
                    return None
                version = latest.id
            return self.dag_bag.get_dag(version, session=session)


class NativeSchedulerJobRunner(SchedulerJobRunner):
    """Airflow scheduler loop whose scheduling calls are Rust-owned."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.native_driver = NativeSchedulerDriver(self)

    def _do_scheduling(self, session) -> int:
        try:
            return self.native_driver.run_iteration()
        finally:
            session.expunge_all()

    def adopt_or_reset_orphaned_tasks(self, *, session=NEW_SESSION) -> int:
        recovered = self.native_driver.recover_queued_workloads()
        if recovered is not None:
            return recovered
        if session is NEW_SESSION:
            return super().adopt_or_reset_orphaned_tasks()
        return super().adopt_or_reset_orphaned_tasks(session=session)


def main() -> None:
    from airflow.jobs.job import Job, run_job
    from airflow.utils.process_utils import set_component_mp_start_method

    set_component_mp_start_method("scheduler")
    runner = NativeSchedulerJobRunner(job=Job())
    run_job(job=runner.job, execute_callable=runner._execute)


if __name__ == "__main__":
    main()
