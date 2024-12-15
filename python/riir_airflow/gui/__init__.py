from fastapi import FastAPI
from nicegui import ui

from .pages.api_router_example import router
from .pages.class_example import ClassExample
from .pages.function_example import FunctionExample
from .pages.home import HomePage


def init(mount_path: str, fastapi_app: FastAPI) -> None:
    HomePage()
    ClassExample()
    FunctionExample()
    fastapi_app.include_router(router)

    ui.run_with(
        fastapi_app,
        mount_path=mount_path,  # NOTE this can be omitted if you want the paths passed to @ui.page to be at the root
        storage_secret="pick your private secret here",  # NOTE setting a secret is optional but allows for persistent storage per user
    )
