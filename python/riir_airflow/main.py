import uvicorn
from airflow.utils import db
from airflow.www.app import cached_app
from fastapi import FastAPI
from fastapi.middleware.wsgi import WSGIMiddleware
from fastapi.responses import RedirectResponse

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


def cli_app():
    init_gui("/gui", web_app)
    _init_af("/airflow", web_app)

    @web_app.get("/")
    async def redirect_to_gui():
        return RedirectResponse(url="/gui")

    server_config = uvicorn.Config(web_app, port=8080, log_level="info")
    server = uvicorn.Server(server_config)
    server.run()
