use std::ffi::OsString;
use std::path::PathBuf;

use crate::HEALTH_BODY;
use crate::cli::{self, ServeMode, Subcommand};
use crate::local_executor::drain_local_commands;
use crate::native::serve;
use crate::scheduler;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyString, PyTuple};

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

type CliDiagnostic = (u8, String);

/// Parse arguments for the installed CLI.
#[pyfunction]
fn parse_cli<'py>(
    py: Python<'py>,
    args: Vec<OsString>,
    program: &str,
) -> PyResult<(Option<Bound<'py, PyTuple>>, Option<CliDiagnostic>)> {
    match cli::parse(args, program) {
        Ok(config) => {
            let mode = config.mode;
            let parsed = match config.command {
                Subcommand::Show => (mode, "show", false, Option::<u16>::None).into_pyobject(py)?,
                Subcommand::Serve { native, port } => {
                    (mode, "serve", native, port).into_pyobject(py)?
                }
                Subcommand::SchedulerStatus { database } => (
                    mode,
                    "scheduler-status",
                    false,
                    Option::<u16>::None,
                    database,
                )
                    .into_pyobject(py)?,
            };
            Ok((Some(parsed), None))
        }
        Err(exit) => Ok((None, Some((exit.code, exit.message)))),
    }
}

#[pyfunction]
fn scheduler_status(py: Python<'_>, database: PathBuf) -> PyResult<String> {
    py.detach(|| scheduler::status(&database))
        .map(|status| status.to_string())
        .map_err(|error| PyRuntimeError::new_err(error.to_string()))
}

#[pyfunction]
fn settings_mode() -> OsString {
    cli::settings_mode()
}

#[pyfunction]
#[pyo3(signature = (mode, port=None))]
fn serve_native(py: Python<'_>, mode: &Bound<'_, PyString>, port: Option<u16>) -> PyResult<bool> {
    let Some(serve_mode) = py_serve_mode(mode)? else {
        return Ok(false);
    };
    let port = port.unwrap_or_else(|| serve_mode.default_port());
    let mode = mode.to_str()?.to_owned();
    py.detach(move || serve(port, &mode))?;
    Ok(true)
}

/// A Python module implemented in Rust.
#[pymodule]
fn _lowlevel(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(hello, m)?)?;
    m.add_function(wrap_pyfunction!(drain_local_commands, m)?)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(format_show_mode, m)?)?;
    m.add_function(wrap_pyfunction!(serve_options, m)?)?;
    m.add_function(wrap_pyfunction!(airflow_mount_path, m)?)?;
    m.add_function(wrap_pyfunction!(health_response, m)?)?;
    m.add_function(wrap_pyfunction!(parse_cli, m)?)?;
    m.add_function(wrap_pyfunction!(scheduler_status, m)?)?;
    m.add_function(wrap_pyfunction!(scheduler::manual_dag_run_candidates, m)?)?;
    m.add_function(wrap_pyfunction!(scheduler::start_manual_dag_run, m)?)?;
    m.add_function(wrap_pyfunction!(scheduler::select_sqlite_task, m)?)?;
    m.add_function(wrap_pyfunction!(scheduler::queue_sqlite_task, m)?)?;
    m.add_function(wrap_pyfunction!(settings_mode, m)?)?;
    m.add_function(wrap_pyfunction!(serve_native, m)?)?;
    Ok(())
}
