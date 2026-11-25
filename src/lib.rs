mod asgi_bridge;
mod cli;
mod local_executor;
mod native;
mod python;

const HEALTH_BODY: &str = r#"{"status":"healthy"}"#;
