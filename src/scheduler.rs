use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::executor::{self, Executor, Workload};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use rusqlite::{
    Connection, ErrorCode, OpenFlags, OptionalExtension, TransactionBehavior, named_params, params,
};
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
    r.log_template_id AS log_template_id, r.context_carrier AS context_carrier,
    d.timetable_type AS timetable_type, d.timetable_partitioned AS timetable_partitioned,
    d.timetable_asset_gated AS timetable_asset_gated,
    d.asset_expression AS asset_expression, d.deadline AS deadline";

const DAG_COLUMNS: &str = "d.dag_id AS dag_id, d.bundle_version AS bundle_version,
    d.timetable_type AS timetable_type, d.next_dagrun AS next_dagrun,
    d.next_dagrun_data_interval_start AS next_dagrun_data_interval_start,
    d.next_dagrun_data_interval_end AS next_dagrun_data_interval_end,
    d.next_dagrun_create_after AS next_dagrun_create_after,
    d.max_active_runs AS max_active_runs, d.timetable_partitioned AS timetable_partitioned,
    d.timetable_asset_gated AS timetable_asset_gated,
    d.asset_expression AS asset_expression, d.deadline AS deadline,
    d.allowed_run_types AS allowed_run_types,
    d.has_task_concurrency_limits AS has_task_concurrency_limits";

const TASK_COLUMNS: &str = "t.id AS id, t.task_id AS task_id, t.dag_id AS dag_id,
    t.run_id AS run_id, t.try_number AS try_number, t.dag_version_id AS dag_version_id,
    t.region_index AS region_index,
    t.context_carrier AS context_carrier, t.queue AS queue,
    t.pool_slots AS pool_slots, t.priority_weight AS priority_weight,
    d.bundle_name AS bundle_name, d.relative_fileloc AS relative_fileloc,
    r.logical_date AS logical_date, r.run_after AS run_after,
    r.data_interval_start AS data_interval_start, r.data_interval_end AS data_interval_end,
    r.bundle_version AS bundle_version, dv.bundle_version AS version_bundle_version,
    dv.version_data AS version_data";

const NATIVE_TASK_REQUIREMENTS: &str = "NOT coalesce(d.has_task_concurrency_limits, 0)
    AND t.executor IS NULL AND t.dag_version_id IS NOT NULL
    AND dv.id IS NOT NULL
    AND (t.region_id = :sentinel_region OR t.region_id IS NULL)
    AND t.region_index = -1 AND r.backfill_id IS NULL
    AND (r.run_type = 'manual' OR (
        r.run_type = 'scheduled' AND r.triggered_by = 'TIMETABLE'
        AND d.timetable_type IN ('CronDataIntervalTimetable', 'CronTriggerTimetable',
            'DeltaDataIntervalTimetable', 'DeltaTriggerTimetable')
        AND d.timetable_partitioned = 0 AND d.timetable_asset_gated = 0
        AND d.asset_expression IS NULL AND d.deadline IS NULL
    ))
    AND coalesce(t.operator, '') != 'LoopGateOperator'
    AND t.queue = 'default' AND t.external_executor_id IS NULL
    AND (t.executor_config IS NULL OR t.executor_config IN (X'80047D942E', X'80057D942E'))
    AND t.pool_slots > 0
    AND t.next_method IS NULL AND t.next_kwargs IS NULL
    AND t.trigger_id IS NULL
    AND NOT coalesce(d.fail_fast, 0)";

const SENTINEL_REGION: &str = "00000000000000000000000000000000";

const UNFINISHED_TASK_STATES_SQL: &str = "'scheduled', 'queued', 'running', 'restarting',
    'up_for_retry', 'up_for_reschedule', 'deferred', 'awaiting_input'";

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

fn optional_string(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<String>> {
    let value = item(dict, key)?;
    if value.is_none() {
        Ok(None)
    } else {
        value.extract()
    }
}

fn optional_i64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<i64>> {
    let value = item(dict, key)?;
    if value.is_none() {
        Ok(None)
    } else {
        value.extract()
    }
}

fn bool_field(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<bool> {
    item(dict, key)?.is_truthy()
}

fn optional_bytes(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<Vec<u8>>> {
    let value = item(dict, key)?;
    if value.is_none() {
        Ok(None)
    } else {
        value.extract()
    }
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
        "timetable_type",
        "asset_expression",
        "deadline",
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
    for key in ["timetable_partitioned", "timetable_asset_gated"] {
        set(&dto, key, row.get::<_, bool>(key).map_err(sql_error)?)?;
    }
    Ok(dto)
}

fn dag_dto<'py>(py: Python<'py>, row: &rusqlite::Row<'_>) -> PyResult<Bound<'py, PyDict>> {
    let dto = PyDict::new(py);
    for key in [
        "dag_id",
        "bundle_version",
        "timetable_type",
        "next_dagrun",
        "next_dagrun_data_interval_start",
        "next_dagrun_data_interval_end",
        "next_dagrun_create_after",
        "asset_expression",
        "deadline",
        "allowed_run_types",
    ] {
        set(&dto, key, get_optional_string(row, key)?)?;
    }
    for key in ["max_active_runs", "active_runs", "log_template_id"] {
        set(
            &dto,
            key,
            row.get::<_, Option<i64>>(key).map_err(sql_error)?,
        )?;
    }
    for key in [
        "timetable_partitioned",
        "timetable_asset_gated",
        "has_task_concurrency_limits",
    ] {
        set(&dto, key, row.get::<_, bool>(key).map_err(sql_error)?)?;
    }
    set(
        &dto,
        "dag_version_id",
        get_optional_string(row, "dag_version_id")?,
    )?;
    Ok(dto)
}

fn transaction_query_run<'py>(
    py: Python<'py>,
    transaction: &rusqlite::Transaction<'_>,
    dag_id: Option<String>,
    logical_date: Option<String>,
) -> rusqlite::Result<Option<Bound<'py, PyDict>>> {
    let sql = format!(
        "SELECT {RUN_COLUMNS} FROM dag_run AS r
         JOIN dag AS d ON d.dag_id = r.dag_id
         WHERE r.dag_id = ?1 AND r.logical_date = ?2 LIMIT 1"
    );
    transaction
        .query_row(&sql, params![dag_id, logical_date], |row| {
            run_dto(py, row).map_err(py_to_sql)
        })
        .optional()
}

fn task_dto<'py>(py: Python<'py>, row: &rusqlite::Row<'_>) -> PyResult<Bound<'py, PyDict>> {
    let dto = PyDict::new(py);
    for key in [
        "id",
        "task_id",
        "dag_id",
        "run_id",
        "dag_version_id",
        "context_carrier",
        "queue",
        "bundle_name",
        "relative_fileloc",
        "logical_date",
        "run_after",
        "data_interval_start",
        "data_interval_end",
        "bundle_version",
        "version_bundle_version",
        "version_data",
    ] {
        set(&dto, key, get_optional_string(row, key)?)?;
    }
    for key in ["try_number", "pool_slots", "priority_weight"] {
        set(
            &dto,
            key,
            row.get::<_, Option<i64>>(key).map_err(sql_error)?,
        )?;
    }
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
    executor: Executor,
}

#[pymethods]
impl NativeScheduler {
    #[new]
    fn new(database: PathBuf) -> PyResult<Self> {
        let path = Path::new(".").join(&database);
        let connection = Connection::open_with_flags(
            &path,
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE,
        )
        .map_err(sql_error)?;
        connection
            .busy_timeout(Duration::from_secs(5))
            .map_err(sql_error)?;
        connection
            .execute_batch("PRAGMA foreign_keys = ON; PRAGMA journal_mode = WAL;")
            .map_err(sql_error)?;
        let executor = Executor::new(path.canonicalize().map_err(executor::error)?);
        Ok(Self {
            connection,
            executor,
        })
    }

    /// Compose the two Rust-owned admission transactions around Airflow's phases.
    fn run_iteration(
        &mut self,
        phases: &Bound<'_, PyAny>,
        dag_run_limit: i64,
        create_limit: i64,
        max_tis: i64,
        job_id: i64,
        now: &str,
        create_runs: bool,
    ) -> PyResult<usize> {
        let lease = self.executor.available(&mut self.connection)?;
        let pending_assets: bool = self
            .connection
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM asset_dag_run_queue LIMIT 1)",
                [],
                |row| row.get(0),
            )
            .map_err(sql_error)?;
        if pending_assets {
            return phases
                .call_method1("delegate_after_run_creation", (lease.is_some(),))?
                .extract::<usize>();
        }

        let status = self.create_scheduled_runs(phases, create_limit, job_id, now, create_runs)?;
        match status.as_str() {
            "fallback" => {
                return phases
                    .call_method1("delegate_after_run_creation", (lease.is_some(),))?
                    .extract::<usize>();
            }
            "locked" => return Ok(0),
            _ => {}
        }

        let (status, _) = self.claim_dag_runs(phases, dag_run_limit, now)?;
        match status.as_str() {
            "fallback" => {
                return phases
                    .call_method1("delegate_after_run_creation", (lease.is_some(),))?
                    .extract::<usize>();
            }
            "locked" => return Ok(0),
            _ => {}
        }

        let delegate = match self.schedule_dependencies(phases, dag_run_limit, job_id, now)? {
            Some(delegate) => delegate,
            None => return Ok(0),
        };
        if !delegate.is_empty() {
            phases.call_method1("delegate_runs", (PyList::new(phases.py(), &delegate)?,))?;
        }
        let Some(lease) = lease else {
            return Ok(0);
        };
        let (status, count) = self.claim_task(phases, max_tis, job_id, now, lease)?;
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
        stale_before: &str,
    ) -> PyResult<Option<usize>> {
        let Some(lease) = self.executor.available(&mut self.connection)? else {
            return Ok(Some(0));
        };
        let py = phases.py();
        let transaction = match self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
        {
            Ok(transaction) => transaction,
            Err(error) if is_busy(&error) => return Ok(Some(0)),
            Err(error) => return Err(sql_error(error)),
        };
        transaction
            .execute(
                "UPDATE job SET state = 'failed'
                WHERE job_type = 'SchedulerJob' AND state = 'running'
                    AND latest_heartbeat < ?1",
                params![stale_before],
            )
            .map_err(sql_error)?;
        let sql = format!(
            "SELECT {TASK_COLUMNS},
                t.queued_by_job_id AS previous_job_id,
                coalesce(t.state = 'queued' AND (
                    (t.external_executor_id IS NOT NULL AND EXISTS (
                        SELECT 1 FROM log AS l WHERE l.task_instance_id = t.id
                            AND l.event = 'queued'
                            AND json_extract(l.extra, '$.executor_id') = t.external_executor_id))
                    OR {NATIVE_TASK_REQUIREMENTS}
                ), 0) AS supported
            FROM task_instance AS t
            JOIN dag_run AS r ON r.dag_id = t.dag_id AND r.run_id = t.run_id
            JOIN dag AS d ON d.dag_id = t.dag_id
            JOIN job AS j ON j.id = t.queued_by_job_id
            LEFT JOIN dag_version AS dv ON dv.id = r.created_dag_version_id
            WHERE t.working_set IS TRUE AND t.state IN ('queued', 'running', 'restarting')
                AND r.state = 'running'
                AND coalesce(j.state, '') != 'running' AND j.id != :job_id
            ORDER BY supported, t.queued_dttm, t.id
            LIMIT 1"
        );
        let candidate = {
            let mut statement = transaction.prepare(&sql).map_err(sql_error)?;
            let rows = statement
                .query_map(
                    named_params! {":job_id": job_id, ":sentinel_region": SENTINEL_REGION},
                    |row| {
                        if row.get::<_, bool>("supported")? {
                            let dto = task_dto(py, row).map_err(py_to_sql)?;
                            Ok(Some((dto, row.get::<_, i64>("previous_job_id")?)))
                        } else {
                            Ok(None)
                        }
                    },
                )
                .map_err(sql_error)?;
            rows.into_iter().next().transpose().map_err(sql_error)?
        };
        let Some(candidate) = candidate else {
            transaction.commit().map_err(sql_error)?;
            return Ok(Some(0));
        };
        // Let Airflow handle all orphans if any attempt falls outside native recovery.
        let Some((dto, previous_job_id)) = candidate else {
            return Ok(None);
        };
        if limit <= 0 {
            transaction.commit().map_err(sql_error)?;
            return Ok(Some(0));
        }
        let Some(workload) = prepare_workload(phases, &dto)? else {
            return Ok(None);
        };
        let updated = transaction
            .execute(
                "UPDATE task_instance SET queued_by_job_id = ?1 WHERE id = ?2",
                params![job_id, workload.id],
            )
            .map_err(sql_error)?;
        if updated != 1 {
            transaction.commit().map_err(sql_error)?;
            return Ok(Some(0));
        }
        workload
            .reserve(&transaction, job_id, Some(previous_job_id))
            .map_err(sql_error)?;
        transaction.commit().map_err(sql_error)?;
        Ok(Some(usize::from(self.launch(workload, lease)?)))
    }

    fn active_task(&self) -> PyResult<Option<String>> {
        executor::active_task(&self.connection).map_err(sql_error)
    }
    fn wait_for_task(&mut self) -> PyResult<()> {
        self.executor.wait(&mut self.connection)
    }
}

impl NativeScheduler {
    fn launch(&mut self, workload: Workload, lease: File) -> PyResult<bool> {
        if let Err(err) = self.executor.launch(workload, lease) {
            eprintln!("Could not launch native task supervisor: {err}");
            self.executor.available(&mut self.connection)?;
            return Ok(false);
        }
        Ok(true)
    }

    fn create_scheduled_runs(
        &mut self,
        phases: &Bound<'_, PyAny>,
        limit: i64,
        job_id: i64,
        now: &str,
        enabled: bool,
    ) -> PyResult<String> {
        if !enabled {
            return Ok("committed".into());
        }
        let py = phases.py();
        let transaction = match self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
        {
            Ok(transaction) => transaction,
            Err(error) if is_busy(&error) => return Ok("locked".into()),
            Err(error) => return Err(sql_error(error)),
        };
        let sql = format!(
            "SELECT {DAG_COLUMNS}, dv.id AS dag_version_id,
                (SELECT max(id) FROM log_template) AS log_template_id,
                coalesce(a.num_running, 0) AS active_runs
             FROM dag AS d
             LEFT JOIN dag_version AS dv ON dv.id = (
                 SELECT id FROM dag_version AS v WHERE v.dag_id = d.dag_id
                 ORDER BY v.version_number DESC LIMIT 1)
             LEFT JOIN (
                 SELECT dag_id, count(*) AS num_running FROM dag_run
                 WHERE state IN ('queued', 'running') AND run_type != 'backfill'
                 GROUP BY dag_id) AS a ON a.dag_id = d.dag_id
             WHERE d.is_paused = 0 AND d.is_draining = 0 AND d.is_stale = 0
                 AND d.has_import_errors = 0 AND d.next_dagrun IS NOT NULL
                 AND d.next_dagrun_create_after IS NOT NULL
                 AND d.next_dagrun_create_after <= ?1
                 AND coalesce(d.exceeds_max_non_backfill, 0) = 0
                 AND coalesce(a.num_running, 0) < coalesce(d.max_active_runs, 0)
             ORDER BY d.next_dagrun_create_after, d.dag_id LIMIT ?2"
        );
        let mut candidates = Vec::new();
        {
            let mut statement = transaction.prepare(&sql).map_err(sql_error)?;
            let rows = statement
                .query_map(params![now, limit], |row| {
                    let dto = dag_dto(py, row).map_err(py_to_sql)?;
                    dto.set_item("job_id", job_id).map_err(py_to_sql)?;
                    let dag_id = get_optional_string(row, "dag_id").map_err(py_to_sql)?;
                    let next_dagrun = get_optional_string(row, "next_dagrun").map_err(py_to_sql)?;
                    let existing = transaction_query_run(py, &transaction, dag_id, next_dagrun)?;
                    dto.set_item("existing_run", existing).map_err(py_to_sql)?;
                    Ok(dto)
                })
                .map_err(sql_error)?;
            for row in rows {
                let dto = row.map_err(sql_error)?;
                let max = optional_i64(&dto, "max_active_runs")?.unwrap_or(0);
                let active = optional_i64(&dto, "active_runs")?.unwrap_or(0);
                let dag_id = optional_string(&dto, "dag_id")?.ok_or_else(|| {
                    PyValueError::new_err("missing dag id in scheduled run candidate")
                })?;
                if max <= 0 || active >= max {
                    transaction
                        .execute(
                            "UPDATE dag SET exceeds_max_non_backfill = 1 WHERE dag_id = ?1",
                            params![dag_id],
                        )
                        .map_err(sql_error)?;
                    continue;
                }
                candidates.push(dto);
            }
        }
        if candidates.is_empty() {
            transaction.commit().map_err(sql_error)?;
            return Ok("committed".into());
        }
        let prepared =
            phases.call_method1("prepare_scheduled_runs", (PyList::new(py, &candidates)?,))?;
        if prepared.is_none() {
            return Ok("fallback".into());
        }
        let prepared = prepared.cast::<PyList>()?;
        for entry in prepared.iter() {
            let entry = entry.cast::<PyDict>()?;
            let dag_id = optional_string(entry, "dag_id")?
                .ok_or_else(|| PyValueError::new_err("missing dag id in prepared scheduled run"))?;
            let next_value = item(entry, "next")?;
            let next = next_value.cast::<PyDict>()?;
            let run = item(entry, "run")?;
            if !run.is_none() {
                let run = run.cast::<PyDict>()?;
                transaction
                    .execute(
                        "INSERT INTO dag_run (
                        dag_id, queued_at, logical_date, data_interval_start,
                        data_interval_end, run_after, state, run_id, creating_job_id,
                        run_type, triggered_by, conf, log_template_id, created_at,
                        updated_at, clear_number, bundle_version, context_carrier,
                        created_dag_version_id)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12,
                        ?13, ?14, ?15, 0, ?16, ?17, ?18)",
                        params![
                            optional_string(run, "dag_id")?,
                            optional_string(run, "queued_at")?,
                            optional_string(run, "logical_date")?,
                            optional_string(run, "data_interval_start")?,
                            optional_string(run, "data_interval_end")?,
                            optional_string(run, "run_after")?,
                            optional_string(run, "state")?,
                            optional_string(run, "run_id")?,
                            optional_i64(run, "creating_job_id")?,
                            optional_string(run, "run_type")?,
                            optional_string(run, "triggered_by")?,
                            optional_string(run, "conf")?,
                            optional_i64(run, "log_template_id")?.unwrap_or(0),
                            optional_string(run, "created_at")?,
                            optional_string(run, "updated_at")?,
                            optional_string(run, "bundle_version")?,
                            optional_string(run, "context_carrier")?,
                            optional_string(run, "created_dag_version_id")?,
                        ],
                    )
                    .map_err(sql_error)?;
                let tasks_value = item(entry, "tasks")?;
                let tasks = tasks_value.cast::<PyList>()?;
                for task in tasks.iter() {
                    let task = task.cast::<PyDict>()?;
                    transaction
                        .execute(
                            "INSERT INTO task_instance (
                            id, task_id, dag_id, run_id, region_index, region_id,
                            try_number, max_tries, working_set, hostname, unixname,
                            queue, pool, pool_slots, priority_weight, operator,
                            custom_operator_name, is_stub, executor_config,
                            task_display_name, dag_version_id, context_carrier)
                         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 1, ?9, ?10, ?11,
                            ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21)",
                            params![
                                optional_string(task, "id")?,
                                optional_string(task, "task_id")?,
                                optional_string(task, "dag_id")?,
                                optional_string(task, "run_id")?,
                                optional_i64(task, "region_index")?,
                                optional_string(task, "region_id")?,
                                optional_i64(task, "try_number")?,
                                optional_i64(task, "max_tries")?,
                                optional_string(task, "hostname")?,
                                optional_string(task, "unixname")?,
                                optional_string(task, "queue")?,
                                optional_string(task, "pool")?,
                                optional_i64(task, "pool_slots")?,
                                optional_i64(task, "priority_weight")?,
                                optional_string(task, "operator")?,
                                optional_string(task, "custom_operator_name")?,
                                item(task, "is_stub")?.is_truthy()?,
                                optional_bytes(task, "executor_config")?,
                                optional_string(task, "task_display_name")?,
                                optional_string(task, "dag_version_id")?,
                                optional_string(task, "context_carrier")?,
                            ],
                        )
                        .map_err(sql_error)?;
                }
            }
            let active: i64 = transaction
                .query_row(
                    "SELECT count(*) FROM dag_run WHERE dag_id = ?1
                        AND run_type != 'backfill' AND state IN ('queued', 'running')",
                    params![dag_id],
                    |row| row.get(0),
                )
                .map_err(sql_error)?;
            let max = candidates
                .iter()
                .find_map(|candidate| {
                    (optional_string(candidate, "dag_id").ok()?.as_deref() == Some(dag_id.as_str()))
                        .then(|| optional_i64(candidate, "max_active_runs").ok().flatten())
                        .flatten()
                })
                .unwrap_or(0);
            let exceeds = max <= 0 || active >= max;
            transaction
                .execute(
                    "UPDATE dag SET next_dagrun = ?1,
                        next_dagrun_data_interval_start = ?2,
                        next_dagrun_data_interval_end = ?3,
                        next_dagrun_create_after = ?4,
                        exceeds_max_non_backfill = ?5
                     WHERE dag_id = ?6",
                    params![
                        optional_string(next, "next_dagrun")?,
                        optional_string(next, "next_dagrun_data_interval_start")?,
                        optional_string(next, "next_dagrun_data_interval_end")?,
                        optional_string(next, "next_dagrun_create_after")?,
                        exceeds,
                        dag_id,
                    ],
                )
                .map_err(sql_error)?;
        }
        transaction.commit().map_err(sql_error)?;
        Ok("committed".into())
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

        for candidate in &candidates {
            if !supported_claim_run(candidate)? {
                return Ok(("fallback".into(), 0));
            }
        }

        let mut claimed = 0;
        for candidate in candidates {
            let id: i64 = item(&candidate, "id")?.extract()?;
            let updated = match transaction.execute(
                "UPDATE dag_run AS r
                SET state = 'running', start_date = ?1, end_date = NULL, updated_at = ?2
                WHERE r.id = ?3 AND r.state = 'queued'
                    AND r.run_type IN ('manual', 'scheduled')
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
            claimed += 1;
        }

        transaction.commit().map_err(sql_error)?;
        Ok(("committed".into(), claimed))
    }

    fn schedule_dependencies(
        &mut self,
        phases: &Bound<'_, PyAny>,
        limit: i64,
        job_id: i64,
        now: &str,
    ) -> PyResult<Option<Vec<i64>>> {
        let py = phases.py();
        let transaction = match self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
        {
            Ok(transaction) => transaction,
            Err(error) if is_busy(&error) => return Ok(None),
            Err(error) => return Err(sql_error(error)),
        };
        let sql = format!(
            "SELECT {RUN_COLUMNS} FROM dag_run AS r
             JOIN dag AS d ON d.dag_id = r.dag_id
             WHERE r.state = 'running' AND d.is_paused = 0 AND d.is_stale = 0
                 AND r.run_after <= CURRENT_TIMESTAMP
             ORDER BY r.last_scheduling_decision IS NOT NULL,
                 r.last_scheduling_decision, r.run_after
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
            return Ok(Some(Vec::new()));
        }
        let graphs = phases.call_method1(
            "prepare_dependency_graphs",
            (PyList::new(py, &candidates)?,),
        )?;
        let graphs = graphs.cast::<PyList>()?;
        if graphs.len() != candidates.len() {
            return Err(PyValueError::new_err(
                "dependency graph callback returned the wrong number of entries",
            ));
        }

        let mut delegate = Vec::new();
        for (dto, graph) in candidates.iter().zip(graphs.iter()) {
            let run_id: i64 = item(dto, "id")?.extract()?;
            if !supported_dependency_run(dto)? || graph.is_none() {
                delegate.push(run_id);
                continue;
            }
            let graph = graph.cast::<PyDict>()?;
            let graph = dependency_graph(&graph)?;
            let dag_id = optional_string(dto, "dag_id")?
                .ok_or_else(|| PyValueError::new_err("missing dag id in dependency candidate"))?;
            let run_name = optional_string(dto, "run_id")?
                .ok_or_else(|| PyValueError::new_err("missing run id in dependency candidate"))?;
            let version = optional_string(dto, "created_dag_version_id")?;
            let tis = dependency_tis(&transaction, &dag_id, &run_name)?;
            let mut success = HashSet::new();
            let mut success_states = HashSet::new();
            let mut failed_states = HashSet::new();
            let mut ready = Vec::new();
            let mut unsupported = tis.is_empty();
            let mut skipped = false;
            let mut finished = true;
            let mut seen = HashSet::new();
            for ti in tis {
                let task_id = ti.task_id;
                if !seen.insert(task_id.clone()) || !graph.upstreams.contains_key(&task_id) {
                    unsupported = true;
                    break;
                }
                if ti.region_index != -1
                    || ti
                        .region_id
                        .as_deref()
                        .is_some_and(|id| id != SENTINEL_REGION)
                    || ti.dag_version_id != version
                {
                    unsupported = true;
                    break;
                }
                match ti.state.as_deref() {
                    Some("success") => {
                        success.insert(task_id.clone());
                        success_states.insert(task_id);
                    }
                    Some("skipped") => {
                        skipped = true;
                        success_states.insert(task_id);
                    }
                    Some("failed") | Some("upstream_failed") => {
                        failed_states.insert(task_id);
                    }
                    Some("scheduled") | Some("queued") | Some("running") => {
                        finished = false;
                    }
                    None => {
                        finished = false;
                        ready.push((ti.id, task_id));
                    }
                    Some("up_for_retry") => match ti.retry_at.as_deref() {
                        None => {
                            unsupported = true;
                            break;
                        }
                        Some(retry_at) if retry_at <= now => {
                            finished = false;
                            ready.push((ti.id, task_id));
                        }
                        Some(_) => {
                            finished = false;
                        }
                    },
                    _ => {
                        unsupported = true;
                        break;
                    }
                }
            }
            if unsupported
                || graph
                    .upstreams
                    .keys()
                    .any(|task_id| !seen.contains(task_id))
            {
                delegate.push(run_id);
                continue;
            }
            let terminal = if finished {
                if graph.leaves.is_empty()
                    || graph.leaves.iter().any(|task_id| !seen.contains(task_id))
                {
                    delegate.push(run_id);
                    continue;
                }
                if graph
                    .leaves
                    .iter()
                    .any(|task_id| failed_states.contains(task_id))
                {
                    Some("failed")
                } else if graph
                    .leaves
                    .iter()
                    .all(|task_id| success_states.contains(task_id))
                {
                    Some("success")
                } else {
                    delegate.push(run_id);
                    continue;
                }
            } else {
                if !failed_states.is_empty() || skipped {
                    delegate.push(run_id);
                    continue;
                }
                None
            };
            if let Some(state) = terminal {
                let updated = match transaction.execute(
                    &format!(
                        "UPDATE dag_run AS r
                         SET state = ?1, end_date = ?2, updated_at = ?2,
                             last_scheduling_decision = ?2
                         WHERE r.id = ?3 AND r.state = 'running'
                             AND NOT EXISTS (
                                 SELECT 1 FROM task_instance AS t
                                 WHERE t.dag_id = r.dag_id AND t.run_id = r.run_id
                                     AND t.working_set IS TRUE
                                     AND (t.state IS NULL OR t.state IN ({UNFINISHED_TASK_STATES_SQL}))
                             )"
                    ),
                    params![state, now, run_id],
                ) {
                    Ok(updated) => updated,
                    Err(error) if is_busy(&error) => return Ok(None),
                    Err(error) => return Err(sql_error(error)),
                };
                if updated != 1 {
                    delegate.push(run_id);
                    continue;
                }
                let updated = match transaction.execute(
                    "UPDATE dag AS d
                     SET exceeds_max_non_backfill = CASE WHEN (
                             SELECT count(*) FROM dag_run AS r
                             WHERE r.dag_id = d.dag_id AND r.run_type != 'backfill'
                                 AND r.state IN ('queued', 'running')
                         ) >= coalesce(d.max_active_runs, 0) THEN 1 ELSE 0 END
                     WHERE d.dag_id = ?1",
                    params![dag_id],
                ) {
                    Ok(updated) => updated,
                    Err(error) if is_busy(&error) => return Ok(None),
                    Err(error) => return Err(sql_error(error)),
                };
                if updated != 1 {
                    return Ok(None);
                }
                continue;
            }
            let ready: Vec<String> = ready
                .into_iter()
                .filter(|(_, task_id)| {
                    graph.upstreams.get(task_id).is_some_and(|parents| {
                        parents.iter().all(|parent| success.contains(parent))
                    })
                })
                .map(|(id, _)| id)
                .collect();

            let updated = match transaction.execute(
                "UPDATE dag_run SET last_scheduling_decision = ?1, scheduled_by_job_id = ?2
                 WHERE id = ?3 AND state = 'running'",
                params![now, job_id, run_id],
            ) {
                Ok(updated) => updated,
                Err(error) if is_busy(&error) => return Ok(None),
                Err(error) => return Err(sql_error(error)),
            };
            if updated != 1 {
                delegate.push(run_id);
                continue;
            }
            for id in ready {
                let updated = match transaction.execute(
                    "UPDATE task_instance AS t
                     SET state = 'scheduled', scheduled_dttm = ?1,
                         try_number = CASE WHEN t.try_number = 0 THEN 1 ELSE t.try_number END,
                         retry_reason = NULL, retry_delay_override = NULL, retry_at = NULL
                     WHERE t.id = ?2 AND t.working_set IS TRUE
                         AND (t.state IS NULL OR (t.state = 'up_for_retry'
                             AND t.retry_at IS NOT NULL AND t.retry_at <= ?1))
                         AND EXISTS (
                             SELECT 1 FROM dag_run AS r
                             WHERE r.id = ?3 AND r.state = 'running'
                                 AND r.dag_id = t.dag_id AND r.run_id = t.run_id
                         )",
                    params![now, id, run_id],
                ) {
                    Ok(updated) => updated,
                    Err(error) if is_busy(&error) => return Ok(None),
                    Err(error) => return Err(sql_error(error)),
                };
                if updated != 1 {
                    return Ok(None);
                }
            }
        }
        transaction.commit().map_err(sql_error)?;
        Ok(Some(delegate))
    }

    fn claim_task(
        &mut self,
        phases: &Bound<'_, PyAny>,
        max_tis: i64,
        job_id: i64,
        now: &str,
        lease: File,
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
                        coalesce({NATIVE_TASK_REQUIREMENTS}, 0) AS supported,
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
                        AND t.pool NOT IN (SELECT value FROM json_each(:starved_pools))
                        AND (t.dag_id, t.task_id) NOT IN (
                            SELECT value ->> 0, value ->> 1 FROM json_each(:starved_tasks)
                        )
                )
                SELECT * FROM ranked WHERE rank <= max_active_tasks
                ORDER BY -priority_weight, logical_date, region_index LIMIT :limit"
            );
            let mut statement = transaction.prepare(&sql).map_err(sql_error)?;
            let rows = statement
                .query_map(
                    named_params! {
                        ":starved_pools": json!(starved_pools).to_string(),
                        ":starved_tasks": json!(starved_tasks).to_string(),
                        ":limit": limit,
                        ":sentinel_region": SENTINEL_REGION,
                    },
                    |row| {
                        let supported = row.get::<_, i64>("supported")? != 0;
                        let pool: String = row.get::<_, String>("selected_pool")?;
                        let slots: i64 = row.get::<_, i64>("pool_slots")?;
                        let dag_id: String = row.get::<_, String>("dag_id")?;
                        let task_id: String = row.get::<_, String>("task_id")?;
                        let dto = task_dto(py, row).map_err(py_to_sql)?;
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

        let Some(workload) = prepare_workload(phases, &dto)? else {
            return Ok(("fallback".into(), 0));
        };
        let updated = match transaction.execute(
            "UPDATE task_instance SET state = 'queued', queued_dttm = ?1,
                queued_by_job_id = ?2, updated_at = ?3
            WHERE id = ?4 AND working_set IS TRUE AND state = 'scheduled'",
            params![now, job_id, now, workload.id],
        ) {
            Ok(updated) => updated,
            Err(error) if is_busy(&error) => return Ok(("locked".into(), 0)),
            Err(error) => return Err(sql_error(error)),
        };
        if updated != 1 {
            transaction.commit().map_err(sql_error)?;
            return Ok(("committed".into(), 0));
        }
        workload
            .reserve(&transaction, job_id, None)
            .map_err(sql_error)?;
        transaction.commit().map_err(sql_error)?;
        Ok((
            "committed".into(),
            usize::from(self.launch(workload, lease)?),
        ))
    }
}

fn supported_claim_run(dto: &Bound<'_, PyDict>) -> PyResult<bool> {
    if optional_i64(dto, "backfill_id")?.is_some() {
        return Ok(false);
    }
    match optional_string(dto, "run_type")?.as_deref() {
        Some("manual") => Ok(true),
        Some("scheduled") => {
            let timetable = optional_string(dto, "timetable_type")?;
            Ok(
                optional_string(dto, "triggered_by")?.as_deref() == Some("TIMETABLE")
                    && matches!(
                        timetable.as_deref(),
                        Some(
                            "CronDataIntervalTimetable"
                                | "CronTriggerTimetable"
                                | "DeltaDataIntervalTimetable"
                                | "DeltaTriggerTimetable"
                        )
                    )
                    && !bool_field(dto, "timetable_partitioned")?
                    && !bool_field(dto, "timetable_asset_gated")?
                    && optional_string(dto, "asset_expression")?.is_none()
                    && optional_string(dto, "deadline")?.is_none(),
            )
        }
        _ => Ok(false),
    }
}

struct DependencyTi {
    id: String,
    task_id: String,
    state: Option<String>,
    retry_at: Option<String>,
    dag_version_id: Option<String>,
    region_index: i64,
    region_id: Option<String>,
}

fn supported_dependency_run(dto: &Bound<'_, PyDict>) -> PyResult<bool> {
    if optional_i64(dto, "backfill_id")?.is_some()
        || optional_string(dto, "created_dag_version_id")?.is_none()
    {
        return Ok(false);
    }
    let timetable = optional_string(dto, "timetable_type")?;
    if !matches!(
        timetable.as_deref(),
        Some(
            "NullTimetable"
                | "CronDataIntervalTimetable"
                | "CronTriggerTimetable"
                | "DeltaDataIntervalTimetable"
                | "DeltaTriggerTimetable"
        )
    ) || bool_field(dto, "timetable_partitioned")?
        || bool_field(dto, "timetable_asset_gated")?
        || optional_string(dto, "asset_expression")?.is_some()
        || optional_string(dto, "deadline")?.is_some()
    {
        return Ok(false);
    }
    match optional_string(dto, "run_type")?.as_deref() {
        Some("manual") => Ok(true),
        Some("scheduled") => Ok(optional_string(dto, "triggered_by")?.as_deref()
            == Some("TIMETABLE")
            && timetable.as_deref() != Some("NullTimetable")),
        _ => Ok(false),
    }
}

struct DependencyGraph {
    upstreams: HashMap<String, Vec<String>>,
    leaves: HashSet<String>,
}

fn dependency_graph(entry: &Bound<'_, PyDict>) -> PyResult<DependencyGraph> {
    let binding = item(entry, "upstreams")?;
    let upstreams = binding.cast::<PyDict>()?;
    let mut predecessors = HashMap::new();
    for (task_id, parents) in upstreams.iter() {
        predecessors.insert(task_id.extract()?, parents.extract()?);
    }
    let leaves: Vec<String> = item(entry, "leaves")?.extract()?;
    Ok(DependencyGraph {
        upstreams: predecessors,
        leaves: leaves.into_iter().collect(),
    })
}

fn dependency_tis(
    transaction: &rusqlite::Transaction<'_>,
    dag_id: &str,
    run_id: &str,
) -> PyResult<Vec<DependencyTi>> {
    let sql = "SELECT t.id AS id, t.task_id AS task_id, t.state AS state,
            t.retry_at AS retry_at,
            t.dag_version_id AS dag_version_id, t.region_index AS region_index,
            t.region_id AS region_id
        FROM task_instance AS t
        WHERE t.dag_id = ?1 AND t.run_id = ?2 AND t.working_set IS TRUE
        ORDER BY t.task_id";
    let mut statement = transaction.prepare(sql).map_err(sql_error)?;
    let rows = statement
        .query_map(params![dag_id, run_id], |row| {
            Ok(DependencyTi {
                id: row.get("id")?,
                task_id: row.get("task_id")?,
                state: row.get("state")?,
                retry_at: row.get("retry_at")?,
                dag_version_id: row.get("dag_version_id")?,
                region_index: row.get("region_index")?,
                region_id: row.get("region_id")?,
            })
        })
        .map_err(sql_error)?;
    rows.collect::<Result<Vec<_>, _>>().map_err(sql_error)
}

fn prepare_workload(
    phases: &Bound<'_, PyAny>,
    dto: &Bound<'_, PyDict>,
) -> PyResult<Option<Workload>> {
    let launch = phases.call_method1("prepare_workload", (dto,))?;
    if launch.is_none() {
        return Ok(None);
    }
    let (generation, python, payload) = launch.extract()?;
    Ok(Some(Workload {
        id: item(dto, "id")?.extract()?,
        generation,
        python,
        payload,
    }))
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
