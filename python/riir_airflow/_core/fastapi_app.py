from fastapi import FastAPI
from fastapi.requests import Request
from riir_airflow._lowlevel import show_state

web_app = FastAPI()


# 아래처럼 함으로서 api 덮어 쓸 수 있음은 확인함.
@web_app.get("/health")
async def h():
    return {"status": "healthy"}


@web_app.get("/show")
async def show(req: Request):
    return show_state(req.state._state)
