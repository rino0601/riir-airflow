# Contribution guide

## issue?
> TBD

## commit? PR rule?
> TBD

## Developement Environment

uv 를 이용한 monorepo python 프로젝트이며, rust 바인딩을 사용할 계획이고, task 매니져로서 make 를 사용합니다.

따라서 make, uv, rustup 을 먼저 설치해야 합니다. 셋 모두 설치법을 찾기 쉬우니 따로 자세히 설명하진 않겠습니다.

- make: 아마 당연히...
- uv: https://docs.astral.sh/uv/getting-started/installation/
- rustup https://www.rust-lang.org/tools/install

make

`make run` 으로 개발 모드로 구동해 볼 수 있습니다. 

그외 다른 사용법은 `uv run make` 하면 설명이 기재되어 있습니다.


추천 git 설정
```sh
git config --global pull.rebase true
git config --global core.editor "vim"
```

### .devcontainer/

> https://containers.dev/ 문서를 보고 가꾸어 나갈 것

