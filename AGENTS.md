# AGENTS.md

이 저장소에서 코드를 수정할 때는 prek 검사를 마친 뒤 커밋한다.

- 훅이 없으면 먼저 `mise run setup`으로 dev 의존성을 동기화하고 git 커밋 훅을 설치한다.
- 커밋 전에 `mise run check`를 실행한다. prek 설정 검사 → 포맷/린트 → 테스트 → 문서 빌드 순서로 돌고, 하나라도 실패하면 즉시 중단된다.
- 훅을 직접 돌릴 때는 `uv run prek run --all-files`, 설정만 검사할 때는 `uv run prek validate-config prek.toml`을 쓴다.

mise 설정은 `mise/config.toml`, 태스크는 `mise/tasks/` 아래 실행 파일이다. shim이 `uv run prek` 런처를 쓰는 이유는 [git 훅과 `--hook-command`](docs/explanation/hook-command.md)에 있다. Python은 uv가, 문서 사이트는 Zensical이 관리한다.
