from riir_airflow._core.compat_app import create_app
from riir_airflow.configuration import settings

app = create_app(settings.mode)
