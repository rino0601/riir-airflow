from datetime import timedelta

from airflow.sdk import DAG, task

with DAG(
    "admission_probe",
    schedule=None,
    max_active_runs=1,
    dagrun_timeout=timedelta(seconds=1),
) as dag:

    @task
    def upstream():
        pass

    @task
    def downstream():
        pass

    upstream() >> downstream()
