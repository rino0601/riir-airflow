import asyncio
import subprocess
from asyncio.subprocess import Process
from typing import TYPE_CHECKING, Any

from airflow.executors.base_executor import BaseExecutor

if TYPE_CHECKING:
    from airflow.executors.base_executor import CommandType
    from airflow.models.taskinstancekey import TaskInstanceKey


class AsgiExecutor(BaseExecutor):
    """
    This executor will only run one task instance at a time.

    It can be used for debugging. It is also the only executor
    that can be used with sqlite since sqlite doesn't support
    multiple connections.

    Since we want airflow to work out of the box, it defaults to this
    AsgiExecutor alongside sqlite as you first install it.
    """

    supports_pickling: bool = False

    is_local: bool = True
    is_single_threaded: bool = True
    is_production: bool = False

    serve_logs: bool = True

    def __init__(self):
        super().__init__()
        self.commands_to_run = []
        # self.pool = ProcessPoolExecutor()
        # self.loop = asyncio.get_running_loop()

    def execute_async(
        self,
        key: "TaskInstanceKey",
        command: "CommandType",
        queue: str | None = None,
        executor_config: Any | None = None,
    ) -> None:
        self.validate_airflow_tasks_run_command(command)
        # self.commands_to_run.append((key, command))
        # 일단 asyncio.create_task 와 --raw 대책 없이
        # --local, --raw 의 일단... 대책
        # command = list(map(lambda x: x.replace("--local", "--raw"), command))
        self.log.info("Booking command(): %s", command)
        coro = asyncio.create_subprocess_exec(command[0], *command[1:])
        # coro = asyncio.create_task(coro)
        self.commands_to_run.append((key, coro))

    async def synca(self) -> None:
        for key, coro in self.commands_to_run:
            await asyncio.sleep(0)
            self.log.info("Executing command(coro): %s", coro)
            self.log.info("1@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
            try:
                proc: Process = await coro
                await proc.wait()
                self.log.info("2@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
                self.log.info(f"{proc.returncode=}")
                if proc.returncode == 0:
                    self.success(key)
            except subprocess.CalledProcessError as e:
                self.fail(key)
                self.log.error("Failed to execute task %s.", e)

        self.commands_to_run = []

    async def aheartbeat(self) -> None:
        self.heartbeat()
        await self.synca()

    def end(self):
        """End the executor."""
        self.heartbeat()

    def terminate(self):
        """Terminate the executor is not doing anything."""
