//! Stream Axum HTTP exchanges on the application's asyncio runtime.

use std::fmt;
use std::future::Future;
use std::net::SocketAddr;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::body::{Body, Bytes, HttpBody};
use axum::http::request::Parts;
use axum::http::{HeaderMap, HeaderName, HeaderValue, Request, StatusCode, Version, header};
use axum::response::{IntoResponse, Response};
use http_body_util::BodyExt;
use percent_encoding::percent_decode_str;
use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use pyo3::types::{PyBytes, PyDict};
use pyo3_async_runtimes::{TaskLocals, generic};
use tokio::runtime::Handle;
use tokio::sync::{mpsc, oneshot, watch};

const FRAME_SIZE: usize = 64 * 1024;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(60);

pub(crate) struct AsgiBridge {
    runtime: Arc<Py<PyAny>>,
    request_limit: usize,
    response_limit: usize,
    timeout: Duration,
}

pub(crate) struct ConnectionInfo {
    pub scheme: String,
    pub root_path: String,
    pub client: Option<SocketAddr>,
    pub server: Option<SocketAddr>,
}

#[derive(Debug)]
pub(crate) enum BridgeError {
    RequestTooLarge,
    RequestBody(axum::Error),
    UnsupportedVersion,
    Timeout,
    Python(PyErr),
    Cancelled,
    CompletionLost,
}

impl fmt::Display for BridgeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::RequestBody(error) => write!(formatter, "could not read request body: {error}"),
            Self::Python(error) => write!(formatter, "{error}"),
            _ => write!(formatter, "{self:?}"),
        }
    }
}

impl std::error::Error for BridgeError {}

impl IntoResponse for BridgeError {
    fn into_response(self) -> Response {
        match self {
            Self::RequestTooLarge => (StatusCode::PAYLOAD_TOO_LARGE, "Request body too large"),
            Self::RequestBody(_) => (StatusCode::BAD_REQUEST, "Could not read request body"),
            Self::UnsupportedVersion => (
                StatusCode::HTTP_VERSION_NOT_SUPPORTED,
                "Unsupported HTTP version",
            ),
            Self::Timeout => (StatusCode::GATEWAY_TIMEOUT, "ASGI request timed out"),
            _ => (StatusCode::INTERNAL_SERVER_ERROR, "ASGI request failed"),
        }
        .into_response()
    }
}

impl AsgiBridge {
    pub(crate) fn new(runtime: Py<PyAny>, request_limit: usize, response_limit: usize) -> Self {
        Self {
            runtime: Arc::new(runtime),
            request_limit,
            response_limit,
            timeout: REQUEST_TIMEOUT,
        }
    }

    /// Run alongside Axum's graceful drain after the shutdown signal.
    pub(crate) async fn close(&self) -> Result<(), BridgeError> {
        let runtime = self.runtime.clone();
        tokio::task::spawn_blocking(move || {
            Python::attach(|py| runtime.bind(py).call_method0("close").map(|_| ()))
        })
        .await
        .map_err(|_| BridgeError::CompletionLost)?
        .map_err(BridgeError::Python)
    }

    pub(crate) async fn call(
        &self,
        request: Request<Body>,
        connection: ConnectionInfo,
    ) -> Result<Response, BridgeError> {
        let (parts, body) = request.into_parts();
        let is_head = parts.method == axum::http::Method::HEAD;
        let version = match parts.version {
            Version::HTTP_10 => "1.0",
            Version::HTTP_11 => "1.1",
            Version::HTTP_2 => "2",
            Version::HTTP_3 => "3",
            _ => return Err(BridgeError::UnsupportedVersion),
        };
        if body.size_hint().lower() > self.request_limit as u64 {
            return Err(BridgeError::RequestTooLarge);
        }

        let (head_tx, head_rx) = oneshot::channel();
        let (body_tx, body_rx) = mpsc::channel(1);
        let (end_tx, end_rx) = oneshot::channel();
        let (closed, dropped) = watch::channel(false);
        let (response_complete, _) = watch::channel(false);
        let (done_tx, done_rx) = oneshot::channel();
        let runtime = self.runtime.clone();
        let response_limit = self.response_limit;
        let timeout = self.timeout.as_secs_f64();
        let transport = Transport {
            handle: Handle::current(),
            request: Arc::new(tokio::sync::Mutex::new(Input {
                body,
                remaining: self.request_limit,
                finished: false,
            })),
            pending_input: Arc::new(Mutex::new(None)),
            request_error: Arc::new(Mutex::new(None)),
            output: Arc::new(Mutex::new(Output {
                head: None,
                head_tx: Some(head_tx),
                body_tx: Some(body_tx),
                end_tx: Some(end_tx),
                done_tx: Some(done_tx),
            })),
            dropped: dropped.clone(),
            response_complete,
        };
        let guard = ExchangeGuard(Some(closed));

        // Own submission even if Hyper drops the handler before Python attaches.
        tokio::spawn(async move {
            let submission = tokio::task::spawn_blocking(move || {
                Python::attach(|py| -> PyResult<_> {
                    let scope = scope(py, parts, version, connection)?;
                    let transport = Py::new(py, transport)?;
                    let future = py.import("riir_airflow._core.asgi_http")?.call_method1(
                        "submit_http",
                        (runtime.bind(py), scope, &transport, response_limit, timeout),
                    );
                    if let Err(error) = &future {
                        transport
                            .borrow(py)
                            .finish(Err(BridgeError::Python(error.clone_ref(py))));
                    }
                    Ok(future?.unbind())
                })
            })
            .await;
            if let Ok(Ok(future)) = submission {
                let mut disconnected = dropped;
                let cancelled = tokio::select! {
                    biased;
                    _ = done_rx => false,
                    result = disconnected.wait_for(|dropped| *dropped) => result.is_ok(),
                };
                if cancelled {
                    let _ = tokio::task::spawn_blocking(move || {
                        Python::attach(|py| future.bind(py).call_method0("cancel").map(|_| ()))
                    })
                    .await;
                }
            }
        });

        let head = head_rx.await.map_err(|_| BridgeError::CompletionLost)??;
        let body = if head.suppress_body || head.length == Some(0) {
            guard.finish();
            if is_head && head.length.is_none() {
                // Axum infers Content-Length from the size hint before stripping a HEAD body.
                Body::from_stream(futures_util::stream::empty::<Result<Bytes, BridgeError>>())
            } else {
                Body::empty()
            }
        } else {
            Body::from_stream(futures_util::stream::unfold(
                ResponseStream {
                    body: body_rx,
                    end: Some(end_rx),
                    remaining: head.length,
                    guard,
                },
                ResponseStream::next,
            ))
        };
        let mut response = Response::new(body);
        *response.status_mut() = head.status;
        *response.headers_mut() = head.headers;
        Ok(response)
    }
}

struct ResponseStream {
    body: mpsc::Receiver<Bytes>,
    end: Option<oneshot::Receiver<Result<(), BridgeError>>>,
    remaining: Option<u64>,
    guard: ExchangeGuard,
}

impl ResponseStream {
    async fn next(mut self) -> Option<(Result<Bytes, BridgeError>, Self)> {
        if let Some(bytes) = self.body.recv().await {
            if let Some(remaining) = &mut self.remaining {
                *remaining -= bytes.len() as u64;
                if *remaining == 0 {
                    // Hyper can stop polling as soon as Content-Length is satisfied.
                    self.guard.disarm();
                }
            }
            return Some((Ok(bytes), self));
        }
        match self.end.take()?.await {
            Ok(Ok(())) => {
                self.guard.disarm();
                None
            }
            Ok(Err(error)) => Some((Err(error), self)),
            Err(_) => Some((Err(BridgeError::CompletionLost), self)),
        }
    }
}

struct ExchangeGuard(Option<watch::Sender<bool>>);

impl ExchangeGuard {
    fn disarm(&mut self) {
        self.0.take();
    }

    fn finish(mut self) {
        self.disarm();
    }
}

impl Drop for ExchangeGuard {
    fn drop(&mut self) {
        if let Some(dropped) = &self.0 {
            dropped.send_replace(true);
        }
    }
}

// Run crossover futures on the server's Tokio executor. Each call originates
// on the Python loop, so it can capture the current asyncio context directly.
struct ServerRuntime;

impl generic::Runtime for ServerRuntime {
    type JoinError = tokio::task::JoinError;
    type JoinHandle = tokio::task::JoinHandle<()>;

    fn spawn<F: Future<Output = ()> + Send + 'static>(future: F) -> Self::JoinHandle {
        tokio::spawn(future)
    }

    fn spawn_blocking<F: FnOnce() + Send + 'static>(function: F) -> Self::JoinHandle {
        tokio::task::spawn_blocking(function)
    }
}

impl generic::ContextExt for ServerRuntime {
    fn scope<F, T>(_: TaskLocals, future: F) -> Pin<Box<dyn Future<Output = T> + Send>>
    where
        F: Future<Output = T> + Send + 'static,
    {
        Box::pin(future)
    }

    fn get_task_locals() -> Option<TaskLocals> {
        None
    }
}

struct Input {
    body: Body,
    remaining: usize,
    finished: bool,
}

impl Input {
    async fn next(&mut self) -> Result<(Bytes, bool), BridgeError> {
        if self.finished {
            return std::future::pending().await;
        }
        while let Some(frame) = self.body.frame().await {
            if let Ok(bytes) = frame
                .inspect_err(|_| self.finished = true)
                .map_err(BridgeError::RequestBody)?
                .into_data()
            {
                let Some(remaining) = self.remaining.checked_sub(bytes.len()) else {
                    self.finished = true;
                    return Err(BridgeError::RequestTooLarge);
                };
                self.remaining = remaining;
                self.finished = self.body.is_end_stream();
                return Ok((bytes, !self.finished));
            }
        }
        self.finished = true;
        Ok((Bytes::new(), false))
    }
}

type RequestChunk = (Bytes, bool);

#[pyclass]
struct Transport {
    handle: Handle,
    request: Arc<tokio::sync::Mutex<Input>>,
    pending_input: Arc<Mutex<Option<RequestChunk>>>,
    request_error: Arc<Mutex<Option<BridgeError>>>,
    output: Arc<Mutex<Output>>,
    dropped: watch::Receiver<bool>,
    response_complete: watch::Sender<bool>,
}

struct Output {
    head: Option<ResponseHead>,
    head_tx: Option<oneshot::Sender<Result<ResponseHead, BridgeError>>>,
    body_tx: Option<mpsc::Sender<Bytes>>,
    end_tx: Option<oneshot::Sender<Result<(), BridgeError>>>,
    done_tx: Option<oneshot::Sender<()>>,
}

impl Output {
    fn publish_head(&mut self) {
        if let Some(sender) = self.head_tx.take() {
            let result = self.head.take().ok_or(BridgeError::CompletionLost);
            let _ = sender.send(result);
        }
    }

    fn finish_body(&mut self, result: Result<(), BridgeError>) {
        if let Some(sender) = self.end_tx.take() {
            let _ = sender.send(result);
        }
        self.body_tx.take();
    }
}

#[pymethods]
impl Transport {
    fn receive<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let request = self.request.clone();
        let pending = self.pending_input.clone();
        let request_error = self.request_error.clone();
        let mut dropped = self.dropped.clone();
        let mut complete = self.response_complete.subscribe();
        let _runtime = self.handle.enter();
        generic::future_into_py::<ServerRuntime, _, _>(py, async move {
            if request_error.lock().unwrap().is_some() {
                return Ok(None);
            }
            tokio::select! {
                biased;
                _ = complete.wait_for(|finished| *finished) => Ok(None),
                _ = dropped.wait_for(|dropped| *dropped) => Ok(None),
                result = async {
                    let mut input = request.lock().await;
                    let chunk = pending.lock().unwrap().clone();
                    if let Some(chunk) = chunk {
                        return Ok(chunk);
                    }
                    let chunk = input.next().await?;
                    // An asyncio caller can cancel its await after Hyper has yielded
                    // a frame. Keep that frame until Python consumes the result.
                    *pending.lock().unwrap() = Some(chunk.clone());
                    Ok(chunk)
                } => match result {
                    Ok((bytes, more)) => Ok(Some((bytes.to_vec(), more))),
                    Err(error @ BridgeError::RequestBody(_)) => {
                        request_error.lock().unwrap().get_or_insert(error);
                        Ok(None)
                    }
                    Err(error) => {
                        request_error.lock().unwrap().get_or_insert(error);
                        Err(PyOSError::new_err("Could not read the HTTP request body"))
                    }
                },
            }
        })
    }

    fn consume_request_chunk(&self) {
        self.pending_input.lock().unwrap().take();
    }

    fn start(
        &self,
        status: u16,
        headers: Vec<(PyBackedBytes, PyBackedBytes)>,
        is_head: bool,
    ) -> PyResult<(Option<u64>, bool, bool)> {
        let head = response_head(status, headers, is_head).map_err(PyValueError::new_err)?;
        let properties = (head.length, head.forbids_body, head.suppress_body);
        self.output.lock().unwrap().head = Some(head);
        Ok(properties)
    }

    fn send_body<'py>(
        &self,
        py: Python<'py>,
        bytes: PyBackedBytes,
        final_body: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        if self.request_error.lock().unwrap().is_some() {
            return Err(PyOSError::new_err("Could not read the HTTP request body"));
        }
        let output = self.output.clone();
        let sender = {
            let mut output = output.lock().unwrap();
            if !bytes.as_ref().is_empty() || final_body {
                output.publish_head();
            }
            output.body_tx.clone().ok_or_else(disconnected)?
        };
        let complete = self.response_complete.clone();
        let _runtime = self.handle.enter();
        generic::future_into_py::<ServerRuntime, _, _>(py, async move {
            for chunk in bytes.as_ref().chunks(FRAME_SIZE) {
                let slot = sender.reserve().await.map_err(|_| disconnected())?;
                slot.send(Bytes::copy_from_slice(chunk));
            }
            if final_body {
                output.lock().unwrap().finish_body(Ok(()));
                complete.send_replace(true);
            }
            Ok(())
        })
    }

    fn done(&self, error: Bound<'_, PyAny>, timed_out: bool, cancelled: bool) {
        let result = if timed_out {
            Err(BridgeError::Timeout)
        } else if cancelled {
            Err(BridgeError::Cancelled)
        } else if error.is_none() {
            Ok(())
        } else {
            Err(BridgeError::Python(PyErr::from_value(error)))
        };
        self.finish(result);
    }
}

impl Transport {
    fn finish(&self, result: Result<(), BridgeError>) {
        self.response_complete.send_replace(true);
        let mut output = self.output.lock().unwrap();
        if let Some(sender) = output.done_tx.take() {
            let _ = sender.send(());
        }
        let result = self
            .request_error
            .lock()
            .unwrap()
            .take()
            .map_or(result, Err);
        match result {
            Ok(()) => output.finish_body(Ok(())),
            Err(error) => {
                if let Some(sender) = output.head_tx.take() {
                    let _ = sender.send(Err(error));
                    output.finish_body(Ok(()));
                } else {
                    let log = !matches!(error, BridgeError::Cancelled) && !*self.dropped.borrow();
                    if log {
                        eprintln!("ASGI response stream failed: {error:?}");
                    }
                    output.finish_body(Err(error));
                }
            }
        }
    }
}

fn disconnected() -> PyErr {
    PyOSError::new_err("HTTP client disconnected")
}

fn address(value: SocketAddr) -> (String, u16) {
    (value.ip().to_string(), value.port())
}

fn scope<'py>(
    py: Python<'py>,
    parts: Parts,
    version: &str,
    connection: ConnectionInfo,
) -> PyResult<Bound<'py, PyDict>> {
    let asgi = PyDict::new(py);
    asgi.set_item("version", "3.0")?;
    asgi.set_item("spec_version", "2.0")?;

    let scope = PyDict::new(py);
    let path = parts.uri.path();
    let headers: Vec<_> = parts
        .headers
        .iter()
        .map(|(name, value)| {
            (
                PyBytes::new(py, name.as_ref()),
                PyBytes::new(py, value.as_bytes()),
            )
        })
        .collect();
    scope.set_item("type", "http")?;
    scope.set_item("asgi", asgi)?;
    scope.set_item("http_version", version)?;
    scope.set_item("method", parts.method.as_str())?;
    scope.set_item("scheme", connection.scheme)?;
    scope.set_item(
        "path",
        percent_decode_str(path).decode_utf8_lossy().as_ref(),
    )?;
    scope.set_item("raw_path", PyBytes::new(py, path.as_bytes()))?;
    scope.set_item(
        "query_string",
        PyBytes::new(py, parts.uri.query().unwrap_or_default().as_bytes()),
    )?;
    scope.set_item("root_path", connection.root_path)?;
    scope.set_item("headers", headers)?;
    scope.set_item("client", connection.client.map(address))?;
    scope.set_item("server", connection.server.map(address))?;
    Ok(scope)
}

struct ResponseHead {
    status: StatusCode,
    headers: HeaderMap,
    length: Option<u64>,
    forbids_body: bool,
    suppress_body: bool,
}

fn response_head(
    status: u16,
    headers: Vec<(PyBackedBytes, PyBackedBytes)>,
    is_head: bool,
) -> Result<ResponseHead, &'static str> {
    let status = StatusCode::from_u16(status).map_err(|_| "Invalid response status")?;
    let mut http_headers = HeaderMap::new();
    // HeaderMap retains each name's values in append order.
    for (name, value) in headers {
        let name =
            HeaderName::from_bytes(name.as_ref()).map_err(|_| "Invalid response header name")?;
        if name == header::TRANSFER_ENCODING {
            continue;
        }
        let value =
            HeaderValue::from_bytes(value.as_ref()).map_err(|_| "Invalid response header value")?;
        http_headers
            .try_append(name, value)
            .map_err(|_| "Too many response headers")?;
    }
    let invalid = "Invalid response Content-Length";
    let mut lengths = http_headers.get_all(header::CONTENT_LENGTH).iter();
    let length = if let Some(value) = lengths.next() {
        let length = value
            .to_str()
            .ok()
            .filter(|value| value.bytes().all(|byte| byte.is_ascii_digit()))
            .and_then(|value| value.parse::<u64>().ok())
            .ok_or(invalid)?;
        if lengths.next().is_some()
            || status == StatusCode::NO_CONTENT
            || (status == StatusCode::RESET_CONTENT && length != 0)
        {
            return Err(invalid);
        }
        Some(length)
    } else {
        None
    };
    let forbids_body = matches!(
        status,
        StatusCode::NO_CONTENT | StatusCode::RESET_CONTENT | StatusCode::NOT_MODIFIED
    );
    Ok(ResponseHead {
        status,
        headers: http_headers,
        length,
        forbids_body,
        suppress_body: is_head || forbids_body,
    })
}

#[cfg(test)]
mod tests;
