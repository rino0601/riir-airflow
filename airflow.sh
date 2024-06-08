export \
    AIRFLOW__LOGGING__LOGGING_LEVEL=INFO \
    AIRFLOW__CORE__EXECUTOR=riir_airflow.executors.asgi_executor.AsgiExecutor
riir-airflow standalone