use std::io;
use std::net::{Ipv4Addr, SocketAddr};

use axum::extract::State;
use axum::http::{HeaderMap, Method, StatusCode, Uri, header};
use axum::response::{IntoResponse, Response};
use axum::routing::on;
use axum::{Router, routing::MethodFilter};
use percent_encoding::percent_decode_str;
use pyo3::prelude::*;
use pyo3::types::PyString;
use serde_json::Value;
use tokio::net::TcpListener;

use crate::{HEALTH_BODY, ServeMode};

/// Recognize the native CLI fast path and leave other forms to Settings.
#[pyfunction]
#[pyo3(signature = (args, env_serve=None))]
pub(crate) fn native_serve_args(
    args: Vec<Bound<'_, PyString>>,
    env_serve: Option<&Bound<'_, PyString>>,
) -> Option<(Option<String>, Option<u16>)> {
    let args = args
        .iter()
        .map(PyStringMethods::to_str)
        .collect::<PyResult<Vec<_>>>()
        .ok()?;
    let (mode, args) = match args.as_slice() {
        ["--mode", mode @ ("dev" | "prod"), rest @ ..] => (Some(*mode), rest),
        args => (None, args),
    };
    let port = match (args, env_serve.is_some()) {
        (["serve", "--native"], false) | (["serve"], true) => None,
        (
            ["serve", "--native", "--port", value] | ["serve", "--port", value, "--native"],
            false,
        )
        | (["serve", "--port", value], true)
            if (1..=5).contains(&value.len())
                && value.bytes().all(|byte| byte.is_ascii_digit()) =>
        {
            Some(value.parse().ok()?)
        }
        _ => return None,
    };
    let env_port = match env_serve {
        Some(value) => native_env_port(value.to_str().ok()?)?,
        None => None,
    };
    Some((mode.map(str::to_owned), port.or(env_port)))
}

/// Accept only the JSON fields and types whose Settings interpretation we mirror.
fn native_env_port(value: &str) -> Option<Option<u16>> {
    let fields = serde_json::from_str::<Value>(value).ok()?;
    let fields = fields.as_object()?;
    if fields.get("native") != Some(&Value::Bool(true))
        || fields
            .keys()
            .any(|key| !matches!(key.as_str(), "native" | "port"))
    {
        return None;
    }
    match fields.get("port") {
        None | Some(Value::Null) => Some(None),
        Some(port) => Some(Some(port.as_u64()?.try_into().ok()?)),
    }
}

fn json(status: StatusCode, body: impl Into<String>) -> Response {
    (
        status,
        [(header::CONTENT_TYPE, "application/json")],
        body.into(),
    )
        .into_response()
}

async fn health() -> Response {
    json(StatusCode::OK, HEALTH_BODY)
}

async fn show() -> Response {
    // Native requests have no application state to format.
    let body = serde_json::json!({"out": "{}"}).to_string();
    json(StatusCode::OK, body)
}

async fn method_not_allowed() -> Response {
    let mut response = json(
        StatusCode::METHOD_NOT_ALLOWED,
        r#"{"detail":"Method Not Allowed"}"#,
    );
    response
        .headers_mut()
        .insert(header::ALLOW, "GET".parse().expect("valid Allow header"));
    response
}

async fn missing(
    State(address): State<SocketAddr>,
    method: Method,
    uri: Uri,
    headers: HeaderMap,
) -> Response {
    let path = percent_decode_str(uri.path()).decode_utf8_lossy();
    match (method, path.as_ref()) {
        (Method::GET, "/health") => return health().await,
        (Method::GET, "/show") => return show().await,
        (_, "/health" | "/show") => return method_not_allowed().await,
        _ => {}
    }

    // Match Starlette's trailing-slash redirect, even for methods it rejects.
    let canonical = path.trim_end_matches('/');
    if matches!(canonical, "/health" | "/show") {
        let address = address.to_string();
        let host = headers
            .get(header::HOST)
            .and_then(|host| host.to_str().ok())
            .unwrap_or(&address);
        let mut location = format!("http://{host}{canonical}");
        if let Some(query) = uri.query().filter(|query| !query.is_empty()) {
            location.push('?');
            location.push_str(query);
        }
        return (
            StatusCode::TEMPORARY_REDIRECT,
            [
                (header::LOCATION, location),
                (header::CONTENT_LENGTH, "0".into()),
            ],
        )
            .into_response();
    }

    json(StatusCode::NOT_FOUND, r#"{"detail":"Not Found"}"#)
}

#[cfg(unix)]
async fn shutdown_signal() -> io::Result<impl Future<Output = ()>> {
    use tokio::signal::unix::{SignalKind, signal};

    let mut interrupt = signal(SignalKind::interrupt())?;
    let mut terminate = signal(SignalKind::terminate())?;
    Ok(async move {
        tokio::select! {
            _ = interrupt.recv() => {}
            _ = terminate.recv() => {}
        }
    })
}

#[cfg(not(unix))]
async fn shutdown_signal() -> io::Result<impl Future<Output = ()>> {
    Ok(async {
        if let Err(error) = tokio::signal::ctrl_c().await {
            eprintln!("Could not listen for Ctrl-C: {error}");
        }
    })
}

async fn run(port: u16) -> io::Result<()> {
    let endpoint = (Ipv4Addr::LOCALHOST, port);
    let listener = TcpListener::bind(endpoint).await.map_err(|error| {
        io::Error::new(
            error.kind(),
            format!("Could not bind {}:{port}: {error}", endpoint.0),
        )
    })?;
    let address = listener.local_addr()?;
    let shutdown = shutdown_signal().await?;
    let router = Router::new()
        // Axum automatically falls back to GET for HEAD unless it has its own handler.
        .route(
            "/health",
            on(MethodFilter::GET, health).head(method_not_allowed),
        )
        .route(
            "/show",
            on(MethodFilter::GET, show).head(method_not_allowed),
        )
        .method_not_allowed_fallback(method_not_allowed)
        .fallback(missing)
        .with_state(address);

    eprintln!("Axum listening on http://{address}");
    axum::serve(listener, router)
        .with_graceful_shutdown(shutdown)
        .await
}

#[pyfunction]
#[pyo3(signature = (mode, port=None))]
pub(crate) fn serve_native(
    py: Python<'_>,
    mode: &Bound<'_, PyString>,
    port: Option<u16>,
) -> PyResult<bool> {
    let Some(mode) = ServeMode::parse(mode)? else {
        return Ok(false);
    };
    let port = port.unwrap_or_else(|| mode.default_port());
    py.detach(|| {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?
            .block_on(run(port))
    })?;
    Ok(true)
}
