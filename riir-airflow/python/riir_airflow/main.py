import typer
import uvicorn
from airflow.utils import db
from airflow.www.app import cached_app
from fastapi.middleware.wsgi import WSGIMiddleware

from riir_airflow._core.fastapi_app import web_app


cli_app = typer.Typer()


@cli_app.callback()
def callback():
    """
    Rewrite Airflow in Rust.
    """


@cli_app.command(name="triggerer")
@cli_app.command(name="scheduler")
@cli_app.command(name="webserver")
@cli_app.command()
def standalone():
    # Set up DB tables
    typer.echo("Checking database is initialized")
    db.initdb()
    typer.echo("Database ready")

    # Then create a "default" admin user if necessary
    from airflow.providers.fab.auth_manager.cli_commands.utils import (
        get_application_builder,
    )

    with get_application_builder() as appbuilder:
        user_name, password = appbuilder.sm.create_admin_standalone()
    # Store what we know about the user for printing later in startup
    _user_info = {"username": user_name, "password": password}
    typer.echo(f"Login with username: {user_name}  password: {password}")

    flask_app = cached_app()
    web_app.mount("/", WSGIMiddleware(flask_app))
    server_config = uvicorn.Config(web_app, port=8080, log_level="info")
    server = uvicorn.Server(server_config)
    server.run()
