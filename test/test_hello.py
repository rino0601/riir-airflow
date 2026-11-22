from riir_airflow import hello


def test_hello():
    assert hello() == "Hello from riir-airflow!"
