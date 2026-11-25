use std::path::Path;

use rusqlite::{Connection, OpenFlags};
use serde_json::{Value, json};

pub fn status(database: &Path) -> rusqlite::Result<Value> {
    let connection = Connection::open_with_flags(database, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    // One SQLite statement sees a consistent snapshot of both tables.
    connection.query_row(
        "SELECT
            (SELECT count(*) FROM dag_run WHERE state = 'queued'),
            (SELECT count(*) FROM dag_run WHERE state = 'running'),
            (SELECT count(*) FROM task_instance WHERE working_set = 1 AND state = 'scheduled'),
            (SELECT count(*) FROM task_instance WHERE working_set = 1 AND state = 'queued'),
            (SELECT count(*) FROM task_instance WHERE working_set = 1 AND state = 'running'),
            (SELECT count(*) FROM task_instance WHERE working_set = 1 AND state = 'up_for_retry')",
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
                }
            }))
        },
    )
}
