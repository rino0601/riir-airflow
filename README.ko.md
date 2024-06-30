# riir-airflow

apache-airflow 를 Rust 로 재작성하여, airflow 운영을 단순화 합니다.

하위 호환을 포기하면, 할 수 있는 선택이 있지 않을까? 라는 생각에서 시작한 실험 프로젝트 입니다.

## Goal ( 해결하고자 하는 apache-airflow 의 문제점)

repo 를 여는 시점에, 이 프로젝트는 4가지 목표를 가지고 있습니다.

### (nearly) drop-in-replacement for apache-airflow

- riir-airflow 는 [Public Interface of Airflow](https://airflow.apache.org/docs/apache-airflow/stable/public-airflow-interface.html) 를 준수 해야 합니다. 
- 따라서 airflow 커뮤니티의 puglins, providers 를 계속 사용할 수 있을 것 입니다.

간단히 예를 들자면, 아래와 같은 provider 설치는 유효할 것입니다.

```shell
pip install riir-airflow apache-airflow-providers-apache-flink[cncf.kubernetes]
# apache-airflow-providers-*** 들이 apache-airflow 에 의존성을 가지고 있기에, apache-airflow 와 그 의존성이 설치되는걸 막지는 못 함.
```

기존 DAG 를 그대로 쓸 수 있을 것 입니다.
다만, dag 해석 과정에서 apache-airflow 의 작동을 고려해야만 할 것 입니다.

아래과 같이 수정한 DAG 는 유효할 것 입니다.

```diff
# https://airflow.apache.org/docs/apache-airflow/stable/tutorial/taskflow.html 의 예제를 참고하세요.
-from airflow.decorators import dag, task
+from riir_airflow.decorators import dag, task
# 그외 다른 모든 airflow 패키지도 앞에 riir_ 만 붙이면 작동 할 것
# 즉, 찾아 바꾸기로 from airflow -> from riir_airflow 로 바꾸면 될 것
```

위와 같이 수정한 후, 만일 apache-airflow-providers-*** 에 대한 의존이 아예 없다면, apache-airflow 의 동작을 고려할 필요가 없어집니다.
기존 프로젝트에 이런 변경을 기대하긴 어렵고, 새로 시작하는 프로젝트에 의존성을 최소화 하고 싶을때 생각해 볼 수 있겠습니다.

`airflow.cfg` 와 관련 `AIRFLOW__XXX__XXX` 환경 변수 들은 그대로 사용 가능할 것 입니다.
몇몇 항목은 후술할 목표에 의해, 의미없는 설정이 될 수 도 있으나, 그러한 설정이 존재해서 작동에 문제가 되진 않을 것 입니다.

현실적으로 apache-airflow 의 모든 디펜던시를 제외하는게 쉽진 않을 것으로, nearly drop-in-replacement 라 할 수 있겠습니다.

### simplify architecture

production level apache-airflow 는 5 ~ 9 가지 컴포넌트로 이루어져 있습니다.
- metastore (보통 postgresql) + (CeleryExecutor 의 경우) broker (보통 redis), + 성능을 위해 pg bouncer
- webserver + (CeleryExecutor 의 경우) flower
- scheduler + security and isolation 을 위해 dag-processor 를 분리하는 경우도 있음
- worker
- triggerer

이들 모두에게 동일한 형상의 DAG, python dependency 를 배포 하는 것은, 사용자의 책임 입니다.
이게 쉽진 않습니다.

떄문에, 과감하게 단순화 하고자 합니다.

- metastore (postgresql 만 고려)
- broker (webserver, pg bouncer 그리고 아래의 컴포넌트들 discovery )
- DAG as a APP (한 dag.py 만을 위한 별도의 컴포넌트. broker 와 1 to many 관계)

이를 달성하기 위해 처음부터 async 하게끔 작성하고, 제한을 설정 합니다. (한계3) 

### simplify python dependency

apache-airflow 는 library 이자, application 이라서, 디펜던시 관리가 까다롭습니다.
https://github.com/apache/airflow/blob/constraints-2-9/constraints-no-providers-3.12.txt 를 보면 177개 디펜던시가 있습니다.
1st party provider 들의 디펜던시까지 포함하면, https://github.com/apache/airflow/blob/constraints-2-9/constraints-3.12.txt 680개가 넘습니다.

위 목록의 대부분의 디펜던시는 선택적이지만, 설치를 할 것이라면 반드시 특정 버젼이어야 하며, 이를 위해 위 링크와 같은 pip constraints 를 공개하고 있습니다.
문제는 이게 pip 만 가능한 옵션이며, rye, pdm, hatch, poetry, ... 등에서는 호환되는 옵션이 없다는 점 입니다.
또한, 내가 원하는 디펜던시를 고르는데 있어서 방해가 될 여지가 높다는 것이죠. 680 가지나 되니까요.

provider 들의 디펜던시까지는 책임질 수 없지만 (제한3), apache-airflow 자체가 기동하는데 있어서 필요한 기능들을 모두 rust 로 재작성한다면, pip 디펜던시를 없앨 수 있습니다.
그렇게 되면, airflow 는 application 이기도 하지만, python 디펜던시 관리측면에서는 library나 다름없게 됩니다.


### simplify DAG deploy

apache-airflow 의 가장 난해한 점은, 저 많은 component에 동일한 형상의 DAG, python dependency 를 유지 시키는 것이, 사용자의 책임이라는 점 입니다.
또한 실질적으로 dag 를 개발하려면, airflow cluster 에 코드를 올려야만 돌려 볼 수 있습니다.
localhost 에서 테스트를 해보고 싶지만, python syntax 정도나 확인해 볼 수 있을 뿐, jinja2 로 렌더링 되는 내용이 그간의 실행 기록에 따라 달라지는 경우는 테스트가 참 곤란하겠습니다.

또한, DAG 를 삭제하는 과정에 있어서 절차가 존재 합니다. 이를 따르지 않고, 바로 dag.py 파일부터 삭제해버린다면, metastore 에 지울 수 없는 row 가 생기게 됩니다.
(시간이 되서 expire 되기를 간절히 기다려야 하겠습니다.)

헌데, DAG A 가 작동함에 있어서 DAG B 의 코드가 필요하던가요?
심지어 airflow dag.py 코드는 상대경로 import 를 쓸 수도 없습니다. 때문에 DAG B의 코드를 참조하는 DAG A의 코드는 애초에 불가능 합니다.

그렇다면, Pod A 가 DAG A 를 담당하게 한다면 어떨까요? 일이 많이 단순해질것 같지 않나요?
Pod A 가 DAG A 를 전담하기에, 다른 DAG의 디펜던시와 충돌할 걱정이 덜어집니다.
Pod A 가 DAG A 를 전담하기에, DAG A 를 정리하고자 한다면, 그 절차를 pod 의 PreStop 에 끼워넣을 수 있을 것 입니다.
Pod A 가 DAG A 를 전담하기에, DAG A 의 코드가 분산 환경에 전파되는걸 기다릴 필요가 없습니다! 이말은 곧, localhost 도 쓸 수 있다는 얘기가 되겠습니다!

https://airflow.apache.org/docs/apache-airflow/stable/best-practices.html#triggering-dags-after-changes
https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dags.html#dag-pausing-deactivation-and-deletion
https://airflow.apache.org/docs/apache-airflow/stable/administration-and-deployment/modules_management.html#don-t-use-relative-imports

## Not a Goal

다음 사항은 목표가 아닙니다.

- python3.12 미만 지원
- 디펜던시 없는 airflow 가 목표지만, apache-airflow 디펜던시가 설치되는걸 막는건 목표가 아님. (provider 들의 디펜던시를 막을 순 없다.)
- 동일한 web ui 지원 (UI 는 Public Interface of Airflow 가 아니기 때문)
- perfact 한 drop-in-replacement. dag.py 코드를 재사용할 순 있지만, 운영중인 인스턴스를 옮기는 수준 까지는 고려하지 않습니다.
- apache-airflow 2.9 미만에서 deprecated 된 기능들의 지원 (즉, 하위 호환을 챙기진 않음)
- 실험 프로젝트 이기 때문에, product ready (백업 전략이나 HA 같은 것)를 노리진 않습니다. (아직은)
- apache-airflow 본 프로젝트에 contribute. / 너무 급진적인 변경이라 아이디어만 가지고는 설득할 수 없을거라 생각합니다. PoC 정도는 만들어놔야 얘기나 꺼내 볼 수 있을 것이고 PoC 나올때까지 본인의 의욕이 지속 될지도 불분명하기 때문에, 지금은 고려하지 않습니다.

## Why rewrite? Why not contribute it?

Not a Goal 에도 언급했지만, 하위 호환성을 모두 깨는 아이디어가 받아들여질 것이라 생각하지 않습니다.

이 프로젝트는 node 와 deno 의 관계에 비유 할 수 있을 것 같습니다.
deno 가 아무리 성장한다 한들, node 를 대체할 지언정, node 에 병합되진 않을 것 입니다. 그러나 deno 가 고도화 될 수록, node 와의 호환성은 높아지며, node 로 작성한 프로젝트들을 deno 에서 쓸 수 있게 될 겁니다.

Ryan Dahl 이 deno 를 만든 이유를 3가지를 들었다던데요.
- 모듈 시스템 설계에 문제가 있었음. 패키지를 중앙에서 관리함
- 오래된 API를 지원해야 함
- 보안 관리 기능이 없음

제가 airflow 를 다시 작성할 아이디어가 떠오른 이유도 이 3가지와 비슷합니다.
- DAG 시스템 설계에 문제가 있음. 모든 컴포넌트에 DAG 코드가 필요함 이 때문에 ReadWriteMany 가능한 네트워크 스토리지나, git repo 가 필요. 즉 중앙 관리가 됨.
- 오래된 API를 지원해야 함. DAG 를 작성하는 방법이 3가지 스타일이 있는데 (dag=DAG() / with DAG() as ... / @dag ) 이 모두를 고려해야 한다니
- 보안 관리 기능이 없음. 충분히 악의적으로 작성된 dag.py 는 전체 클러스터의 운영을 마비 시킬 수 있음.

deno 비슷한 어떤 것이 될거라 예상하며, 재작성이라서 얻을 수 있는 잇점에 대해 말해보고자 합니다.

### Why? - on apache-airflow side

하위 호환성을 무시 해도 되겠습니다. 1.x 시절 DAG 들을 위해 아직도 남아있는 경고문구들을 싹 날리고 dag 파라미터도 정돈 시킬 수 있겠습니다.
물론 여유가 된다면, 차차 지원해 나갈 수도 있을 것입니다. deno 가 node 호환성을 조금씩 챙기는것과 마찬가지로요.

기존 아키텍쳐에 갇힌 사고를 하지 않아도 됩니다. 우린 리모델링이 아니라 아예 재건축을 하려 하고 있습니다. 기존의 설계를 부수고 처음부터 다시 생각합니다.

이미 나온, 그리고 앞으로 나올 좋은 것들만 골라서 챙길 수 있습니다. 제일 쉽게 예를 들 수 있느 것이 pyproject.toml 입니다. apache-airflow 가 처음 등장할때 이는 없었죠.
python3.13은 아직 안나왔지만, 이 프로젝트가 실험 딱지를 떼는것 보단 빨리나오리라 기대하고, A Per-Interpreter GIL 을 고려해 볼 수도 있을 것 입니다.
이런 신기술을 고려하는데 있어서 방해되는 문제가 없습니다! (애초에, 아무것도 없기 때문에!)
https://peps.python.org/pep-0684/
https://peps.python.org/pep-0554/

하위 호환성 포기와 비슷한 얘기일 수 있는데, 쓸데 없는 기능을 버릴 수 있습니다.
이를테면 daemon 기능을 airflow 가 스스로 가져야 할까요? 어차피 dockerlize 하면 daemon 일 이유 없지 않을까요? airflow 스스로는 스케쥴링에 집중하는게 옳겠습니다.

### Why? - on maintainer's side

저는 2017년 첫 입사 후, 지금(2024년 5월)까지 본업에서 apache-airflow 를 쓰고 있습니다.
오래된 기업이 그러하듯, 늘 끔찍한 레거시와 싸우는게, 재밌는 새 도구 도입하는 것보다 훨씬 일의 양이 많기 때문에... airflow 를 7년이나 써왔지만, airflow 의 모든 기능에 대해 정통하진 않습니다.

그럴 이유가, 1.7.1.3 (세상에 이제 pypi 에서 조회 조차 안되네요 / timezone 기능이 없고 utc 아닌 시스템 time 을 그대로 따르는 마지막 버젼입니다.) 버젼에서 너무 오래 있었고, 1.10.15로 단숨에 점프시키는데 3년 가까운 시간을 썼습니다. 이 과정에서 airflow 1.x 의 코드는 전부 읽고 이해 하는 과정이 필요 했습니다. `airflow scheduler` 명령이 시작하는 순간부터 워커에서 작업이 완료되는 순간까지 모든 부분의 코드를 찾아내는데 도가 텄었죠. 이 때의 저는 airflow 1.10.15 를 완전히 이해 했다고 자신합니다. 

airflow 2.x 로의 마이그레이션도 1.7 에서 1.10 가는 것 만큼이나 어려웠습니다. 
1.7에서 1.10으로 넘길때와 다르게, 회사에서는 저를 더이상 주니어로 취급하지 않았고, 주어진 시간은 촉박했습니다.
airflow 2.x 의 핵심적인 변경이 무엇인지 이해할 시간은 주어지지 않았고, 마이그레이션 가이드를 안전하게 서비스 인스턴스에 적용하는데 집중하게 됩니다.
저 혼자 회사에서 쓰는 수십개의 1.10.15 인스턴스들을 안전하게 2.3.4 를 거쳐 2.4.3 으로 올리는데 고군 분투 하는 사이에, apache-airflow 는 오픈소스의 힘=다수의 협력을 통해 2.9.1 까지 진행 했습니다. (그 사이 -운좋게 KPI가 그렇게 설정되었기에- 컨트리뷰트도 2번쯤인가 했죠. 모두 timezone 관련입니다. 그런 레거시와 싸울 사람만 겪을 수 있는 불편이었던 거죠.)
때문에 제 지식은 2.4 버젼에 머물러 있습니다. 
그리고 이전과 달리, airflow 가 스케쥴링 하는 과정을 이해한다 자신할 수 없습니다. HA 가능해진 스케쥴러들이 어떻게 그게 가능해졌고, 어떻게 서로 충돌을 피하는지에 대해 자신있게 답하지 못합니다. (postgresql가 제공 하는 lock 에 의존하나? 정도로 이해하고 있습니다만), 스케쥴러 에러로그에서 나타난 문장을 보고 어떤 옵션을 조절해야겠다 판단 할 수 없습니다.
회사에서 시키는 일만 하다가는 제논의 역설마냥 영원히 최신버젼 airflow 에 정통하지 못한 사람이 될 겁니다.
토이 프로젝트는 내 마음대로 내 여가시간에 하는 거니, 내가 마음껏 airflow 2.x 의 스케쥴러 코드를 보는데 시간을 써도 뭐라할 사람이 없을겁니다.

경력 8년차, airflow 잘 모름 딱지를 가지고 싶지 않습니다.
경력 10년차. airflow 존나 잘 알음 딱지를 가지고 싶습니다.

그리고 어쩌다 간혹 무슨일 하세요? 란 질문에
airflow 운영해요- 라고 답하면, airflow DAG 작성하시는 구나. 라고 이해 하는데 너무 싫습니다.
airflow 를 만들었어요 라고 할 수 있는 사람이 되면 멋질 것 같습니다.

그리고 다른 관점에서, 이 프로젝트를 하면서, asyncio 를 극한까지 다뤄보고 싶으며, pyo3를 통한 rust 프로그래밍도 연습해보고자 하는 목적도 있습니다.

### Why not [spotify/luigi](https://github.com/spotify/luigi)?

한 dag.py 만을 위한 별도의 컴포넌트 라는 문구를 보고 luigi 를 아는 사람이라면 luigi를 떠올렸을 수도 있을 것 같습니다.
제가 luigi 를 직접 써보진 않았지만, 문서를 훑고, quickstart 정도는 해봤으며, 그러한 면에서 이 아이디어가 luigi에서 영감을 받았다고 할 수 있을 것 같습니다.

그러나 luigi 는 airflow 가 아니기에 airflow의 생태계를 이용할 수 없습니다.
제 목표는 레거시 dag.py 코드는 살리는 것이기 때문에 (인스턴스는 버려도 됨) luigi는 옵션이 되지 않습니다.

## limitation

- apache-airflow-providers-*** 들이 apache-airflow 에 의존성을 가지고 있기에, apache-airflow 와 그 의존성이 설치되는걸 막지는 못 함.
- DAG as a APP 을 위해, dag 파일 layout 이 강제될 것 같으며, AIRFLOW__CORE__DAGS_FOLDER 와 AIRFLOW__CORE__EXECUTOR 를 강제 하게 될 것

이러한 제한을 깰 아이디어가 있다면 제안해주세요. 환영합니다.

## Plan

### 1단계 - asyncio-lize interface

우선 손에 익은 FastAPI 를 이용해서 ASGI 기반 app 으로 탈바꿈 시킵니다.

airflow 의 public interface 를 준수하는지 체크하는 테스트 코드를 작성합니다.

이 과정에서 사실상 apache-airflow 의 포장갈이여도 무방합니다.

### 2단계 - rewirte core

python 으로 인터페이스의 처리부를 하나하나 다른 지점으로 옮깁니다.
tiggerer 가 worker 랑 같은 프로세스에서 처리된다던가 하는게 쉬운 첫번째 스텝이 될 것 같습니다.

이 과정에서, 저는 airflow 2.x 의 코드를 모두 살펴보게 될 것 입니다.

### 3단계 - dog fooding

이 때쯤, DAG as a APP 이 가능할 것이라 예상합니다.
실제 DAG 의 요구사항을 여전히 충족하는지 확인하기 위해 사용 사례를 일부러라도 만들어서 운영해봅니다.

### 4단계 - riir core

python 디펜던시 줄이기를 위해 코어 로직을 rust 화 하기 시작합니다.
설정에 사용할 pydantic 의 경우, pydantic core 가 rust 이기 때문에 어떻게든 방법이 있을 것 입니다.

이 과정에서 저는 목표 했던 pyo3 를 이용한 rust 개발에 익숙해지게 될 것 입니다.

여기까지만 진행해도 개인적으로는 성공 입니다.

### 5단계 - riir interface

python 디펜던시를 멸종시키기 위해 interface 또한 rust 화 하기 시작합니다.

FastAPI 가 걱정되는 부분인데, robyn 이란 프로젝트가 이 시점 쯤에 기대하는 수준으로 올라와 있기를 기대 해봅니다. 
https://robyn.tech/documentation/api_reference/future-roadmap

https://github.com/emmett-framework/granian 의 사례도 참고해 볼만 합니다. (crates 로 공개 되어 있진 않지만)

이게 완료되면, 세상과 공유할 만큼 진보 시켰다 할 수 있겠습니다.

### 6단계 - what's next?

이 프로젝트 아이디어를 정리하는 2024년 5월 시점에, 감히 이 너머를 계획할 수는 없습니다.

