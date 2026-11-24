import subprocess
import sys
import tomllib
from pathlib import Path

from riir_airflow import __version__, version

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def cargo_package_version() -> str:
    with (PROJECT_ROOT / "Cargo.toml").open("rb") as cargo_toml:
        return tomllib.load(cargo_toml)["package"]["version"]


def test_version_matches_cargo_package_version() -> None:
    assert version() == cargo_package_version()


def test_dunder_version_matches_version() -> None:
    assert __version__ == version()


def test_cli_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "riir_airflow", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == f"riir-airflow {version()}\n"
    assert result.stderr == ""
