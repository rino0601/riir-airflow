# 현재 아키텍처

riir-airflow의 Python CLI는 기본 ASGI 서버와 선택형 Axum 서버를 제공합니다.
두 방식 모두 PyO3를 통해 Rust 코드를 사용합니다. ASGI 방식에서 Apache Airflow는
별도의 웹 애플리케이션으로 마운트되며 DAG, 메타데이터, 인증, UI와 Airflow API를 관리합니다.

```text
riir-airflow serve → Uvicorn → riir_airflow.asgi:app (Starlette)
                              ├─ /health, /show → PyO3 → Rust
                              ├─ /openapi.json, /docs, /redoc → riir API 문서
                              └─ /airflow/* → Airflow 앱의 라우터 (마운트된 경우)
riir-airflow serve --native → PyO3 → Axum → /health, /show
```

## CLI에서 서버 시작까지

패키지의 `riir-airflow` 명령과 `python -m riir_airflow`는 모두
`riir_airflow.__main__:cli_app`으로 들어갑니다. 이 엔트리 포인트는
PyO3를 통해 Rust의 clap 파서를 호출합니다. 이 파서가
명령과 도움말을 처리하고, Rust에서 읽은 `RIIR_MODE`와 `RIIR_SERVE` 위에
명령줄 옵션을 적용합니다.

- `show`는 선택한 모드를 Rust 함수 `format_show_mode`에 넘기고 반환된 문자열을 표준 출력에 씁니다.
- `serve --native`는 모드와 포트 옵션을 Rust 함수 `serve_native`에 넘깁니다.
  이 함수는 모드를 확인하고 포트를 결정한 뒤 Axum 서버를 시작합니다.
  기본 ASGI 서버는 Rust 함수 `serve_options`에서 Uvicorn 설정을 받습니다.
  `riir-airflow[compat]`가 필요하며, 같은 Python 인터프리터에서
  `python -m uvicorn riir_airflow.asgi:app`을 별도 프로세스로 실행합니다.
  지원하지 않는 모드에는 안내 메시지를 출력하고 서버를 시작하지 않습니다.

| 모드 | `serve`가 넘기는 Uvicorn 옵션 | ASGI 앱에서 같은 모드를 읽었을 때 |
| --- | --- | --- |
| `dev` | 포트 8080, 자동 다시 로드 | riir 라우트만 등록 |
| `prod` | 포트 80, 로그 수준 `info`, 워커 1개 | riir 라우트와 `/airflow` 마운트 등록 |

CLI는 선택한 모드를 Uvicorn 프로세스에 `RIIR_MODE`로 전달합니다.
예를 들어 초기화된 Airflow 환경에서 `riir-airflow --mode prod serve`를
실행하면 포트 80에서 시작하고 `/airflow`를 마운트합니다. 두 방식 모두
`serve --port PORT`로 포트를 지정할 수 있습니다. 환경 변수 목록은
[CLI와 설정](../reference/cli-and-configuration.md)을 참고하세요.

## riir HTTP 요청 처리

Uvicorn은 요청을 ASGI로 전달하고 Python의 Starlette가 경로와 메서드에 따라
핸들러를 호출합니다. Maturin이 빌드하는 `riir_airflow._lowlevel`은
`src/lib.rs`의 PyO3 확장 모듈입니다. Python은 이 모듈을 함수로 호출하고
반환값으로 HTTP 응답을 구성합니다.

| 경로 | 처리 |
| --- | --- |
| `GET /health` | Rust가 JSON 본문 `{"status":"healthy"}`, 상태 코드 200, 미디어 타입을 반환하고 Starlette가 응답을 보냅니다. 이 응답은 Airflow나 DB 상태를 조회하지 않습니다. |
| `GET /show` | Python이 요청의 상태 딕셔너리를 넘기고, Rust가 Python의 `pprint.pformat`을 호출한 뒤 결과를 `out`에 담습니다. Starlette가 이를 JSON으로 직렬화합니다. |
| `/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc` | Python이 패키지에 들어 있는 스키마와 API 문서 HTML을 제공합니다. 스키마와 Swagger UI·ReDoc 페이지는 ASGI의 `root_path`를 반영합니다. |

Axum은 루프백 주소에서 HTTP/1 요청을 받아 `/health`, `/show`와 오류 응답을
직접 처리합니다. 네이티브 요청에는 애플리케이션 상태가 없으므로 `/show`는
Rust에서 빈 딕셔너리의 표현인 `{}`를 `out`에 담습니다.
Airflow 마운트, API 문서, 자동 리로드는 ASGI 방식에서 제공됩니다.

## Airflow 초기화와 하위 라우팅

`riir_airflow.asgi`를 가져올 때 Rust의 `airflow_mount_path`가 마운트 경로를
결정합니다. `dev`이면 마운트를 생략하고 그 외 값이면 `/airflow`를 반환합니다.
이 초기화 경로에서는 `AIRFLOW_HOME/standalone_admin_password.txt`의 존재를 확인합니다.
파일이 없으면 Airflow의 DB 초기화와 FAB 관리자 생성 루틴을 호출한 뒤 `cached_app()`으로
Airflow 앱을 얻습니다.

Airflow 3의 FastAPI 앱은 Starlette 기반이므로 그대로 마운트합니다. 초기화 코드에는
이전 Airflow의 Flask 앱을 불러오는 경로도 있으며, 이 WSGI 앱은
`a2wsgi.WSGIMiddleware`로 감싸 ASGI로 연결합니다.

Starlette의 마운트는 일치한 `/airflow` 접두사를 ASGI `root_path`에 더해 하위 앱으로
요청을 전달합니다. 하위 라우터는 마운트 기준 경로를 처리합니다. 예를 들어
`/airflow/api/v2/dags`는 Airflow의 `/api/v2/dags` 라우트로 전달됩니다.
Airflow는 자체 UI와 API, 그리고 `/execution` 하위 앱의 라우팅을 담당합니다.
인증이나 URL 생성에 사용하는 설정도 Airflow 앱이 관리합니다.

이 서버 시작 경로는 Airflow 웹 앱을 초기화합니다. 스케줄러 프로세스는 따로 시작해야 하며,
저장소의 개발 설정에 지정된 Python `AsgiExecutor`는 Airflow가 선택할 수 있는
실행기입니다. 태스크 명령을 큐에 담고 처리할 때 서브프로세스로 순차 실행합니다.
