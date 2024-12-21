import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.wsgi import WSGIMiddleware
from fastapi.responses import RedirectResponse

from riir_airflow._core.fastapi_app import web_app as _fastapi_app
from riir_airflow.configuration import settings
from riir_airflow.gui import init as _init_gui


def _init_af(mount_path: str, fastapi_app: FastAPI) -> FastAPI:
    from airflow.utils import db
    from airflow.www.app import cached_app

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
    if settings.mode != "dev":
        app = _init_af("/airflow", app)

    # Define the redirect route
    @app.get("/")
    async def redirect_to_gui():
        return RedirectResponse(url="/gui")

    return app


app = _init_web_app()
