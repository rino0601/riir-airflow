import asyncio

from multiprocessing import Process
import typer
import uvicorn
from airflow.utils import db
from airflow.api_internal.internal_api_call import InternalApiConfig
from airflow.executors.executor_loader import ExecutorLoader
from airflow.jobs.job import Job, run_job
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner
from airflow.www.app import cached_app
from fastapi.middleware.wsgi import WSGIMiddleware
from contextlib import contextmanager
from sh import Command

from riir_airflow._core.fastapi_app import web_app


cli_app = typer.Typer()


@cli_app.callback()
def callback():
    """
    Rewrite Airflow in Rust.
    """


@cli_app.command()
def poc():
    uvicorn.run(web_app)


@contextmanager
def _serve_logs(skip_serve_logs: bool = False):
    """Start serve_logs sub-process."""
    from airflow.utils.serve_logs import serve_logs

    sub_proc = None
    executor_class, _ = ExecutorLoader.import_default_executor_cls()
    if executor_class.serve_logs:
        if skip_serve_logs is False:
            sub_proc = Process(target=serve_logs)
            sub_proc.start()
    try:
        yield
    finally:
        if sub_proc:
            sub_proc.terminate()


@cli_app.command()
def scheduler():
    job_runner = SchedulerJobRunner(job=Job())
    ExecutorLoader.validate_database_executor_compatibility(job_runner.job.executor)
    InternalApiConfig.force_database_direct_access()
    run_job(job=job_runner.job, execute_callable=job_runner._execute)


async def create_scheduler():
    class SchedulerJobAsyncRunner(SchedulerJobRunner):
        def _execute(self) -> int | None:
            return super()._execute()

        def _run_scheduler_loop(self) -> None:
            return super()._run_scheduler_loop()

    job_runner = SchedulerJobAsyncRunner(job=Job())
    ExecutorLoader.validate_database_executor_compatibility(job_runner.job.executor)
    InternalApiConfig.force_database_direct_access()
    run_job(job=job_runner.job, execute_callable=job_runner._execute)


async def create_scheduler_proc():
    scheduler_cmd = Command("riir-airflow").bake("scheduler")
    async for line in scheduler_cmd(_async=True, _err_to_out=True):
        typer.echo(line.rstrip())


# 아래처럼 함으로서 api 덮어 쓸 수 있음은 확인함.
# @web_app.get("/health")
# async def h():
#     return {
#         "dag_processor": {"latest_dag_processor_heartbeat": None, "status": None},
#         "metadatabase": {"status": "healthy"},
#         "scheduler": {
#             "latest_scheduler_heartbeat": "2024-05-06T11:20:08.814069+00:00",
#             "status": "unhealthy",
#         },
#         "triggerer": {
#             "latest_triggerer_heartbeat": "2024-05-06T11:20:06.237693+00:00",
#             "status": "unhealthy",
#         },
#     }


@cli_app.command(name="triggerer")
# @cli_app.command(name="scheduler")
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
