# 호환성 테스트 실행하기

Airflow 공개 인터페이스 호환성을 확인하는 테스트는 세 종류입니다. 기준 Airflow 버전은 **3.4.0**이고, 설치된 Airflow가 없으면 코퍼스 테스트는 skip됩니다.

## 전체 테스트

```sh
mise run test
# 또는
uv run pytest -q
```

기본 실행은 공개 인터페이스 계약 테스트와 example DAG 파싱 코퍼스를 포함합니다. `differential` 마커가 붙은 차분 테스트는 여기서 제외됩니다.

## 차분 테스트만 실행하기

```sh
mise run test-differential
```

이 명령은 `uv run pytest -q -m differential`과 같습니다. `dags/simple.py`를 apache-airflow와 riir-airflow 양쪽에서 읽어 taskflow 결과, 의존 그래프, DAG 직렬화 결과를 비교합니다.

## 개별 실행 예시

```sh
uv run pytest -q test/test_public_interface.py
uv run pytest -q test/test_example_dags.py
uv run pytest -q test/test_differential.py -m differential
```

`test/test_example_dags.py`는 `airflow.__file__` 기준으로 `example_dags`를 찾으므로 Airflow 설치 경로를 옮겨도 동작합니다.

## 성공 확인

- `test_public_interface.py`: 데코레이터 심볼과 `inspect.signature`가 Airflow 3.4.0과 같고, `dags/simple.py`의 import를 `riir_airflow`로 바꿔도 DAG가 만들어집니다.
- `test_example_dags.py`: 3.4.0 wheel의 Python 파일 60개를 파싱하고, 공개 import를 riir shim으로 바꾼 복사본이 Apache 쪽과 같은 DAG 집합을 등록합니다.
- `test_differential.py`: 양쪽의 최종 합계(1236.70), 출력 줄, task id·의존성, 직렬화 결과가 일치합니다.

테스트 설계 이유는 [호환성 테스트 설계](../explanation/compatibility-testing.md)에 있습니다.
