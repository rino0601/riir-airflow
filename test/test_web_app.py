import json
import pprint
import sys
from pprint import pformat
from unittest.mock import Mock

import pytest
from riir_airflow.asgi import app
from starlette.testclient import TestClient


def test_show_empty_state_without_pprint(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "pprint", None)

    response = client.get("/show")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.content == b'{"out":"{}"}'


def test_read_main(client):
    state = {"z": "한글", "a": {"items": list(range(30))}}
    client.app_state.update(state)

    response = client.get("/show")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"out": pformat(state)}


@pytest.mark.parametrize("state", [{"k": "한" * 71}, {"'": 'a"\n\ud800'}])
def test_show_single_string_entry_without_pprint(client, monkeypatch, state):
    expected = {"out": pformat(state)}
    client.app_state.update(state)
    monkeypatch.setitem(sys.modules, "pprint", None)

    response = client.get("/show")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert (
        response.content
        == json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode()
    )


@pytest.mark.parametrize(("value", "key_length"), [(True, 70), (False, 69), (None, 70)])
@pytest.mark.parametrize("escaped", [False, True])
def test_show_single_bool_or_none_without_pprint(
    client, monkeypatch, value, key_length, escaped
):
    key = "'\"\\\n\t\ud800" if escaped else "🦀" * key_length
    state = {key: value}
    expected = pformat(state)
    if not escaped:
        assert len(expected) == 80
    client.app_state.update(state)
    monkeypatch.setitem(sys.modules, "pprint", None)

    response = client.get("/show")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert (
        response.content
        == json.dumps(
            {"out": expected}, ensure_ascii=False, separators=(",", ":")
        ).encode()
    )


class CustomInt(int):
    def __repr__(self):
        return "<custom int>"

    def __int__(self):
        raise AssertionError("int subclass must use pprint")


@pytest.mark.parametrize(
    ("state", "uses_pprint"),
    [
        ({"k": 0}, False),
        ({"'\"\\\n\ud800": -42}, False),
        ({"k": -(2**63)}, False),
        ({"k": 2**63 - 1}, False),
        ({"k": -(2**63) - 1}, True),
        ({"k": 2**63}, True),
        ({"k": CustomInt(1)}, True),
        pytest.param({"🦀" * 72: -1}, False, id="width-80"),
        pytest.param({"🦀" * 73: -1}, True, id="width-81"),
        ({"\n" * 38: 0}, True),
    ],
)
def test_show_single_int_entry(client, monkeypatch, state, uses_pprint):
    expected = pformat(state)
    formatter = Mock(wraps=pformat)
    monkeypatch.setattr(pprint, "pformat", formatter)
    client.app_state.update(state)

    response = client.get("/show")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"out": expected}
    if uses_pprint:
        formatter.assert_called_once_with(state)
    else:
        formatter.assert_not_called()


class CustomString(str):
    def __repr__(self):
        return "<custom>"


@pytest.mark.parametrize(
    ("state", "uses_pprint", "width"),
    [
        ({"z": "last", "a": "first"}, False, None),
        ({"🦀": "한글", "z": "'\"\\\n", "\ud800": "v"}, True, None),
        ({"🦀": "한글", "\ud800": "'\"\\\n"}, False, None),
        ({"z": "a'b", "\n": 'a"b'}, False, None),
        ({"z": "🦀" * 61, "a": "한"}, False, 80),
        ({"z": "🦀" * 62, "a": "한"}, True, 81),
        ({"z": "v", "a": CustomString("v")}, True, None),
        ({"z": "v", CustomString("a"): "v"}, True, None),
        ({"z": "v", "a": 0}, True, None),
        ({"z": "v", 0: "v"}, True, None),
    ],
)
def test_show_two_string_entries(client, monkeypatch, state, uses_pprint, width):
    expected = pformat(state)
    if width is not None:
        assert len(pformat(state, width=100)) == width
    formatter = Mock(wraps=pformat)
    monkeypatch.setattr(pprint, "pformat", formatter)
    client.app_state.update(state)

    response = client.get("/show")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert (
        response.content
        == json.dumps(
            {"out": expected}, ensure_ascii=False, separators=(",", ":")
        ).encode()
    )
    if uses_pprint:
        formatter.assert_called_once_with(state)
    else:
        formatter.assert_not_called()


def test_show_uses_pprint_for_wide_or_custom_entries(client, monkeypatch):
    formatter = Mock(wraps=pformat)
    monkeypatch.setattr(pprint, "pformat", formatter)
    for state in (
        {"k": "한" * 72},
        {"k": "한 " * 60},
        {"k": CustomString("value")},
        {CustomString("key"): "v"},
        {"🦀" * 71: True},
        {"🦀" * 70: False},
        {"🦀" * 71: None},
        {"\n" * 36: True},
        {CustomString("key"): True},
        {CustomString("key"): 1},
        {"k": 1, "other": 0},
        {"k": None, "other": False},
    ):
        client.app_state.clear()
        client.app_state.update(state)
        formatter.reset_mock()

        response = client.get("/show")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {"out": pformat(state)}
        formatter.assert_called_once_with(state)


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
