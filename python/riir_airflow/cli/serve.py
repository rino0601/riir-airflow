from dataclasses import dataclass
from os import environ
from sys import executable

from riir_airflow._lowlevel import serve_options
from riir_airflow.cli._native import INVALID_MODE, run_native
from riir_airflow.configuration import get_settings


@dataclass
class Serve:
    native: bool = False
    port: int | None = None

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
        env = {
            key: value for key, value in environ.items() if key.lower() != "riir_mode"
        }
        for line in uvicorn_cmd(_iter=True, _env={**env, "RIIR_MODE": mode}):
            print(line, end="")

    def cli_cmd(self) -> None:
        mode = get_settings().mode
        if self.native:
            run_native(mode, self.port)
            return

        options = serve_options(mode)
        if options is None:
            print(INVALID_MODE)
            return
        if self.port is not None:
            options["port"] = self.port
        self._run_server(mode, **options)
