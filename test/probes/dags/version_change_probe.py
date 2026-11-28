from airflow.sdk import DAG, task

with DAG("admission_probe", schedule=None, max_active_runs=1) as dag:

    @task
    def upstream():
        pass

    @task
    def downstream():
        pass

    @task
    def extra():
        pass

    upstream() >> downstream() >> extra()
