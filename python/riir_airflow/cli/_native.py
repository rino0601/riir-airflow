from os import environ

from riir_airflow._lowlevel import serve_native

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
    # Keep help, validation errors, and other argument forms with the CLI model.
    mode = None
    if args[:1] == ["--mode"] and len(args) >= 2 and args[1] in {"dev", "prod"}:
        mode = args[1]
        args = args[2:]

    match args:
        case ["serve", "--native"]:
            port = None
        case ["serve", "--native", "--port", value] | [
            "serve",
            "--port",
            value,
            "--native",
        ]:
            if not (value.isascii() and value.isdecimal() and len(value) <= 5):
                return False
            port = int(value)
            if port > 65535:
                return False
        case _:
            return False

    env = {key.lower(): value for key, value in environ.items()}
    # The settings model can merge JSON subcommand values from the environment.
    if "riir_serve" in env or "riir_show" in env:
        return False
    run_native(mode if mode is not None else env.get("riir_mode", "dev"), port)
    return True
