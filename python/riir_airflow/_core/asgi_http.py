"""Exchange streaming HTTP messages with an app on the ASGI runtime."""

import asyncio

from .asgi_bridge import RequestTimeout


class HTTPProtocolError(ValueError):
    """The application sent an invalid or unsupported HTTP message."""


class ResponseTooLarge(HTTPProtocolError):
    """The application exceeded the configured response body limit."""


class _Exchange:
    def __init__(self, scope, transport, limit):
        self.transport = transport
        self.is_head = scope["method"] == "HEAD"
        self.limit = limit
        self.status = None
        self.complete = False
        self.error = None
        self.sent = 0
        self.send_lock = asyncio.Lock()
        self.receive_lock = asyncio.Lock()

    async def receive(self):
        async with self.receive_lock:
            result = await self.transport.receive()
            if result is None:
                return {"type": "http.disconnect"}
            self.transport.consume_request_chunk()
            body, more = result
            return {"type": "http.request", "body": bytes(body), "more_body": more}

    async def send(self, message):
        async with self.send_lock:
            if self.error is not None:
                raise self.error
            if self.complete:
                return
            try:
                await self._send(message)
            except (ValueError, OSError) as error:
                self.error = error
                raise

    async def _send(self, message):
        if not isinstance(message, dict):
            raise HTTPProtocolError("Expected an ASGI HTTP message dictionary")
        event = message.get("type")
        if self.status is None and event == "http.response.start":
            status = message.get("status")
            if not isinstance(status, int) or isinstance(status, bool):
                raise HTTPProtocolError("Expected a final HTTP status code")
            status = int.__int__(status)
            if not 200 <= status <= 599:
                raise HTTPProtocolError("Expected a final HTTP status code")
            if message.get("trailers", False) is not False:
                raise HTTPProtocolError("HTTP response trailers are unsupported")
            headers = _headers(message.get("headers", ()))
            self.length, self.forbids_body, self.suppress_body = self.transport.start(
                status, headers, self.is_head
            )
            self.status = status
        elif self.status is not None and event == "http.response.body":
            body = message.get("body", b"")
            more = message.get("more_body", False)
            if type(body) is not bytes or type(more) is not bool:
                raise HTTPProtocolError("Expected bytes body and boolean more_body")
            self.sent += len(body)
            if self.sent > self.limit:
                raise ResponseTooLarge(
                    "ASGI response body exceeds the configured limit"
                )
            if self.forbids_body and body:
                raise HTTPProtocolError("Response status forbids a body")
            if self.length is not None:
                metadata_only = self.sent == 0 and (self.is_head or self.status == 304)
                if self.sent > self.length or (
                    not more and not metadata_only and self.sent != self.length
                ):
                    raise HTTPProtocolError("Invalid response Content-Length")
            await self.transport.send_body(
                b"" if self.suppress_body else body, not more
            )
            self.complete = not more
        else:
            raise HTTPProtocolError("Unexpected ASGI HTTP response event")

    def check_complete(self):
        if self.error is not None:
            raise self.error
        if not self.complete:
            raise HTTPProtocolError(
                "ASGI app returned without a complete HTTP response"
            )


def _headers(headers):
    pairs = []
    try:
        for name, value in headers:
            if type(name) is not bytes or type(value) is not bytes:
                raise HTTPProtocolError("Expected byte pairs for HTTP response headers")
            if name != name.lower():
                raise HTTPProtocolError("Expected lowercase HTTP response header names")
            pairs.append((name, value))
    except (TypeError, ValueError) as error:
        if isinstance(error, HTTPProtocolError):
            raise
        raise HTTPProtocolError(
            "Expected an iterable of HTTP response header pairs"
        ) from error
    return pairs


def submit_http(runtime, scope, transport, response_limit, timeout):
    """Schedule the bounded exchange and report its final result."""
    exchange = _Exchange(scope, transport, response_limit)
    future = runtime.submit(scope, exchange.receive, exchange.send, timeout=timeout)

    def completed(future):
        try:
            future.result()
            exchange.check_complete()
        except BaseException as error:
            transport.done(error, isinstance(error, RequestTimeout), future.cancelled())
        else:
            transport.done(None, False, False)

    future.add_done_callback(completed)
    return future
