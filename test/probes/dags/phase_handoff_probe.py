from airflow.sdk import DAG, task

with DAG("admission_probe", schedule=None, max_active_runs=1) as dag:

    @task
    def upstream():
        pass

    @task
    def downstream():
        pass

    upstream() >> downstream()


def note_success(context):
    pass


with DAG(
    "callback_probe",
    schedule=None,
    max_active_runs=1,
    on_success_callback=note_success,
) as callback_dag:

    @task
    def work():
        pass

    work()
