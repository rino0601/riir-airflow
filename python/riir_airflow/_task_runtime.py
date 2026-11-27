"""Private SDK supervisor entry and outcome transport for the native executor."""

import json
import os
import sys
from datetime import UTC, datetime, timedelta

import httpx2
from airflow.api_fastapi.execution_api.datamodels.taskinstance import (
    TIEnterRunningPayload,
    TIStateUpdate,
)
from airflow.executors.workloads import ExecuteTask
from airflow.sdk.api.client import Client, raise_on_4xx_5xx
from airflow.sdk.execution_time.supervisor import supervise_task
from pydantic import TypeAdapter
from uuid6 import uuid7

from riir_airflow import _lowlevel

_OUTCOME = TypeAdapter(TIStateUpdate)


def _sql_datetime(value):
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


class OutcomeClient(Client):
    def __init__(self, database, generation, workload, server):
        super().__init__(base_url=server, token=workload.token)
        self.database = database
        self.generation = generation
        self.attempt = workload.ti.id
        self.state_path = f"task-instances/{self.attempt}/state"
        self.run_path = f"task-instances/{self.attempt}/run"

    def _request_without_retry(self, method, url, *args, **kwargs):
        if method == "PATCH" and url == self.run_path:
            startup = TIEnterRunningPayload.model_validate_json(kwargs["content"])
            status = self._native_status(
                method,
                url,
                _lowlevel.register_task_group,
                self.database,
                self.generation,
                startup.pid,
            )
            if not status:
                return self._response(method, url, 409)
        if method == "PATCH" and url == self.state_path:
            status = self._outcome(method, url, kwargs["content"])
            if status:
                return self._response(method, url, status)
        return super()._request_without_retry(method, url, *args, **kwargs)

    def _outcome(self, method, url, content):
        report = _OUTCOME.validate_json(content)
        payload = report.model_dump(mode="json", exclude_unset=True)
        end = getattr(report, "end_date", None)
        delay = getattr(report, "retry_delay_seconds", None)
        retry_at = None
        if end is not None:
            payload["end_date"] = _sql_datetime(end)
            if delay is not None:
                retry_at = _sql_datetime(end + timedelta(seconds=delay))
        content = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return self._native_status(
            method,
            url,
            _lowlevel.report_outcome,
            self.database,
            self.generation,
            self.attempt.hex,
            content,
            uuid7().hex,
            retry_at,
            _sql_datetime(datetime.now(UTC)),
        )

    def _native_status(self, method, url, function, *args):
        try:
            return function(*args)
        except RuntimeError as error:
            request = self.build_request(method, url)
            response = httpx2.Response(503, request=request)
            raise httpx2.HTTPStatusError(
                "Native task metadata transaction could not be committed",
                request=request,
                response=response,
            ) from error

    def _response(self, method, url, status):
        response = httpx2.Response(
            status,
            request=self.build_request(method, url),
            json={"detail": {"reason": "invalid_state"}} if status == 409 else None,
        )
        raise_on_4xx_5xx(response)
        return response


def run():
    database, generation, lease_fd = sys.argv[1:]
    os.set_inheritable(int(lease_fd), False)
    message = json.load(sys.stdin)
    workload = ExecuteTask.model_validate(message["workload"])
    if not _lowlevel.register_attempt(database, generation):
        raise SystemExit(1)
    with OutcomeClient(database, generation, workload, message["server"]) as client:
        raise SystemExit(
            supervise_task(
                ti=workload.ti,
                bundle_info=workload.bundle_info,
                dag_rel_path=workload.dag_rel_path,
                token=workload.token,
                log_path=workload.log_path,
                client=client,
                subprocess_logs_to_stdout=True,
            )
        )
