import airflow.decorators
import riir_airflow.decorators as riir_decorators
from riir_airflow.decorators import dag, task


def test_dag_is_reexported():
    assert dag is airflow.decorators.dag
    assert callable(dag)


def test_task_is_reexported():
    assert task is airflow.decorators.task
    assert callable(task)


def test_all():
    assert riir_decorators.__all__ == ["dag", "task"]
