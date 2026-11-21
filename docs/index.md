# Riir Airflow

Rust와 Python으로 Airflow를 다시 만드는 실험적인 프로젝트입니다. 현재는 매우 초기 단계이며, 문서에 적힌 것보다 하지 않은 것이 훨씬 많습니다.

## 시작하기

처음이라면 [개발 환경 시작하기](tutorials/getting-started.md)를 따라가 보세요. 이미 익숙하다면 [CLI와 설정](reference/cli-and-configuration.md)에서 필요한 값을 찾고, [기여하기](how-to/contributing.md)에서 개발 흐름을 확인할 수 있습니다.

## 예제 DAG

`dags/simple.py`는 TaskFlow API로 extract → transform → load를 연결하는 가장 작은 예제입니다.

```python
import json

import pendulum
from airflow.decorators import dag, task


@dag(
    schedule=None,
    start_date=pendulum.datetime(2021, 1, 1, tz="UTC"),
    catchup=False,
    tags=["example"],
)
def tutorial_taskflow_api():
    @task()
    def extract():
        data_string = '{"1001": 301.27, "1002": 433.21, "1003": 502.22}'
        return json.loads(data_string)

    @task(multiple_outputs=True)
    def transform(order_data_dict: dict):
        return {"total_order_value": sum(order_data_dict.values())}

    @task()
    def load(total_order_value: float):
        print(f"Total order value is: {total_order_value:.2f}")

    order_data = extract()
    order_summary = transform(order_data)
    load(order_summary["total_order_value"])


tutorial_taskflow_api()
```

## 개발 명령

```sh
mise install         # uv와 Rust 툴체인 설치
mise run setup       # 의존성 동기화, .env 생성, prek 훅 설치
mise run check       # prek 설정 검사, 포맷/린트, 테스트, 문서 빌드
cargo check          # Rust 확장 모듈 확인
uv run pytest        # Python 테스트
```

문서는 [Diátaxis 4분류 개요](explanation/diataxis.md)에 따라 튜토리얼, 실전 가이드, 레퍼런스, 설명으로 나뉩니다. 과거 작업 기록은 [블로그](blog/index.md)에 있습니다.
