---
draft: true
date: 2024-12-08
---

# NiceUI

https://nicegui.io/ 란 것을 발견

Elixir 의 Phoenix LiveView 를 부러워 했었는데, python+fastapi 생태계에 이미 비슷한 포지션이 있었다.

체험해보자

## 그전에 코드 정리

지난번 개인 코딩을 언제 했는지 기억이 안나는데, codespace 가 사라진걸로 보아 30일은 넘었다.
다행히 지난번에 CONTRIBUTING.md 를 잘 써놓아서 초기 셋업을 하는데느 3분도 걸리지 않았음.

한달간 배운 바가 있으므로, 이를 적용해보자.

- [x] Elixir 코드 정리
  > codespace 가 날아가서 소스만 정리하면 되네?
- [x] make configure 만들 것
  > python 환경변수 동기화
  > 불필요한 node stack 제거
- [x] mkdocstrings 셋업
  > 이제 이거 없이는 정리가 안되는 기분임...


## nice ui 의 튜토리얼을 따라해보자

- 오 ... filewatch 되어 있네, save 할때마다 ui 변경이 눈에 보임
- 지금 코드의 typer 를 버리고 pydantic-settings 의 기능으로 대체 해야 할 것 같다. 이제 typer 안쓰고 싶음
- 레고 부품을 잔뜩 준비해두고, 알아서 조립하라는 식의 문서. 완성된 테마는 아직 못 봄. (끝까지 보면 뒤에 있을지도?)
- 레고 블록 20개 쯤을 조립하면, 이런 모양 할 수 있다... 같은건 있는데, 2천 피스 완성품 같은건 안보임.
- `ui.run()` 은 uvicorn 설정을 가지고 있다. 그러니까.. server 역할도 가지고 있다.
- 회사 일에서 쓰려면 server 부분을 격리 할 수 있어야 한다. fastapi 예제를 보면 ui.run_with(app) 으로 가능은 해보인다.
- 다만 자세한 문서는 없고 코드를 직접 열어봐야 한다.
- https://nicegui.io/documentation/section_pages_routing#api_responses
- reference 문서를 보려면..? 기능 소개 페이지 안쪽에 see more 등으로 있음.
- 재사용성을 위한 전략은 (extends 매크로 같은) https://github.com/zauberzeug/nicegui/tree/main/examples/modularization/ 를 볼 것.
- 이거 패키지 디펜던시 충돌만 없다면, 회사에서도 쓸 수 있어 보임!

## 내부 api 에 따라 동작하게 바꿔보자

다음 기회에