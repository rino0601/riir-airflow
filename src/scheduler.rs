use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use rusqlite::types::ValueRef;
use rusqlite::{Connection, ErrorCode, OpenFlags, TransactionBehavior, params};
use serde_json::{Value, json};

const RUN_COLUMNS: &str = "r.id AS id, r.dag_id AS dag_id, r.run_id AS run_id,
    r.run_type AS run_type, r.backfill_id AS backfill_id,
    r.created_dag_version_id AS created_dag_version_id,
    r.bundle_version AS bundle_version, r.logical_date AS logical_date,
    r.data_interval_start AS data_interval_start,
    r.data_interval_end AS data_interval_end, r.run_after AS run_after,
    r.start_date AS start_date, r.end_date AS end_date, r.updated_at AS updated_at,
    r.state AS state, r.clear_number AS clear_number, r.triggered_by AS triggered_by,
    r.conf AS conf, r.queued_at AS queued_at, r.created_at AS created_at,
    r.log_template_id AS log_template_id, r.context_carrier AS context_carrier";

const TASK_COLUMNS: &str = "t.id AS id, t.task_id AS task_id, t.dag_id AS dag_id,
    t.run_id AS run_id, t.try_number AS try_number,
    t.dag_version_id AS dag_version_id, t.region_id AS region_id,
    t.region_index AS region_index, t.hostname AS hostname,
    t.context_carrier AS context_carrier, t.queue AS queue, t.pool AS pool,
    t.pool_slots AS pool_slots, t.priority_weight AS priority_weight,
    t.external_executor_id AS external_executor_id,
    t.executor_config AS executor_config, t.next_method AS next_method,
    t.next_kwargs AS next_kwargs, t.trigger_id AS trigger_id,
    t.state AS state, t.queued_dttm AS queued_dttm,
    t.queued_by_job_id AS queued_by_job_id, t.updated_at AS updated_at,
    d.bundle_name AS bundle_name, d.relative_fileloc AS relative_fileloc,
    d.fail_fast AS fail_fast, d.has_task_concurrency_limits AS has_task_concurrency_limits,
    r.logical_date AS logical_date, r.run_after AS run_after,
    r.data_interval_start AS data_interval_start,
    r.data_interval_end AS data_interval_end, r.bundle_version AS bundle_version,
    r.created_dag_version_id AS created_dag_version_id, r.state AS run_state,
    dv.bundle_version AS version_bundle_version, dv.version_data AS version_data";

const SENTINEL_REGION: &str = "00000000000000000000000000000000";

fn sql_error(error: rusqlite::Error) -> PyErr {
    PyRuntimeError::new_err(error.to_string())
}

fn is_busy(error: &rusqlite::Error) -> bool {
    matches!(
        error.sqlite_error_code(),
        Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked)
    )
}

fn get_optional_string(row: &rusqlite::Row<'_>, name: &str) -> Result<Option<String>, PyErr> {
    row.get::<_, Option<String>>(name).map_err(sql_error)
}

fn get_optional_blob(row: &rusqlite::Row<'_>, name: &str) -> Result<Option<Vec<u8>>, PyErr> {
    match row.get_ref(name).map_err(sql_error)? {
        ValueRef::Null => Ok(None),
        ValueRef::Blob(value) => Ok(Some(value.to_vec())),
        ValueRef::Text(value) => Ok(Some(value.to_vec())),
        ValueRef::Integer(_) | ValueRef::Real(_) => Err(PyValueError::new_err(format!(
            "unexpected scalar in blob column: {name}"
        ))),
    }
}

fn py_to_sql(error: PyErr) -> rusqlite::Error {
    rusqlite::Error::FromSqlConversionFailure(0, rusqlite::types::Type::Text, Box::new(error))
}

fn set<'py>(dict: &Bound<'py, PyDict>, key: &str, value: impl IntoPyObject<'py>) -> PyResult<()> {
    dict.set_item(key, value)
}

fn item<'py>(dict: &Bound<'py, PyDict>, key: &str) -> PyResult<Bound<'py, pyo3::types::PyAny>> {
    dict.get_item(key)?
        .ok_or_else(|| PyValueError::new_err(format!("missing DTO field: {key}")))
}

fn run_dto<'py>(py: Python<'py>, row: &rusqlite::Row<'_>) -> PyResult<Bound<'py, PyDict>> {
    let dto = PyDict::new(py);
    set(&dto, "id", row.get::<_, i64>("id").map_err(sql_error)?)?;
    for key in [
        "dag_id",
        "run_id",
        "run_type",
        "created_dag_version_id",
        "bundle_version",
        "logical_date",
        "data_interval_start",
        "data_interval_end",
        "run_after",
        "start_date",
        "end_date",
        "updated_at",
        "state",
        "triggered_by",
        "conf",
        "queued_at",
        "created_at",
        "context_carrier",
    ] {
        set(&dto, key, get_optional_string(row, key)?)?;
    }
    for key in ["backfill_id", "clear_number", "log_template_id"] {
        set(
            &dto,
            key,
            row.get::<_, Option<i64>>(key).map_err(sql_error)?,
        )?;
    }
    Ok(dto)
}

fn task_dto<'py>(
    py: Python<'py>,
    row: &rusqlite::Row<'_>,
    state: &str,
) -> PyResult<Bound<'py, PyDict>> {
    let dto = PyDict::new(py);
    for key in [
        "id",
        "task_id",
        "dag_id",
        "run_id",
        "dag_version_id",
        "region_id",
        "hostname",
        "context_carrier",
        "queue",
        "pool",
        "external_executor_id",
        "next_method",
        "next_kwargs",
        "trigger_id",
        "queued_dttm",
        "updated_at",
        "bundle_name",
        "relative_fileloc",
        "logical_date",
        "run_after",
        "data_interval_start",
        "data_interval_end",
        "bundle_version",
        "created_dag_version_id",
        "run_state",
        "version_bundle_version",
        "version_data",
    ] {
        set(&dto, key, get_optional_string(row, key)?)?;
    }
    set(
        &dto,
        "executor_config",
        get_optional_blob(row, "executor_config")?,
    )?;
    set(
        &dto,
        "state",
        row.get::<_, Option<String>>("state")
            .map_err(sql_error)?
            .unwrap_or_else(|| state.to_owned()),
    )?;
    for key in [
        "try_number",
        "region_index",
        "pool_slots",
        "priority_weight",
        "queued_by_job_id",
    ] {
        set(
            &dto,
            key,
            row.get::<_, Option<i64>>(key).map_err(sql_error)?,
        )?;
    }
    set(
        &dto,
        "fail_fast",
        row.get::<_, i64>("fail_fast").map_err(sql_error)? != 0,
    )?;
    set(
        &dto,
        "has_task_concurrency_limits",
        row.get::<_, i64>("has_task_concurrency_limits")
            .map_err(sql_error)?
            != 0,
    )?;
    Ok(dto)
}

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

#[pyclass(unsendable)]
pub(crate) struct NativeScheduler {
    connection: Connection,
}

#[pymethods]
impl NativeScheduler {
    #[new]
    fn new(database: PathBuf) -> PyResult<Self> {
        let path = Path::new(".").join(database);
        let connection = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE,
        )
        .map_err(sql_error)?;
        connection
            .busy_timeout(Duration::from_secs(5))
            .map_err(sql_error)?;
        connection
            .execute_batch("PRAGMA foreign_keys = ON; PRAGMA journal_mode = WAL;")
            .map_err(sql_error)?;
        Ok(Self { connection })
    }

    /// Compose the two Rust-owned admission transactions around Airflow's phases.
    fn run_iteration(
        &mut self,
        phases: &Bound<'_, PyAny>,
        dag_run_limit: i64,
        max_tis: i64,
        job_id: i64,
        now: &str,
    ) -> PyResult<usize> {
        phases.call_method0("before_runs")?;

        let (status, _) = self.claim_dag_runs(phases, dag_run_limit, now)?;
        match status.as_str() {
            "fallback" => {
                return phases
                    .call_method0("delegate_iteration")?
                    .extract::<usize>();
            }
            "locked" => return Ok(0),
            _ => {}
        }

        phases.call_method0("between_phases")?;
        let (status, count) = self.claim_task(phases, max_tis, job_id, now)?;
        match status.as_str() {
            "fallback" => phases
                .call_method0("delegate_task_phase")?
                .extract::<usize>(),
            "locked" => Ok(0),
            _ => Ok(count),
        }
    }

    /// Rebuild the single executor slot from a queued row left by a dead job.
    fn recover_queued_workloads(
        &mut self,
        phases: &Bound<'_, PyAny>,
        job_id: i64,
        limit: i64,
    ) -> PyResult<usize> {
        let py = phases.py();
        let transaction = match self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
        {
            Ok(transaction) => transaction,
            Err(error) if is_busy(&error) => return Ok(0),
            Err(error) => return Err(sql_error(error)),
        };
        let sql = format!(
            "SELECT {TASK_COLUMNS}
            FROM task_instance AS t
            JOIN dag_run AS r ON r.dag_id = t.dag_id AND r.run_id = t.run_id
            JOIN dag AS d ON d.dag_id = t.dag_id
            JOIN job AS j ON j.id = t.queued_by_job_id
            LEFT JOIN dag_version AS dv ON dv.id = r.created_dag_version_id
            WHERE t.working_set IS TRUE AND t.state = 'queued' AND r.state = 'running'
                AND coalesce(j.state, '') != 'running' AND j.id != ?1
            ORDER BY t.queued_dttm, t.id
            LIMIT ?2"
        );
        let dto = {
            let mut statement = transaction.prepare(&sql).map_err(sql_error)?;
            let rows = statement
                .query_map(params![job_id, limit], |row| {
                    task_dto(py, row, "queued").map_err(py_to_sql)
                })
                .map_err(sql_error)?;
            rows.into_iter().next().transpose().map_err(sql_error)?
        };
        let Some(dto) = dto else {
            transaction.commit().map_err(sql_error)?;
            return Ok(0);
        };
        if !phases
            .call_method1("prepare_task", (dto.clone(),))?
            .is_truthy()?
        {
            transaction.commit().map_err(sql_error)?;
            return Ok(0);
        }
        let id: String = item(&dto, "id")?.extract()?;
        let updated = transaction
            .execute(
                "UPDATE task_instance SET queued_by_job_id = ?1 WHERE id = ?2",
                params![job_id, id],
            )
            .map_err(sql_error)?;
        if updated != 1 {
            transaction.commit().map_err(sql_error)?;
            return Ok(0);
        }
        dto.set_item("queued_by_job_id", job_id)?;
        if let Err(error) = phases.call_method1("task_claimed", (dto,)) {
            let _ = phases.call_method0("discard_pending_workload");
            return Err(error);
        }
        transaction.commit().map_err(sql_error)?;
        Ok(1)
    }

    fn claim_dag_runs(
        &mut self,
        phases: &Bound<'_, PyAny>,
        limit: i64,
        now: &str,
    ) -> PyResult<(String, usize)> {
        let py = phases.py();
        let transaction = match self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
        {
            Ok(transaction) => transaction,
            Err(error) if is_busy(&error) => return Ok(("locked".into(), 0)),
            Err(error) => return Err(sql_error(error)),
        };

        let sql = format!(
            "WITH running AS (
                SELECT dag_id, backfill_id, count(*) AS num_running
                FROM dag_run WHERE state = 'running'
                GROUP BY dag_id, backfill_id
            )
            SELECT {RUN_COLUMNS}
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
            LIMIT ?1"
        );
        let candidates = {
            let mut statement = transaction.prepare(&sql).map_err(sql_error)?;
            let rows = statement
                .query_map([limit], |row| run_dto(py, row).map_err(py_to_sql))
                .map_err(sql_error)?;
            rows.collect::<Result<Vec<_>, _>>().map_err(sql_error)?
        };
        if candidates.is_empty() {
            transaction.commit().map_err(sql_error)?;
            return Ok(("committed".into(), 0));
        }

        if !phases
            .call_method1("prepare_dag_runs", (PyList::new(py, &candidates)?,))?
            .is_truthy()?
        {
            return Ok(("fallback".into(), 0));
        }

        let mut claimed = 0;
        for candidate in candidates {
            let id: i64 = item(&candidate, "id")?.extract()?;
            let updated = match transaction.execute(
                "UPDATE dag_run AS r
                SET state = 'running', start_date = ?1, end_date = NULL, updated_at = ?2
                WHERE r.id = ?3 AND r.state = 'queued' AND r.run_type = 'manual'
                    AND r.backfill_id IS NULL
                    AND (
                        SELECT count(*) FROM dag_run AS a
                        WHERE a.dag_id = r.dag_id AND a.backfill_id IS NULL AND a.state = 'running'
                    ) < (SELECT d.max_active_runs FROM dag AS d WHERE d.dag_id = r.dag_id)",
                params![now, now, id],
            ) {
                Ok(updated) => updated,
                Err(error) if is_busy(&error) => return Ok(("locked".into(), 0)),
                Err(error) => return Err(sql_error(error)),
            };
            if updated != 1 {
                continue;
            }
            {
                let dto = &candidate;
                dto.set_item("state", "running")?;
                dto.set_item("start_date", now)?;
                dto.set_item("end_date", py.None())?;
                dto.set_item("updated_at", now)?;
            }
            if !phases
                .call_method1("dag_run_claimed", (candidate.clone(),))?
                .is_truthy()?
            {
                return Err(PyValueError::new_err(
                    "dag run claim callback rejected a committed candidate",
                ));
            }
            claimed += 1;
        }

        transaction.commit().map_err(sql_error)?;
        Ok(("committed".into(), claimed))
    }

    fn claim_task(
        &mut self,
        phases: &Bound<'_, PyAny>,
        max_tis: i64,
        job_id: i64,
        now: &str,
    ) -> PyResult<(String, usize)> {
        if max_tis <= 0 {
            return Ok(("committed".into(), 0));
        }
        let py = phases.py();
        let transaction = match self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
        {
            Ok(transaction) => transaction,
            Err(error) if is_busy(&error) => return Ok(("locked".into(), 0)),
            Err(error) => return Err(sql_error(error)),
        };

        let (limit, mut starved_pools, pools, mut starved_tasks) =
            match pool_capacity(&transaction, max_tis)? {
                Some(capacity) => capacity,
                None => return Ok(("committed".into(), 0)),
            };
        let selected = loop {
            let sql = format!(
                "WITH active AS (
                    SELECT dag_id, run_id, count(*) AS occupied
                    FROM task_instance
                    WHERE working_set IS TRUE AND state IN ('queued', 'running')
                    GROUP BY dag_id, run_id
                ), ranked AS (
                    SELECT {TASK_COLUMNS}, t.pool AS selected_pool,
                        d.max_active_tasks AS max_active_tasks,
                        (NOT coalesce(d.has_task_concurrency_limits, 0)
                            AND t.executor IS NULL AND t.dag_version_id IS NOT NULL
                            AND dv.id IS NOT NULL
                            AND (t.region_id = ?4 OR t.region_id IS NULL)
                            AND t.pool_slots > 0
                            AND t.next_method IS NULL AND t.next_kwargs IS NULL
                            AND t.trigger_id IS NULL
                            AND NOT coalesce(d.fail_fast, 0)) AS supported,
                        row_number() OVER (
                            PARTITION BY t.dag_id, t.run_id
                            ORDER BY -t.priority_weight, r.logical_date, t.region_index
                        ) AS rank
                    FROM task_instance AS t
                    JOIN dag_run AS r ON r.dag_id = t.dag_id AND r.run_id = t.run_id
                    JOIN dag AS d ON d.dag_id = t.dag_id
                    LEFT JOIN dag_version AS dv ON dv.id = r.created_dag_version_id
                    LEFT JOIN active AS a ON a.dag_id = t.dag_id AND a.run_id = t.run_id
                    WHERE t.working_set IS TRUE AND t.state = 'scheduled'
                        AND r.state = 'running' AND NOT d.is_paused AND d.bundle_name IS NOT NULL
                        AND coalesce(a.occupied, 0) < d.max_active_tasks
                        AND t.pool NOT IN (SELECT value FROM json_each(?1))
                        AND (t.dag_id, t.task_id) NOT IN (
                            SELECT value ->> 0, value ->> 1 FROM json_each(?2)
                        )
                )
                SELECT * FROM ranked WHERE rank <= max_active_tasks
                ORDER BY -priority_weight, logical_date, region_index LIMIT ?3"
            );
            let mut statement = transaction.prepare(&sql).map_err(sql_error)?;
            let rows = statement
                .query_map(
                    params![
                        json!(starved_pools).to_string(),
                        json!(starved_tasks).to_string(),
                        limit,
                        SENTINEL_REGION,
                    ],
                    |row| {
                        let supported = row.get::<_, i64>("supported")? != 0;
                        let pool: String = row.get::<_, String>("selected_pool")?;
                        let slots: i64 = row.get::<_, i64>("pool_slots")?;
                        let dag_id: String = row.get::<_, String>("dag_id")?;
                        let task_id: String = row.get::<_, String>("task_id")?;
                        let dto = task_dto(py, row, "scheduled").map_err(py_to_sql)?;
                        Ok((supported, pool, slots, dag_id, task_id, dto))
                    },
                )
                .map_err(sql_error)?;

            let mut found = None;
            let mut count = 0;
            for row in rows {
                let (supported, pool, slots, _, _, dto) = row.map_err(sql_error)?;
                count += 1;
                if !supported {
                    return Ok(("fallback".into(), 0));
                }
                if found.is_some() {
                    continue;
                }
                match pools.get(&pool) {
                    None => starved_pools.push(pool),
                    Some(&(total, open)) if (total != -1 && slots > total) || slots > open => {
                        let dag_id: String = item(&dto, "dag_id")?.extract()?;
                        let task_id: String = item(&dto, "task_id")?.extract()?;
                        starved_tasks.push((dag_id, task_id));
                    }
                    Some(_) => found = Some(dto),
                }
            }
            if found.is_some() || count < limit {
                break found;
            }
        };

        let Some(dto) = selected else {
            transaction.commit().map_err(sql_error)?;
            return Ok(("committed".into(), 0));
        };

        if !phases
            .call_method1("prepare_task", (dto.clone(),))?
            .is_truthy()?
        {
            return Ok(("fallback".into(), 0));
        }

        let id: String = item(&dto, "id")?.extract()?;
        let updated = match transaction.execute(
            "UPDATE task_instance SET state = 'queued', queued_dttm = ?1,
                queued_by_job_id = ?2, updated_at = ?3
            WHERE id = ?4 AND working_set IS TRUE AND state = 'scheduled'",
            params![now, job_id, now, id],
        ) {
            Ok(updated) => updated,
            Err(error) if is_busy(&error) => return Ok(("locked".into(), 0)),
            Err(error) => return Err(sql_error(error)),
        };
        if updated != 1 {
            transaction.commit().map_err(sql_error)?;
            return Ok(("committed".into(), 0));
        }
        {
            let claimed = &dto;
            claimed.set_item("state", "queued")?;
            claimed.set_item("queued_dttm", now)?;
            claimed.set_item("queued_by_job_id", job_id)?;
            claimed.set_item("updated_at", now)?;
        }
        if let Err(error) = phases.call_method1("task_claimed", (dto,)) {
            let _ = phases.call_method0("discard_pending_workload");
            return Err(error);
        }
        if let Err(error) = transaction.commit() {
            let _ = phases.call_method0("discard_pending_workload");
            return Err(sql_error(error));
        }
        Ok(("committed".into(), 1))
    }
}

fn pool_capacity(
    transaction: &rusqlite::Transaction<'_>,
    max_tis: i64,
) -> PyResult<
    Option<(
        i64,
        Vec<String>,
        HashMap<String, (i64, i64)>,
        Vec<(String, String)>,
    )>,
> {
    let mut statement = transaction
        .prepare(
            "SELECT p.pool, p.slots, coalesce(sum(t.pool_slots), 0)
            FROM slot_pool AS p
            LEFT JOIN task_instance AS t ON t.pool = p.pool AND t.working_set IS TRUE
                AND (t.state IN ('queued', 'running')
                    OR (p.include_deferred AND t.state = 'deferred'))
            GROUP BY p.pool, p.slots",
        )
        .map_err(sql_error)?;
    let rows = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, i64>(2)?,
            ))
        })
        .map_err(sql_error)?;

    let mut pools = HashMap::new();
    let mut starved_pools = Vec::new();
    let mut free = 0_i64;
    for row in rows {
        let (name, total, used) = row.map_err(sql_error)?;
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
    Ok((limit > 0).then_some((limit, starved_pools, pools, Vec::new())))
}
