from pydantic import BaseModel


class Show(BaseModel):
    def cli_cmd(self) -> None:
        from riir_airflow.configuration import settings

        print(f'show "{settings.mode=}"')
