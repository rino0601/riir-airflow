// The native fallback will call the adapter once route wiring lands.
#[allow(dead_code)]
mod asgi_bridge;
mod cli;
mod native;
mod python;

const HEALTH_BODY: &str = r#"{"status":"healthy"}"#;
const EMPTY_SHOW_STATE: &str = "{}";
