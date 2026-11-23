"""ASGI 앱의 WSGI 마운트가 더 이상 deprecated starlette 미들웨어를 쓰지 않는지 검증."""

import sys

import pytest
from a2wsgi import WSGIMiddleware
from riir_airflow import asgi
from starlette.applications import Starlette
from starlette.routing import Mount


@pytest.fixture
def initialized_airflow_home(monkeypatch, tmp_path):
    (tmp_path / "standalone_admin_password.txt").touch()
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path))


def test_deprecated_wsgi_middleware_module_is_not_imported():
    assert "fastapi.middleware.wsgi" not in sys.modules
    assert "starlette.middleware.wsgi" not in sys.modules


def test_wsgi_app_is_mounted_with_a2wsgi(monkeypatch, initialized_airflow_home):
    wsgi_app = object()
    monkeypatch.setattr("airflow.api_fastapi.app.cached_app", lambda: wsgi_app)

    app = Starlette()
    mounted_app = asgi._init_af("/airflow", app)

    assert mounted_app is app
    mount = next(route for route in app.routes if isinstance(route, Mount))
    assert mount.path == "/airflow"
    assert isinstance(mount.app, WSGIMiddleware)
    assert mount.app.app is wsgi_app


def test_airflow_asgi_app_is_mounted_directly(monkeypatch, initialized_airflow_home):
    from fastapi import FastAPI

    airflow_app = FastAPI()
    monkeypatch.setattr("airflow.api_fastapi.app.cached_app", lambda: airflow_app)

    app = Starlette()
    asgi._init_af("/airflow", app)

    mount = next(route for route in app.routes if isinstance(route, Mount))
    assert mount.app is airflow_app


@pytest.mark.parametrize(
    ("mode", "expected_path"),
    [("dev", None), ("prod", "/airflow"), ("", "/airflow"), ("DEV", "/airflow")],
)
def test_web_app_mount_mode(monkeypatch, mode, expected_path):
    calls = []
    web_app = Starlette()
    mounted_app = Starlette()

    def mount(path, app):
        calls.append((path, app))
        return mounted_app

    monkeypatch.setattr(asgi.settings, "mode", mode)
    monkeypatch.setattr(asgi, "web_app", web_app)
    monkeypatch.setattr(asgi, "_init_af", mount)

    result = asgi._init_web_app()

    if expected_path is None:
        assert result is web_app
        assert calls == []
    else:
        assert result is mounted_app
        assert calls == [(expected_path, web_app)]
