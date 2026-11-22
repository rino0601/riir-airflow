from pydantic import BaseModel
from riir_airflow._lowlevel import serve_options
from riir_airflow.configuration import get_settings
from sh import Command


class Serve(BaseModel):
    # repository: CliPositionalArg[str]
    # directory: CliPositionalArg[str]

    def _run_server(self, mode: str, **kwargs):
        uvicorn_cmd = Command("uvicorn").bake("riir_airflow.asgi:app", **kwargs)
        print(f"Starting Uvicorn server in {mode} mode...")
        for line in uvicorn_cmd(_iter=True):
            print(line, end="")

    def cli_cmd(self) -> None:
        mode = get_settings().mode
        options = serve_options(mode)
        if options is None:
            print("Invalid parameter. Use 'prod' or 'dev'.")
            return

        self._run_server(mode, **options)
