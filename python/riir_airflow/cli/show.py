from pydantic import BaseModel
from riir_airflow.configuration import get_settings


class Show(BaseModel):
    def cli_cmd(self) -> None:
        settings = get_settings()
        print(f'show "{settings.mode=}"')
