from datetime import datetime

from airflow.sdk import DAG, task

with DAG(
    "schedule_probe",
    schedule="@daily",
    start_date=datetime(2026, 1, 1),
    catchup=True,
    max_active_runs=1,
) as dag:

    @task
    def work():
        pass

    work()
