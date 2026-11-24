use std::io;
use std::net::{Ipv4Addr, SocketAddr};
use std::sync::Arc;

use axum::extract::{ConnectInfo, Request, State};
use axum::http::{Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::on;
use axum::{Router, routing::MethodFilter};
use percent_encoding::percent_decode_str;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use tokio::net::TcpListener;

use crate::asgi_bridge::{AsgiBridge, ConnectionInfo};
use crate::{EMPTY_SHOW_STATE, HEALTH_BODY};

const ASGI_BODY_LIMIT: usize = 64 * 1024 * 1024;

struct ServerState {
    address: SocketAddr,
    bridge: Option<AsgiBridge>,
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
    State(state): State<Arc<ServerState>>,
    ConnectInfo(client): ConnectInfo<SocketAddr>,
    request: Request,
) -> Response {
    let uri = request.uri();
    let path = percent_decode_str(uri.path()).decode_utf8_lossy();
    match (request.method(), path.as_ref()) {
        (&Method::GET, "/health") => return health().await,
        (&Method::GET, "/show") => return show().await,
        (_, "/health" | "/show") => return method_not_allowed().await,
        _ => {}
    }

    // Match Starlette's trailing-slash redirect, even for methods it rejects.
    let canonical = path.trim_end_matches('/');
    if matches!(canonical, "/health" | "/show") {
        let address = state.address.to_string();
        let host = request
            .headers()
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

    if let Some(bridge) = &state.bridge {
        let connection = ConnectionInfo {
            scheme: "http".into(),
            root_path: String::new(),
            client: Some(client),
            server: Some(state.address),
        };
        return bridge
            .call(request, connection)
            .await
            .unwrap_or_else(|error| {
                eprintln!("ASGI fallback failed: {error}");
                error.into_response()
            });
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

fn start_bridge(mode: &str) -> PyResult<Option<AsgiBridge>> {
    Python::attach(|py| {
        let airflow = py
            .import("importlib.util")?
            .call_method1("find_spec", ("airflow",))?;
        if airflow.is_none() {
            return Ok(None);
        }
        let runtime = py
            .import("riir_airflow._core.compat_app")?
            .call_method1("start_native_runtime", (mode,))?;
        Ok(Some(AsgiBridge::new(
            runtime.unbind(),
            ASGI_BODY_LIMIT,
            ASGI_BODY_LIMIT,
        )))
    })
}

async fn run(port: u16, mode: &str) -> PyResult<()> {
    let endpoint = (Ipv4Addr::LOCALHOST, port);
    let listener = TcpListener::bind(endpoint).await.map_err(|error| {
        io::Error::new(
            error.kind(),
            format!("Could not bind {}:{port}: {error}", endpoint.0),
        )
    })?;
    let address = listener.local_addr()?;
    let shutdown = shutdown_signal().await?;
    let state = Arc::new(ServerState {
        address,
        bridge: start_bridge(mode)?,
    });
    let router = router(state.clone());

    eprintln!("Axum listening on http://{address}");
    let served = axum::serve(
        listener,
        router.into_make_service_with_connect_info::<SocketAddr>(),
    )
    .with_graceful_shutdown(shutdown)
    .await;
    if let Some(bridge) = &state.bridge {
        bridge
            .close()
            .await
            .map_err(|error| PyRuntimeError::new_err(format!("ASGI shutdown failed: {error}")))?;
    }
    Ok(served?)
}

fn router(state: Arc<ServerState>) -> Router {
    Router::new()
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
        .with_state(state)
}

pub fn serve(port: u16, mode: &str) -> PyResult<()> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(run(port, mode))
}

#[cfg(test)]
mod tests {
    use axum::extract::Path;
    use axum::routing::get;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpStream;

    use super::*;

    #[tokio::test]
    async fn native_handler_errors_do_not_reach_the_fallback() {
        Python::initialize();
        // Any call to this fake runtime fails with a different response body.
        let runtime = Python::attach(|py| py.None());
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
        let address = listener.local_addr().unwrap();
        let app = router(Arc::new(ServerState {
            address,
            bridge: Some(AsgiBridge::new(runtime, 1024, 1024)),
        }))
        .route(
            "/native-error/{status}",
            get(|Path(status): Path<u16>| async move {
                (StatusCode::from_u16(status).unwrap(), "native error")
            }),
        );
        let server = tokio::spawn(async move {
            axum::serve(
                listener,
                app.into_make_service_with_connect_info::<SocketAddr>(),
            )
            .await
            .unwrap();
        });

        for status in [404, 405, 500] {
            let mut stream = TcpStream::connect(address).await.unwrap();
            let request = format!(
                "GET /native-error/{status} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            );
            stream.write_all(request.as_bytes()).await.unwrap();
            let mut response = String::new();
            tokio::time::timeout(
                std::time::Duration::from_secs(2),
                stream.read_to_string(&mut response),
            )
            .await
            .unwrap()
            .unwrap();
            assert!(response.starts_with(&format!("HTTP/1.1 {status} ")));
            assert!(response.ends_with("\r\n\r\nnative error"));
        }
        server.abort();
    }
}
