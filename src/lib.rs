mod native;
mod native_cli;

pub use native::serve;
pub use native_cli::native_serve_port;

#[cfg(feature = "python")]
mod python;

const HEALTH_BODY: &str = r#"{"status":"healthy"}"#;
