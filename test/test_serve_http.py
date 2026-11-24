"""Exercise the installed console script and both HTTP listeners."""

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Uses Unix process signals")
STARTUP_TIMEOUT = 60


def cli(port, *args, mode="dev"):
    executable = Path(sys.executable).with_name("riir-airflow")
    return [str(executable), "--mode", mode, "serve", "--port", str(port), *args]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def request(port, method, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        # Both listeners see the same authority when building redirect locations.
        connection.request(method, path, headers={"Host": "test.example:8080"})
        response = connection.getresponse()
        headers = {
            name.lower(): value
            for name, value in response.getheaders()
            if name.lower() in {"content-type", "content-length", "allow", "location"}
        }
        return response.status, headers, response.read()
    finally:
        connection.close()


@contextmanager
def server(directory, port, *args, env=None, mode="dev"):
    with (directory / f"serve-{port}.log").open("w+") as log:
        process = subprocess.Popen(
            cli(port, *args, mode=mode),
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
        ("GET", "/show", 200),
        ("GET", "/%73how?ignored=yes", 200),
        ("GET", "/show?state=%7B%22z%22%3A%22%ED%95%9C%EA%B8%80%22%7D", 200),
        ("HEAD", "/show", 405),
        ("HEAD", "/health", 405),
        ("POST", "/show", 405),
        ("OPTIONS", "/health", 405),
        ("GET", "/missing", 404),
        ("HEAD", "/missing", 404),
        ("POST", "/health//?keep=%2F", 307),
        ("POST", "/show//?state=%7B%7D", 307),
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
                if status == 200 and path.startswith(("/show", "/%73how")):
                    assert native[1]["content-type"] == "application/json"
                    assert native[2] == b'{"out":"{}"}'


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
        assert request(port, "GET", "/show")[2] == b'{"out":"{}"}'


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
from starlette.responses import JSONResponse


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

    @app.post("/probe")
    async def probe(request: Request):
        ports = listener_ports()
        if request.query_params.get("wait"):
            Path("request-in-flight").touch()
            while not Path("finish-request").exists():
                await asyncio.sleep(0.01)
        return JSONResponse(
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

    return app


airflow_api.cached_app = cached_app
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
        assert request(port, "GET", "/%73how")[2] == b'{"out":"{}"}'
        assert request(port, "POST", "/health")[0] == 405
        assert request(port, "POST", "/%73how")[0] == 405
        assert request(port, "POST", "/show//?keep=%2F")[0] == 307
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


def test_native_dev_uses_cli_mode(tmp_path, fake_airflow):
    fake_airflow["RIIR_MODE"] = "prod"
    port = free_port()
    with server(tmp_path, port, "--native", env=fake_airflow):
        assert request(port, "GET", "/docs")[0] == 200
        assert request(port, "GET", "/airflow/docs")[0] == 404
        assert not (tmp_path / "factory-called").exists()


def test_native_without_compat_retains_404(tmp_path):
    (tmp_path / "sitecustomize.py").write_text(
        """
import importlib.util
import sys

find_spec = importlib.util.find_spec
importlib.util.find_spec = lambda name: None if name == "airflow" else find_spec(name)


class RejectCompat:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"airflow", "a2wsgi", "starlette"}:
            raise AssertionError(f"Unexpected compat import: {fullname}")


sys.meta_path.insert(0, RejectCompat())
"""
    )
    port = free_port()
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    with server(tmp_path, port, "--native", mode="prod", env=env):
        assert request(port, "GET", "/health")[0] == 200
        assert request(port, "GET", "/docs")[2] == b'{"detail":"Not Found"}'


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
