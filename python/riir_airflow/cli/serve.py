from os import environ
from sys import executable

from pydantic import BaseModel, Field
from pydantic_settings import CliImplicitFlag
from riir_airflow._lowlevel import serve_native, serve_options
from riir_airflow.configuration import get_settings


class Serve(BaseModel):
    native: CliImplicitFlag[bool] = Field(
        default=False, description="Serve /health and /show directly with Axum."
    )
    port: int | None = Field(default=None, ge=0, le=65535)

    def _run_server(self, mode: str, **kwargs):
        try:
            from sh import Command
        except ImportError:
            raise SystemExit(
                "ASGI serve requires riir-airflow[compat]. "
                "Install the compatibility extra or pass --native."
            ) from None

        uvicorn_cmd = Command(executable).bake(
            "-m", "uvicorn", "riir_airflow.asgi:app", **kwargs
        )
        print(f"Starting Uvicorn server in {mode} mode...")
        for line in uvicorn_cmd(_iter=True, _env={**environ, "RIIR_MODE": mode}):
            print(line, end="")

    def cli_cmd(self) -> None:
        mode = get_settings().mode
        options = serve_options(mode)
        if options is None:
            print("Invalid parameter. Use 'prod' or 'dev'.")
            return

        if self.port is not None:
            options["port"] = self.port
        if self.native:
            try:
                serve_native(options["port"])
            except KeyboardInterrupt:
                # Python can deliver SIGINT after Axum has drained the listener.
                return
            except OSError as exc:
                raise SystemExit(str(exc)) from None
            return

        self._run_server(mode, **options)
