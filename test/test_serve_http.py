"""Exercise the installed console script and both HTTP listeners."""

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from riir_airflow import _lowlevel

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Uses Unix process signals")
STARTUP_TIMEOUT = 60


def cli(port, *args, mode="dev"):
    executable = Path(sys.executable).with_name("riir-airflow")
    return [str(executable), "--mode", mode, "serve", "--port", str(port), *args]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def http_request(port, method, path, *, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        # Both listeners see the same authority when building redirect locations.
        connection.request(
            method, path, body, {"Host": "test.example:8080", **(headers or {})}
        )
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        connection.close()


def request(port, method, path):
    status, headers, body = http_request(port, method, path)
    return (
        status,
        {
            name.lower(): value
            for name, value in headers
            if name.lower() in {"content-type", "content-length", "allow", "location"}
        },
        body,
    )


@contextmanager
def server(directory, port, *args, env=None, mode="dev", launcher=None):
    executable, *arguments = cli(port, *args, mode=mode)
    command = [*(launcher or [executable]), *arguments]
    with (directory / f"serve-{port}.log").open("w+") as log:
        process = subprocess.Popen(
            command,
            cwd=directory,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env=env,
        )
        try:
            deadline = time.monotonic() + STARTUP_TIMEOUT
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    if request(port, "GET", "/health")[0] == 200:
                        break
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(0.05)
            else:
                log.seek(0)
                pytest.fail(f"Server did not become ready:\n{log.read()}")
            yield process
        finally:
            # The compatibility server also starts a reloader and worker.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


@pytest.mark.parametrize("use_environment", [False, True])
def test_installed_native_server_dispatches_through_rust(tmp_path, use_environment):
    (tmp_path / "sitecustomize.py").write_text(
        """
import sys
from pathlib import Path

from riir_airflow import _lowlevel


class RejectSettingsImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] == "pydantic_settings" or fullname == "riir_airflow.configuration":
            raise AssertionError(f"Unexpected native startup import: {fullname}")


sys.meta_path.insert(0, RejectSettingsImports())

parse = _lowlevel.parse_cli


def record_native_dispatch(*args):
    result = parse(*args)
    Path("native-dispatch.txt").write_text(repr(result))
    return result


_lowlevel.parse_cli = record_native_dispatch
"""
    )
    port = free_port()
    env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in {"riir_mode", "riir_serve", "riir_show"}
    }
    env["PYTHONPATH"] = str(tmp_path)
    args = ["--native"]
    if use_environment:
        env.update(riir_serve='{"native": true, "port": 0}', RIIR_MODE="prod")
        args = []
    with server(tmp_path, port, *args, env=env):
        assert request(port, "GET", "/health")[0] == 200
        assert (
            tmp_path / "native-dispatch.txt"
        ).read_text() == f"(('dev', 'serve', True, {port}), None)"


def test_installed_servers_match_on_shared_http_routes(tmp_path):
    paths = [
        ("GET", "/health", 200),
        ("GET", "/show", 404),
        ("GET", "/%73how?ignored=yes", 404),
        ("HEAD", "/show", 404),
        ("HEAD", "/health", 405),
        ("POST", "/show", 404),
        ("OPTIONS", "/health", 405),
        ("GET", "/missing", 404),
        ("HEAD", "/missing", 404),
        ("POST", "/health//?keep=%2F", 307),
        ("POST", "/show//?keep=%2F", 404),
    ]
    native_port = free_port()
    with server(tmp_path, native_port, "--native"):
        asgi_port = free_port()
        with server(tmp_path, asgi_port):
            for method, path, status in paths:
                native = request(native_port, method, path)
                asgi = request(asgi_port, method, path)
                assert native[0] == status, (method, path, native)
                assert native == asgi, (method, path, native, asgi)


@pytest.mark.parametrize("shutdown_signal", [signal.SIGINT, signal.SIGTERM])
def test_native_collision_and_shutdown(tmp_path, shutdown_signal):
    port = free_port()
    with server(tmp_path, port, "--native") as process:
        collision = subprocess.run(
            cli(port, "--native"), capture_output=True, text=True, timeout=10
        )
        assert collision.returncode != 0
        assert f"Could not bind 127.0.0.1:{port}" in collision.stderr
        assert request(port, "GET", "/health")[0] == 200

        # Keep an idle HTTP connection open while the listener shuts down.
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            process.send_signal(shutdown_signal)
            assert process.wait(timeout=5) == 0

    with server(tmp_path, port, "--native"):
        assert request(port, "GET", "/health")[0] == 200


@pytest.fixture
def fake_airflow(tmp_path):
    (tmp_path / "standalone_admin_password.txt").touch()
    (tmp_path / "sitecustomize.py").write_text(
        """
import asyncio
import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from airflow.api_fastapi import app as airflow_api
from fastapi import FastAPI, Request
from riir_airflow._core import compat_app
from starlette.responses import JSONResponse, StreamingResponse


def listener_ports():
    if sys.platform != "linux":
        return None
    ports = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            candidate = socket.socket(fileno=int(fd))
        except OSError:
            continue
        try:
            if (
                candidate.family in (socket.AF_INET, socket.AF_INET6)
                and candidate.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
            ):
                ports.append(candidate.getsockname()[1])
        finally:
            candidate.detach()
    return ports


@asynccontextmanager
async def lifespan(app):
    if os.environ.get("FAIL_STARTUP"):
        raise RuntimeError("fake Airflow startup failed")
    Path("started").touch()
    try:
        yield {"ready": True}
    finally:
        Path("stopped").touch()


def cached_app():
    Path("factory-called").touch()
    if os.environ.get("FAIL_FACTORY"):
        raise ImportError("fake Airflow initialization failed")
    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(404)
    async def missing(request, exc):
        return JSONResponse({"source": "airflow"}, status_code=404)

    @app.head("/stream")
    async def stream():
        return StreamingResponse(iter([b"hello"]), media_type="application/octet-stream")

    @app.post("/probe")
    async def probe(request: Request):
        ports = listener_ports()
        if request.query_params.get("wait"):
            Path("request-in-flight").touch()
            while not Path("finish-request").exists():
                await asyncio.sleep(0.01)
        response = JSONResponse(
            {
                "root_path": request.scope["root_path"],
                "ready": request.state.ready,
                "body": (await request.body()).decode(),
                "pid": os.getpid(),
                "client": request.client.host,
                "listener_ports": ports,
            },
            status_code=201,
        )
        response.set_cookie("first", "one")
        response.set_cookie("second", "two")
        return response

    return app


airflow_api.cached_app = cached_app

create_web_app = compat_app.create_web_app


def app_with_fallback_marker():
    app = create_web_app()

    async def missing(request, exc):
        return JSONResponse({"source": "compat"}, status_code=404)

    app.exception_handlers[404] = missing
    return app


compat_app.create_web_app = app_with_fallback_marker
"""
    )
    return {**os.environ, "PYTHONPATH": str(tmp_path), "AIRFLOW_HOME": str(tmp_path)}


@pytest.mark.parametrize("shutdown_signal", [signal.SIGINT, signal.SIGTERM])
def test_native_prod_uses_mounted_app(tmp_path, fake_airflow, shutdown_signal):
    fake_airflow["RIIR_MODE"] = "dev"
    port = free_port()
    with server(tmp_path, port, "--native", mode="prod", env=fake_airflow) as process:
        assert (tmp_path / "started").is_file()
        assert request(port, "GET", "/health")[2] == b'{"status":"healthy"}'
        assert request(port, "POST", "/health")[0] == 405
        for method, path in (("GET", "/show"), ("POST", "/%73how"), ("POST", "/show/")):
            status, _, body = request(port, method, path)
            assert (status, body) == (404, b'{"source":"compat"}')
        assert request(port, "GET", "/docs")[0] == 200
        assert request(port, "GET", "/airflow/docs")[0] == 200
        status, _, body = request(port, "GET", "/airflow/missing")
        assert (status, body) == (404, b'{"source":"airflow"}')

        # The in-flight HTTP request drains before the mounted app shuts down.
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("POST", "/airflow/probe?wait=1", b"through Axum")
            deadline = time.monotonic() + 5
            while not (tmp_path / "request-in-flight").exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            process.send_signal(shutdown_signal)
            (tmp_path / "finish-request").touch()
            response = connection.getresponse()
            assert response.status == 201
            assert [
                value
                for name, value in response.getheaders()
                if name.lower() == "set-cookie"
            ] == [
                "first=one; Path=/; SameSite=lax",
                "second=two; Path=/; SameSite=lax",
            ]
            assert json.loads(response.read()) == {
                "root_path": "/airflow",
                "ready": True,
                "body": "through Axum",
                "pid": process.pid,
                "client": "127.0.0.1",
                "listener_ports": [port] if sys.platform == "linux" else None,
            }
        finally:
            connection.close()

        assert process.wait(timeout=10) == 0
        assert (tmp_path / "stopped").is_file()


def test_native_delegated_head_preserves_unknown_content_length(tmp_path, fake_airflow):
    port = free_port()
    with server(tmp_path, port, "--native", mode="prod", env=fake_airflow):
        assert request(port, "HEAD", "/airflow/stream") == (
            200,
            {"content-type": "application/octet-stream"},
            b"",
        )


def test_native_dev_uses_cli_mode(tmp_path, fake_airflow):
    fake_airflow["RIIR_MODE"] = "prod"
    port = free_port()
    with server(tmp_path, port, "--native", env=fake_airflow):
        assert request(port, "GET", "/docs")[0] == 200
        assert request(port, "GET", "/airflow/docs")[0] == 404
        assert not (tmp_path / "factory-called").exists()


@pytest.mark.parametrize("mode", ["dev", "prod"])
def test_native_without_compat_retains_404(tmp_path, mode):
    # Run with only the riir package and the standard library on sys.path.
    # Linking just the package also works when pytest is using a built wheel.
    package = Path(_lowlevel.__file__).parent
    (tmp_path / "riir_airflow").symlink_to(package, target_is_directory=True)
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    launcher = [sys.executable, "-S", "-m", "riir_airflow"]
    port = free_port()
    with server(tmp_path, port, "--native", mode=mode, env=env, launcher=launcher):
        assert request(port, "GET", "/health")[0] == 200
        for path in ("/docs", "/show"):
            assert request(port, "GET", path) == (
                404,
                {"content-type": "application/json", "content-length": "22"},
                b'{"detail":"Not Found"}',
            )


@pytest.mark.parametrize(
    ("flag", "error"),
    [
        ("FAIL_FACTORY", "fake Airflow initialization failed"),
        ("FAIL_STARTUP", "fake Airflow startup failed"),
    ],
)
def test_native_surfaces_installed_compat_failure(tmp_path, fake_airflow, flag, error):
    port = free_port()
    result = subprocess.run(
        cli(port, "--native", mode="prod"),
        cwd=tmp_path,
        env={**fake_airflow, flag: "1"},
        capture_output=True,
        text=True,
        timeout=STARTUP_TIMEOUT,
    )
    assert result.returncode != 0
    assert error in result.stderr
    assert "Axum listening" not in result.stderr
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))


@pytest.fixture
def isolated_airflow(tmp_path):
    # Airflow config can name a database, config file, plugins, or secrets backend.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("AIRFLOW", "RIIR_"))
        and key.upper() != "PYTHONPATH"
    }
    password_file = tmp_path / "passwords.json"
    password_file.write_text(json.dumps({"tester": "test-password"}))
    return {
        **env,
        "AIRFLOW_HOME": str(tmp_path),
        "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": f"sqlite:///{tmp_path / 'airflow.db'}",
        "AIRFLOW__CORE__LOAD_EXAMPLES": "False",
        "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS": "tester:admin",
        "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE": str(password_file),
        "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS": "False",
        "AIRFLOW__API_AUTH__JWT_SECRET": "local-differential-test-signing-key",
    }


def comparable(response):
    status, headers, body = response
    # Exclude server identity, dates, and connection/framing headers.
    # Retain every selected header value in wire order.
    selected = {}
    for name, value in headers:
        name = name.lower()
        if name in {
            "allow",
            "cache-control",
            "content-encoding",
            "content-length",
            "content-type",
            "location",
            "set-cookie",
            "vary",
            "www-authenticate",
        }:
            selected.setdefault(name, []).append(value)
    if selected.get("content-type", [""])[0].startswith("application/json"):
        body = json.loads(body)
    return status, selected, body


def login(port, password):
    return http_request(
        port,
        "POST",
        "/airflow/auth/token",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"username": "tester", "password": password}),
    )


@pytest.mark.differential
def test_real_airflow_fallback_matches_uvicorn(tmp_path, isolated_airflow):
    def exercise(port):
        denied = comparable(login(port, "wrong-password"))
        status, headers, body = comparable(login(port, "test-password"))
        assert status == 201
        token = body.pop("access_token")
        assert token
        # Compare the response without the per-login signed JWT or its length.
        # The requests below use the token as issued by this server.
        headers.pop("content-length")
        auth = {"Authorization": f"Bearer {token}"}

        dags = "/airflow/api/v2/dags"
        cases = [
            (dags, auth, 200),
            (dags, {}, 401),
            (dags + "?limit=not%20an%20integer", auth, 422),
            ("/airflow/docs", {}, 200),
            ("/airflow?keep=%2F&keep=%2B", {}, 307),
            ("/airflow/api/v2/no-such-route", auth, 404),
        ]

        def fetch(case):
            path, request_headers, expected_status = case
            response = comparable(
                http_request(port, "GET", path, headers=request_headers)
            )
            assert response[0] == expected_status, (path, response)
            return response

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(fetch, cases))

        assert denied[0] == 401
        assert results[0][2] == {"dags": [], "total_entries": 0}
        assert "not an integer" in str(results[2][2])
        assert b"/airflow/openapi.json" in results[3][2]
        assert results[4][1]["location"] == [
            "http://test.example:8080/airflow/?keep=%2F&keep=%2B"
        ]
        return denied, (status, headers, body), results

    uvicorn_port = free_port()
    with server(tmp_path, uvicorn_port, mode="prod", env=isolated_airflow):
        uvicorn = exercise(uvicorn_port)

    native_port = free_port()
    with server(
        tmp_path, native_port, "--native", mode="prod", env=isolated_airflow
    ) as process:
        assert exercise(native_port) == uvicorn
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
