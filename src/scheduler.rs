use std::collections::HashMap;
use std::path::Path;

use pyo3::prelude::*;

use rusqlite::{Connection, OpenFlags};
use serde_json::{Value, json};

pub fn status(database: &Path) -> rusqlite::Result<Value> {
    // Prefix relative paths so SQLite treats names like `file:metadata.db` literally.
    let path = Path::new(".").join(database);
    let connection = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    // One SQLite statement sees a consistent snapshot of both tables.
    connection.query_row(
        "SELECT
            (SELECT count(*) FROM dag_run WHERE state = 'queued'),
            (SELECT count(*) FROM dag_run WHERE state = 'running'),
            (SELECT count(*) FROM task_instance WHERE working_set IS TRUE AND state = 'scheduled'),
            (SELECT count(*) FROM task_instance WHERE working_set IS TRUE AND state = 'queued'),
            (SELECT count(*) FROM task_instance WHERE working_set IS TRUE AND state = 'running'),
            (SELECT count(*) FROM task_instance WHERE working_set IS TRUE AND state = 'up_for_retry'),
            (SELECT count(*) FROM task_instance WHERE working_set IS TRUE AND state = 'restarting'),
            (SELECT count(*) FROM task_instance WHERE working_set IS TRUE AND state = 'up_for_reschedule'),
            (SELECT count(*) FROM task_instance WHERE working_set IS TRUE AND state = 'deferred'),
            (SELECT count(*) FROM task_instance WHERE working_set IS TRUE AND state = 'awaiting_input'),
            (SELECT count(*) FROM task_instance WHERE working_set IS TRUE AND state IS NULL)",
        [],
        |row| {
            Ok(json!({
                "dag_runs": {
                    "queued": row.get::<_, u64>(0)?,
                    "running": row.get::<_, u64>(1)?,
                },
                "task_instances": {
                    "scheduled": row.get::<_, u64>(2)?,
                    "queued": row.get::<_, u64>(3)?,
                    "running": row.get::<_, u64>(4)?,
                    "up_for_retry": row.get::<_, u64>(5)?,
                    "restarting": row.get::<_, u64>(6)?,
                    "up_for_reschedule": row.get::<_, u64>(7)?,
                    "deferred": row.get::<_, u64>(8)?,
                    "awaiting_input": row.get::<_, u64>(9)?,
                    "no_state": row.get::<_, u64>(10)?,
                }
            }))
        },
    )
}

/// Track the occupied slots in one ordered scheduler admission batch.
#[pyclass(module = "riir_airflow._lowlevel")]
pub(crate) struct DagRunAdmission {
    running: HashMap<String, i64>,
}

#[pymethods]
impl DagRunAdmission {
    #[new]
    fn new(running: HashMap<String, i64>) -> Self {
        Self { running }
    }

    fn admit(&mut self, dag_id: &str, max_active_runs: Option<i64>) -> bool {
        let active_runs = self.running.entry(dag_id.to_owned()).or_default();
        // Airflow treats a missing or zero limit as unbounded at admission.
        if max_active_runs.is_some_and(|limit| limit != 0 && *active_runs >= limit) {
            return false;
        }
        *active_runs += 1;
        true
    }
}
