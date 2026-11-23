import sys

import pytest
from riir_airflow.__main__ import cli_app
from riir_airflow._lowlevel import native_serve_args
from riir_airflow.cli import _native, serve
from riir_airflow.cli.serve import Serve
from riir_airflow.cli.show import Show
from riir_airflow.configuration import Settings


def test_settings_default_mode(monkeypatch):
    monkeypatch.delenv("RIIR_MODE", raising=False)
    assert Settings().mode == "dev"


def test_settings_prod_mode(monkeypatch):
    monkeypatch.setenv("RIIR_MODE", "prod")
    assert Settings().mode == "prod"


def test_show_cli_cmd(capsys):
    Show().cli_cmd()
    output = capsys.readouterr().out
    assert "dev" in output
    assert output == "show \"settings.mode='dev'\"\n"


def test_cli_mode_show(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["riir-airflow", "--mode", "prod", "show"])
    cli_app()
    assert capsys.readouterr().out == "show \"settings.mode='prod'\"\n"


@pytest.mark.parametrize("mode", ["", "can't", "both'\"", "한글\n\t\\"])
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
    ("args", "expected"),
    [
        (["serve", "--native"], (None, None)),
        (["serve", "--native", "--port", "00000"], (None, 0)),
        (["--mode", "prod", "serve", "--port", "65535", "--native"], ("prod", 65535)),
        (["serve"], None),
        (["serve", "--native", "--help"], None),
        (["--mode", "staging", "serve", "--native"], None),
        *[
            (["serve", "--native", "--port", port], None)
            for port in ["", "+80", "65536", "000080", "８０", "\udcff"]
        ],
    ],
)
def test_native_serve_argument_fast_path(args, expected):
    assert native_serve_args(args) == expected


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
