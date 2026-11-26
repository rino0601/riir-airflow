import asyncio
from typing import TYPE_CHECKING, Any

from airflow.executors.local_executor import LocalExecutor
from airflow.executors.workloads import WorkloadType

from riir_airflow._lowlevel import drain_local_commands

if TYPE_CHECKING:
    from airflow.models.taskinstancekey import TaskInstanceKey


class AsgiExecutor(LocalExecutor):
    """Run Airflow task workloads through a single local worker."""

    supports_pickling: bool = False
    supports_multi_team: bool = False
    is_single_threaded: bool = True
    is_production: bool = False
    supported_workload_types = frozenset({WorkloadType.EXECUTE_TASK})

    def __init__(self):
        super().__init__(parallelism=1)
        self.commands_to_run = []

    def execute_async(
        self,
        key: "TaskInstanceKey",
        command: list[str],
        queue: str | None = None,
        executor_config: Any | None = None,
    ) -> None:
        """Queue a command for callers of the legacy command interface."""
        self.log.info("Booking command(): %s", command)
        coro = asyncio.create_subprocess_exec(command[0], *command[1:])
        self.commands_to_run.append((key, coro))

    async def synca(self) -> None:
        # Await both subprocess phases in the caller's asyncio task.
        commands = drain_local_commands(self)
        for key, coro in commands:
            await asyncio.sleep(0)
            self.log.info("Executing command(coro): %s", coro)
            try:
                proc = await coro
                returncode = await proc.wait()
            except Exception as error:
                commands.execution_failed(key, error)
            else:
                commands.command_exited(key, returncode)

    async def aheartbeat(self) -> None:
        self.heartbeat()
        await self.synca()
