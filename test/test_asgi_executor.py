import asyncio

from airflow.executors.workloads import WorkloadType
from airflow.models.taskinstancekey import TaskInstanceKey
from airflow.utils.state import TaskInstanceState
from riir_airflow.executors import asgi_executor
from riir_airflow.executors.asgi_executor import AsgiExecutor

KEY = TaskInstanceKey("dag", "task", "run", 1)


def test_executor_accepts_only_task_workloads_with_one_slot():
    executor = AsgiExecutor()
    assert executor.parallelism == 1
    assert executor.supported_workload_types == {WorkloadType.EXECUTE_TASK}


class FakeProcess:
    def __init__(self, returncode: int):
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


def fake_spawn(returncode: int):
    async def spawn(*command):
        return FakeProcess(returncode)

    return spawn


def run_command(monkeypatch, command, spawn):
    monkeypatch.setattr(asgi_executor.asyncio, "create_subprocess_exec", spawn)
    executor = AsgiExecutor()
    executor.execute_async(KEY, command)
    asyncio.run(executor.synca())
    return executor


def test_synca_success(monkeypatch):
    executor = run_command(monkeypatch, ["true"], fake_spawn(0))
    assert executor.event_buffer[KEY][0] == TaskInstanceState.SUCCESS
    assert executor.commands_to_run == []


def test_synca_fail(monkeypatch):
    executor = run_command(monkeypatch, ["false"], fake_spawn(1))
    assert executor.event_buffer[KEY][0] == TaskInstanceState.FAILED


async def failing_spawn(*command):
    raise OSError("spawn failed")


def test_synca_spawn_error(monkeypatch):
    executor = run_command(monkeypatch, ["missing-command"], failing_spawn)
    assert executor.event_buffer[KEY][0] == TaskInstanceState.FAILED


def test_execute_async_simple_command(monkeypatch):
    executor = run_command(monkeypatch, ["echo", "hello"], fake_spawn(0))
    assert executor.event_buffer[KEY][0] == TaskInstanceState.SUCCESS
