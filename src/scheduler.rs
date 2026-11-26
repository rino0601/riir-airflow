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

/// Select the bounded Airflow 3.4 batch on SQLite, including backfill rows
/// so the adapter can delegate mixed batches intact.
#[pyfunction]
pub(crate) fn manual_dag_run_candidates(
    connection: &Bound<'_, PyAny>,
    limit: i64,
) -> PyResult<Option<Vec<i64>>> {
    let rows = connection
        .call_method1(
            "exec_driver_sql",
            (
                "WITH running AS (
                    SELECT dag_id, backfill_id, count(*) AS num_running
                    FROM dag_run WHERE state = 'running'
                    GROUP BY dag_id, backfill_id
                )
                SELECT r.id, r.run_type, r.backfill_id
                FROM dag_run AS r
                JOIN dag AS d ON d.dag_id = r.dag_id AND d.is_paused = 0 AND d.is_stale = 0
                LEFT JOIN backfill_dag_run AS bdr
                    ON bdr.dag_run_id = r.id AND bdr.backfill_id = r.backfill_id
                LEFT JOIN backfill AS b ON b.id = bdr.backfill_id
                LEFT JOIN running AS a
                    ON a.dag_id = r.dag_id
                    AND coalesce(a.backfill_id, -1) = coalesce(r.backfill_id, -1)
                WHERE r.state = 'queued' AND r.run_after <= CURRENT_TIMESTAMP
                    AND coalesce(a.num_running, 0) < coalesce(b.max_active_runs, d.max_active_runs)
                    AND coalesce(b.is_paused, 0) = 0
                ORDER BY bdr.sort_ordinal, r.last_scheduling_decision, a.num_running, r.run_after
                LIMIT ?",
                (limit,),
            ),
        )?
        .call_method0("all")?;

    let mut candidates = Vec::new();
    for row in rows.try_iter()? {
        let row = row?;
        if row.get_item(1)?.extract::<String>()? != "manual" || !row.get_item(2)?.is_none() {
            return Ok(None);
        }
        candidates.push(row.get_item(0)?.extract()?);
    }
    Ok(Some(candidates))
}

/// Claim a queued run on Airflow's connection. The capacity check counts
/// earlier claims in this transaction, and the queued predicate prevents
/// a repeated claim from changing the start metadata or notifying again.
#[pyfunction]
pub(crate) fn start_manual_dag_run(
    connection: &Bound<'_, PyAny>,
    id: i64,
    now: &str,
) -> PyResult<bool> {
    let updated = connection.call_method1(
        "exec_driver_sql",
        (
            "UPDATE dag_run AS r
            SET state = 'running', start_date = ?, end_date = NULL, updated_at = ?
            WHERE r.id = ? AND r.state = 'queued' AND r.run_type = 'manual'
                AND r.backfill_id IS NULL
                AND (
                    SELECT count(*) FROM dag_run AS a
                    WHERE a.dag_id = r.dag_id AND a.backfill_id IS NULL AND a.state = 'running'
                ) < (SELECT d.max_active_runs FROM dag AS d WHERE d.dag_id = r.dag_id)",
            (now, now, id),
        ),
    )?;
    Ok(updated.getattr("rowcount")?.extract::<i64>()? == 1)
}
