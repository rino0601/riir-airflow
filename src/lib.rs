use pyo3::prelude::*;
use pyo3::types::PyString;

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

/// A Python module implemented in Rust.
#[pymodule]
fn _lowlevel(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(hello, m)?)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(format_show_mode, m)?)?;
    Ok(())
}
