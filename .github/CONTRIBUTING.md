# Contribution guide

## issue?
> TBD

## commit? PR rule?
> TBD

## Developement Environment

uv 를 이용한 monorepo python 프로젝트이며, rust 바인딩을 사용할 계획이고, task 매니져로서 mise를 사용합니다.

따라서 mise, uv, rustup 을 먼저 설치해야 합니다. 셋 모두 설치법을 찾기 쉬우니 따로 자세히 설명하진 않겠습니다.

- mise: https://mise.jdx.dev/getting-started.html
- uv: https://docs.astral.sh/uv/getting-started/installation/
- rustup https://www.rust-lang.org/tools/install

```sh
mise install
mise run setup
```

`mise run run` 으로 개발 모드로 구동해 볼 수 있습니다.

그외 다른 사용법은 `mise tasks ls` 하면 설명이 기재되어 있습니다.


추천 git 설정
```sh
git config --global push.autoSetupRemote true
git config --global pull.rebase true
git config --global core.editor "vim"
```

### docs

[Zensical](https://zensical.org/) 로 문서 사이트를 만듭니다. 설정은 `zensical.toml`, 원고는 `docs/` 아래에 있습니다.
문서는 [Diátaxis](https://diataxis.fr/) 4분류(튜토리얼, how-to, 레퍼런스, 설명)에 따라 나누고, 한 문서에 한 모드만 적용합니다.
`uv run zensical build` 로 `site/` 에 산출물을 만들고, `uv run zensical serve` 로 미리 볼 수 있습니다.
