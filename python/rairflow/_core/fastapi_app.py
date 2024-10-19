from pprint import pformat
from fastapi import FastAPI
from fastapi.requests import Request
from airflow.jobs.job import Job

from typing import TypedDict
from fastapi.responses import ORJSONResponse

from contextlib import asynccontextmanager

from rairflow._core.scheduler_loop import (
    AsyncSchedulerJobRunner,
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


# 아래처럼 함으로서 api 덮어 쓸 수 있음은 확인함.
# @web_app.get("/health")
# async def h():
#     return {
#         "dag_processor": {"latest_dag_processor_heartbeat": None, "status": None},
#         "metadatabase": {"status": "healthy"},
#         "scheduler": {
#             "latest_scheduler_heartbeat": "2024-05-06T11:20:08.814069+00:00",
#             "status": "unhealthy",
#         },
#         "triggerer": {
#             "latest_triggerer_heartbeat": "2024-05-06T11:20:06.237693+00:00",
#             "status": "unhealthy",
#         },
#     }


@web_app.get("/show")
async def show(req: Request):
    return dict(out=pformat(req.state._state))


# @web_app.put("/down")
# async def down(req: Request):
#     scheduler: SchedulerStateDict = req.state.scheduler
#     scheduler["scheduler_on"] = False
#     return req.state._state
