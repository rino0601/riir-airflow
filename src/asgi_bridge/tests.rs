use std::ffi::CString;
use std::time::Duration;

use axum::body::to_bytes;
use pyo3::types::PyModule;

use super::*;

struct App {
    bridge: AsgiBridge,
}

impl App {
    fn new(code: &str, request_limit: usize, response_limit: usize) -> Self {
        Python::initialize();
        let runtime = Python::attach(|py| -> PyResult<_> {
            let code = CString::new(code).unwrap();
            let module = PyModule::from_code(py, &code, c"fake_asgi.py", c"fake_asgi")?;
            let factory = module.getattr("factory")?;
            let runtime = py
                .import("riir_airflow._core.asgi_bridge")?
                .getattr("ASGIRuntime")?;
            Ok(runtime.call1((factory,))?.unbind())
        })
        .unwrap();
        Self {
            bridge: AsgiBridge::new(runtime, request_limit, response_limit),
        }
    }
}

impl Drop for App {
    fn drop(&mut self) {
        Python::attach(|py| {
            self.bridge.runtime.bind(py).call_method0("close").unwrap();
        });
    }
}

fn connection() -> ConnectionInfo {
    ConnectionInfo {
        scheme: "https".into(),
        root_path: "/mount".into(),
        client: Some("[::1]:43100".parse().unwrap()),
        server: Some("127.0.0.1:8080".parse().unwrap()),
    }
}

async fn read_body(response: Response) -> Bytes {
    to_bytes(response.into_body(), 1024).await.unwrap()
}

#[tokio::test]
async fn get_and_post_preserve_scope_and_response() {
    let app = App::new(
        r#"
def factory():
    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            assert (await receive())["type"] == "lifespan.startup"
            scope["state"]["ready"] = True
            await send({"type": "lifespan.startup.complete"})
            assert (await receive())["type"] == "lifespan.shutdown"
            await send({"type": "lifespan.shutdown.complete"})
            return
        method = scope["method"]
        assert scope == {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.0" if method == "GET" else "1.1",
            "method": method,
            "scheme": "https",
            "path": "/mount/café/part",
            "raw_path": b"/mount/caf%C3%a9%2Fpart",
            "query_string": b"q=%FF+%2f&q=",
            "root_path": "/mount",
            "headers": [(b"x-dup", b"one"), (b"x-dup", b"\xff")],
            "client": ("::1", 43100),
            "server": ("127.0.0.1", 8080),
            "state": {"ready": True},
        }
        request = await receive()
        assert request == {
            "type": "http.request",
            "body": b"" if method == "GET" else b"\x00\x80\xff",
            "more_body": False,
        }
        await send({
            "type": "http.response.start",
            "status": 202 if method == "GET" else 418,
            "headers": iter([
                (b"set-cookie", b"a=1"),
                (b"content-type", b"application/octet-stream"),
                (b"set-cookie", b"b=2"),
                (b"transfer-encoding", b"chunked"),
            ]),
        })
        await send({"type": "http.response.body", "body": b"\xff", "more_body": True})
        await send({"type": "http.response.body", "body": request["body"]})
    return app
"#,
        3,
        4,
    );
    for (method, version, body, status) in [
        ("GET", Version::HTTP_10, b"".as_slice(), 202),
        ("POST", Version::HTTP_11, b"\x00\x80\xff".as_slice(), 418),
    ] {
        let request = Request::builder()
            .method(method)
            .version(version)
            .uri("/mount/caf%C3%a9%2Fpart?q=%FF+%2f&q=")
            .header("x-dup", "one")
            .header("x-dup", b"\xff".as_slice())
            .body(Body::from(body))
            .unwrap();
        let response = app.bridge.call(request, connection()).await.unwrap();
        assert_eq!(response.status(), status);
        assert_eq!(
            response
                .headers()
                .get_all(header::SET_COOKIE)
                .iter()
                .collect::<Vec<_>>(),
            ["a=1", "b=2"],
        );
        assert_eq!(
            response.headers()[header::CONTENT_TYPE],
            "application/octet-stream"
        );
        assert!(!response.headers().contains_key(header::TRANSFER_ENCODING));
        assert_eq!(read_body(response).await, [b"\xff", body].concat());
    }
}

#[tokio::test]
async fn invalid_messages_and_application_exceptions_return_safe_errors() {
    let app = App::new(
        r#"
def factory():
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope["path"]
        start = {"type": "http.response.start", "status": 200}
        body = {"type": "http.response.body"}
        match path:
            case "/empty":
                return
            case "/early-body":
                await send(body)
            case "/bad-status":
                await send(dict(start, status=True))
            case "/bad-message":
                await send(None)
            case "/caught-invalid":
                try:
                    await send(dict(start, headers=[("x", b"value")]))
                except ValueError:
                    pass
            case "/bad-header":
                await send(dict(start, headers=[(b"x", b"a\nb")]))
                await send(body)
            case "/bad-length":
                await send(dict(start, headers=[(b"content-length", b"3")]))
                await send(dict(body, body=b"abcd"))
            case "/repeated-start":
                await send(start)
                await send(start)
            case "/missing-final":
                await send(start)
                await send(dict(body, more_body=True))
            case "/bad-body":
                await send(start)
                await send(dict(body, body="not bytes"))
            case "/exception":
                await send(start)
                raise RuntimeError("private token")
    return app
"#,
        4,
        4,
    );
    for (path, diagnostic) in [
        ("/empty", "HTTPProtocolError"),
        ("/early-body", "HTTPProtocolError"),
        ("/bad-status", "HTTPProtocolError"),
        ("/bad-message", "HTTPProtocolError"),
        ("/caught-invalid", "HTTPProtocolError"),
        ("/bad-header", "Invalid response header value"),
        ("/bad-length", "Invalid response Content-Length"),
        ("/repeated-start", "HTTPProtocolError"),
        ("/missing-final", "HTTPProtocolError"),
        ("/bad-body", "HTTPProtocolError"),
        ("/exception", "private token"),
    ] {
        let request = Request::builder().uri(path).body(Body::empty()).unwrap();
        let error = app.bridge.call(request, connection()).await.unwrap_err();
        assert!(
            format!("{error:?}").contains(diagnostic),
            "{path}: {error:?}"
        );
        let response = error.into_response();
        assert_eq!(
            response.status(),
            StatusCode::INTERNAL_SERVER_ERROR,
            "{path}"
        );
        assert_eq!(read_body(response).await, "ASGI request failed");
    }
}

#[tokio::test]
async fn bounds_both_bodies() {
    let app = App::new(
        r#"
def factory():
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        assert (await receive())["body"] == b"123"
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": b"12", "more_body": True})
        await send({"type": "http.response.body", "body": b"34"})
    return app
"#,
        3,
        3,
    );
    let too_large = Request::new(Body::from("1234"));
    let error = app.bridge.call(too_large, connection()).await.unwrap_err();
    assert!(matches!(error, BridgeError::RequestTooLarge));
    assert_eq!(
        error.into_response().status(),
        StatusCode::PAYLOAD_TOO_LARGE
    );
    let fits = Request::new(Body::from("123"));
    let response = app.bridge.call(fits, connection()).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let mut body = response.into_body();
    assert_eq!(
        body.frame().await.unwrap().unwrap().into_data().unwrap(),
        "12"
    );
    let error = body.frame().await.unwrap().unwrap_err();
    assert!(format!("{error:?}").contains("ResponseTooLarge"));
}

#[tokio::test]
async fn requests_complete_concurrently_on_single_thread_tokio() {
    let app = App::new(
        r#"
import asyncio

def factory():
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        if scope["path"] == "/first":
            first_entered.set()
            await asyncio.wait_for(second_entered.wait(), timeout=3)
        else:
            second_entered.set()
            await asyncio.wait_for(first_entered.wait(), timeout=3)
        await send({"type": "http.response.start", "status": 200})
        await send({"type": "http.response.body", "body": scope["raw_path"]})
    return app
"#,
        0,
        8,
    );
    let call = |path| {
        app.bridge.call(
            Request::builder().uri(path).body(Body::empty()).unwrap(),
            connection(),
        )
    };
    let (first, second) = tokio::time::timeout(Duration::from_secs(5), async {
        tokio::join!(call("/first"), call("/second"))
    })
    .await
    .unwrap();
    assert_eq!(read_body(first.unwrap()).await, "/first");
    assert_eq!(read_body(second.unwrap()).await, "/second");
}

mod http;
