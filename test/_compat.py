"""호환성 테스트가 공유하는 DAG 로더와 관측 도구.

Airflow 코어를 교체하기 전 단계에서 ``DAG.test()``는 메타스토어와 executor가 필요하다.
여기서는 공개 TaskFlow 객체(``python_callable``, ``op_args``, 의존 그래프)만 읽어
같은 DAG의 계산 결과를 메타스토어 없이 로컬에서 재현한다. 즉 내부 구현이 아니라
공개 인터페이스만 관측한다.
"""

from __future__ import annotations

import io
import re
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import airflow
from airflow.sdk import DAG, XComArg

BASELINE_AIRFLOW_VERSION = "3.4.0"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIMPLE_DAG_PATH = PROJECT_ROOT / "dags" / "simple.py"
EXAMPLE_DAGS_PATH = Path(airflow.__file__).resolve().parent / "example_dags"
_AIRFLOW_DECORATORS_IMPORT = "from airflow.decorators import dag, task"
_RIIR_DECORATORS_IMPORT = "from riir_airflow.decorators import dag, task"


def load_simple_dag(*, use_riir: bool) -> DAG:
    """`dags/simple.py`를 읽어 DAG 객체를 반환한다.

    `use_riir`가 참이면 README의 치환 규칙대로 decorators import만
    `riir_airflow`로 바꾼다. 마지막 호출은 결과를 잡아두도록 수정한다.
    """
    source = SIMPLE_DAG_PATH.read_text(encoding="utf-8")
    if _AIRFLOW_DECORATORS_IMPORT not in source:
        raise AssertionError(
            f"{SIMPLE_DAG_PATH}에서 decorators import를 찾지 못했습니다."
        )

    if use_riir:
        source = source.replace(_AIRFLOW_DECORATORS_IMPORT, _RIIR_DECORATORS_IMPORT, 1)
    source, count = re.subn(
        r"(?m)^tutorial_taskflow_api\(\)\s*$",
        "dag_object = tutorial_taskflow_api()",
        source,
    )
    if count != 1:
        raise AssertionError(f"{SIMPLE_DAG_PATH}의 DAG 호출 지점을 찾지 못했습니다.")

    namespace = "riir" if use_riir else "airflow"
    module_name = f"_compat_simple_dag_{namespace}"
    module = types.ModuleType(module_name)
    module.__file__ = str(SIMPLE_DAG_PATH)
    sys.modules[module_name] = module
    try:
        exec(compile(source, str(SIMPLE_DAG_PATH), "exec"), module.__dict__)
        return module.dag_object
    finally:
        sys.modules.pop(module_name, None)


def task_graph(dag: DAG) -> dict[str, dict[str, Any]]:
    """TaskFlow DAG의 공개 그래프 속성을 비교 가능한 형태로 만든다."""
    return {
        task.task_id: {
            "operator": type(task).__name__,
            "upstream": sorted(task.upstream_task_ids),
            "downstream": sorted(task.downstream_task_ids),
            "multiple_outputs": getattr(task, "multiple_outputs", False),
        }
        for task in dag.tasks
    }


def _resolve(value: Any, xcoms: dict[tuple[str, str], Any]) -> Any:
    if isinstance(value, XComArg):
        return xcoms[(value.operator.task_id, value.key)]
    if isinstance(value, dict):
        return {key: _resolve(item, xcoms) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_resolve(item, xcoms) for item in value)
    return value


def _topological_order(dag: DAG) -> list[Any]:
    remaining = {task.task_id: task for task in dag.tasks}
    ordered: list[str] = []
    while remaining:
        ready = sorted(
            task_id
            for task_id, task in remaining.items()
            if task.upstream_task_ids.issubset(ordered)
        )
        if not ready:
            raise AssertionError("DAG에 순환 참조가 있습니다.")
        for task_id in ready:
            ordered.append(task_id)
            remaining.pop(task_id)
    return [dag.get_task(task_id) for task_id in ordered]


def run_inline(dag: DAG) -> tuple[dict[tuple[str, str], Any], list[str]]:
    """TaskFlow 태스크를 위상순으로 실행하고 XCom과 출력 줄을 돌려준다."""
    xcoms: dict[tuple[str, str], Any] = {}
    captured = io.StringIO()
    with redirect_stdout(captured):
        for task in _topological_order(dag):
            python_callable = getattr(task, "python_callable", None)
            if python_callable is None:
                raise AssertionError(f"{task.task_id}는 TaskFlow 태스크가 아닙니다.")
            args = [_resolve(arg, xcoms) for arg in task.op_args]
            kwargs = {
                key: _resolve(value, xcoms) for key, value in task.op_kwargs.items()
            }
            result = python_callable(*args, **kwargs)
            if getattr(task, "multiple_outputs", False):
                xcoms.update(
                    {(task.task_id, key): value for key, value in result.items()}
                )
            else:
                xcoms[(task.task_id, "return_value")] = result
    lines = [
        line for line in captured.getvalue().splitlines() if "Total order value" in line
    ]
    return xcoms, lines
