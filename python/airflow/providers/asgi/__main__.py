from pydantic_settings import (
    CliApp,
    CliSubCommand,
)

from .cli.serve import Serve
from .cli.show import Show
from .configuration import Settings


class CLI(Settings, cli_parse_args=True):
    """asdf232323233asdf"""

    serve: CliSubCommand[Serve]
    show: CliSubCommand[Show]

    def cli_cmd(self) -> None:
        CliApp.run_subcommand(self)


settings = CLI()
cli_app = settings.cli_cmd

if __name__ == "__main__":
    cli_app()
