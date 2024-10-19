import os
import pytest
from fastapi.testclient import TestClient

from rairflow.__main__ import web_app


@pytest.fixture
def client():
    assert "AIRFLOW_HOME" in os.environ
    # assert "AIRFLOW__CORE__EXECUTOR" in os.environ
    # assert (
    #     os.environ["AIRFLOW__CORE__EXECUTOR"]
    #     == "riir_airflow.executors.asgi_executor.AsgiExecutor"
    # )

    # unittest 일때는 무한루프를 풀어야한다. 방법 알 때까지 일단 state 조회 기능은 포기
    # # FastAPI TestClient 생성
    # with TestClient(web_app) as client:
    #     yield client
    yield TestClient(web_app)
