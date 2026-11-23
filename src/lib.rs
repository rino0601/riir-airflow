pub mod cli;
mod native;

pub use native::serve;

#[cfg(feature = "python")]
mod python;

const HEALTH_BODY: &str = r#"{"status":"healthy"}"#;
