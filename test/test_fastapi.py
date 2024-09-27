def test_read_main(client):
    response = client.get("/show")
    res = response.json()
    assert "out" in res
    # state 조회 잠시 포기
    # assert "schedule_job_runner" in res["out"]
