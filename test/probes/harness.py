import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path


class ProbeHarness:
    def __init__(self, root: Path, test_dir: Path, airflow_version: str):
        self._root = root
        self._test_dir = test_dir
        self._airflow_version = airflow_version

    def _dag_sources(self, names: Iterable[str]) -> dict[str, str]:
        dags = self._test_dir / "probes" / "dags"
        return {name: (dags / name).read_text() for name in names}

    def run(
        self,
        parsed_dag_factory,
        dag_names: Iterable[str],
        probe: str,
        mode: str,
        *,
        fresh_parse: bool = False,
        **config: str,
    ):
        import airflow

        assert airflow.__version__ == self._airflow_version
        worker = self._root / "holding_worker"
        source = (self._test_dir / "probes" / "holding_worker.py").read_text()
        _, _, body = source.partition("\n")
        worker.write_text(f"#!{sys.executable}\n{body}")
        worker.chmod(0o700)
        env = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("AIRFLOW")
        }
        env.update(
            AIRFLOW_HOME=str(self._root),
            AIRFLOW__CORE__LOAD_EXAMPLES="False",
            AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION="False",
            AIRFLOW__CORE__EXECUTOR="riir_airflow.executors.asgi_executor.AsgiExecutor",
            AIRFLOW__API_AUTH__JWT_SECRET="admission-probe-test-secret-" * 3,
            AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=(
                f"sqlite:///{self._root / 'airflow.db'}"
            ),
        )
        env.update(config)
        _, env = parsed_dag_factory.prepare(
            self._root,
            env,
            self._dag_sources(dag_names),
            fresh=fresh_parse,
        )
        result = subprocess.run(
            [
                sys.executable,
                str(self._test_dir / "probes" / "admission.py"),
                probe,
                mode,
            ],
            env=env,
            cwd=self._root,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stdout + result.stderr
