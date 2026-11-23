import sys

import pytest
from riir_airflow.__main__ import cli_app
from riir_airflow.cli import _native, serve
from riir_airflow.cli.serve import Serve
from riir_airflow.cli.show import Show
from riir_airflow.configuration import Settings, get_settings


def test_settings_default_mode(monkeypatch):
    monkeypatch.delenv("RIIR_MODE", raising=False)
    assert Settings().mode == "dev"


def test_settings_prod_mode(monkeypatch):
    monkeypatch.setenv("RIIR_MODE", "prod")
    assert Settings().mode == "prod"


def test_settings_explicit_mode_wins(monkeypatch):
    monkeypatch.setenv("RIIR_MODE", "prod")
    assert Settings(mode="dev").model_dump() == {"mode": "dev"}


def test_show_cli_cmd(capsys):
    Show().cli_cmd()
    output = capsys.readouterr().out
    assert "dev" in output
    assert output == "show \"settings.mode='dev'\"\n"


def test_cli_mode_show(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["riir-airflow", "--mode", "prod", "show"])
    cli_app()
    assert capsys.readouterr().out == "show \"settings.mode='prod'\"\n"


@pytest.mark.parametrize("mode", ["", "-1", "-.5", "can't", "both'\"", "한글\n\t\\"])
def test_cli_show_preserves_mode_repr(monkeypatch, capsys, mode):
    monkeypatch.setattr(sys, "argv", ["riir-airflow", "--mode", mode, "show"])
    cli_app()
    assert capsys.readouterr().out == f'show "settings.mode={mode!r}"\n'


def test_cli_default_mode_show(monkeypatch, capsys):
    monkeypatch.delenv("RIIR_MODE", raising=False)
    monkeypatch.setattr(sys, "argv", ["riir-airflow", "show"])
    cli_app()
    assert capsys.readouterr().out == "show \"settings.mode='dev'\"\n"


def test_cli_env_mode_show(monkeypatch, capsys):
    monkeypatch.setenv("RIIR_MODE", "prod")
    monkeypatch.setattr(sys, "argv", ["riir-airflow", "show"])
    cli_app()
    assert capsys.readouterr().out == "show \"settings.mode='prod'\"\n"


def test_cli_mode_prod_serve(monkeypatch):
    calls = []
    monkeypatch.setattr(
        Serve, "_run_server", lambda self, mode, **kwargs: calls.append((mode, kwargs))
    )
    monkeypatch.setattr(sys, "argv", ["riir-airflow", "--mode", "prod", "serve"])
    cli_app()
    assert calls == [("prod", {"port": 80, "log_level": "info", "workers": 1})]


def test_cli_mode_dev_serve(monkeypatch):
    calls = []
    monkeypatch.setattr(
        Serve, "_run_server", lambda self, mode, **kwargs: calls.append((mode, kwargs))
    )
    monkeypatch.setattr(sys, "argv", ["riir-airflow", "--mode", "dev", "serve"])
    cli_app()
    assert calls == [("dev", {"port": 8080, "reload": True})]


@pytest.mark.parametrize(
    ("env_mode", "mode_args", "port_args", "expected"),
    [
        (None, [], [], ("dev", None)),
        ("prod", [], [], ("prod", None)),
        ("prod", ["--mode", "dev"], ["--port", "0"], ("dev", 0)),
    ],
)
def test_native_cli_skips_uvicorn_options(
    monkeypatch, env_mode, mode_args, port_args, expected
):
    if env_mode is None:
        monkeypatch.delenv("RIIR_MODE", raising=False)
    else:
        monkeypatch.setenv("RIIR_MODE", env_mode)

    def unexpected_options(*args):
        pytest.fail("Native serve must skip Uvicorn options")

    calls = []
    monkeypatch.setattr(serve, "serve_options", unexpected_options)
    monkeypatch.setattr(
        _native, "serve_native", lambda mode, port: calls.append((mode, port)) or True
    )
    monkeypatch.setattr(
        sys, "argv", ["riir-airflow", *mode_args, "serve", "--native", *port_args]
    )
    cli_app()
    assert calls == [expected]


def test_native_cli_preserves_subcommand_environment(monkeypatch):
    monkeypatch.setenv("riir_mode", "prod")
    monkeypatch.setenv("riir_serve", '{"port": 12345}')
    monkeypatch.setattr(sys, "argv", ["riir-airflow", "serve", "--native"])
    calls = []
    monkeypatch.setattr(
        _native, "serve_native", lambda mode, port: calls.append((mode, port)) or True
    )
    cli_app()
    assert calls == [("prod", 12345)]


@pytest.mark.parametrize(
    ("args", "env_serve", "expected"),
    [
        ([], '{"NATIVE":"YES","port":"1_000.0"}', ("dev", 1000)),
        ([], '{"native":true,"port":1000,"PORT":0}', ("dev", 0)),
        (["--port=null"], '{"native":true,"port":1000}', ("dev", None)),
        (["--port=+80", "--native"], '{"native":null,"port":-1}', ("dev", 80)),
        (["--no-native", "--native"], "invalid JSON", ("dev", None)),
    ],
)
def test_native_cli_resolves_serve_options(monkeypatch, args, env_serve, expected):
    monkeypatch.setenv("RIIR_MODE", "dev")
    monkeypatch.setenv("RIIR_SERVE", env_serve)
    monkeypatch.setattr(sys, "argv", ["riir-airflow", "serve", *args])
    calls = []
    monkeypatch.setattr(
        _native, "serve_native", lambda mode, port: calls.append((mode, port)) or True
    )
    cli_app()
    assert calls == [expected]


def test_environment_native_selection_preserves_cli_negation(monkeypatch):
    monkeypatch.setenv("RIIR_MODE", "dev")
    monkeypatch.setenv("RIIR_SERVE", '{"native": true, "port": 12345}')
    monkeypatch.setattr(sys, "argv", ["riir-airflow", "serve", "--no-native"])
    calls = []
    monkeypatch.setattr(
        Serve, "_run_server", lambda self, mode, **kwargs: calls.append((mode, kwargs))
    )
    cli_app()
    assert calls == [("dev", {"port": 12345, "reload": True})]


def test_environment_selected_native_serve_uses_environment_mode_and_port(monkeypatch):
    monkeypatch.setenv("RIIR_MODE", "prod")
    monkeypatch.setenv("RIIR_SERVE", '{"native": true, "port": 12345}')
    monkeypatch.setattr(sys, "argv", ["riir-airflow", "serve"])
    calls = []
    monkeypatch.setattr(
        _native, "serve_native", lambda mode, port: calls.append((mode, port)) or True
    )
    cli_app()
    assert calls == [("prod", 12345)]


@pytest.mark.parametrize(
    ("args", "settings", "code", "message"),
    [
        ([], None, 1, "CLI subcommand is required"),
        (["bogus"], None, 2, "unrecognized subcommand"),
        (["show", "--mode", "dev"], None, 2, "--mode"),
        (["serve", "--port"], None, 2, "--port"),
        (["serve", "--native=true"], None, 2, "--native"),
        (["--mode=null", "show"], None, 1, "mode: Input should be a valid string"),
        (["serve", "--port", "-1"], None, 1, "greater than or equal to 0"),
        (["serve", "--port", "-.5"], None, 1, "valid integer"),
        (
            ["serve", "--port=bad"],
            None,
            1,
            "serve.port: Input should be a valid integer",
        ),
        (
            ["serve"],
            '{"native": true, "port": 65536}',
            1,
            "less than or equal to 65535",
        ),
        (
            ["serve"],
            '{"native":null}',
            1,
            "serve.native: Input should be a valid boolean",
        ),
    ],
)
def test_cli_errors(monkeypatch, capsys, args, settings, code, message):
    if settings is not None:
        monkeypatch.setenv("RIIR_SERVE", settings)
    monkeypatch.setattr(sys, "argv", ["riir-airflow", *args])
    with pytest.raises(SystemExit) as exc:
        cli_app()
    assert exc.value.code == code
    output = capsys.readouterr()
    assert not output.out
    assert message in output.err


@pytest.mark.parametrize(
    ("args", "features"),
    [
        (["-h"], ["--mode", "serve", "show"]),
        (["serve", "--help"], ["--native", "--no-native", "--port"]),
        (["show", "--help"], ["show", "--help"]),
    ],
)
def test_cli_help(monkeypatch, capsys, args, features):
    monkeypatch.setenv("RIIR_SERVE", '{"port": 65536}')
    monkeypatch.setattr(sys, "argv", ["riir-airflow", *args])
    with pytest.raises(SystemExit) as exc:
        cli_app()
    assert exc.value.code == 0
    output = capsys.readouterr()
    assert not output.err
    assert all(feature in output.out for feature in features)


def test_cli_restores_settings_context_on_error(monkeypatch):
    initial_settings = get_settings()

    def fail(self, mode, **options):
        assert mode == "prod"
        assert get_settings().mode == "prod"
        raise RuntimeError("server failed")

    monkeypatch.setenv("RIIR_MODE", "dev")
    monkeypatch.setattr(Serve, "_run_server", fail)
    monkeypatch.setattr(sys, "argv", ["riir-airflow", "--mode=prod", "serve"])
    with pytest.raises(RuntimeError, match="server failed"):
        cli_app()
    assert get_settings() is initial_settings


@pytest.mark.parametrize("mode", ["", "Prod", "staging"])
@pytest.mark.parametrize("native_args", [[], ["--native"]])
def test_cli_invalid_serve_mode(monkeypatch, capsys, mode, native_args):
    def unexpected_server(*args, **kwargs):
        pytest.fail("Uvicorn must not start for an invalid mode")

    monkeypatch.setattr(Serve, "_run_server", unexpected_server)
    monkeypatch.setattr(
        sys, "argv", ["riir-airflow", "--mode", mode, "serve", *native_args]
    )
    cli_app()
    assert capsys.readouterr().out == "Invalid parameter. Use 'prod' or 'dev'.\n"
