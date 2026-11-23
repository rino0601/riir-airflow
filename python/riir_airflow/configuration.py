from contextvars import ContextVar
from dataclasses import dataclass, field

from riir_airflow._lowlevel import settings_mode


@dataclass
class Settings:
    mode: str = field(default_factory=settings_mode)

    def __post_init__(self) -> None:
        if not isinstance(self.mode, str):
            raise TypeError("mode must be a string")

    def model_dump(self) -> dict[str, str]:
        return {"mode": self.mode}


settings = Settings()

active_settings: ContextVar[Settings | None] = ContextVar(
    "active_settings", default=None
)


def get_settings() -> Settings:
    return active_settings.get() or settings
