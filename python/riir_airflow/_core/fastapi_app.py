from pprint import pformat

from fastapi import FastAPI
from fastapi.requests import Request

web_app = FastAPI()


# 아래처럼 함으로서 api 덮어 쓸 수 있음은 확인함.
@web_app.get("/health")
async def h():
    return {"status": "healthy"}


@web_app.get("/show")
async def show(req: Request):
    return dict(out=pformat(req.state._state))
