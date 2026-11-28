from pathlib import Path

from airflow.sdk import DAG, task


def note_success(context):
    Path("dag_callback").write_text(context["dag_run"].run_id)


with DAG(
    "admission_probe",
    schedule=None,
    max_active_runs=1,
    on_success_callback=note_success,
) as dag:

    @task
    def upstream():
        pass

    @task
    def downstream():
        pass

    upstream() >> downstream()
