import subprocess
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

    def execute_async(
        self,
        key: "TaskInstanceKey",
        command: "CommandType",
        queue: str | None = None,
        executor_config: Any | None = None,
    ) -> None:
        self.validate_airflow_tasks_run_command(command)
        self.commands_to_run.append((key, command))

    def sync(self) -> None:
        for key, command in self.commands_to_run:
            self.log.info("Executing command: %s", command)
            self.log.info("@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
            try:
                # TODO. 여기를 ProcessPoolExecutor 로 바꿔야
                subprocess.check_call(command, close_fds=True)
                self.success(key)
            except subprocess.CalledProcessError as e:
                self.fail(key)
                self.log.error("Failed to execute task %s.", e)

        self.commands_to_run = []

    def end(self):
        """End the executor."""
        self.heartbeat()

    def terminate(self):
        """Terminate the executor is not doing anything."""
