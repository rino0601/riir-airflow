import sys

import pytest
from riir_airflow.__main__ import cli_app
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
