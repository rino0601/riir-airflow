import os
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from a2wsgi import WSGIMiddleware
from riir_airflow._core.asgi_bridge import ASGIRuntime
from riir_airflow._core.web_app import create_web_app
from riir_airflow._lowlevel import airflow_mount_path
from starlette.applications import Starlette


def _init_af(mount_path: str, app: Starlette, *, manage_lifespan: bool) -> Starlette:
    from airflow.configuration import AIRFLOW_HOME
    from airflow.utils import db

    try:  # Airflow 2의 Flask UI. Airflow 3에서는 ASGI 앱을 쓴다.
        from airflow.www.app import cached_app
    except ModuleNotFoundError as exc:
        if exc.name not in {"airflow.www", "airflow.www.app"}:
            raise
        from airflow.api_fastapi.app import cached_app, get_auth_manager_cls

        if manage_lifespan:
            from airflow.api_fastapi.auth.tokens import get_signing_args

            # Match Airflow's API server bootstrap before the execution API
            # lifespan validates the signing configuration.
            get_signing_args()
    else:
        get_auth_manager_cls = None

    already_initialized = Path(
        os.environ.get("AIRFLOW_HOME", AIRFLOW_HOME), "standalone_admin_password.txt"
    ).exists()
    if not already_initialized:
        # Set up DB tables
        db.initdb()

        # Airflow 3 defaults to SimpleAuthManager, which creates its own
        # credentials. Keep standalone admin creation for the FAB manager.
        if get_auth_manager_cls is None or any(
            base.__module__.startswith("airflow.providers.fab.auth_manager.")
            for base in get_auth_manager_cls().__mro__
        ):
            from airflow.providers.fab.auth_manager.cli_commands.utils import (
                get_application_builder,
            )

            with get_application_builder() as appbuilder:
                appbuilder.sm.create_admin_standalone()

    airflow_app = cached_app()
    if isinstance(airflow_app, Starlette):
        app.mount(mount_path, airflow_app)
        if manage_lifespan:
            # Starlette dispatches lifespan only to the outer router. Airflow's
            # context starts its own FastAPI mounts and disposes its async engine.
            @asynccontextmanager
            async def lifespan(_: Starlette):
                async with airflow_app.router.lifespan_context(airflow_app) as state:
                    yield state

            app.router.lifespan_context = lifespan
    else:
        app.mount(mount_path, WSGIMiddleware(airflow_app))
    return app


def create_app(mode: str, *, manage_lifespan: bool = False) -> Starlette:
    app = create_web_app()
    mount_path = airflow_mount_path(mode)
    if mount_path is not None:
        return _init_af(mount_path, app, manage_lifespan=manage_lifespan)
    return app


def start_native_runtime(mode: str) -> ASGIRuntime:
    return ASGIRuntime(partial(create_app, mode, manage_lifespan=True))
