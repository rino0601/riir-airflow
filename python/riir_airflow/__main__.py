import sys

from riir_airflow.cli._native import try_native_serve


def cli_app() -> None:
    if try_native_serve(sys.argv[1:]):
        return

    from riir_airflow.cli._settings import CLI

    CLI().cli_cmd()


if __name__ == "__main__":
    cli_app()
