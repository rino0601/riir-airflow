import asyncio
from datetime import datetime
import itertools
from pathlib import Path
from typing import TypedDict

from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
from airflow.api_internal.internal_api_call import InternalApiConfig
from airflow.executors.executor_loader import ExecutorLoader
from airflow.utils.state import JobState
from airflow.stats import Stats
from airflow.configuration import conf
from airflow.jobs.job import perform_heartbeat
from airflow.utils.event_scheduler import EventScheduler
from airflow.utils.session import create_session

import itertools
import multiprocessing
import os
import signal
import sys
import time
import warnings
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache, partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Collection, Iterable, Iterator

from sqlalchemy import and_, delete, func, not_, or_, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import lazyload, load_only, make_transient, selectinload
from sqlalchemy.sql import expression

from airflow import settings
from airflow.callbacks.callback_requests import (
    DagCallbackRequest,
    SlaCallbackRequest,
    TaskCallbackRequest,
)
from airflow.callbacks.pipe_callback_sink import PipeCallbackSink
from airflow.configuration import conf
from airflow.exceptions import RemovedInAirflow3Warning
from airflow.executors.executor_loader import ExecutorLoader
from airflow.jobs.base_job_runner import BaseJobRunner
from airflow.jobs.job import Job, perform_heartbeat
from airflow.models.dag import DAG, DagModel
from airflow.models.dagbag import DagBag
from airflow.models.dagrun import DagRun
from airflow.models.dataset import (
    DagScheduleDatasetReference,
    DatasetDagRunQueue,
    DatasetEvent,
    DatasetModel,
    TaskOutletDatasetReference,
)
from airflow.models.serialized_dag import SerializedDagModel
from airflow.models.taskinstance import SimpleTaskInstance, TaskInstance
from airflow.stats import Stats
from airflow.ti_deps.dependencies_states import EXECUTION_STATES
from airflow.timetables.simple import DatasetTriggeredTimetable
from airflow.utils import timezone
from airflow.utils.event_scheduler import EventScheduler
from airflow.utils.log.logging_mixin import LoggingMixin
from airflow.utils.log.task_context_logger import TaskContextLogger
from airflow.utils.retries import (
    MAX_DB_RETRIES,
    retry_db_transaction,
    run_with_db_retries,
)
from airflow.utils.session import NEW_SESSION, create_session, provide_session
from airflow.utils.sqlalchemy import (
    is_lock_not_available_error,
    prohibit_commit,
    tuple_in_condition,
    with_row_locks,
)
from airflow.utils.state import DagRunState, JobState, State, TaskInstanceState
from airflow.utils.types import DagRunType


class SchedulerStateDict(TypedDict):
    scheduler_on: bool
    tick: datetime | None
    foo: int | None


class AsyncSchedulerJobRunner(SchedulerJobRunner):
    scheduler_on: bool = True

    def _execute(self) -> int | None:
        return super()._execute()

    def _run_scheduler_loop(self) -> None:
        return super()._run_scheduler_loop()

    async def __aenter__(self):
        """._execute() 의 앞부분 재현"""
        ExecutorLoader.validate_database_executor_compatibility(self.job.executor)
        InternalApiConfig.force_database_direct_access()

        self.job.prepare_for_execution()  # session 유지 일단 포기해봄. (1)

        # 시그널 처리는 uvicorn 소관인것 아닐까?
        return asyncio.create_task(self.scheduler_loop())

    async def __aexit__(self, exc_type, exc, tb):
        """._execute() 의 뒷부분 재현"""
        if exc_type is None:
            # 정상 종료
            self.job.state = JobState.SUCCESS
        elif exc_type is SystemExit:
            # ^C 또는 SIGTERM 예외 처리
            self.job.state = JobState.SUCCESS
            return True  # 예외를 억제합니다. cached!
        else:
            # 다른 예외 처리
            self.job.state = JobState.FAILED
            return False  # 예외를 다시 발생시킵니다. like raise

        self.job.complete_execution()  # session 유지 일단 포기해봄. (2)
        self.scheduler_on = False
        print("DOWN!")

    def _setup_before_scheduler_loop(self):
        from airflow.dag_processing.manager import DagFileProcessorAgent

        self.log.info("Starting the scheduler")

        executor_class, _ = ExecutorLoader.import_default_executor_cls()

        # DAGs can be pickled for easier remote execution by some executors
        pickle_dags = self.do_pickle and executor_class.supports_pickling

        self.log.info(
            "Processing each file at most %s times", self.num_times_parse_dags
        )

        # When using sqlite, we do not use async_mode
        # so the scheduler job and DAG parser don't access the DB at the same time.
        async_mode = not self.using_sqlite

        processor_timeout_seconds: int = conf.getint(
            "core", "dag_file_processor_timeout"
        )
        processor_timeout = timedelta(seconds=processor_timeout_seconds)
        if not self._standalone_dag_processor and not self.processor_agent:
            self.processor_agent = DagFileProcessorAgent(
                dag_directory=Path(self.subdir),
                max_runs=self.num_times_parse_dags,
                processor_timeout=processor_timeout,
                dag_ids=[],
                pickle_dags=pickle_dags,
                async_mode=async_mode,
            )

        # try: # 일단 포기
        self.job.executor.job_id = self.job.id
        if self.processor_agent:
            self.log.debug("Using PipeCallbackSink as callback sink.")
            self.job.executor.callback_sink = PipeCallbackSink(
                get_sink_pipe=self.processor_agent.get_callbacks_pipe
            )
        else:
            from airflow.callbacks.database_callback_sink import (
                DatabaseCallbackSink,
            )

            self.log.debug("Using DatabaseCallbackSink as callback sink.")
            self.job.executor.callback_sink = DatabaseCallbackSink()

        self.job.executor.start()

        self.register_signals()

        if self.processor_agent:
            self.processor_agent.start()

        self.execute_start_time = timezone.utcnow()

    def _teardown_after_scheduler_loop(self):
        if self.processor_agent:
            # Stop any processors
            self.processor_agent.terminate()

            # Verify that all files were processed, and if so, deactivate DAGs that
            # haven't been touched by the scheduler as they likely have been
            # deleted.
            if self.processor_agent.all_files_processed:
                self.log.info(
                    "Deactivating DAGs that haven't been touched since %s",
                    self.execute_start_time.isoformat(),
                )
                DAG.deactivate_stale_dags(self.execute_start_time)

        settings.Session.remove()  # type: ignore
        # except Exception:
        #     self.log.exception("Exception when executing SchedulerJob._run_scheduler_loop")
        #     raise
        # finally:
        #     try:
        #         self.job.executor.end()
        #     except Exception:
        #         self.log.exception("Exception when executing Executor.end")
        #     if self.processor_agent:
        #         try:
        #             self.processor_agent.end()
        #         except Exception:
        #             self.log.exception("Exception when executing DagFileProcessorAgent.end")
        #     self.log.info("Exited execute loop")

    async def scheduler_loop(self):
        self._setup_before_scheduler_loop()
        # if not self.processor_agent and not self._standalone_dag_processor:
        #     raise ValueError("Processor agent is not started.")
        is_unit_test: bool = conf.getboolean("core", "unit_test_mode")
        timers = EventScheduler()

        # Check on start up, then every configured interval
        self.adopt_or_reset_orphaned_tasks()

        timers.call_regular_interval(
            conf.getfloat("scheduler", "orphaned_tasks_check_interval", fallback=300.0),
            self.adopt_or_reset_orphaned_tasks,
        )

        timers.call_regular_interval(
            conf.getfloat("scheduler", "trigger_timeout_check_interval", fallback=15.0),
            self.check_trigger_timeouts,
        )

        timers.call_regular_interval(
            conf.getfloat("scheduler", "pool_metrics_interval", fallback=5.0),
            self._emit_pool_metrics,
        )

        timers.call_regular_interval(
            conf.getfloat("scheduler", "zombie_detection_interval", fallback=10.0),
            self._find_zombies,
        )

        timers.call_regular_interval(60.0, self._update_dag_run_state_for_paused_dags)

        timers.call_regular_interval(
            conf.getfloat("scheduler", "task_queued_timeout_check_interval"),
            self._fail_tasks_stuck_in_queued,
        )

        timers.call_regular_interval(
            conf.getfloat("scheduler", "parsing_cleanup_interval"),
            self._orphan_unreferenced_datasets,
        )

        if self._standalone_dag_processor:
            timers.call_regular_interval(
                conf.getfloat("scheduler", "parsing_cleanup_interval"),
                self._cleanup_stale_dags,
            )

        for loop_count in itertools.count(start=1):
            self.log.info("Entering scheduler loop count %d", loop_count)
            with Stats.timer("scheduler.scheduler_loop_duration") as timer:
                if self.using_sqlite and self.processor_agent:
                    self.processor_agent.run_single_parsing_loop()
                    # For the sqlite case w/ 1 thread, wait until the processor
                    # is finished to avoid concurrent access to the DB.
                    self.log.debug(
                        "Waiting for processors to finish since we're using sqlite"
                    )
                    self.processor_agent.wait_until_finished()

                with create_session() as session:
                    num_queued_tis = self._do_scheduling(session)

                    await self.job.executor.aheartbeat()
                    session.expunge_all()
                    num_finished_events = self._process_executor_events(session=session)
                if self.processor_agent:
                    self.processor_agent.heartbeat()

                # Heartbeat the scheduler periodically
                perform_heartbeat(
                    job=self.job,
                    heartbeat_callback=self.heartbeat_callback,
                    only_if_necessary=True,
                )

                # Run any pending timed events
                next_event = timers.run(blocking=False)
                self.log.debug("Next timed event is in %f", next_event)

            self.log.debug("Ran scheduling loop in %.2f seconds", timer.duration)

            if not is_unit_test and not num_queued_tis and not num_finished_events:
                # If the scheduler is doing things, don't sleep. This means when there is work to do, the
                # scheduler will run "as quick as possible", but when it's stopped, it can sleep, dropping CPU
                # usage when "idle"
                await asyncio.sleep(
                    min(self._scheduler_idle_sleep_time, next_event or 0)
                )

            if loop_count >= self.num_runs > 0:
                self.log.info(
                    "Exiting scheduler loop as requested number of runs (%d - got to %d) has been reached",
                    self.num_runs,
                    loop_count,
                )
                break
            if self.processor_agent and self.processor_agent.done:
                self.log.info(
                    "Exiting scheduler loop as requested DAG parse count (%d) has been reached after %d"
                    " scheduler loops",
                    self.num_times_parse_dags,
                    loop_count,
                )
                break
        self._teardown_after_scheduler_loop()
