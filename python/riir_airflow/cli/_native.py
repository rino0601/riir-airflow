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
