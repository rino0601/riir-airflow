def test_read_main(client):
    response = client.get("/show")
    res = response.json()
    assert "out" in res
