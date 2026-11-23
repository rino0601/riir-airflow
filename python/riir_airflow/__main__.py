import sys
from pathlib import Path

from riir_airflow._lowlevel import parse_cli


def _run(mode: str, command: str, native: bool, port: int | None) -> None:
    if native:
        from riir_airflow.cli._native import run_native

        run_native(mode, port)
        return

    from riir_airflow.configuration import Settings, active_settings

    token = active_settings.set(Settings(mode=mode))
    try:
        if command == "show":
            from riir_airflow.cli.show import Show

            Show().cli_cmd()
        else:
            from riir_airflow.cli.serve import Serve

            Serve(port=port).cli_cmd()
    finally:
        active_settings.reset(token)


def cli_app() -> None:
    config, exit_status = parse_cli(sys.argv[1:], Path(sys.argv[0]).name)
    if exit_status is not None:
        code, message = exit_status
        print(message, end="", file=sys.stderr if code else sys.stdout)
        raise SystemExit(code)
    _run(*config)


if __name__ == "__main__":
    cli_app()
