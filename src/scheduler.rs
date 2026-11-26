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

/// Choose at most one task for the single-slot executor. The boolean indicates
/// whether this batch can be handled natively without changing Airflow's behavior.
#[pyfunction]
pub(crate) fn select_sqlite_task(
    connection: &Bound<'_, PyAny>,
    max_tis: i64,
) -> PyResult<(bool, Option<String>)> {
    use std::collections::HashMap;

    let pool_rows = connection
        .call_method1(
            "exec_driver_sql",
            ("SELECT p.pool, p.slots, coalesce(sum(t.pool_slots), 0)
              FROM slot_pool AS p
              LEFT JOIN task_instance AS t ON t.pool = p.pool AND t.working_set IS TRUE
                  AND (t.state IN ('queued', 'running')
                      OR (p.include_deferred AND t.state = 'deferred'))
              GROUP BY p.pool, p.slots",),
        )?
        .call_method0("all")?;

    let mut pools = HashMap::new();
    let mut starved_pools = Vec::new();
    let mut free = 0_i64;
    for row in pool_rows.try_iter()? {
        let row = row?;
        let name: String = row.get_item(0)?.extract()?;
        let total: i64 = row.get_item(1)?.extract()?;
        let used: i64 = row.get_item(2)?.extract()?;
        let open = if total == -1 {
            i64::MAX
        } else {
            total.saturating_sub(used)
        };
        if open <= 0 {
            starved_pools.push(name.clone());
        }
        free = free.saturating_add(open.max(0));
        pools.insert(name, (total, open));
    }

    let limit = max_tis.min(free);
    if limit <= 0 {
        return Ok((true, None));
    }

    let mut starved_tasks = Vec::<(String, String)>::new();
    loop {
        let rows = connection
            .call_method1(
                "exec_driver_sql",
                (
                    "WITH active AS (
                        SELECT dag_id, run_id, count(*) AS occupied
                        FROM task_instance
                        WHERE working_set IS TRUE AND state IN ('queued', 'running')
                        GROUP BY dag_id, run_id
                    ), ranked AS (
                        SELECT t.id, t.dag_id, t.task_id, t.pool, t.pool_slots,
                            (NOT coalesce(d.has_task_concurrency_limits, 0)
                                AND t.executor IS NULL AND t.dag_version_id IS NOT NULL
                                AND t.pool_slots > 0) AS supported,
                            t.priority_weight, r.logical_date, t.region_index, d.max_active_tasks,
                            row_number() OVER (
                                PARTITION BY t.dag_id, t.run_id
                                ORDER BY -t.priority_weight, r.logical_date, t.region_index
                            ) AS rank
                        FROM task_instance AS t
                        JOIN dag_run AS r ON r.dag_id = t.dag_id AND r.run_id = t.run_id
                        JOIN dag AS d ON d.dag_id = t.dag_id
                        LEFT JOIN active AS a ON a.dag_id = t.dag_id AND a.run_id = t.run_id
                        WHERE t.working_set IS TRUE AND t.state = 'scheduled'
                            AND r.state = 'running' AND NOT d.is_paused AND d.bundle_name IS NOT NULL
                            AND coalesce(a.occupied, 0) < d.max_active_tasks
                            AND t.pool NOT IN (SELECT value FROM json_each(?))
                            AND (t.dag_id, t.task_id) NOT IN (
                                SELECT value ->> 0, value ->> 1 FROM json_each(?)
                            )
                    )
                    SELECT id, dag_id, task_id, pool, pool_slots, supported
                    FROM ranked WHERE rank <= max_active_tasks
                    ORDER BY -priority_weight, logical_date, region_index LIMIT ?",
                    (
                        json!(starved_pools).to_string(),
                        json!(starved_tasks).to_string(),
                        limit,
                    ),
                ),
            )?
            .call_method0("all")?;

        let mut selected = None;
        for row in rows.try_iter()? {
            let row = row?;
            if row.get_item(5)?.extract::<i64>()? == 0 {
                return Ok((false, None));
            }
            if selected.is_some() {
                continue;
            }
            let pool: String = row.get_item(3)?.extract()?;
            let slots: i64 = row.get_item(4)?.extract()?;
            match pools.get(&pool) {
                None => starved_pools.push(pool),
                Some(&(total, open)) if (total != -1 && slots > total) || slots > open => {
                    starved_tasks.push((row.get_item(1)?.extract()?, row.get_item(2)?.extract()?));
                }
                Some(_) => selected = Some(row.get_item(0)?.extract()?),
            }
        }
        // Retry a full, entirely starved batch with the new filters applied
        // before ranking, so lower-priority rows can enter the next batch.
        if selected.is_some() || (rows.len()? as i64) < limit {
            return Ok((true, selected));
        }
    }
}

/// Change only a live scheduled attempt. Airflow still owns the transaction
/// and only builds an executor workload after this guarded update succeeds.
#[pyfunction]
pub(crate) fn queue_sqlite_task(
    connection: &Bound<'_, PyAny>,
    id: &str,
    job_id: i64,
    now: &str,
) -> PyResult<bool> {
    let updated = connection.call_method1(
        "exec_driver_sql",
        (
            "UPDATE task_instance SET state = 'queued', queued_dttm = ?,
                queued_by_job_id = ?, updated_at = ?
            WHERE id = ? AND working_set IS TRUE AND state = 'scheduled'",
            (now, job_id, now, id),
        ),
    )?;
    Ok(updated.getattr("rowcount")?.extract::<i64>()? == 1)
}
