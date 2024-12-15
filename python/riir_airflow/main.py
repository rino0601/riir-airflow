import uvicorn
from airflow.utils import db
from airflow.www.app import cached_app
from fastapi import FastAPI
from fastapi.middleware.wsgi import WSGIMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from pydantic_settings import CliApp, CliPositionalArg, CliSubCommand

from riir_airflow._core.fastapi_app import web_app
from riir_airflow.gui import init as init_gui


def _init_af(mount_path: str, fastapi_app: FastAPI) -> None:
    # Set up DB tables
    db.initdb()

    # Then create a "default" admin user if necessary
    from airflow.providers.fab.auth_manager.cli_commands.utils import (
        get_application_builder,
    )

    with get_application_builder() as appbuilder:
        user_name, password = appbuilder.sm.create_admin_standalone()

    flask_app = cached_app()
    web_app.mount("/airflow", WSGIMiddleware(flask_app))


class Init(BaseModel):
    directory: CliPositionalArg[str]

    def cli_cmd(self) -> None:
        print(f'git init "{self.directory}"')
        # > git init "dir"
        self.directory = "ran the git init cli cmd"


class Clone(BaseModel):
    """
    #!/usr/bin/env bash

    # use path of this example as working directory; enables starting this script from anywhere
    cd "$(dirname "$0")"

    if [ "$1" = "prod" ]; then
        echo "Starting Uvicorn server in production mode..."
        # we also use a single worker in production mode so socket.io connections are always handled by the same worker
        uvicorn main:app --workers 1 --log-level info --port 80
    elif [ "$1" = "dev" ]; then
        echo "Starting Uvicorn server in development mode..."
        # reload implies workers = 1
        uvicorn main:app --reload --log-level debug --port 8000
    else
        echo "Invalid parameter. Use 'prod' or 'dev'."
        exit 1
    fi
    """

    repository: CliPositionalArg[str]
    directory: CliPositionalArg[str]

    def cli_cmd(self) -> None:
        print(f'git clone from "{self.repository}" into "{self.directory}"')
        self.directory = "ran the clone cli cmd"


class Git(BaseModel):
    clone: CliSubCommand[Clone]
    init: CliSubCommand[Init]

    def cli_cmd(self) -> None:
        CliApp.run_subcommand(self)


def cli_app():
    init_gui("/gui", web_app)
    _init_af("/airflow", web_app)

    @web_app.get("/")
    async def redirect_to_gui():
        return RedirectResponse(url="/gui")

    server_config = uvicorn.Config(web_app, port=8080, log_level="info")
    server = uvicorn.Server(server_config)
    server.run()
    # TODO.
    # cmd = CliApp.run(Git)
