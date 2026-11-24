use std::ffi::OsString;

use crate::cli::{self, ServeMode, Subcommand};
use crate::native::serve;
use crate::{EMPTY_SHOW_STATE, HEALTH_BODY};

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

fn py_serve_mode(mode: &Bound<'_, PyString>) -> PyResult<Option<ServeMode>> {
    if mode.as_any().eq("prod")? {
        Ok(Some(ServeMode::Prod))
    } else if mode.as_any().eq("dev")? {
        Ok(Some(ServeMode::Dev))
    } else {
        Ok(None)
    }
}

/// Resolve the Uvicorn options for a supported serve mode.
#[pyfunction]
fn serve_options<'py>(mode: &Bound<'py, PyString>) -> PyResult<Option<Bound<'py, PyDict>>> {
    let Some(serve_mode) = py_serve_mode(mode)? else {
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

/// Build the HTTP response for a successful health check.
#[pyfunction]
fn health_response(py: Python<'_>) -> (Bound<'_, PyBytes>, u16, &'static str) {
    (
        PyBytes::new(py, HEALTH_BODY.as_bytes()),
        200,
        "application/json",
    )
}

/// Build the response for the ASGI request-state endpoint.
#[pyfunction]
fn show_state<'py>(state: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let py = state.py();
    let response = PyDict::new(py);
    // Dict subclasses may override repr even when they are empty.
    if state
        .cast_exact::<PyDict>()
        .is_ok_and(|dict| dict.is_empty())
    {
        response.set_item("out", EMPTY_SHOW_STATE)?;
    } else {
        let formatted = py.import("pprint")?.call_method1("pformat", (state,))?;
        response.set_item("out", formatted)?;
    }
    Ok(response)
}

type ParsedCli = (OsString, &'static str, bool, Option<u16>);
type CliDiagnostic = (u8, String);

/// Parse arguments for the installed CLI.
#[pyfunction]
fn parse_cli(args: Vec<OsString>, program: &str) -> (Option<ParsedCli>, Option<CliDiagnostic>) {
    match cli::parse(args, program) {
        Ok(config) => {
            let (command, native, port) = match config.command {
                Subcommand::Show => ("show", false, None),
                Subcommand::Serve { native, port } => ("serve", native, port),
            };
            (Some((config.mode, command, native, port)), None)
        }
        Err(exit) => (None, Some((exit.code, exit.message))),
    }
}

#[pyfunction]
fn settings_mode() -> OsString {
    cli::settings_mode()
}

#[pyfunction]
#[pyo3(signature = (mode, port=None))]
fn serve_native(py: Python<'_>, mode: &Bound<'_, PyString>, port: Option<u16>) -> PyResult<bool> {
    let Some(mode) = py_serve_mode(mode)? else {
        return Ok(false);
    };
    let port = port.unwrap_or_else(|| mode.default_port());
    py.detach(|| serve(port))?;
    Ok(true)
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
    m.add_function(wrap_pyfunction!(parse_cli, m)?)?;
    m.add_function(wrap_pyfunction!(settings_mode, m)?)?;
    m.add_function(wrap_pyfunction!(serve_native, m)?)?;
    Ok(())
}
