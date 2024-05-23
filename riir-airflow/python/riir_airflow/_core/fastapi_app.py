from fastapi import FastAPI
from fastapi.requests import Request
from airflow.jobs.job import Job

from typing import TypedDict
from fastapi.responses import ORJSONResponse

from contextlib import asynccontextmanager
from random import shuffle

from riir_airflow._core.scheduler_loop import (
    AsyncSchedulerJobRunner,
    SchedulerStateDict,
)


class ASGIStateDict(TypedDict):
    schedule_job_runner: AsyncSchedulerJobRunner


@asynccontextmanager
async def _lifespan(app: FastAPI):
    async with AsyncSchedulerJobRunner(job=Job()) as scheduler:
        yield ASGIStateDict(schedule_job_runner=scheduler)


web_app = FastAPI(lifespan=_lifespan, default_response_class=ORJSONResponse)

"""
@web_app.get("/")
def do_not_impl_root():
    this path reserved for airflow web ui!
"""


@web_app.get("/show")
async def show(req: Request):
    return req.state._state


@web_app.put("/down")
async def down(req: Request):
    scheduler: SchedulerStateDict = req.state.scheduler
    scheduler["scheduler_on"] = False
    return req.state._state
