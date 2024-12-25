import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.wsgi import WSGIMiddleware

from ._core.fastapi_app import web_app as _fastapi_app


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


app = _init_af("/airflow", _fastapi_app)
