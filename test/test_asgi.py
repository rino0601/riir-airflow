"""ASGI 앱의 WSGI 마운트가 더 이상 deprecated starlette 미들웨어를 쓰지 않는지 검증."""

import sys

import pytest
from a2wsgi import WSGIMiddleware
from riir_airflow._core import compat_app
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
    mounted_app = compat_app._init_af("/airflow", app, manage_lifespan=False)

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
    compat_app._init_af("/airflow", app, manage_lifespan=False)

    mount = next(route for route in app.routes if isinstance(route, Mount))
    assert mount.app is airflow_app


def test_fresh_airflow_with_non_fab_auth(monkeypatch, tmp_path):
    from airflow.api_fastapi import app as airflow_api
    from airflow.api_fastapi.auth import tokens
    from airflow.utils import db

    initialized = []
    signing_args = []
    airflow_app = Starlette()
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path))
    monkeypatch.setattr(db, "initdb", lambda: initialized.append(True))
    monkeypatch.setattr(tokens, "get_signing_args", lambda: signing_args.append(True))
    monkeypatch.setattr(airflow_api, "get_auth_manager_cls", lambda: object)
    monkeypatch.setattr(airflow_api, "cached_app", lambda: airflow_app)

    app = compat_app.create_app("prod", manage_lifespan=True)

    assert initialized == [True]
    assert signing_args == [True]
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

    def mount(path, app, *, manage_lifespan):
        calls.append((path, app, manage_lifespan))
        return mounted_app

    monkeypatch.setattr(compat_app, "create_web_app", lambda: web_app)
    monkeypatch.setattr(compat_app, "_init_af", mount)

    result = compat_app.create_app(mode)

    if expected_path is None:
        assert result is web_app
        assert calls == []
    else:
        assert result is mounted_app
        assert calls == [(expected_path, web_app, False)]
