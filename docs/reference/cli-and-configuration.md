# CLI와 설정

## 명령

| 이름 | 엔트리 포인트 | 설명 |
| --- | --- | --- |
| `riir-airflow` | `riir_airflow.__main__:cli_app` | 최상위 CLI입니다. |
| `riir-airflow --version` | Rust CLI 파서 | 설치된 버전을 출력합니다. 기본 패키지만 설치하면 됩니다. |
| `riir-airflow show` | `riir_airflow.cli.show:Show` | 현재 `RIIR_MODE` 값을 출력합니다. |
| `riir-airflow serve` | `riir_airflow.cli.serve:Serve` | `dev` 모드에서는 8080 포트, `prod` 모드에서는 80 포트로 ASGI 앱을 실행합니다. `riir-airflow[compat]` 설치가 필요합니다. |
| `riir-airflow serve --native` | Rust Axum 서버 | `127.0.0.1`에서 `/health`를 제공합니다. 기본 패키지만 설치하면 됩니다. |
| `riir-airflow scheduler-status --database PATH` | Rust SQLite 조회 | 기존 Airflow 3.4 SQLite 메타데이터의 스케줄링 상태별 건수를 JSON으로 출력합니다. |

`scheduler-status`는 단일 호스트의 SQLite 파일 경로를 받아 읽기 전용으로 엽니다.
한 스냅샷에서 `queued` 및 `running` DAG run과 현재 작업 집합에 속한
`scheduled`, `queued`, `running`, `up_for_retry` 태스크 인스턴스를 집계합니다.

최상위 `--mode` 옵션이 `show`와 `serve`가 사용하는 모드를 결정하며, 없으면 `RIIR_MODE`(기본값 `dev`)를 사용합니다.

두 방식 모두 `serve --port PORT`로 포트를 바꿀 수 있습니다. Axum 서버에는
`compat`가 설치되어 있으면 미매칭 HTTP 경로를 같은 프로세스의 호환 ASGI 앱에
전달합니다. 두 서버의 호환 앱은 `dev`와 `prod`에서 riir API 문서를 제공합니다.
`prod`에서는 Airflow UI·API·인증을 `/airflow`에 마운트합니다.
자동 리로드는 Uvicorn의 `dev` 모드에서 제공됩니다. 기본 wheel만 설치한 Axum은
미매칭 경로에 JSON 404를 반환합니다. Axum은 루프백의 단일 HTTP/1 포트를
사용하며 WebSocket은 지원 범위 밖입니다.

설치한 Python 환경에서는 `python -m riir_airflow`로 같은 하위 명령과 옵션을 사용할 수 있습니다.

## 설정

명령줄 옵션, 환경 변수, 기본값 순으로 설정을 적용합니다. 환경 변수 이름과
`RIIR_SERVE`의 JSON 필드 이름은 대소문자를 구분하지 않습니다.

| 환경 변수 | 기본값 | 타입 | 설명 |
| --- | --- | --- | --- |
| `RIIR_MODE` | `dev` | 문자열 | 실행 모드. `dev` 또는 `prod`를 사용합니다. |
| `RIIR_SERVE` | `{}` | JSON 객체 | `serve`의 `native`와 `port`를 지정합니다. 예를 들어 `{"native":true,"port":8081}`을 사용할 수 있습니다. |

`serve --no-native`는 환경 변수의 네이티브 서버 선택을 덮어씁니다.
`--port null` 또는 JSON의 `"port":null`은 모드의 기본 포트를 선택합니다.

Python에서는 `riir_airflow.configuration.Settings()`로 환경 변수에서 모드를
읽고 `Settings(mode="prod")`로 직접 지정할 수 있습니다. CLI가 Python 명령을
실행하는 동안 `get_settings()`는 해석을 마친 모드를 반환합니다.

기본 wheel에는 Python 런타임 의존성이 없습니다. `compat`를 설치하면
Airflow가 사용하는 FastAPI를 통해 `pydantic-settings`가 전이 의존성으로 설치될 수 있습니다.

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
| `mise run check` | prek 설정 검사, `mise run format-check`, Ruff 린트, 일반 및 차분 테스트, Zensical 빌드 |
| `mise run build` | maturin으로 pyo3 확장 모듈 빌드 |
| `mise run format` | Python `ruff format .`, Rust `cargo fmt --all` |
| `mise run format-check` | Python `ruff format --check .`, Rust `cargo fmt --all -- --check` (파일 수정 없음) |
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
