"""Airflow 3 공개 SDK를 그대로 재노출하는 단계 1 shim.

예제 DAG 코퍼스가 ``from airflow.sdk import ...``를 ``from riir_airflow.sdk import ...``로
치환했을 때도 동일한 공개 객체를 받도록 한다. 실제 코어를 교체할 때는 이 모듈과
:mod:`riir_airflow.decorators`부터 구현을 바꾸면 된다.
"""

from airflow.sdk import *  # noqa: F403
