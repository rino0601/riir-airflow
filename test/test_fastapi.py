from pprint import pformat

import pytest


def test_read_main(client):
    state = {"z": "한글", "a": {"items": list(range(30))}}
    client.app_state.update(state)

    response = client.get("/show")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"out": pformat(state)}


def test_show_propagates_repr_errors(client):
    class BrokenRepr:
        def __repr__(self):
            raise ValueError("request state repr failed")

    client.app_state["broken"] = BrokenRepr()
    with pytest.raises(ValueError, match="request state repr failed"):
        client.get("/show")


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "healthy"}
