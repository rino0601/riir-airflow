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
