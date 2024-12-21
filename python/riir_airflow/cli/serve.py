from pydantic import BaseModel
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
        from riir_airflow.configuration import settings

        match settings.mode:
            case "prod":
                self._run_server("prod", port=80, log_level="info", workers=1)
            case "dev":
                # uvicorn riir_airflow.asgi:app --port 8080 --reload
                self._run_server("dev", port=8080, reload=True)
            case _:
                print("Invalid parameter. Use 'prod' or 'dev'.")
                return
