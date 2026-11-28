from airflow.sdk import DAG, task

with DAG("admission_probe", schedule=None, max_active_runs=2, max_active_tasks=1):

    @task
    def work():
        pass

    work.override(task_id="low", priority_weight=10)()
    work.override(task_id="high", priority_weight=50)()
    work.override(
        task_id="weighted", priority_weight=100, pool="weighted", pool_slots=2
    )()
    work.override(task_id="holder", priority_weight=1, pool="weighted")()
