import os
from pathlib import Path

from a2wsgi import WSGIMiddleware
from starlette.applications import Starlette

from riir_airflow._core.web_app import web_app
from riir_airflow._lowlevel import airflow_mount_path
from riir_airflow.configuration import settings


def _init_af(mount_path: str, app: Starlette) -> Starlette:
    from airflow.utils import db

    try:  # Airflow 2의 Flask UI. Airflow 3에서는 ASGI 앱을 쓴다.
        from airflow.www.app import cached_app
    except ImportError:
        from airflow.api_fastapi.app import cached_app

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

    airflow_app = cached_app()
    if isinstance(airflow_app, Starlette):
        app.mount(mount_path, airflow_app)
    else:
        app.mount(mount_path, WSGIMiddleware(airflow_app))
    return app


def _init_web_app() -> Starlette:
    mount_path = airflow_mount_path(settings.mode)
    if mount_path is not None:
        return _init_af(mount_path, web_app)
    return web_app


app = _init_web_app()
