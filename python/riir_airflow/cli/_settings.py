from pydantic_settings import (
    CliApp,
    CliSubCommand,
)
from riir_airflow.cli.serve import Serve
from riir_airflow.cli.show import Show
from riir_airflow.configuration import Settings, active_settings


class CLI(Settings, cli_parse_args=True):
    """riir-airflow CLI: serve/show 하위 명령을 제공한다."""

    serve: CliSubCommand[Serve]
    show: CliSubCommand[Show]

    def cli_cmd(self) -> None:
        token = active_settings.set(self)
        try:
            CliApp.run_subcommand(self)
        finally:
            active_settings.reset(token)
