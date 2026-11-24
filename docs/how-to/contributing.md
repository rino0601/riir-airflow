# 기여하기

기여하려면 포크한 저장소를 체크아웃한 뒤 개발 환경을 만들고, 커밋 전에 검사를 통과시키면 됩니다. 자세한 배경은 [git 훅과 `--hook-command`](../explanation/hook-command.md)에 있습니다.

## 1. 개발 환경 만들기

```sh
mise install
mise run setup
```

uv가 Python 3.13과 의존성을 관리하고, prek이 git 커밋 훅을 설치합니다.

## 2. 코드 수정 후 검사하기

```sh
mise run format
mise run check
```

`mise run format`은 Ruff로 Python을, rustfmt로 Rust를 포맷합니다. 포맷만 확인하려면 파일을 수정하지 않는 `mise run format-check`를 실행합니다.

`mise run check`는 prek 설정 검사, `mise run format-check`, Ruff 린트, 일반 테스트와 차분 테스트, Zensical 빌드를 순서대로 실행합니다. 하나라도 실패하면 커밋하기 전에 원인을 고칩니다.

훅 설정을 검사하거나 전체 파일에 훅을 직접 실행하려면 다음 명령을 씁니다.

```sh
uv run prek validate-config prek.toml
uv run prek run --all-files
```

## 3. 커밋과 PR

```sh
git add .
git commit -m "설명 있는 커밋 메시지"
git push
```

git 커밋 훅이 `mise run format`을 실행한 뒤 `mise run check`를 실행합니다. 포매터가 파일을 수정하면 커밋이 중단되고 수정 사항은 작업 트리에 남습니다. 변경 내용을 확인하고 해당 파일을 다시 스테이징한 뒤 커밋하세요. GitHub에서 PR을 열면 CI가 `mise run check`로 파일 수정 없이 검사하고 문서를 빌드합니다.

원래 기여 안내는 [.github/CONTRIBUTING.md](https://github.com/rino0601/riir-airflow/blob/main/.github/CONTRIBUTING.md)에도 있습니다. 문서 사이트는 Zensical로 빌드되므로 일반 Markdown을 사용하세요.
