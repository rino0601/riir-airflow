//! Buffered Axum-to-ASGI HTTP conversion.

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use axum::body::{Body, to_bytes};
use axum::http::request::Parts;
use axum::http::{
    HeaderMap, HeaderName, HeaderValue, Method, Request, StatusCode, Version, header,
};
use axum::response::{IntoResponse, Response};
use percent_encoding::percent_decode_str;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyCFunction, PyDict};
use tokio::sync::oneshot;

pub(crate) struct AsgiBridge {
    runtime: Arc<Py<PyAny>>,
    request_limit: usize,
    response_limit: usize,
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
    Python(PyErr),
    InvalidResponse(&'static str),
    CompletionLost,
}

impl IntoResponse for BridgeError {
    fn into_response(self) -> Response {
        match self {
            Self::RequestTooLarge => (StatusCode::PAYLOAD_TOO_LARGE, "Request body too large"),
            Self::RequestBody(_) => (StatusCode::BAD_REQUEST, "Could not read request body"),
            Self::UnsupportedVersion => (
                StatusCode::HTTP_VERSION_NOT_SUPPORTED,
                "Unsupported HTTP version",
            ),
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
        }
    }

    pub(crate) async fn call(
        &self,
        request: Request<Body>,
        connection: ConnectionInfo,
    ) -> Result<Response, BridgeError> {
        let (parts, body) = request.into_parts();
        let version = match parts.version {
            Version::HTTP_10 => "1.0",
            Version::HTTP_11 => "1.1",
            Version::HTTP_2 => "2",
            Version::HTTP_3 => "3",
            _ => return Err(BridgeError::UnsupportedVersion),
        };
        let body = to_bytes(body, self.request_limit).await.map_err(|error| {
            if std::error::Error::source(&error)
                .is_some_and(|source| source.is::<http_body_util::LengthLimitError>())
            {
                BridgeError::RequestTooLarge
            } else {
                BridgeError::RequestBody(error)
            }
        })?;
        let (sender, receiver) = oneshot::channel();
        let runtime = self.runtime.clone();
        let response_limit = self.response_limit;

        // Even attaching to Python can wait for the GIL. Only submission runs
        // on the blocking pool. The Python loop completes the request by callback.
        tokio::task::spawn_blocking(move || {
            Python::attach(|py| {
                let is_head = parts.method == Method::HEAD;
                let scope = scope(py, parts, version, connection)?;
                let sender = Mutex::new(Some(sender));
                let done = PyCFunction::new_closure(
                    py,
                    Some(c"complete_asgi_http"),
                    None,
                    move |args, _| {
                        let result = complete(args, is_head);
                        if let Some(sender) =
                            sender.lock().expect("completion lock poisoned").take()
                        {
                            let _ = sender.send(result);
                        }
                    },
                )?;
                py.import("riir_airflow._core.asgi_http")?.call_method1(
                    "submit_http",
                    (
                        runtime.bind(py),
                        scope,
                        PyBytes::new(py, &body),
                        response_limit,
                        done,
                    ),
                )?;
                Ok(())
            })
        })
        .await
        .map_err(|_| BridgeError::CompletionLost)?
        .map_err(BridgeError::Python)?;
        receiver.await.map_err(|_| BridgeError::CompletionLost)?
    }
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

type BufferedResponse<'py> = (
    u16,
    Vec<(Bound<'py, PyBytes>, Bound<'py, PyBytes>)>,
    Bound<'py, PyBytes>,
);

fn complete(
    args: &Bound<'_, pyo3::types::PyTuple>,
    is_head: bool,
) -> Result<Response, BridgeError> {
    let (response, error): (Bound<'_, PyAny>, Bound<'_, PyAny>) =
        args.extract().map_err(BridgeError::Python)?;
    if !error.is_none() {
        return Err(BridgeError::Python(PyErr::from_value(error)));
    }
    let (status, headers, body): BufferedResponse<'_> =
        response.extract().map_err(BridgeError::Python)?;
    let status = StatusCode::from_u16(status)
        .map_err(|_| BridgeError::InvalidResponse("Invalid response status"))?;
    let mut http_headers = HeaderMap::new();
    // HeaderMap retains each name's values in append order.
    for (name, value) in headers {
        let name = HeaderName::from_bytes(name.as_bytes())
            .map_err(|_| BridgeError::InvalidResponse("Invalid response header name"))?;
        if name == header::TRANSFER_ENCODING {
            continue;
        }
        let value = HeaderValue::from_bytes(value.as_bytes())
            .map_err(|_| BridgeError::InvalidResponse("Invalid response header value"))?;
        http_headers
            .try_append(name, value)
            .map_err(|_| BridgeError::InvalidResponse("Too many response headers"))?;
    }
    let body = body.as_bytes();
    validate_content_length(&http_headers, status, body.len(), is_head)?;
    let mut response = Response::new(Body::from(body.to_vec()));
    *response.status_mut() = status;
    *response.headers_mut() = http_headers;
    Ok(response)
}

fn validate_content_length(
    headers: &HeaderMap,
    status: StatusCode,
    body_len: usize,
    is_head: bool,
) -> Result<(), BridgeError> {
    let no_content = matches!(
        status,
        StatusCode::NO_CONTENT | StatusCode::RESET_CONTENT | StatusCode::NOT_MODIFIED
    );
    if no_content && body_len != 0 {
        return Err(BridgeError::InvalidResponse(
            "Response status forbids a body",
        ));
    }
    let mut values = headers.get_all(header::CONTENT_LENGTH).iter();
    if let Some(value) = values.next() {
        // Hyper expects the advertised length to agree with the buffered body.
        let length = value
            .to_str()
            .ok()
            .filter(|value| value.bytes().all(|byte| byte.is_ascii_digit()))
            .and_then(|value| value.parse::<u64>().ok());
        let metadata_only = body_len == 0 && (is_head || status == StatusCode::NOT_MODIFIED);
        if status == StatusCode::NO_CONTENT
            || values.next().is_some()
            || length.is_none()
            || (!metadata_only && length != Some(body_len as u64))
            || (status == StatusCode::RESET_CONTENT && length != Some(0))
        {
            return Err(BridgeError::InvalidResponse(
                "Invalid response Content-Length",
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests;
