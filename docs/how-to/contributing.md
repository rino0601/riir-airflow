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
mise run check
```

이 명령은 `prek validate-config`, `ruff format --check`, `ruff check`, `pytest`, `zensical build`를 순서대로 실행합니다. 하나라도 실패하면 커밋하기 전에 원인을 고칩니다.

훅 설정만 따로 확인하려면 다음 명령을 씁니다.

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

git 커밋 훅이 `uv run mise run check`를 자동으로 실행합니다. GitHub에서 PR을 열면 CI가 같은 검사와 문서 빌드를 다시 수행합니다.

원래 기여 안내는 [.github/CONTRIBUTING.md](https://github.com/rino0601/riir-airflow/blob/main/.github/CONTRIBUTING.md)에도 있습니다. 문서 사이트는 Zensical로 빌드되므로 일반 Markdown을 사용하세요.
