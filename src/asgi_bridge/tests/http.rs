//! Real HTTP/1.1 tests against Axum and embedded Python.

use std::io;

use axum::{Router, extract::State};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};

use super::*;

const WAIT: Duration = Duration::from_secs(10);
const CHUNKS: usize = 512;
const STREAM_APP: &str = r#"
import asyncio

def factory():
    uploaded = asyncio.Event()
    finish_split = asyncio.Event()
    progress = 0
    active = set()
    cleaned = set()

    async def reply(send, body, status=200, more=False):
        await send({"type": "http.response.start", "status": status})
        await send({"type": "http.response.body", "body": body, "more_body": more})

    async def app(scope, receive, send):
        nonlocal progress
        if scope["type"] == "lifespan":
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            assert not active, active
            await send({"type": "lifespan.shutdown.complete"})
            return

        path = scope["path"]
        if path == "/release":
            finish_split.set()
            await reply(send, b"released")
            return
        if path == "/state":
            await reply(send, f"{progress};{','.join(sorted(active))};{','.join(sorted(cleaned))}".encode())
            return

        active.add(path)
        try:
            if path == "/split":
                message = await receive()
                assert message == {"type": "http.request", "body": b"abc", "more_body": True}
                await reply(send, b"first", 202, more=True)
                body = bytearray()
                while True:
                    message = await receive()
                    assert message["type"] == "http.request"
                    body.extend(message["body"])
                    if not message["more_body"]:
                        break
                assert body == b"defg"
                uploaded.set()
                await finish_split.wait()
                await send({"type": "http.response.body", "body": bytes(body)})
                assert (await receive())["type"] == "http.disconnect"
            elif path == "/uploaded":
                await uploaded.wait()
                await reply(send, b"uploaded")
            elif path in ("/large", "/disconnect"):
                await send({"type": "http.response.start", "status": 200})
                for _ in range(512):
                    await send({
                        "type": "http.response.body", "body": b"z" * 65536, "more_body": True
                    })
                    progress += 1
                await send({"type": "http.response.body"})
            elif path == "/receive-limit":
                while (await receive())["more_body"]:
                    pass
            elif path == "/early-error":
                raise RuntimeError("private early error")
            elif path in ("/early-timeout", "/disconnect-before-start"):
                await asyncio.Future()
            elif path == "/length":
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-length", b"1")]})
                await send({"type": "http.response.body", "body": b"x", "more_body": True})
                await finish_split.wait()
                await send({"type": "http.response.body"})
            elif path == "/background":
                await reply(send, b"background")
                await finish_split.wait()
            elif path in ("/late-timeout", "/late-error", "/signal"):
                await reply(send, b"begun", 201, more=True)
                if path == "/late-error":
                    await finish_split.wait()
                    raise RuntimeError("private late error")
                await asyncio.Future()
        finally:
            active.remove(path)
            cleaned.add(path)
            if path == "/signal":
                print("request cleaned", flush=True)

    return app
"#;

async fn router(app: Arc<App>, request: Request<Body>) -> Response {
    app.bridge
        .call(request, connection())
        .await
        .unwrap_or_else(IntoResponse::into_response)
}

async fn serve(
    listener: TcpListener,
    app: Arc<App>,
    shutdown: impl Future<Output = ()> + Send + 'static,
) {
    let (stop, stopped) = oneshot::channel();
    let server = axum::serve(
        listener,
        Router::new()
            .fallback(axum::routing::any(
                |State(app): State<Arc<App>>, request| router(app, request),
            ))
            .with_state(app.clone()),
    )
    .with_graceful_shutdown(async {
        let _ = stopped.await;
    });
    let close = async {
        shutdown.await;
        let _ = stop.send(());
        app.bridge.close().await.unwrap();
        assert_runtime_clean(&app);
    };
    let (served, ()) = tokio::join!(async { server.await }, close);
    served.unwrap();
}

fn assert_runtime_clean(app: &App) {
    Python::attach(|py| {
        let runtime = app.bridge.runtime.bind(py);
        assert!(!runtime.getattr("_requests").unwrap().is_truthy().unwrap());
        assert!(
            !runtime
                .getattr("_thread")
                .unwrap()
                .call_method0("is_alive")
                .unwrap()
                .is_truthy()
                .unwrap()
        );
    });
}

struct Server {
    addr: SocketAddr,
    stop: oneshot::Sender<()>,
    task: tokio::task::JoinHandle<()>,
}

impl Server {
    async fn start(timeout: Duration) -> Self {
        let mut app = App::new(STREAM_APP, 16, CHUNKS * FRAME_SIZE);
        app.bridge.timeout = timeout;
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let (stop, signal) = oneshot::channel();
        let task = tokio::spawn(serve(listener, Arc::new(app), async {
            let _ = signal.await;
        }));
        Self { addr, stop, task }
    }

    async fn stop(self) {
        self.stop.send(()).unwrap();
        tokio::time::timeout(WAIT, self.task)
            .await
            .unwrap()
            .unwrap();
    }
}

struct Client(BufReader<TcpStream>);

impl Client {
    async fn open(addr: SocketAddr, request: &str) -> Self {
        let stream = TcpStream::connect(addr).await.unwrap();
        let mut client = Self(BufReader::new(stream));
        client.write(request).await;
        client
    }

    async fn get(addr: SocketAddr, path: &str) -> Self {
        Self::open(
            addr,
            &format!("GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n"),
        )
        .await
    }

    async fn write(&mut self, payload: &str) {
        self.0
            .get_mut()
            .write_all(payload.as_bytes())
            .await
            .unwrap();
    }

    async fn line(&mut self) -> io::Result<String> {
        let mut line = String::new();
        if self.0.read_line(&mut line).await? == 0 {
            return Err(io::ErrorKind::UnexpectedEof.into());
        }
        Ok(line)
    }

    async fn head(&mut self, status: u16) {
        assert_eq!(
            self.line()
                .await
                .unwrap()
                .split_whitespace()
                .nth(1)
                .unwrap(),
            status.to_string()
        );
        while self.line().await.unwrap() != "\r\n" {}
    }

    async fn chunk(&mut self) -> io::Result<Vec<u8>> {
        let length = usize::from_str_radix(self.line().await?.trim(), 16).unwrap();
        let mut data = vec![0; length];
        self.0.read_exact(&mut data).await?;
        assert_eq!(self.line().await?, "\r\n");
        Ok(data)
    }
}

async fn get(addr: SocketAddr, path: &str) -> Vec<u8> {
    let mut client = Client::get(addr, path).await;
    client.head(200).await;
    let chunk = client.chunk().await.unwrap();
    assert!(client.chunk().await.unwrap().is_empty());
    chunk
}

#[tokio::test]
async fn split_upload_and_first_response_chunk_before_completion() {
    tokio::time::timeout(WAIT, async {
        let server = Server::start(WAIT).await;
        let mut client = Client::open(server.addr,
            "POST /split HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n").await;
        client.head(202).await;
        assert_eq!(client.chunk().await.unwrap(), b"first");
        client.write("4\r\ndefg\r\n0\r\n\r\n").await;
        assert_eq!(get(server.addr, "/uploaded").await, b"uploaded");
        assert_eq!(get(server.addr, "/release").await, b"released");
        assert_eq!(client.chunk().await.unwrap(), b"defg");
        assert!(client.chunk().await.unwrap().is_empty());

        let mut over_limit = Client::open(server.addr,
            "POST /receive-limit HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n11\r\n12345678901234567\r\n0\r\n\r\n").await;
        over_limit.head(413).await;
        server.stop().await;
    }).await.unwrap();
}

#[tokio::test]
async fn large_response_backpressure_and_midstream_disconnect() {
    tokio::time::timeout(WAIT, async {
        let server = Server::start(WAIT).await;
        let mut slow = Client::get(server.addr, "/large").await;
        slow.head(200).await;
        let first = slow.chunk().await.unwrap();
        assert_eq!(first.len(), FRAME_SIZE);
        assert!(first.iter().all(|&byte| byte == b'z'));
        // While the socket is unread, a concurrent request can still finish.
        // 32 MiB exceeds the HTTP and socket write buffers.
        let state = String::from_utf8(get(server.addr, "/state").await).unwrap();
        let (_, active) = state.split_once(';').unwrap();
        assert!(active.starts_with("/large;"), "{state}");
        let mut total = first.len();
        loop {
            let chunk = slow.chunk().await.unwrap();
            if chunk.is_empty() {
                break;
            }
            assert!(chunk.iter().all(|&byte| byte == b'z'));
            total += chunk.len();
        }
        assert_eq!(total, CHUNKS * FRAME_SIZE);

        let early = Client::get(server.addr, "/disconnect-before-start").await;
        wait_for_state(server.addr, |state| {
            state.contains(";/disconnect-before-start;")
        })
        .await;
        drop(early);
        wait_for_state(server.addr, |state| {
            state.contains(";;/disconnect-before-start")
        })
        .await;

        let mut dropped = Client::get(server.addr, "/disconnect").await;
        dropped.head(200).await;
        assert!(!dropped.chunk().await.unwrap().is_empty());
        drop(dropped);
        wait_for_state(server.addr, |state| state.contains(";;/disconnect,")).await;
        server.stop().await;
    })
    .await
    .unwrap();
}

async fn wait_for_state(addr: SocketAddr, check: impl Fn(&str) -> bool) {
    loop {
        let state = String::from_utf8(get(addr, "/state").await).unwrap();
        if check(&state) {
            return;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}

#[tokio::test]
async fn fixed_length_and_final_chunk_flush_while_apps_are_still_running() {
    tokio::time::timeout(WAIT, async {
        let server = Server::start(WAIT).await;
        let mut fixed = Client::get(server.addr, "/length").await;
        fixed.head(200).await;
        assert_eq!(fixed.0.read_u8().await.unwrap(), b'x');
        assert_eq!(get(server.addr, "/background").await, b"background");
        let state = String::from_utf8(get(server.addr, "/state").await).unwrap();
        assert_eq!(state, "0;/background,/length;");
        assert_eq!(get(server.addr, "/release").await, b"released");
        wait_for_state(server.addr, |state| state == "0;;/background,/length").await;
        server.stop().await;
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn error_and_timeout_before_and_after_headers() {
    tokio::time::timeout(WAIT, async {
        let server = Server::start(Duration::from_secs(1)).await;
        for (path, status) in [("/early-error", 500), ("/early-timeout", 504)] {
            Client::get(server.addr, path).await.head(status).await;
        }
        for path in ["/late-error", "/late-timeout"] {
            let mut client = Client::get(server.addr, path).await;
            client.head(201).await;
            assert_eq!(client.chunk().await.unwrap(), b"begun");
            if path == "/late-error" {
                assert_eq!(get(server.addr, "/release").await, b"released");
            }
            let error = client.chunk().await.unwrap_err();
            assert!(matches!(
                error.kind(),
                io::ErrorKind::UnexpectedEof | io::ErrorKind::ConnectionReset
            ));
        }
        let state = String::from_utf8(get(server.addr, "/state").await).unwrap();
        assert_eq!(
            state,
            "0;;/early-error,/early-timeout,/late-error,/late-timeout"
        );
        server.stop().await;
    })
    .await
    .unwrap();
}

#[cfg(unix)]
#[tokio::test]
async fn sigterm_cancels_active_http_before_lifespan_shutdown() {
    use std::process::Stdio;
    use tokio::process::Command;

    const CHILD: &str = "RIIR_ASGI_SIGTERM_TEST_CHILD";
    if std::env::var_os(CHILD).is_some() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let signal = crate::native::shutdown_signal().await.unwrap();
        let app = Arc::new(App::new(STREAM_APP, 0, 128));
        println!("{}", listener.local_addr().unwrap());
        serve(listener, app, signal).await;
        println!("lifespan and runtime stopped");
        return;
    }

    tokio::time::timeout(WAIT, async {
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "asgi_bridge::tests::http::sigterm_cancels_active_http_before_lifespan_shutdown",
                "--nocapture",
            ])
            .env(CHILD, "1")
            .stdout(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .unwrap();
        let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
        let addr = loop {
            if let Ok(addr) = lines
                .next_line()
                .await
                .unwrap()
                .expect("child exited before opening listener")
                .parse()
            {
                break addr;
            }
        };
        let mut client = Client::get(addr, "/signal").await;
        client.head(201).await;
        assert_eq!(client.chunk().await.unwrap(), b"begun");
        let status = Command::new("kill")
            .args(["-TERM", &child.id().unwrap().to_string()])
            .status()
            .await
            .unwrap();
        assert!(status.success());
        assert!(client.chunk().await.is_err());
        let mut output = String::new();
        while let Some(line) = lines.next_line().await.unwrap() {
            output.push_str(&line);
            output.push('\n');
        }
        assert!(child.wait().await.unwrap().success(), "{output}");
        assert!(
            output.contains("request cleaned\nlifespan and runtime stopped"),
            "{output}"
        );
    })
    .await
    .unwrap();
}
