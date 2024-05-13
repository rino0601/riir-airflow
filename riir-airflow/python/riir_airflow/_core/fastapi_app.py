from datetime import datetime
from fastapi import FastAPI
from fastapi.requests import Request
from airflow.jobs.job import Job

from typing import TypedDict
from fastapi.responses import ORJSONResponse
from airflow.jobs.scheduler_job_runner import SchedulerJobRunner

import asyncio
from contextlib import asynccontextmanager
from random import shuffle


class SchedulerStateDict(TypedDict):
    scheduler_on: bool
    tick: datetime | None
    foo: int | None


class AsyncSchedulerJobRunner(SchedulerJobRunner):
    scheduler_on: bool = True

    def _execute(self) -> int | None:
        return super()._execute()

    def _run_scheduler_loop(self) -> None:
        return super()._run_scheduler_loop()

    async def __aenter__(self):
        """._execute() 의 앞부분 재현"""
        # 시그널 처리는 uvicorn 소관인것 아닐까?
        return asyncio.create_task(self.scheduler_loop())

    async def __aexit__(self, exc_type, exc, tb):
        """._execute() 의 뒷부분 재현"""
        print("DOWN!")

    async def scheduler_loop(self):
        chance = [False, False, False, False, False, False, False, True]
        try:
            while self.scheduler_on:
                print(datetime.now())
                print(chance)
                if chance[0]:
                    print("Bye!")
                    self.scheduler_on = False
                shuffle(chance)
                await asyncio.sleep(1)
        except Exception:
            self.scheduler_on = False
            raise
        print("DOWN!")


class ASGIStateDict(TypedDict):
    schedule_job_runner: AsyncSchedulerJobRunner


@asynccontextmanager
async def _lifespan(app: FastAPI):
    async with AsyncSchedulerJobRunner(job=Job()) as scheduler:
        yield ASGIStateDict(schedule_job_runner=scheduler)


web_app = FastAPI(lifespan=_lifespan, default_response_class=ORJSONResponse)


@web_app.get("/")
def index():
    return {"msg": "Hello World"}


@web_app.get("/show")
async def show(req: Request):
    return req.state._state


@web_app.put("/down")
async def down(req: Request):
    scheduler: SchedulerStateDict = req.state.scheduler
    scheduler["scheduler_on"] = False
    return req.state._state
