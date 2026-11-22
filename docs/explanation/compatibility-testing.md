# 호환성 테스트 설계

이 프로젝트의 1단계 목표는 "Airflow의 공개 인터페이스를 준수하는지 확인하는 테스트"입니다. Apache Airflow의 테스트 스위트를 그대로 가져오지 않고 세 가지 관측 지점을 직접 만들었습니다. 기준 버전은 Airflow **3.4.0**입니다.

## 세 종류의 테스트

**공개 인터페이스 계약 테스트**는 `airflow.sdk`의 데코레이터와 `riir_airflow.decorators`를 `inspect.signature` 수준에서 비교합니다. `dags/simple.py`의 `from airflow.decorators`를 `from riir_airflow.decorators`로 치환해도 import와 파싱이 되는지, DAG와 task의 공개 속성이 유지되는지 봅니다. 내부 구현이 같은지는 묻지 않습니다.

**차분 테스트**는 같은 DAG를 apache-airflow와 riir-airflow 양쪽에서 읽어 최종 결과, task id와 의존 그래프, DAG 직렬화 결과를 비교합니다. 지금은 얇은 shim 덕분에 결과가 같지만, 나중에 Rust 코어로 교체할 때 이 테스트가 회귀를 잡는 안전망이 됩니다. DAG 실행과 직렬화가 무거워 `differential` 마커로 분리하고 기본 `mise run check`/`pytest`에서는 제외했습니다. 별도 명령은 [호환성 테스트 실행하기](../how-to/compatibility-testing.md)에 있습니다.

**예제 DAG 파싱 코퍼스**는 설치된 Airflow의 `example_dags` Python 파일 60개를 입력으로 씁니다. 경로는 `airflow.__file__`에서 유도하고, 각 파일의 공개 import를 riir shim으로 바꾼 복사본을 Airflow의 `DagBag`으로 파싱해 Apache 쪽과 같은 DAG 집합을 등록하는지 확인합니다.

## Airflow 자체 테스트를 쓰지 않는 이유

Apache Airflow 소스 저장소는 약 825 MB이고, 설치된 3.4.0 wheel에는 `tests/`가 없고 `example_dags/`만 들어 있습니다. 전체 테스트를 vendor링하거나 서브모듈로 넣는 것은 이 저장소의 목적에 맞지 않습니다.

Airflow 3.x 테스트는 `airflow-core/tests`, `task-sdk/tests` 등에 흩어져 있으며 내부 DB 세션, executor, provider fixture에 강하게 결합돼 있습니다. 그런 테스트를 그대로 통과시키려면 내부 구조까지 Airflow와 동일하게 유지해야 하는데, 이는 이 프로젝트의 목표인 내부 단순화와 Rust화에 정면으로 충돌합니다. 그래서 공개 경계에서 관측하는 테스트만 유지합니다.

## shim을 택한 이유

`riir_airflow.decorators`가 아직 없을 때 xfail로 남기는 방법도 있었지만, 그러면 실제로 지원해야 할 심볼이 사라져도 테스트가 조용히 통과합니다. README 1단계가 "사실상 apache-airflow의 포장갑이어도 무방"이라고 명시하므로, 공개 객체를 재노출하는 얇은 shim을 추가해 테스트를 항상 초록으로 유지했습니다. 코어를 교체할 때는 shim의 구현만 바꾸고 계약 테스트는 그대로 회귀 검증에 사용할 수 있습니다.
