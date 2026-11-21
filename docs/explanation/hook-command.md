# 왜 git 훅에 `--hook-command`를 쓰는가

prek이 git shim을 만들 때 기본으로 넣는 것은 실행 파일의 절대경로입니다. 그 경로는 설치 방식과 위치에 따라 바뀌므로, 시간이 지나면 훅이 가리키는 파일이 사라집니다. `--hook-command`는 대신 런처 명령을 적어 이 경로 의존성을 없앱니다.

## 버전이 바뀌면 경로가 달라진다

mise가 관리하는 uv는 버전별 디렉터리에 들어 있습니다. uv가 올라가면 `uv run prek`가 이용하는 실행 파일의 경로도 함께 바뀝니다. 기존 훅에 적힌 옛 경로는 더 이상 존재하지 않으므로 커밋할 때마다 실행에 실패합니다. 경로에 버전이 박혀 있는 한 업그레이드가 곧 훅 수리를 뜻합니다.

## worktree마다 `.venv` 경로가 다르다

uv가 만든 `.venv`는 저장소 안에 있으며 worktree마다 따로 생깁니다. 메인 워크트리의 `.venv`를 가리키는 shim은 새 worktree에서는 엉뚱한 환경을 잡고, 새 worktree에서 만든 shim은 메인 쪽에 다시 옮겨 쓰기 어렵습니다. shim 하나의 절대경로로 여러 worktree를 동시에 만족시킬 수 없는 것입니다.

## 런처 명령은 그때마다 경로를 다시 계산한다

`--hook-command 'uv run prek'`는 훅에 `exec uv run prek hook-impl ...`를 적습니다. 훅이 실행되는 순간 셸이 `uv`를 PATH에서 찾고, uv가 현재 프로젝트의 `.venv`에서 prek을 찾습니다. 버전이 바뀌거나 worktree가 바뀌어도 그 시점의 올바른 실행 파일을 다시 계산하므로, 훅을 다시 설치하지 않아도 따라옵니다.

`mise run setup`에 `--force`가 붙어 있는 이유도 여기에 있습니다. 이미 있는 훅을 같은 런처 문자열로 덮어써야 예전에 깨진 절대경로 shim이 남지 않습니다. 설치 절차는 [기여하기](../how-to/contributing.md)에 있습니다.

## 여전히 필요한 조건

이 방식은 `uv`가 PATH에 있어야 동작합니다. worktree에서 mise 설정이 신뢰되지 않은 상태라면 mise가 uv를 PATH에 넣지 못하므로 훅이 여전히 실패할 수 있습니다. `--allow-missing-config`는 설정 파일이 없는 worktree에서도 커밋이 막히지 않게 하는 안전장치이며, 이 저장소에는 `prek.toml`이 있어 같은 shim이 정의된 검사를 실제로 실행합니다. 도구 버전과 경로 값은 [CLI와 설정](../reference/cli-and-configuration.md)에 정리되어 있습니다.
