from os import environ

from riir_airflow._lowlevel import native_serve_args, serve_native

INVALID_MODE = "Invalid parameter. Use 'prod' or 'dev'."


def run_native(mode: str, port: int | None) -> None:
    try:
        if not serve_native(mode, port):
            print(INVALID_MODE)
    except KeyboardInterrupt:
        # Python can deliver SIGINT after Axum has drained the listener.
        return
    except OSError as exc:
        raise SystemExit(str(exc)) from None


def try_native_serve(args: list[str]) -> bool:
    parsed = native_serve_args(args)
    if parsed is None:
        return False
    mode, port = parsed

    env = {key.lower(): value for key, value in environ.items()}
    # The settings model can merge JSON subcommand values from the environment.
    if "riir_serve" in env or "riir_show" in env:
        return False
    run_native(mode if mode is not None else env.get("riir_mode", "dev"), port)
    return True
