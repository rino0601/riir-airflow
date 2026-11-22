# CLI와 설정

## 명령

| 이름 | 엔트리 포인트 | 설명 |
| --- | --- | --- |
| `riir-airflow` | `riir_airflow.__main__:cli_app` | 최상위 CLI. `serve`, `show` 하위 명령을 제공합니다. |
| `riir-airflow show` | `riir_airflow.cli.show:Show` | 현재 `RIIR_MODE` 값을 출력합니다. |
| `riir-airflow serve` | `riir_airflow.cli.serve:Serve` | `dev` 모드에서는 8080 포트, `prod` 모드에서는 80 포트로 ASGI 앱을 실행합니다. |

최상위 `--mode` 옵션이 `show`와 `serve`가 사용하는 모드를 결정하며, 없으면 `RIIR_MODE`(기본값 `dev`)를 사용합니다.

## Python 설정

`riir_airflow.configuration.Settings`는 `RIIR_` 접두사를 붙인 환경 변수를 읽습니다.

| 환경 변수 | 기본값 | 타입 | 설명 |
| --- | --- | --- | --- |
| `RIIR_MODE` | `dev` | 문자열 | 실행 모드. `dev` 또는 `prod`를 사용합니다. |

## Airflow 환경 변수

`mise run configure`가 생성하는 `.env`에 다음 값이 기록됩니다.

| 이름 | 값 |
| --- | --- |
| `SQLALCHEMY_SILENCE_UBER_WARNING` | `1` |
| `AIRFLOW_HOME` | `.venv/airflow` |
| `AIRFLOW__LOGGING__LOGGING_LEVEL` | `INFO` |
| `AIRFLOW__CORE__EXECUTOR` | `riir_airflow.executors.asgi_executor.AsgiExecutor` |
| `AIRFLOW__CORE__DAGS_FOLDER` | `.venv/dags` |
| `AIRFLOW__CORE__LOAD_EXAMPLES` | `False` |
| `AIRFLOW__WEBSERVER__EXPOSE_CONFIG` | `True` |

## 개발 명령

| 명령 | 동작 |
| --- | --- |
| `mise run setup` | `uv sync`, `.env` 생성, prek 훅 설치 |
| `mise run check` | prek 설정 검사, 포맷/린트, 테스트, Zensical 빌드 |
| `mise run build` | maturin으로 pyo3 확장 모듈 빌드 |
| `mise run format` | `ruff format` |
| `mise run format-check` | `ruff format --check` |
| `mise run lint` | `ruff check` |
| `mise run lint-fix` | `ruff check --fix` |
| `mise run test` | `pytest` |
| `mise run docs` | `zensical serve` |
| `mise run docs-check` | `zensical build` |
| `mise run run` | 애플리케이션 실행 |
| `mise run clean` | 로컬 캐시와 Airflow 상태 정리 |
| `mise run clean-hard` | 캐시 정리 후 가상환경 재생성 |

## 버전

| 도구 | 버전 | 관리 주체 |
| --- | --- | --- |
| Python | 3.13 | uv (`UV_PYTHON`) |
| uv | 0.13.9 | mise |
| Rust | 1.100.0 | mise |
| pyo3 | 0.30.1 | Cargo |
| Apache Airflow | 3.4 이상 | uv (`uv.lock`) |
