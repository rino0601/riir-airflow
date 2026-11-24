"""Run an ASGI application on a dedicated asyncio loop."""

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from threading import Lock, Thread, current_thread
from typing import Any

type Message = dict[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type ASGIApp = Callable[[Message, Receive, Send], Awaitable[None]]


class LifespanError(RuntimeError):
    """The app reported a lifespan failure or broke the lifespan protocol."""


class RequestTimeout(TimeoutError):
    """An ASGI request exceeded its runtime deadline."""


class _Lifespan:
    def __init__(self, app: ASGIApp) -> None:
        self.state: Message = {}
        self._events: asyncio.Queue[Message] = asyncio.Queue()
        self._reply: asyncio.Future[Message]
        self._phase = "startup"
        self._task = asyncio.create_task(self._run(app))

    async def _run(self, app: ASGIApp) -> None:
        scope = {
            "type": "lifespan",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "state": self.state,
        }
        await app(scope, self._events.get, self._send)

    async def _send(self, message: Message) -> None:
        event = message.get("type")
        if self._reply.done() or event not in (
            f"lifespan.{self._phase}.complete",
            f"lifespan.{self._phase}.failed",
        ):
            # Record protocol errors even if the application catches the exception.
            error = LifespanError(f"Unexpected lifespan event: {event!r}")
            if not self._reply.done():
                self._reply.set_exception(error)
            raise error
        self._reply.set_result(message)

    async def exchange(self, phase: str) -> bool:
        self._phase = phase
        reply = self._reply = asyncio.get_running_loop().create_future()
        self._events.put_nowait({"type": f"lifespan.{phase}"})
        await asyncio.wait((reply, self._task), return_when=asyncio.FIRST_COMPLETED)
        if reply.done():
            message = reply.result()
            if message["type"] == f"lifespan.{phase}.failed":
                detail = message.get("message", "")
                raise LifespanError(f"ASGI lifespan {phase} failed: {detail}")
            if phase == "shutdown" or not self._task.done():
                return True
        elif phase == "startup" and not self._task.cancelled():
            # An app may raise or return without sending a lifespan response.
            # ASGI requires the server to continue in this case.
            self.state.clear()
            return False

        error = LifespanError(f"ASGI lifespan app exited during {phase}")
        if self._task.cancelled():
            raise error
        raise error from self._task.exception()

    async def finish(self) -> None:
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)


class ASGIRuntime:
    """Start an app factory on a private loop and wait for lifespan startup.

    Submit ASGI calls from other threads. The returned concurrent future
    reports completion and exceptions, and cancelling it requests cancellation
    of the call. Close from outside the loop to cancel and await active calls,
    run lifespan shutdown, and join the loop thread. App code must cooperate
    with asyncio cancellation for close to finish.
    """

    def __init__(self, app_factory: Callable[[], ASGIApp]) -> None:
        self._lock = Lock()
        self._closing = False
        self._started: Future[None] = Future()
        self._stopped: Future[None] = Future()
        self._requests: set[asyncio.Task[None]] = set()
        self._thread = Thread(
            target=self._run, args=(app_factory,), name="asgi-runtime"
        )
        self._thread.start()
        try:
            self._started.result()
        except BaseException:
            self.close()
            raise

    def _run(self, app_factory: Callable[[], ASGIApp]) -> None:
        try:
            with asyncio.Runner() as runner:
                runner.run(self._serve(app_factory))
        except BaseException as error:
            with self._lock:
                self._closing = True
            if self._started.done():
                self._stopped.set_exception(error)
            else:
                self._started.set_exception(error)
                self._stopped.set_result(None)
        else:
            self._stopped.set_result(None)

    async def _serve(self, app_factory: Callable[[], ASGIApp]) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._app = app_factory()
        lifespan = self._lifespan = _Lifespan(self._app)
        try:
            supported = await lifespan.exchange("startup")
            with self._lock:
                if self._closing:
                    self._stop.set()
                self._started.set_result(None)
            await self._stop.wait()
            requests = tuple(self._requests)
            for request in requests:
                if not request.cancelling():
                    request.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
            if supported:
                await lifespan.exchange("shutdown")
        finally:
            await lifespan.finish()

    def submit(
        self,
        scope: Message,
        receive: Receive,
        send: Send,
        *,
        timeout: float | None = None,
    ) -> Future[None]:
        """Schedule an ASGI call and return immediately."""
        with self._lock:
            if self._closing:
                raise RuntimeError("ASGI runtime is closed")
            return asyncio.run_coroutine_threadsafe(
                self._call(scope.copy(), receive, send, timeout), self._loop
            )

    async def _call(
        self, scope: Message, receive: Receive, send: Send, timeout: float | None
    ) -> None:
        task = asyncio.create_task(self._invoke(scope, receive, send, timeout))
        self._requests.add(task)
        try:
            # Closing the runtime and cancelling the caller's future may
            # overlap. Keep either path from interrupting request cleanup.
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done() and not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            self._requests.remove(task)

    async def _invoke(
        self, scope: Message, receive: Receive, send: Send, timeout: float | None
    ) -> None:
        scope["state"] = self._lifespan.state.copy()
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                await self._app(scope, receive, send)
        except TimeoutError as error:
            if deadline.expired():
                raise RequestTimeout from error
            raise

    def close(self) -> None:
        """Stop accepting calls, shut down the app, and wait for the thread."""
        if current_thread() is self._thread:
            raise RuntimeError("Cannot close the ASGI runtime from its loop thread")
        with self._lock:
            if not self._closing:
                self._closing = True
                if self._started.done():
                    self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join()
        self._stopped.result()
