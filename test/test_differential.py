"""같은 DAG를 apache-airflow/riir-airflow 양쪽에서 비교하는 차분 테스트.

기본 ``pytest``에서는 제외되고 ``mise run test-differential``로 따로 실행한다.
"""

import pytest

airflow = pytest.importorskip("airflow", reason="Apache Airflow가 필요합니다.")

from airflow.serialization.serialized_objects import DagSerialization  # noqa: E402

from ._compat import load_simple_dag, run_inline, task_graph  # noqa: E402

pytestmark = pytest.mark.differential


@pytest.fixture(scope="module")
def dag_pair():
    return load_simple_dag(use_riir=False), load_simple_dag(use_riir=True)


def test_taskflow_result_matches(dag_pair):
    apache_xcoms, apache_lines = run_inline(dag_pair[0])
    riir_xcoms, riir_lines = run_inline(dag_pair[1])

    assert apache_xcoms == riir_xcoms
    assert apache_xcoms[("transform", "total_order_value")] == pytest.approx(1236.70)
    assert apache_lines == riir_lines == ["Total order value is: 1236.70"]


def test_task_graph_matches(dag_pair):
    expected = {
        "extract": {
            "operator": "_PythonDecoratedOperator",
            "upstream": [],
            "downstream": ["transform"],
            "multiple_outputs": False,
        },
        "transform": {
            "operator": "_PythonDecoratedOperator",
            "upstream": ["extract"],
            "downstream": ["load"],
            "multiple_outputs": True,
        },
        "load": {
            "operator": "_PythonDecoratedOperator",
            "upstream": ["transform"],
            "downstream": [],
            "multiple_outputs": False,
        },
    }

    assert task_graph(dag_pair[0]) == task_graph(dag_pair[1])
    assert task_graph(dag_pair[1]) == expected


def test_serialized_dag_matches(dag_pair):
    apache_serialized = DagSerialization.to_dict(dag_pair[0])
    riir_serialized = DagSerialization.to_dict(dag_pair[1])

    assert apache_serialized == riir_serialized
    serialized_task_ids = {
        payload["task_id"]
        for encoded in riir_serialized["dag"]["tasks"]
        for payload in encoded.values()
        if isinstance(payload, dict) and "task_id" in payload
    }
    assert serialized_task_ids == {"extract", "transform", "load"}
