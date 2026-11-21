# 개발 환경 시작하기

이 튜토리얼에서는 riir-airflow를 처음 체크아웃한 상태에서 테스트가 통과하는 것까지 한 번에 확인합니다. 명령은 저장소 루트에서 실행합니다.

## 1. 툴체인 설치

```sh
mise install
```

mise가 `mise/config.toml`에 적힌 uv와 Rust를 설치합니다. Python 3.13은 uv가 직접 설치하므로 별도로 설치하지 않습니다.

## 2. 의존성과 훅 준비

```sh
mise run setup
```

uv가 `uv.lock`을 기준으로 의존성을 동기화하고, `.env`에 Airflow 환경 변수를 기록한 뒤 prek git 훅을 설치합니다.

## 3. 테스트 실행

```sh
uv run pytest
```

`test/test_fastapi.py`의 테스트가 통과하면 준비가 끝났습니다. Airflow 앱 위주로 남은 ASGI 앱이 정상적으로 만들어진 것입니다.

## 4. 전체 검사

```sh
mise run check
```

prek 설정 검사, 포맷/린트, pytest, Zensical 문서 빌드가 순서대로 실행됩니다. `site/`에 문서 사이트가 생성되면 성공입니다.

이제 [기여하기](../how-to/contributing.md)에서 커밋 전 흐름을 확인하세요.
