use std::io;
use std::net::{Ipv4Addr, SocketAddr};

use axum::extract::State;
use axum::http::{HeaderMap, Method, StatusCode, Uri, header};
use axum::response::{IntoResponse, Response};
use axum::routing::on;
use axum::{Router, routing::MethodFilter};
use percent_encoding::percent_decode_str;
use tokio::net::TcpListener;

use crate::{EMPTY_SHOW_STATE, HEALTH_BODY};

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
    let body = serde_json::json!({"out": EMPTY_SHOW_STATE}).to_string();
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
pub(crate) async fn shutdown_signal() -> io::Result<impl Future<Output = ()>> {
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
pub(crate) async fn shutdown_signal() -> io::Result<impl Future<Output = ()>> {
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

pub fn serve(port: u16) -> io::Result<()> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(run(port))
}
