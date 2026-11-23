from pprint import pformat

import pytest
from riir_airflow.asgi import app
from starlette.testclient import TestClient


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


@pytest.mark.parametrize("root_path", ["", "/proxy"])
def test_health(root_path):
    client = TestClient(app, root_path=root_path)
    response = client.get(root_path + "/health")
    assert response.status_code == 200
    assert dict(response.headers) == {
        "content-length": "20",
        "content-type": "application/json",
    }
    assert response.content == b'{"status":"healthy"}'


@pytest.mark.parametrize("path", ["/health", "/show"])
@pytest.mark.parametrize("method", ["HEAD", "POST", "OPTIONS"])
def test_riir_endpoints_only_accept_get(client, path, method):
    response = client.request(method, path)
    assert response.status_code == 405
    assert dict(response.headers) == {
        "allow": "GET",
        "content-length": "31",
        "content-type": "application/json",
    }
    assert response.content == (
        b"" if method == "HEAD" else b'{"detail":"Method Not Allowed"}'
    )


def test_missing_route_and_slash_redirect(client):
    missing = client.get("/missing")
    assert missing.status_code == 404
    assert missing.headers["content-type"] == "application/json"
    assert missing.content == b'{"detail":"Not Found"}'

    redirect = client.post("/health/", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "http://testserver/health"
    assert redirect.content == b""


def test_show_returns_internal_server_error_over_http():
    class BrokenRepr:
        def __repr__(self):
            raise ValueError("request state repr failed")

    client = TestClient(app, raise_server_exceptions=False)
    client.app_state["broken"] = BrokenRepr()
    response = client.get("/show")
    assert response.status_code == 500
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.content == b"Internal Server Error"


def test_api_documentation():
    client = TestClient(app, root_path="/proxy")
    response = client.get("/proxy/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["servers"] == [{"url": "/proxy"}]
    assert set(schema["paths"]) == {"/health", "/show"}

    pages = {
        "/docs": "/proxy/docs/oauth2-redirect",
        "/redoc": 'spec-url="/proxy/openapi.json"',
        "/docs/oauth2-redirect": "Swagger UI: OAuth2 Redirect",
    }
    for path, content in pages.items():
        response = client.get("/proxy" + path)
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        assert content in response.text
        head = client.head("/proxy" + path)
        assert head.status_code == 200
        assert head.headers == response.headers
        assert head.content == b""

    rejected = client.post("/proxy/docs")
    assert rejected.status_code == 405
    assert rejected.headers["allow"] == "GET, HEAD"
    assert rejected.json() == {"detail": "Method Not Allowed"}
