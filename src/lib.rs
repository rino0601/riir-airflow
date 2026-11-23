mod native;

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyString};

/// Prints a message.
#[pyfunction]
fn hello() -> PyResult<String> {
    Ok("Hello from riir-airflow!".into())
}

#[pyfunction]
fn version() -> PyResult<String> {
    Ok(env!("CARGO_PKG_VERSION").into())
}

#[pyfunction]
fn format_show_mode(mode: &Bound<'_, PyString>) -> PyResult<String> {
    let mode_repr = mode.repr()?;
    Ok(format!("show \"settings.mode={}\"", mode_repr.to_str()?))
}

enum ServeMode {
    Prod,
    Dev,
}

impl ServeMode {
    fn parse(mode: &Bound<'_, PyString>) -> PyResult<Option<Self>> {
        if mode.as_any().eq("prod")? {
            Ok(Some(Self::Prod))
        } else if mode.as_any().eq("dev")? {
            Ok(Some(Self::Dev))
        } else {
            Ok(None)
        }
    }

    fn default_port(&self) -> u16 {
        match self {
            Self::Prod => 80,
            Self::Dev => 8080,
        }
    }
}

/// Resolve the Uvicorn options for a supported serve mode.
#[pyfunction]
fn serve_options<'py>(mode: &Bound<'py, PyString>) -> PyResult<Option<Bound<'py, PyDict>>> {
    let Some(serve_mode) = ServeMode::parse(mode)? else {
        return Ok(None);
    };
    let options = PyDict::new(mode.py());
    options.set_item("port", serve_mode.default_port())?;
    match serve_mode {
        ServeMode::Prod => {
            options.set_item("log_level", "info")?;
            options.set_item("workers", 1)?;
        }
        ServeMode::Dev => options.set_item("reload", true)?,
    }
    Ok(Some(options))
}

/// Resolve the Airflow mount path for the ASGI application.
#[pyfunction]
fn airflow_mount_path(mode: &Bound<'_, PyAny>) -> PyResult<Option<&'static str>> {
    Ok(mode.ne("dev")?.then_some("/airflow"))
}

const HEALTH_BODY: &str = r#"{"status":"healthy"}"#;

/// Build the HTTP response for a successful health check.
#[pyfunction]
fn health_response(py: Python<'_>) -> (Bound<'_, PyBytes>, u16, &'static str) {
    (
        PyBytes::new(py, HEALTH_BODY.as_bytes()),
        200,
        "application/json",
    )
}

fn format_show_state<'py>(state: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    state
        .py()
        .import("pprint")?
        .call_method1("pformat", (state,))
}

/// Build the response for the ASGI request-state endpoint.
#[pyfunction]
fn show_state<'py>(state: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let py = state.py();
    let formatted = format_show_state(state)?;
    let response = PyDict::new(py);
    response.set_item("out", formatted)?;
    Ok(response)
}

/// A Python module implemented in Rust.
#[pymodule]
fn _lowlevel(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(hello, m)?)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(format_show_mode, m)?)?;
    m.add_function(wrap_pyfunction!(serve_options, m)?)?;
    m.add_function(wrap_pyfunction!(airflow_mount_path, m)?)?;
    m.add_function(wrap_pyfunction!(health_response, m)?)?;
    m.add_function(wrap_pyfunction!(show_state, m)?)?;
    m.add_function(wrap_pyfunction!(native::native_serve_args, m)?)?;
    m.add_function(wrap_pyfunction!(native::serve_native, m)?)?;
    Ok(())
}
