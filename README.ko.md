# riir-airflow

apache-airflow 를 Rust 로 재작성하여, airflow 운영을 단순화 합니다.

하위 호환을 포기하면, 할 수 있는 선택이 있지 않을까? 라는 생각에서 시작한 실험 프로젝트 입니다.

## Goal ( 해결하고자 하는 apache-airflow 의 문제점)

repo 를 여는 시점에, 이 프로젝트는 4가지 목표를 가지고 있습니다.

### (nearly) drop-in-replacement for apache-airflow

- riir-airflow 는 [Public Interface of Airflow](https://airflow.apache.org/docs/apache-airflow/stable/public-airflow-interface.html) 를 준수 해야 합니다. 
- 따라서 airflow 커뮤니티의 puglins, providers 를 계속 사용할 수 있을 것 입니다.
- 패키지명 까지 일치 시킬 수는 없기 때문에, DAG 에서 import 경로는 고쳐주어야 할 것 입니다. (한계1)

간단히 예를 들자면, 아래와 같은 provider 설치는 유효할 것입니다.

```shell
pip install riir-airflow apache-airflow-providers-apache-flink[cncf.kubernetes]
# 한계2. apache-airflow-providers-*** 들이 apache-airflow 에 의존성을 가지고 있기에, apache-airflow 와 그 의존성이 설치되는걸 막지는 못 함.
```

아래과 같이 수정한 DAG 는 유효할 것 입니다.

```diff
# https://airflow.apache.org/docs/apache-airflow/stable/tutorial/taskflow.html 의 예제를 참고하세요.
-from airflow.decorators import dag, task
+from riir_airflow.decorators import dag, task
# 그외 다른 모든 airflow 패키지도 앞에 riir_ 만 붙이면 작동 할 것
# 즉, 찾아 바꾸기로 from airflow -> from riir_airflow 로 바꾸면 될 것
```

`airflow.cfg` 와 관련 `AIRFLOW__XXX__XXX` 환경 변수 들은 그대로 사용 가능할 것 입니다.
몇몇 항목은 후술할 목표에 의해, 의미없는 설정이 될 수 도 있으나, 그러한 설정이 존재해서 작동에 문제가 되진 않을 것 입니다.

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
- riir_airflow (metastore 를 제외한 나머지)

이를 달성하기 위해 처음부터 async 하게끔 작성하고, 제한을 설정 합니다. 

(가령 dag 1개만 가능 이라던지. -대신 metastore 에 여러개의 riir_airflow 가 붙을 수 있음-)

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
- 동일한 web ui 지원 (UI 는 Public Interface of Airflow 가 아니기 때문)
- perfact 한 drop-in-replacement. dag.py 코드를 재사용할 순 있지만, 운영중인 인스턴스를 옮기는 수준 까지는 고려하지 않습니다.
- apache-airflow 2.9 미만에서 deprecated 된 기능들의 지원 (즉, 하위 호환을 챙기진 않음)
- 실험 프로젝트 이기 때문에, product ready (백업 전략이나 HA 같은 것)를 노리진 않습니다. (아직은)
- apache-airflow 본 프로젝트에 contribute. / 너무 급진적인 

## Why rewrite? 

### Why not contribute it?

### Why? - on apache-airflow side

### Why? - on maintainer's side

### Why not [spotify/luigi](https://github.com/spotify/luigi)?


## limitation


## Plan

### 1단계 - asyncio-lize

### 2단계 - riir
