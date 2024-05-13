import time
from fastapi.testclient import TestClient


from riir_airflow.main import web_app


def test_read_main():
    with TestClient(web_app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.json() == {"msg": "Hello World"}
        time.sleep(2)
        response = client.get("/show")
        tick = response.json()["scheduler"]["tick"]
        assert response.json()["scheduler"]["scheduler_on"] is True
        time.sleep(2)
        response = client.get("/show")
        assert response.json()["scheduler"]["scheduler_on"] is True
        assert response.json()["scheduler"]["tick"] != tick
