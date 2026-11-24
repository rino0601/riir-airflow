"""Exercise the ASGI runtime with apps that require no optional dependencies."""

import asyncio
from concurrent.futures import CancelledError, ThreadPoolExecutor
from threading import Event, current_thread

import pytest
from riir_airflow._core.asgi_bridge import ASGIRuntime, LifespanError

TIMEOUT = 5


async def receive():
    return {"type": "http.request"}


async def send(message):
    pass


def test_factory_lifespan_and_concurrent_calls_share_one_loop():
    startup_entered = Event()
    allow_startup = Event()
    events = []
    calls = []
    resource = object()

    def factory():
        loop = asyncio.get_running_loop()
        thread = current_thread()
        both_entered = asyncio.Event()

        async def app(scope, receive, send):
            assert asyncio.get_running_loop() is loop
            assert current_thread() is thread
            if scope["type"] == "lifespan":
                events.append((await receive())["type"])
                startup_entered.set()
                assert await asyncio.to_thread(allow_startup.wait, TIMEOUT)
                scope["state"]["resource"] = resource
                await send({"type": "lifespan.startup.complete"})
                events.append((await receive())["type"])
                await send({"type": "lifespan.shutdown.complete"})
                return

            assert scope["state"].pop("resource") is resource
            calls.append(scope["path"])
            if len(calls) == 2:
                both_entered.set()
            await both_entered.wait()
            assert await receive() == {"type": "http.request"}
            await send({"type": "http.response.start", "status": 200})

        return app

    messages = []

    async def record(message):
        messages.append(message)

    with ThreadPoolExecutor() as pool:
        starting = pool.submit(ASGIRuntime, factory)
        try:
            assert startup_entered.wait(TIMEOUT)
            assert not starting.done()
        finally:
            allow_startup.set()
        runtime = starting.result(TIMEOUT)

    try:
        first = runtime.submit({"type": "http", "path": "/one"}, receive, record)
        second = runtime.submit({"type": "http", "path": "/two"}, receive, record)
        first.result(TIMEOUT)
        second.result(TIMEOUT)
    finally:
        runtime.close()

    runtime.close()
    assert events == ["lifespan.startup", "lifespan.shutdown"]
    assert calls == ["/one", "/two"]
    assert messages == [{"type": "http.response.start", "status": 200}] * 2


def test_unsupported_lifespan_and_request_exception():
    events = []

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            events.append((await receive())["type"])
            raise ValueError("unsupported scope")
        raise LookupError("request failed")

    runtime = ASGIRuntime(lambda: app)
    try:
        call = runtime.submit({"type": "http"}, receive, send)
        with pytest.raises(LookupError, match="request failed"):
            call.result(TIMEOUT)
    finally:
        runtime.close()
    assert events == ["lifespan.startup"]


@pytest.mark.parametrize("failed_phase", ["startup", "shutdown"])
def test_explicit_lifespan_failure_joins_thread(failed_phase):
    threads = []

    async def app(scope, receive, send):
        threads.append(current_thread())
        assert await receive() == {"type": "lifespan.startup"}
        if failed_phase == "shutdown":
            await send({"type": "lifespan.startup.complete"})
            assert await receive() == {"type": "lifespan.shutdown"}
        await send({"type": f"lifespan.{failed_phase}.failed", "message": "broken"})

    with pytest.raises(LifespanError, match=f"{failed_phase} failed: broken"):
        runtime = ASGIRuntime(lambda: app)
        runtime.close()
    assert len(threads) == 1
    assert not threads[0].is_alive()


def test_factory_exception_joins_thread():
    threads = []

    def factory():
        threads.append(current_thread())
        raise ValueError("factory failed")

    with pytest.raises(ValueError, match="factory failed"):
        ASGIRuntime(factory)
    assert not threads[0].is_alive()


def test_cancellation_and_close_clean_up_requests_before_shutdown():
    threads = []
    entered = {path: Event() for path in ("/cancel", "/close")}
    cleaned = {path: Event() for path in entered}

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            threads.append(current_thread())
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            assert all(event.is_set() for event in cleaned.values())
            await send({"type": "lifespan.shutdown.complete"})
            return
        path = scope["path"]
        entered[path].set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            cleaned[path].set()

    runtime = ASGIRuntime(lambda: app)
    try:
        cancelled = runtime.submit({"type": "http", "path": "/cancel"}, receive, send)
        closing = runtime.submit({"type": "http", "path": "/close"}, receive, send)
        assert all(event.wait(TIMEOUT) for event in entered.values())
        assert cancelled.cancel()
        with pytest.raises(CancelledError):
            cancelled.result(TIMEOUT)
        assert cleaned["/cancel"].wait(TIMEOUT)
    finally:
        runtime.close()

    with pytest.raises(CancelledError):
        closing.result(TIMEOUT)
    with pytest.raises(RuntimeError, match="closed"):
        runtime.submit({"type": "http"}, receive, send)
    assert not threads[0].is_alive()


def test_cancelling_future_during_close_does_not_interrupt_cleanup():
    entered = Event()
    cleaning = Event()
    release = Event()
    cleaned = Event()

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            return
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            assert await asyncio.to_thread(release.wait, TIMEOUT)
            cleaned.set()

    runtime = ASGIRuntime(lambda: app)
    with ThreadPoolExecutor() as pool:
        try:
            call = runtime.submit({"type": "http"}, receive, send)
            assert entered.wait(TIMEOUT)
            closing = pool.submit(runtime.close)
            assert cleaning.wait(TIMEOUT)
            assert call.cancel()
        finally:
            release.set()
            runtime.close()
        closing.result(TIMEOUT)
    assert cleaned.is_set()
