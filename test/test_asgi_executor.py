import asyncio
import os
import signal
import subprocess
import sys

import pytest
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


@pytest.mark.parametrize(
    ("command", "state"),
    [
        ([sys.executable, "-c", "raise SystemExit(0)"], TaskInstanceState.SUCCESS),
        ([sys.executable, "-c", "raise SystemExit(17)"], TaskInstanceState.FAILED),
        (None, TaskInstanceState.FAILED),
    ],
)
def test_synca_real_commands(command, state, tmp_path):
    executor = AsgiExecutor()
    executor.execute_async(KEY, command or [str(tmp_path / "missing")])
    asyncio.run(executor.synca())
    assert executor.event_buffer[KEY][0] == state
    assert executor.commands_to_run == []


def test_synca_preserves_child_process_context(monkeypatch, tmp_path, capfd):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RIIR_EXECUTOR_PROBE", "inherited")
    executor = AsgiExecutor()
    with open(os.devnull) as descriptor:
        fd = descriptor.fileno()
        os.set_inheritable(fd, True)
        executor.execute_async(
            KEY,
            [
                sys.executable,
                "-c",
                """
import os, sys

try:
    os.fstat(int(sys.argv[1]))
except OSError as error:
    import errno
    assert error.errno == errno.EBADF, error
else:
    raise AssertionError("unexpected inherited descriptor")
print(os.getcwd())
print(os.environ["RIIR_EXECUTOR_PROBE"], file=sys.stderr)
print(sys.argv[2])
""",
                str(fd),
                "one argument with spaces",
            ],
        )
        asyncio.run(executor.synca())

    assert executor.event_buffer[KEY][0] == TaskInstanceState.SUCCESS
    stdout, stderr = capfd.readouterr()
    assert str(tmp_path.resolve()) in stdout.splitlines()
    assert "one argument with spaces" in stdout.splitlines()
    assert stderr.strip() == "inherited"


@pytest.mark.parametrize("cancel_during_startup", [True, False])
def test_synca_cancellation_preserves_child_lifetime(
    monkeypatch, cancel_during_startup
):
    real_spawn = asyncio.create_subprocess_exec
    real_popen = subprocess.Popen
    children = []
    processes = []

    def track_child(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    async def spawn(*command):
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        if cancel_during_startup:
            loop.call_soon(task.cancel)
        process = await real_spawn(*command)
        processes.append(process)
        if not cancel_during_startup:
            loop.call_soon(task.cancel)
        return process

    monkeypatch.setattr(subprocess, "Popen", track_child)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def run():
        executor = AsgiExecutor()
        executor.execute_async(
            KEY, [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        queue = executor.commands_to_run
        try:
            with pytest.raises(asyncio.CancelledError):
                await executor.synca()
            assert executor.commands_to_run is queue
            assert KEY not in executor.event_buffer
            (child,) = children
            if cancel_during_startup:
                assert child.returncode == -signal.SIGKILL
            else:
                assert child.returncode is None
        finally:
            for child in children:
                if child.returncode is None:
                    child.kill()
            for process in processes:
                await process.wait()

    asyncio.run(run())
