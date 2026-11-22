def test_read_main(client):
    response = client.get("/show")
    assert response.headers["content-type"].startswith("application/json")
    res = response.json()
    assert "out" in res


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "healthy"}
