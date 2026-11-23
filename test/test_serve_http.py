"""Exercise the installed console script and both HTTP listeners."""

import http.client
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


def cli(port, *args):
    executable = Path(sys.executable).with_name("riir-airflow")
    return [str(executable), "--mode", "dev", "serve", "--port", str(port), *args]


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
def server(directory, port, *args, env=None):
    with (directory / f"serve-{port}.log").open("w+") as log:
        process = subprocess.Popen(
            cli(port, *args),
            cwd=directory,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env=env,
        )
        try:
            deadline = time.monotonic() + 30
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


def test_installed_native_server_skips_settings_imports(tmp_path):
    (tmp_path / "sitecustomize.py").write_text(
        """
import sys


class RejectSettingsImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] == "pydantic_settings" or fullname == "riir_airflow.configuration":
            raise AssertionError(f"Unexpected native startup import: {fullname}")


sys.meta_path.insert(0, RejectSettingsImports())
"""
    )
    port = free_port()
    with server(
        tmp_path, port, "--native", env={**os.environ, "PYTHONPATH": str(tmp_path)}
    ):
        assert request(port, "GET", "/health")[0] == 200


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
