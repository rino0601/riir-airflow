"""Airflow 데코레이터 공개 인터페이스를 재노출하는 단계 1 shim.

Airflow 3.4에서 ``airflow.decorators``는 ``airflow.sdk``로 위임하는 경로이므로,
여기서는 같은 공개 객체를 직접 가져와 노출한다. 내부 구현을 복제하지 않고
``from airflow.decorators import dag, task``를 ``from riir_airflow.decorators ...``로
바꾼 DAG가 그대로 동작하는지 검증하기 위한 얇은 포장이다.
"""

from airflow.sdk import (
    chain,
    cross_downstream,
    dag,
    get_current_context,
    task,
    task_group,
)

__all__ = [
    "chain",
    "cross_downstream",
    "dag",
    "get_current_context",
    "task",
    "task_group",
]
