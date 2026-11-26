"""Explicit native scheduler driver for the bounded Airflow 3.4 SQLite subset."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from functools import cached_property, lru_cache, partial
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from airflow import __version__
from airflow.configuration import conf
from airflow.executors.base_executor import get_execution_api_server_url
from airflow.executors.workloads import BundleInfo, ExecuteTask, TaskInstanceDTO
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner, prohibit_commit
from airflow.listeners.listener import get_listener_manager
from airflow.models.dag import DagModel
from airflow.models.dag_version import DagVersion
from airflow.models.dagrun import DagRun
from airflow.models.taskinstance import SENTINEL_REGION_ID
from airflow.models.taskinstance import TaskInstance as TI
from airflow.sdk import timezone
from airflow.sdk.execution_time.coordinator import get_coordinator_manager
from airflow.settings import task_instance_mutation_hook
from airflow.utils.log.task_log_address import TaskLogContext, render_task_log_filename
from airflow.utils.session import NEW_SESSION, create_session
from airflow.utils.state import DagRunState
from airflow.utils.types import DagRunTriggeredByType, DagRunType
from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import make_transient
from uuid6 import uuid7

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


def _sql_datetime(value) -> str | None:
    if value is None:
        return None
    parsed = _datetime(value) if isinstance(value, str) else value
    return parsed.astimezone(dt_timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


_SCHEDULE_TYPES = frozenset(
    {
        "CronDataIntervalTimetable",
        "CronTriggerTimetable",
        "DeltaDataIntervalTimetable",
        "DeltaTriggerTimetable",
    }
)
_TIMETABLE_MODULES = frozenset(
    {"airflow.timetables.interval", "airflow.timetables.trigger"}
)
_RUN_CONFIG = TI.__table__.c.executor_config.type.bind_processor(dialect())
_RUN_CARRIER = DagRun.__table__.c.context_carrier.type.bind_processor(dialect())
_TI_CARRIER = TI.__table__.c.context_carrier.type.bind_processor(dialect())


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
        if __version__ != "3.4.0" or sys.platform != "linux":
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
        return (
            not get_listener_manager().has_listeners
            and not get_coordinator_manager().has_coordinators()
            and not conf.getboolean("state_store", "clear_on_success")
        )

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
            DagModel.NUM_DAGS_PER_DAGRUN_QUERY,
            max_tis,
            self.runner.job.id,
            now,
            self.runner._scheduler_use_job_schedule,
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

    @property
    def dag_bag(self) -> DBDagBag:
        return self.runner.scheduler_dag_bag

    def before_runs(self) -> None:
        with create_session() as session:
            with prohibit_commit(session) as guard:
                if self.runner._scheduler_use_job_schedule:
                    self.runner._create_dagruns_for_dags(guard, session)
                guard.commit()

    def prepare_scheduled_runs(self, dtos: list[dict]) -> list[dict] | None:
        if not dtos or get_listener_manager().has_listeners:
            return [] if not dtos else None
        if get_coordinator_manager().has_coordinators():
            return None
        if not getattr(task_instance_mutation_hook, "is_noop", False):
            return None
        prepared: list[dict] = []
        for candidate in dtos:
            allowed = _json(candidate["allowed_run_types"])
            if (
                candidate["timetable_type"] not in _SCHEDULE_TYPES
                or candidate["timetable_partitioned"]
                or candidate["timetable_asset_gated"]
                or candidate["asset_expression"] is not None
                or candidate["deadline"] is not None
                or candidate["has_task_concurrency_limits"]
                or (allowed is not None and "scheduled" not in allowed)
            ):
                return None
            version = _uuid(candidate["dag_version_id"])
            dag = self._load_dag(candidate["dag_id"], version)
            if dag is None:
                return None
            timetable = dag.timetable
            if (
                not timetable.periodic
                or timetable.partitioned
                or timetable.asset_gated
                or timetable.asset_triggered
                or type(timetable).__module__ not in _TIMETABLE_MODULES
                or dag.has_dynamic_nodes
                or dag.deadline
            ):
                return None
            model = DagModel(
                dag_id=candidate["dag_id"],
                next_dagrun=_datetime(candidate["next_dagrun"]),
                next_dagrun_data_interval_start=_datetime(
                    candidate["next_dagrun_data_interval_start"]
                ),
                next_dagrun_data_interval_end=_datetime(
                    candidate["next_dagrun_data_interval_end"]
                ),
                next_dagrun_create_after=_datetime(
                    candidate["next_dagrun_create_after"]
                ),
            )
            next_info = timetable.next_run_info_from_dag_model(dag_model=model)
            if (
                next_info is None
                or next_info.partition_key is not None
                or next_info.partition_date is not None
            ):
                return None
            existing = candidate.get("existing_run")
            if existing is not None:
                reference = DagRun(
                    dag_id=existing["dag_id"],
                    run_id=existing["run_id"],
                    logical_date=_datetime(existing["logical_date"]),
                    data_interval=(
                        (
                            _datetime(existing["data_interval_start"]),
                            _datetime(existing["data_interval_end"]),
                        )
                        if existing["data_interval_start"]
                        and existing["data_interval_end"]
                        else None
                    ),
                    run_after=_datetime(existing["run_after"]),
                    run_type=existing["run_type"],
                )
            else:
                run_id = timetable.generate_run_id(
                    run_type=DagRunType.SCHEDULED,
                    run_after=next_info.run_after,
                    data_interval=next_info.data_interval,
                    partition_key=None,
                )
                reference = DagRun(
                    dag_id=candidate["dag_id"],
                    run_id=run_id,
                    logical_date=next_info.logical_date,
                    data_interval=next_info.data_interval,
                    run_after=next_info.run_after,
                    conf={},
                    run_type=DagRunType.SCHEDULED,
                    triggered_by=DagRunTriggeredByType.TIMETABLE,
                    state=DagRunState.QUEUED,
                    creating_job_id=candidate["job_id"],
                    bundle_version=candidate["bundle_version"],
                )
                reference.created_dag_version_id = version
                reference.log_template_id = candidate["log_template_id"]
            model.calculate_dagrun_date_fields(dag=dag, reference_run=reference)
            tasks: list[dict] = []
            if existing is None:
                for task in dag.task_dict.values():
                    if task.get_needs_expansion():
                        return None
                    if (
                        task.start_date is not None
                        and reference.logical_date is not None
                        and task.start_date > reference.logical_date
                    ):
                        continue
                    if (
                        task.end_date is not None
                        and reference.logical_date is not None
                        and reference.logical_date > task.end_date
                    ):
                        continue
                    mapping = TI.insert_mapping(
                        reference.run_id,
                        task,
                        region_index=-1,
                        region_id=SENTINEL_REGION_ID,
                        dag_version_id=version,
                        dag_run=reference,
                    )
                    if mapping["executor_config"]:
                        return None
                    tasks.append(
                        {
                            "id": uuid7().hex,
                            "task_id": mapping["task_id"],
                            "dag_id": mapping["dag_id"],
                            "run_id": reference.run_id,
                            "region_index": mapping["region_index"],
                            "region_id": mapping["region_id"].hex,
                            "try_number": mapping["try_number"],
                            "max_tries": mapping["max_tries"],
                            "hostname": mapping["hostname"],
                            "unixname": mapping["unixname"],
                            "queue": mapping["queue"],
                            "pool": mapping["pool"],
                            "pool_slots": mapping["pool_slots"],
                            "priority_weight": mapping["priority_weight"],
                            "operator": mapping["operator"],
                            "custom_operator_name": mapping["custom_operator_name"],
                            "is_stub": bool(mapping["is_stub"]),
                            "executor_config": _RUN_CONFIG(mapping["executor_config"]),
                            "task_display_name": mapping[
                                "_task_display_property_value"
                            ],
                            "dag_version_id": version.hex,
                            "context_carrier": json.dumps(
                                _TI_CARRIER(mapping["context_carrier"])
                            ),
                        }
                    )
            run_dto = None
            if existing is None:
                run_dto = {
                    "dag_id": reference.dag_id,
                    "run_id": reference.run_id,
                    "logical_date": _sql_datetime(reference.logical_date),
                    "data_interval_start": _sql_datetime(reference.data_interval_start),
                    "data_interval_end": _sql_datetime(reference.data_interval_end),
                    "run_after": _sql_datetime(reference.run_after),
                    "queued_at": _sql_datetime(reference.queued_at),
                    "state": DagRunState.QUEUED.value,
                    "run_type": DagRunType.SCHEDULED.value,
                    "triggered_by": DagRunTriggeredByType.TIMETABLE.name,
                    "bundle_version": candidate["bundle_version"],
                    "conf": json.dumps(reference.conf or {}),
                    "creating_job_id": candidate["job_id"],
                    "log_template_id": reference.log_template_id,
                    "created_at": _sql_datetime(reference.queued_at),
                    "updated_at": _sql_datetime(reference.queued_at),
                    "context_carrier": json.dumps(
                        _RUN_CARRIER(reference.context_carrier)
                    ),
                    "created_dag_version_id": version.hex,
                }
            prepared.append(
                {
                    "dag_id": candidate["dag_id"],
                    "run": run_dto,
                    "tasks": tasks,
                    "next": {
                        "next_dagrun": _sql_datetime(model.next_dagrun),
                        "next_dagrun_data_interval_start": _sql_datetime(
                            model.next_dagrun_data_interval_start
                        ),
                        "next_dagrun_data_interval_end": _sql_datetime(
                            model.next_dagrun_data_interval_end
                        ),
                        "next_dagrun_create_after": _sql_datetime(
                            model.next_dagrun_create_after
                        ),
                    },
                }
            )
        return prepared

    def prepare_dag_runs(self, dtos: list[dict]) -> bool:
        if any(
            dto["run_type"] not in {"manual", "scheduled"}
            or dto["backfill_id"] is not None
            or (
                dto["run_type"] == "scheduled"
                and (
                    dto["triggered_by"] != DagRunTriggeredByType.TIMETABLE.name
                    or dto["timetable_type"] not in _SCHEDULE_TYPES
                    or dto["timetable_partitioned"]
                    or dto["timetable_asset_gated"]
                    or dto["asset_expression"] is not None
                    or dto["deadline"] is not None
                )
            )
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
            run.triggered_by = dto["triggered_by"]
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

    def delegate_after_run_creation(self, slot_available: bool) -> int:
        self.before_runs()
        with create_session() as session:
            with prohibit_commit(session) as guard:
                self.runner._start_queued_dagruns(session)
                guard.commit()
        self.between_phases()
        if not slot_available or self.runner.executor.slots_available <= 0:
            return 0
        return self.delegate_task_phase()

    def delegate_task_phase(self) -> int:
        with create_session() as session:
            return self.runner._critical_section_enqueue_task_instances(session=session)

    def prepare_workload(self, dto: dict) -> tuple[str, str, str] | None:
        version = _uuid(dto["dag_version_id"])
        dag = self._load_dag(dto["dag_id"], version)
        if (
            dag is None
            or dto["task_id"] not in dag.task_dict
            or not dto["relative_fileloc"]
        ):
            return None
        task = dag.get_task(dto["task_id"])
        if task.outlets:
            return None
        ti = TaskInstanceDTO(
            id=dto["id"],
            task_id=dto["task_id"],
            dag_id=dto["dag_id"],
            run_id=dto["run_id"],
            dag_version_id=version,
            try_number=dto["try_number"],
            pool_slots=dto["pool_slots"],
            priority_weight=dto["priority_weight"] or 0,
            context_carrier=_json(dto["context_carrier"]),
            queue=dto["queue"],
        )
        bundle_version = dto["bundle_version"]
        bundle = BundleInfo(
            name=dto["bundle_name"],
            version=bundle_version,
            version_data=(
                _json(dto["version_data"])
                if bundle_version and bundle_version == dto["version_bundle_version"]
                else None
            ),
        )
        context = TaskLogContext(
            filename_template=conf.get("logging", "log_filename_template"),
            logical_date=_datetime(dto["logical_date"] or dto["run_after"]).isoformat(),
            data_interval_start=(
                _datetime(dto["data_interval_start"]).isoformat()
                if dto["data_interval_start"]
                else ""
            ),
            data_interval_end=(
                _datetime(dto["data_interval_end"]).isoformat()
                if dto["data_interval_end"]
                else ""
            ),
        )
        server = get_execution_api_server_url(self.runner.executor.conf)
        workload = ExecuteTask(
            ti=ti,
            dag_rel_path=Path(dto["relative_fileloc"]),
            token=ExecuteTask.generate_token(
                str(ti.id), self.runner.executor.jwt_generator
            ),
            log_path=render_task_log_filename(ti, ti.try_number, context=context),
            bundle_info=bundle,
        )
        return (
            uuid7().hex,
            sys.executable,
            json.dumps(
                {"workload": workload.model_dump(mode="json"), "server": server}
            ),
        )

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

    def _execute(self):
        try:
            return super()._execute()
        finally:
            if "native" in vars(self.native_driver):
                self.native_driver.native.wait_for_task()

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

    def _get_tis_stuck_in_queued(self, session):
        active = self._native_active_task()
        return (
            ti for ti in super()._get_tis_stuck_in_queued(session) if ti.id != active
        )

    def _find_task_instances_without_heartbeats(self, *, session):
        active = self._native_active_task()
        return [
            ti
            for ti in super()._find_task_instances_without_heartbeats(session=session)
            if ti.id != active
        ]

    def _native_active_task(self):
        if self.native_driver.supported():
            return _uuid(self.native_driver.native.active_task())
        return None


def main() -> None:
    from airflow.jobs.job import Job, run_job
    from airflow.utils.process_utils import set_component_mp_start_method

    set_component_mp_start_method("scheduler")
    runner = NativeSchedulerJobRunner(job=Job())
    run_job(job=runner.job, execute_callable=runner._execute)


if __name__ == "__main__":
    main()
