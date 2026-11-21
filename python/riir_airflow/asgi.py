import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.wsgi import WSGIMiddleware

from riir_airflow._core.fastapi_app import web_app as _fastapi_app
from riir_airflow.configuration import settings


def _init_af(mount_path: str, fastapi_app: FastAPI) -> FastAPI:
    from airflow.utils import db

    try:  # Airflow 2의 Flask UI. Airflow 3에서는 FastAPI 앱을 쓴다.
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
    if isinstance(airflow_app, FastAPI):
        fastapi_app.mount(mount_path, airflow_app)
    else:
        fastapi_app.mount(mount_path, WSGIMiddleware(airflow_app))
    return fastapi_app


def _init_web_app() -> FastAPI:
    if settings.mode != "dev":
        return _init_af("/airflow", _fastapi_app)
    return _fastapi_app


app = _init_web_app()
