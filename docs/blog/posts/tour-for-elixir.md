---
date: 2024-10-13
---

# Elixir 체험

의욕이 안나서 다이슨 스피어와 팩토리오를 돌다가 그 둘이 질려서 다시 복귀

로그 남기기 조차 귀찮았던 보름 전에는 mike 설정만 진행하였고, 이전 로그들 다듬는건 더더더 나중의 일이 될 듯 하다.

최근 몇달간 내 머리속에 흥미만 일으키고 실제로 손은 못 대본 elixir 를 오늘 한번은 손을 대보는걸 목표로 한다!

또한 이날은 3b1b 가 manim 튜토리얼을 영상으로 올렸고, 이 또한 내 흥미를 끌었음. 언제 한번 공유할때 써먹으면 멋질듯 함

우선 엉망이 된 git 상태를 좀 다듬고 시작..!

## install 하기

codespace 환경에서 다음을 따라하면 됨.

```sh
sudo apt update
sudo apt install erlang-dev elixir

command -v iex mix elixir 
```

근데 현 시점 elixir 문서는 1.17, OTP 24 이나... codespace 에서 별다른 조치 안하고 설치한 apt 설치로는 

1.9 에 OTP 22 로 설치된다. 너무 낮아서 가이드 따라하는데 걸림돌이 생길 듯 하다.

https://elixir-lang.org/install.html 에 다양한 옵션이 있음. pyenv 포지션에 해당하는건 asdf 인듯함

nodeenv 같은걸 만들 수도 있겠음. 아직 없는 듯 함.

pyo3 처럼 섞어 쓰기 괜찮은 궁함이라 생각된다면, 내가 프로젝트를 만들어 보는건 어떨까?

asdf 선호하진 않지만, 일단 체험을 위해 asdf 를 설치해보자.

asdf 설치는 단순 git clone 을 정해진 위치에 놓는것으로 끝나는 듯 하다.
일단 bashrc 에 추가하진 않았음. 나는 이 셋업이 아직 맘에 들지 않으므로...!

```sh
git clone https://github.com/asdf-vm/asdf.git ~/.asdf --branch v0.14.1
source "$HOME/.asdf/asdf.sh"

asdf plugin add erlang https://github.com/asdf-vm/asdf-erlang.git
asdf plugin-add elixir https://github.com/asdf-vm/asdf-elixir.git

asdf install erlang latest
asdf install elixir latest
asdf local erlang latest  
asdf local elixir latest  
asdf current
```

erlang 설치가 생각보다 쉽지 않은 듯 함? 일단 무시하고 가이드를 따라 해본다.

```sh
Crash dump is being written to: erl_crash.dump...done
riir-airflow@rino0601 ➜ /workspaces/riir-airflow (tour-elixir) $ asdf exec elixir -v
{"init terminating in do_boot",{undef,[{elixir,start_cli,[],[]},{init,start_em,1,[]},{init,do_boot,3,[]}]}}
init terminating in do_boot ({undef,[{elixir,start_cli,[],[]},{init,start_em,1,[]},{init,do_boot,3,[]}]})

Crash dump is being written to: erl_crash.dump...done
riir-airflow@rino0601 ➜ /workspaces/riir-airflow (tour-elixir) $ asdf install erlang latest
ERROR: 'asdf_27.1.1' is not a kerl-managed Erlang/OTP installation.
ERROR: no build named 'asdf_27.1.1'!
Downloading (from GitHub) Erlang/OTP 27.1.1 to /home/codespace/.asdf/downloads/erlang/27.1.1...
Extracting source code for normal build...
Building (normal) Erlang/OTP 27.1.1 (asdf_27.1.1); please wait...
Initializing (build) log file at /home/codespace/.asdf/plugins/erlang/kerl-home/builds/asdf_27.1.1/otp_build_27.1.1.log.
APPLICATIONS INFORMATION (See: /home/codespace/.asdf/plugins/erlang/kerl-home/builds/asdf_27.1.1/otp_build_27.1.1.log)
 * wx             : No OpenGL headers found, wx will NOT be usable
 * No GLU headers found, wx will NOT be usable
 * wxWidgets was not compiled with --enable-webview or wxWebView developer package is not installed, wxWebView will NOT be available
 *         wxWidgets must be installed on your system.
 *         Please check that wx-config is in path, the directory
 *         where wxWidgets libraries are installed (returned by
 *         'wx-config --libs' or 'wx-config --static --libs' command)
 *         is in LD_LIBRARY_PATH or equivalent variable and
 *         wxWidgets version is 3.0.2 or above.
```

좀 오래 걸릴 뿐 성공은 했다.

```sh
riir-airflow@rino0601 ➜ /workspaces/riir-airflow (tour-elixir) $ asdf current
elixir          1.17.3-otp-27   /workspaces/riir-airflow/.tool-versions
erlang          27.1.1          /workspaces/riir-airflow/.tool-versions
riir-airflow@rino0601 ➜ /workspaces/riir-airflow (tour-elixir) $ asdf exec elixir -v      
Erlang/OTP 27 [erts-15.1.1] [source] [64-bit] [smp:2:2] [ds:2:2:10] [async-threads:1] [jit:ns]

Elixir 1.17.3 (compiled with Erlang/OTP 27)
riir-airflow@rino0601 ➜ /workspaces/riir-airflow (tour-elixir) $ 
```

쉘을 닫고 새로 여니, nodeenv 랑 asdf 랑 궁합이 맞지 않는다는걸 발견했다.

```
nvm is not compatible with the "NPM_CONFIG_PREFIX" environment variable: currently set to "/workspaces/riir-airflow/.venv"
Run `unset NPM_CONFIG_PREFIX` to unset it.
```

쉘을 닫고 새로 여니, 이제야 최신 버젼으로 잡힌다.

## Introduction to Mix

https://hexdocs.pm/elixir/introduction-to-mix.html 를 따라해보았다. elixir 프로젝트는 어떻게 만드는가?

`mix new kv --module KV` 하여 코드를 만들었고, 파일 확장자는 인식 되나 syntax highlight 는 자동으로 되지 않았다. 

관련 vscode 익스텐션을 찾자 -> 제일 위가 쓸만해보이고 믿음직해보여서 설치.

음.. 키보드가 불편해서 뭔가 더 만져보기는 그런데, pip 보단 쓸만하고 cargo 보단 좀 덜한? 느낌의 프로젝트 관리도구로 mix 가 있다고 이해함.

docs 에 해당하는게 hex 인듯 한데, 바로는 되지 않았음. 따로 배워야 하는 듯 함.
조금 둘러보았으나 여전히 잘 안됨. 흠.. 잘 만든 elixir 프로젝트 코드 모델이 필요한데...

흥미가 좀 덜 당겨서 좀 더 재밌어보이는 주제로 잠시 점프할 생각이 들었다.

## phoenix/up_and_running

https://hexdocs.pm/phoenix/up_and_running.html 를 따라해보자.

역시 웹서버를 띄워야 재밌지.

오.. 로컬에 postgresql 이 당연히 떠있을 것이고, sqlite3 를 쓰지 않음. 독특하네?

phoenix 가 꽤 친절한...? 웹 프레임웤크임을 알게 되었다. 백엔드 개발자 입장에서 만든 느낌임.
그러나 더 체험해보려면 postgresql localhost:5432 가 필요하다.

으악

## 배포는 어떻게 하는가?

개발할 때는 erlang 과 elixir 를 직접 설치해야 했다.
배포시에 이 둘을 신경써야 하는가?
머신에 이 둘이 있어야 하는 파일이 생기나?

phoenix 또는 elixir 문서 어딘가를 잘 둘러보면 설명이 되어 있을 것 같긴 하나 오늘은 발견 못 함.

## 오늘의 결론

지금까지의 인상으로는...

phoenix 를 통해서 개발 생산성을 높힐 수 있어보인다. 
한 가지 언어로 프론트와 백엔드 모두 처리할 수 있다.
그럼 node 랑 같은거 아냐? 싶은데, node 는 프론트가 백엔드로 밀고 들어온 것이고, elixir 는 그 반대 같은 느낌..?
백엔드쪽 문제가 운영이나 비용문제에 있어서 더 기여도가 크다고 생각하기 때문에, 이쪽이 훨씬 매력적인듯 하다.

그러나 이런 장점은 '신규 프로젝트에, elixir 능통자가 이미 있다면' 이란 가정이고
기존 시스템을 대체 하는데 에는 딱히 매력 없는 듯 하다. 기존 참여자들 모두 학습 허들을 넘어야 하고 기존 레거시 코드를 '딸깍' 하고 바꿀 순 없기 때문이다.
아직 ffi 어떻게 하는지 이해 못해서 더욱 불가해 보이는 걸 수도 있다.

그러나 여전히 언어 철학은 매력적이고 rust 보다 쉬워보이는건 사실이며, 유용해보이긴 한다.

좀 더 쉬운 주제 없을까?

