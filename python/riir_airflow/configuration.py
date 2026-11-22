from contextvars import ContextVar

from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RIIR_",
    )

    # Basic settings
    mode: str = "dev"

    # # CLI Subcommands
    # serve: CliSubCommand[Serve]
    # show: CliSubCommand[Show]

    # def cli_cmd(self) -> None:
    #     CliApp.run_subcommand(self)


settings = Settings()

active_settings: ContextVar[Settings | None] = ContextVar(
    "active_settings", default=None
)


def get_settings() -> Settings:
    return active_settings.get() or settings
