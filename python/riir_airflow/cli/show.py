from riir_airflow._lowlevel import format_show_mode
from riir_airflow.configuration import get_settings


class Show:
    def cli_cmd(self) -> None:
        settings = get_settings()
        print(format_show_mode(settings.mode))
