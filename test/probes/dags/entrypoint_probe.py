from airflow.sdk import DAG, task

with DAG("admission_probe", schedule=None, max_active_runs=1) as dag:

    @task
    def work():
        pass

    work()

with DAG("unrelated_probe", schedule=None):

    @task
    def unrelated():
        pass

    unrelated()
