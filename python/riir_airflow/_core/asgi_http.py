"""Exchange buffered HTTP messages with an app on the ASGI runtime."""

import asyncio


class HTTPProtocolError(ValueError):
    """The application sent an invalid or unsupported HTTP message."""


class ResponseTooLarge(HTTPProtocolError):
    """The application exceeded the configured response body limit."""


class _Exchange:
    def __init__(self, body, limit):
        self.request_body = body
        self.limit = limit
        self.status = None
        self.headers = ()
        self.body = bytearray()
        self.complete = False
        self.error = None

    async def receive(self):
        if self.request_body is None:
            # This adapter has no transport disconnect information.
            await asyncio.get_running_loop().create_future()
        body, self.request_body = self.request_body, None
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(self, message):
        if self.error is not None:
            raise self.error
        if self.complete:
            return
        try:
            self._send(message)
        except HTTPProtocolError as error:
            self.error = error
            raise

    def _send(self, message):
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
            self.headers = _headers(message.get("headers", ()))
            self.status = status
        elif self.status is not None and event == "http.response.body":
            body = message.get("body", b"")
            more = message.get("more_body", False)
            if type(body) is not bytes or type(more) is not bool:
                raise HTTPProtocolError("Expected bytes body and boolean more_body")
            if len(body) > self.limit - len(self.body):
                raise ResponseTooLarge(
                    "ASGI response body exceeds the configured limit"
                )
            self.body.extend(body)
            self.complete = not more
        else:
            raise HTTPProtocolError("Unexpected ASGI HTTP response event")

    def result(self):
        if self.error is not None:
            raise self.error
        if not self.complete:
            raise HTTPProtocolError(
                "ASGI app returned without a complete HTTP response"
            )
        return self.status, self.headers, bytes(self.body)


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


def submit_http(runtime, scope, body, response_limit, done):
    """Invoke done with the response or exception once the runtime call ends."""
    exchange = _Exchange(body, response_limit)
    future = runtime.submit(scope, exchange.receive, exchange.send)

    def completed(future):
        try:
            # The concurrent future is already done when the callback runs.
            future.result()
            response = exchange.result()
        except BaseException as error:
            done(None, error)
        else:
            done(response, None)

    future.add_done_callback(completed)
