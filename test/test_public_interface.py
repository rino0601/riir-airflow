"""Airflow 공개 인터페이스의 겉모습만 검증하는 계약 테스트."""

import inspect

import pytest

airflow = pytest.importorskip("airflow", reason="Apache Airflow가 필요합니다.")

import airflow.sdk as airflow_sdk  # noqa: E402
from riir_airflow import decorators as riir_decorators  # noqa: E402

from ._compat import BASELINE_AIRFLOW_VERSION, load_simple_dag, task_graph  # noqa: E402

_PUBLIC_DECORATORS = (
    "dag",
    "task",
    "task_group",
    "chain",
    "cross_downstream",
    "get_current_context",
)


def test_airflow_baseline_version():
    assert airflow.__version__ == BASELINE_AIRFLOW_VERSION, (
        f"호환성 기준 버전({BASELINE_AIRFLOW_VERSION})이 바뀌었습니다. "
        "버전을 올릴 때는 코퍼스와 기대값을 함께 검토하세요."
    )


@pytest.mark.parametrize("name", _PUBLIC_DECORATORS)
def test_decorator_is_reexported(name):
    assert hasattr(riir_decorators, name)
    assert name in riir_decorators.__all__
    assert callable(getattr(riir_decorators, name))


@pytest.mark.parametrize("name", _PUBLIC_DECORATORS)
def test_decorator_signature_matches(name):
    assert inspect.signature(getattr(riir_decorators, name)) == inspect.signature(
        getattr(airflow_sdk, name)
    )


def test_dag_decorator_exposes_public_entrypoint():
    parameters = inspect.signature(riir_decorators.dag).parameters
    assert parameters["dag_id_or_func"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def test_task_decorator_is_callable_collection():
    assert callable(riir_decorators.task)
    assert inspect.signature(riir_decorators.task) == inspect.signature(
        airflow_sdk.task
    )


def test_simple_dag_parses_with_riir_imports():
    dag = load_simple_dag(use_riir=True)

    assert dag.dag_id == "tutorial_taskflow_api"
    assert set(task_graph(dag)) == {"extract", "transform", "load"}
    assert "example" in dag.tags
    assert dag.start_date is not None


def test_simple_dag_exposes_public_task_properties():
    dag = load_simple_dag(use_riir=True)

    assert dag.get_task("extract").downstream_task_ids == {"transform"}
    assert dag.get_task("transform").upstream_task_ids == {"extract"}
    assert dag.get_task("load").upstream_task_ids == {"transform"}
