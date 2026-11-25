# AGENTS.md

## 작업 선택

- 프로젝트의 목적은 Apache Airflow의 기능과 실행 책임을 Rust로 대체해 운영을 단순화하는 것이다. Rust 코드 비중 자체를 목표로 삼지 않는다.
- 기존 이슈의 완료 조건을 직접 충족하는 작업을 우선하고, 착수 전에 이번 변경이 진전시킬 완료 조건을 확인한다.
- 명시적으로 요구되지 않은 CLI, API, 디버그 및 관측 기능은 추가하지 않는다. 핵심 기능 구현에 필요한 내부 도구와 테스트는 허용한다.
- 부수 기능 개선이나 예외 처리보다 핵심 기능 재구현을 우선한다. 핵심 기능 구현을 막는 결함은 먼저 해결한다.
- Python을 Rust로 기계적으로 번역하지 않는다. PyO3를 통한 Python 생태계 재사용은 허용하고, 불필요한 하위 호환성 유지에 시간을 쓰지 않는다.
- PR에는 대체한 Apache Airflow 실행 책임 또는 핵심 구현에 필요한 선행 작업과, 진전시킨 이슈 완료 조건을 명시한다. 테스트 통과만으로 목표 달성을 주장하지 않는다.

## 포맷·린트·테스트·커밋

이 저장소에서 코드를 수정할 때는 prek 검사를 마친 뒤 커밋한다.

- 훅이 없으면 먼저 `mise run setup`으로 dev 의존성을 동기화하고 git 커밋 훅을 설치한다.
- 코드 수정 후 `mise run format`으로 Python과 Rust를 포맷하고 `mise run check`를 실행한다. 자동 수정할 수 있는 문제는 포매터와 린터로 먼저 해결한다.
- `mise run check`는 prek 설정 검사 → 포맷/린트 → 테스트 → 문서 빌드 순서로 돌고, 하나라도 실패하면 즉시 중단된다.
- 커밋 훅이 포맷을 수정하면 커밋이 중단된다. 수정 내용을 확인하고 다시 스테이징한 뒤 커밋한다.
- 훅을 직접 돌릴 때는 `uv run prek run --all-files`, 설정만 검사할 때는 `uv run prek validate-config prek.toml`을 쓴다.

mise 설정은 `mise/config.toml`, 태스크는 `mise/tasks/` 아래 실행 파일이다. shim이 `uv run prek` 런처를 쓰는 이유는 [git 훅과 `--hook-command`](docs/explanation/hook-command.md)에 있다. Python은 uv가, 문서 사이트는 Zensical이 관리한다.
