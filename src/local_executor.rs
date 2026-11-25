use pyo3::prelude::*;
use pyo3::types::{PyIterator, PyList};

#[pyclass]
pub(crate) struct LocalCommands {
    executor: Py<PyAny>,
    pending: Py<PyIterator>,
}

#[pyfunction]
pub(crate) fn drain_local_commands(executor: Bound<'_, PyAny>) -> PyResult<LocalCommands> {
    let pending = executor.getattr("commands_to_run")?.try_iter()?.unbind();
    Ok(LocalCommands {
        executor: executor.unbind(),
        pending,
    })
}

#[pymethods]
impl LocalCommands {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(&self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        if let Some(command) = self.pending.bind(py).clone().next() {
            return command.map(|command| Some(command.unbind()));
        }

        // Clear only after normal exhaustion. Cancellation leaves the same queue
        // and coroutine objects available as in the Python loop.
        self.executor
            .bind(py)
            .setattr("commands_to_run", PyList::empty(py))?;
        Ok(None)
    }

    fn execution_failed(&self, key: &Bound<'_, PyAny>, error: &Bound<'_, PyAny>) -> PyResult<()> {
        let executor = self.executor.bind(key.py());
        executor.call_method1("fail", (key,))?;
        executor
            .getattr("log")?
            .call_method1("error", ("Failed to execute task %s: %s", key, error))?;
        Ok(())
    }

    fn command_exited(
        &self,
        key: &Bound<'_, PyAny>,
        returncode: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let executor = self.executor.bind(key.py());
        executor.getattr("log")?.call_method1(
            "info",
            ("Task %s exited with return code %s", key, returncode),
        )?;
        if returncode.eq(0)? {
            executor.call_method1("success", (key,))?;
        } else {
            executor.call_method1("fail", (key,))?;
            executor.getattr("log")?.call_method1(
                "error",
                ("Task %s failed with return code %s", key, returncode),
            )?;
        }
        Ok(())
    }
}
