from pydantic_settings import (
    CliApp,
    CliSubCommand,
)

from riir_airflow.cli.serve import Serve
from riir_airflow.cli.show import Show
from riir_airflow.configuration import Settings


class CLI(Settings, cli_parse_args=True):
    """riir-airflow CLI: serve/show 하위 명령을 제공한다."""

    serve: CliSubCommand[Serve]
    show: CliSubCommand[Show]

    def cli_cmd(self) -> None:
        CliApp.run_subcommand(self)


settings = CLI()
cli_app = settings.cli_cmd

if __name__ == "__main__":
    cli_app()
