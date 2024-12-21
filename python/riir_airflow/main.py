import os
from pathlib import Path

from airflow.utils import db
from airflow.www.app import cached_app
from fastapi import FastAPI
from fastapi.middleware.wsgi import WSGIMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from pydantic_settings import (
    BaseSettings,
    CliApp,
    CliSubCommand,
    SettingsConfigDict,
)
from sh import Command

from riir_airflow._core.fastapi_app import web_app as _fastapi_app
from riir_airflow.gui import init as _init_gui


class Show(BaseModel):
    def cli_cmd(self) -> None:
        print(f'show "{settings.redis_host=}"')


class Serve(BaseModel):
    # repository: CliPositionalArg[str]
    # directory: CliPositionalArg[str]

    def _run_server(self, mode: str, **kwargs):
        uvicorn_cmd = Command("uvicorn").bake("riir_airflow.main:web_app", **kwargs)
        print(f"Starting Uvicorn server in {mode} mode...")
        for line in uvicorn_cmd(_iter=True):
            print(line, end="")

    def cli_cmd(self) -> None:
        match os.getenv("MODE", "dev"):
            case "prod":
                self._run_server("prod", port=80, log_level="info", workers=1)
            case "dev":
                self._run_server("dev", port=8080, reload=True)
            case _:
                print("Invalid parameter. Use 'prod' or 'dev'.")
                return


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="my_prefix_",
        cli_parse_args=True,
    )

    # Basic settings
    redis_host: str = "localhost"

    # CLI Subcommands
    serve: CliSubCommand[Serve]
    show: CliSubCommand[Show]

    def cli_cmd(self) -> None:
        CliApp.run_subcommand(self)


def _init_af(mount_path: str, fastapi_app: FastAPI) -> FastAPI:
    already_initialized = Path(
        os.environ["AIRFLOW_HOME"], "standalone_admin_password.txt"
    ).exists()
    if not already_initialized:
        # Set up DB tables
        db.initdb()

        # Then create a "default" admin user if necessary
        from airflow.providers.fab.auth_manager.cli_commands.utils import (
            get_application_builder,
        )

        with get_application_builder() as appbuilder:
            user_name, password = appbuilder.sm.create_admin_standalone()

    flask_app = cached_app()
    fastapi_app.mount(mount_path, WSGIMiddleware(flask_app))
    return fastapi_app


def _init_web_app() -> FastAPI:
    # Initialize the GUI and Airflow
    app = _init_gui("/gui", _fastapi_app)
    app = _init_af("/airflow", app)

    # Define the redirect route
    @app.get("/")
    async def redirect_to_gui():
        return RedirectResponse(url="/gui")

    return app


settings = Settings()
web_app = _init_web_app()


def cli_app():
    settings.cli_cmd()
