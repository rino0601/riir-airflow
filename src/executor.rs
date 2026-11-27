use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::fd::AsRawFd;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::Duration;

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use rusqlite::{
    Connection, ErrorCode, OptionalExtension, Transaction, TransactionBehavior, params,
};
use serde_json::Value;

pub(crate) fn error(err: impl std::fmt::Display) -> PyErr {
    PyRuntimeError::new_err(err.to_string())
}

pub(crate) struct Workload {
    pub(crate) id: String,
    pub(crate) generation: String,
    pub(crate) python: PathBuf,
    pub(crate) payload: String,
}

impl Workload {
    pub(crate) fn reserve(
        &self,
        tx: &Transaction<'_>,
        job_id: i64,
        previous_job_id: Option<i64>,
    ) -> rusqlite::Result<()> {
        tx.execute(
            "UPDATE task_instance SET external_executor_id = ?1 WHERE id = ?2",
            params![self.generation, self.id],
        )?;
        tx.execute(
            "INSERT INTO log (dttm, event, task_instance_id, task_id, dag_id, run_id,
                              map_index, try_number, logical_date, owner, extra)
             SELECT CURRENT_TIMESTAMP, 'queued', t.id, t.task_id, t.dag_id, t.run_id,
                    t.region_index, t.try_number, r.logical_date, nullif(d.owners, ''),
                    json_object('executor_id', ?1, 'job_id', ?2, 'previous_job_id', ?3)
             FROM task_instance AS t
             JOIN dag_run AS r ON r.dag_id = t.dag_id AND r.run_id = t.run_id
             JOIN dag AS d ON d.dag_id = t.dag_id
             WHERE t.id = ?4",
            params![self.generation, job_id, previous_job_id, self.id],
        )?;
        Ok(())
    }
}

struct Attempt {
    id: String,
    generation: String,
    state: String,
    try_number: i64,
    previous_job_id: Option<i64>,
    boot: Option<String>,
    process_start: Option<String>,
    process_group: Option<i32>,
    task_group: Option<i32>,
}

fn active_attempt(db: &Connection) -> rusqlite::Result<Option<Attempt>> {
    db.query_row(
        "SELECT t.id, t.external_executor_id, t.state, t.try_number,
                json_extract(l.extra, '$.previous_job_id'),
                json_extract(l.extra, '$.boot_id'),
                json_extract(l.extra, '$.process_start'),
                json_extract(l.extra, '$.process_group'),
                json_extract(l.extra, '$.task_group')
         FROM task_instance AS t
         JOIN log AS l ON l.task_instance_id = t.id AND l.event = 'queued'
             AND json_extract(l.extra, '$.executor_id') = t.external_executor_id
         WHERE t.working_set IS TRUE AND t.external_executor_id IS NOT NULL
             AND t.state IN ('queued', 'running', 'restarting')
             AND t.queued_by_job_id IS NOT NULL
         ORDER BY l.id DESC LIMIT 1",
        [],
        |row| {
            Ok(Attempt {
                id: row.get(0)?,
                generation: row.get(1)?,
                state: row.get(2)?,
                try_number: row.get(3)?,
                previous_job_id: row.get(4)?,
                boot: row.get(5)?,
                process_start: row.get(6)?,
                process_group: row.get(7)?,
                task_group: row.get(8)?,
            })
        },
    )
    .optional()
}

pub(crate) fn active_task(db: &Connection) -> rusqlite::Result<Option<String>> {
    Ok(active_attempt(db)?.map(|attempt| attempt.id))
}

pub(crate) struct Executor {
    database: PathBuf,
    lease_path: PathBuf,
    child: Option<Child>,
}

impl Executor {
    pub(crate) fn new(database: PathBuf) -> Self {
        let mut lease_path = database.as_os_str().to_owned();
        lease_path.push(".riir-lock");
        Self {
            database,
            lease_path: PathBuf::from(lease_path),
            child: None,
        }
    }

    pub(crate) fn available(&mut self, db: &mut Connection) -> PyResult<Option<File>> {
        if let Some(child) = &mut self.child {
            match child.try_wait().map_err(error)? {
                None => return Ok(None),
                Some(status) => {
                    eprintln!("Native task supervisor {} exited with {status}", child.id());
                    self.child = None;
                }
            }
        }
        let lease = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(&self.lease_path)
            .map_err(error)?;
        if unsafe { libc::flock(lease.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            let err = io::Error::last_os_error();
            return if err.kind() == io::ErrorKind::WouldBlock {
                Ok(None)
            } else {
                Err(error(err))
            };
        }

        let Some(attempt) = active_attempt(db).map_err(error)? else {
            return Ok(Some(lease));
        };
        if attempt_live(&attempt).map_err(error)? {
            return Ok(None);
        }
        let tx = match db.transaction_with_behavior(TransactionBehavior::Immediate) {
            Ok(tx) => tx,
            Err(err)
                if matches!(
                    err.sqlite_error_code(),
                    Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked)
                ) =>
            {
                return Ok(None);
            }
            Err(err) => return Err(error(err)),
        };
        finish_lost(&tx, &attempt).map_err(error)?;
        tx.commit().map_err(error)?;
        Ok(Some(lease))
    }

    pub(crate) fn wait(&mut self, db: &mut Connection) -> PyResult<()> {
        if let Some(mut child) = self.child.take() {
            let status = child.wait().map_err(error)?;
            eprintln!("Native task supervisor {} exited with {status}", child.id());
            self.available(db)?;
        }
        Ok(())
    }

    pub(crate) fn launch(&mut self, workload: Workload, lease: File) -> PyResult<()> {
        let fd = lease.as_raw_fd();
        let mut command = Command::new(workload.python);
        command
            .args(["-c", "from riir_airflow._task_runtime import run; run()"])
            .arg(&self.database)
            .arg(&workload.generation)
            .arg(fd.to_string())
            .stdin(Stdio::piped())
            .process_group(0);
        unsafe {
            command.pre_exec(move || {
                if libc::fcntl(fd, libc::F_SETFD, 0) == -1 {
                    return Err(io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = command.spawn().map_err(error)?;
        let mut input = child.stdin.take().expect("piped supervisor stdin");
        self.child = Some(child);
        input.write_all(workload.payload.as_bytes()).map_err(error)
    }
}

fn boot_id() -> PyResult<String> {
    fs::read_to_string("/proc/sys/kernel/random/boot_id").map_err(error)
}

fn process_start(pid: i32) -> Option<String> {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    stat.rsplit_once(')')
        .and_then(|(_, fields)| fields.split_whitespace().nth(19))
        .map(str::to_owned)
}

fn own_process_start() -> PyResult<String> {
    process_start(std::process::id() as i32)
        .ok_or_else(|| error("cannot read native task process start time"))
}

fn group_exists(pgid: i32) -> PyResult<bool> {
    if unsafe { libc::kill(-pgid, 0) } == 0 {
        return Ok(true);
    }
    let err = io::Error::last_os_error();
    match err.raw_os_error() {
        Some(libc::ESRCH) => Ok(false),
        Some(libc::EPERM) => Ok(true),
        _ => Err(error(err)),
    }
}

fn attempt_live(attempt: &Attempt) -> PyResult<bool> {
    if attempt.boot.as_deref() != Some(boot_id()?.as_str()) {
        return Ok(false);
    }
    if let (Some(pid), Some(start)) = (attempt.process_group, &attempt.process_start) {
        if process_start(pid).as_deref() != Some(start.as_str()) {
            return Ok(false);
        }
    }
    for group in [attempt.process_group, attempt.task_group]
        .into_iter()
        .flatten()
    {
        if group_exists(group)? {
            return Ok(true);
        }
    }
    Ok(false)
}

fn finish_lost(tx: &Transaction<'_>, attempt: &Attempt) -> rusqlite::Result<()> {
    let started = matches!(attempt.state.as_str(), "running" | "restarting");
    tx.execute(
        "UPDATE task_instance SET
            state = CASE WHEN ?1 THEN state WHEN ?2 IS NULL THEN 'scheduled' ELSE 'queued' END,
            queued_by_job_id = CASE WHEN ?1 THEN queued_by_job_id ELSE ?2 END,
            queued_dttm = CASE WHEN ?1 THEN queued_dttm
                WHEN ?2 IS NULL THEN NULL ELSE queued_dttm END,
            external_executor_id = NULL,
            last_heartbeat_at = CASE WHEN ?1 THEN '1970-01-01 00:00:00.000000'
                ELSE last_heartbeat_at END,
            updated_at = CURRENT_TIMESTAMP
         WHERE id = ?3 AND external_executor_id = ?4 AND working_set IS TRUE
             AND try_number = ?5 AND state = ?6",
        params![
            started,
            attempt.previous_job_id,
            attempt.id,
            attempt.generation,
            attempt.try_number,
            attempt.state
        ],
    )?;
    Ok(())
}

fn open(path: &Path) -> PyResult<Connection> {
    let db = Connection::open(path).map_err(error)?;
    db.busy_timeout(Duration::from_secs(5)).map_err(error)?;
    db.execute_batch("PRAGMA foreign_keys = ON")
        .map_err(error)?;
    Ok(db)
}

fn queued_log(
    db: &Connection,
    generation: &str,
    state_filter: &str,
) -> rusqlite::Result<Option<(i64, Option<i64>)>> {
    let sql = format!(
        "SELECT l.id, json_extract(l.extra, '$.task_group')
         FROM log AS l JOIN task_instance AS t ON t.id = l.task_instance_id
         WHERE l.event = 'queued' AND json_extract(l.extra, '$.executor_id') = ?1
             AND t.external_executor_id = ?1 AND t.working_set IS TRUE
             AND t.state IN ({state_filter}) AND t.try_number = l.try_number
         ORDER BY l.id DESC LIMIT 1"
    );
    db.query_row(&sql, params![generation], |row| {
        Ok((row.get(0)?, row.get(1)?))
    })
    .optional()
}

#[pyfunction]
pub(crate) fn register_attempt(path: PathBuf, generation: String) -> PyResult<bool> {
    if unsafe { libc::getpgrp() } != std::process::id() as i32 {
        return Ok(false);
    }
    let db = open(&path)?;
    let log = queued_log(&db, &generation, "'queued'").map_err(error)?;
    let Some((id, _)) = log else {
        return Ok(false);
    };
    let pid = std::process::id() as i32;
    let boot = boot_id()?;
    let start = own_process_start()?;
    db.execute(
        "UPDATE log SET extra = json_set(extra, '$.boot_id', ?1, '$.process_start', ?2,
                                          '$.process_group', ?3)
         WHERE id = ?4 AND json_extract(extra, '$.executor_id') = ?5
             AND (json_extract(extra, '$.boot_id') IS NULL OR json_extract(extra, '$.boot_id') = ?1)
             AND (json_extract(extra, '$.process_start') IS NULL
                  OR json_extract(extra, '$.process_start') = ?2)
             AND (json_extract(extra, '$.process_group') IS NULL
                  OR json_extract(extra, '$.process_group') = ?3)",
        params![boot, start, pid, id, generation],
    )
    .map(|count| count == 1)
    .map_err(error)
}

#[pyfunction]
pub(crate) fn register_task_group(path: PathBuf, generation: String, pid: i32) -> PyResult<bool> {
    if pid <= 1 || unsafe { libc::getpgid(pid) } != pid {
        return Ok(false);
    }
    let db = open(&path)?;
    let log = queued_log(&db, &generation, "'queued', 'running'").map_err(error)?;
    let Some((id, group)) = log else {
        return Ok(false);
    };
    let boot = boot_id()?;
    let start = own_process_start()?;
    db.execute(
        "UPDATE log SET extra = json_set(extra, '$.task_group', ?1)
         WHERE id = ?2 AND json_extract(extra, '$.executor_id') = ?3
             AND json_extract(extra, '$.boot_id') = ?4
             AND json_extract(extra, '$.process_start') = ?5
             AND (json_extract(extra, '$.task_group') IS NULL
                  OR json_extract(extra, '$.task_group') = ?1)",
        params![pid, id, generation, boot, start],
    )
    .map(|count| count == 1 && group.is_none_or(|value| value == i64::from(pid)))
    .map_err(error)
}

#[pyfunction]
pub(crate) fn report_outcome(
    path: PathBuf,
    generation: String,
    id: String,
    payload: String,
    successor_id: String,
    retry_at: Option<String>,
    now: String,
) -> PyResult<u16> {
    let mut db = open(&path)?;
    let value: Value = serde_json::from_str(&payload).map_err(error)?;
    let state = value["state"]
        .as_str()
        .ok_or_else(|| error("missing task state"))?;
    let empty =
        |name: &str| value[name].is_null() || value[name].as_array().is_some_and(Vec::is_empty);
    let native = matches!(state, "success" | "skipped" | "failed" | "up_for_retry")
        && empty("task_outlets")
        && empty("outlet_events")
        && value["loop_decision"].is_null();
    let tx = db
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(error)?;
    let row = tx
        .query_row(
            "SELECT t.state, t.working_set IS TRUE, l.id,
                    json_extract(l.extra, '$.boot_id'),
                    json_extract(l.extra, '$.process_start'),
                    json_extract(l.extra, '$.delegated')
             FROM task_instance AS t JOIN log AS l ON l.task_instance_id = t.id
                 AND l.event = 'queued' AND json_extract(l.extra, '$.executor_id') = ?1
             WHERE t.id = ?2 AND t.external_executor_id = ?1",
            params![generation, id],
            |row| {
                Ok((
                    row.get::<_, Option<String>>(0)?,
                    row.get::<_, Option<bool>>(1)?.unwrap_or(false),
                    row.get::<_, i64>(2)?,
                    row.get::<_, Option<String>>(3)?,
                    row.get::<_, Option<String>>(4)?,
                    row.get::<_, Option<i64>>(5)?,
                ))
            },
        )
        .optional()
        .map_err(error)?;
    let Some((previous, current, log_id, boot, start, delegated)) = row else {
        return Ok(409);
    };
    if boot.as_deref() != Some(boot_id()?.as_str())
        || start.as_deref() != Some(own_process_start()?.as_str())
    {
        return Ok(409);
    }
    let receipt: Option<String> = tx
        .query_row(
            "SELECT json_extract(extra, '$.report') FROM log
             WHERE task_instance_id = ?1 AND event = ?2
                 AND json_extract(extra, '$.executor_id') = ?3
             ORDER BY id DESC LIMIT 1",
            params![id, state, generation],
            |row| row.get(0),
        )
        .optional()
        .map_err(error)?;
    if let Some(receipt) = receipt {
        return Ok(if receipt == payload { 204 } else { 409 });
    }
    if delegated == Some(1) {
        return Ok(0);
    }
    if !current
        || (previous.as_deref() != Some("running")
            && !(state == "server_terminated" && previous.as_deref() == Some("restarting")))
    {
        return Ok(409);
    }
    if !native {
        tx.execute(
            "UPDATE log SET extra = json_set(extra, '$.delegated', 1) WHERE id = ?1",
            params![log_id],
        )
        .map_err(error)?;
        tx.commit().map_err(error)?;
        return Ok(0);
    }

    let reason = value["retry_reason"]
        .as_str()
        .filter(|reason| !reason.is_empty())
        .map(|s| s.chars().take(500).collect::<String>());
    let end_date = value["end_date"]
        .as_str()
        .ok_or_else(|| error("missing end date"))?;
    let retry = state == "up_for_retry";
    tx.execute(
        "UPDATE task_instance SET
            state = CASE WHEN ?1 THEN 'failed' ELSE ?2 END,
            working_set = CASE WHEN ?1 THEN NULL ELSE working_set END,
            archived_reason = CASE WHEN ?1 THEN 'retry' ELSE archived_reason END,
            trigger_id = CASE WHEN ?1 THEN NULL ELSE trigger_id END,
            end_date = ?3, updated_at = ?4,
            duration = round((julianday(?3) - julianday(start_date)) * 86400, 3),
            rendered_map_index = CASE WHEN ?5 THEN ?6 ELSE rendered_map_index END,
            retry_reason = CASE WHEN ?1 OR (?2 = 'failed' AND ?7 IS NOT NULL)
                THEN ?7 ELSE retry_reason END,
            retry_delay_override = CASE WHEN ?1 THEN ?8 ELSE retry_delay_override END,
            next_method = NULL, next_kwargs = NULL
         WHERE id = ?9",
        params![
            retry,
            state,
            end_date,
            now,
            value.get("rendered_map_index").is_some(),
            value["rendered_map_index"].as_str(),
            reason,
            value["retry_delay_seconds"].as_f64(),
            id
        ],
    )
    .map_err(error)?;

    if retry {
        tx.execute(
            "INSERT INTO task_instance (
                id, task_id, dag_id, run_id, region_index, region_id, state, try_number,
                max_tries, working_set, pool, pool_slots, queue, priority_weight, operator,
                custom_operator_name, is_stub, executor, executor_config, updated_at,
                rendered_map_index, context_carrier, trigger_timeout, task_display_name,
                dag_version_id, retry_delay_override, retry_reason, retry_at)
             SELECT ?1, task_id, dag_id, run_id, region_index, region_id, 'up_for_retry',
                try_number + 1, max_tries, 1, pool, pool_slots, queue, priority_weight,
                operator, custom_operator_name, is_stub, executor, executor_config, updated_at,
                rendered_map_index, context_carrier, trigger_timeout, task_display_name,
                dag_version_id, retry_delay_override, retry_reason, ?2
             FROM task_instance WHERE id = ?3",
            params![successor_id, retry_at, id],
        )
        .map_err(error)?;
        tx.execute(
            "INSERT INTO task_instance_note (ti_id, user_id, content, created_at, updated_at)
             SELECT ?1, user_id, content, created_at, updated_at
             FROM task_instance_note WHERE ti_id = ?2",
            params![successor_id, id],
        )
        .map_err(error)?;
    }
    tx.execute(
        "INSERT INTO log (dttm, event, task_instance_id, task_id, dag_id, run_id,
                          map_index, try_number, logical_date, owner, extra)
         SELECT ?1, ?2, t.id, t.task_id, t.dag_id, t.run_id, t.region_index,
                t.try_number, r.logical_date, nullif(d.owners, ''),
                CASE WHEN t.hostname IS NOT NULL AND t.hostname <> ''
                     THEN json_object('executor_id', ?3, 'report', ?4, 'host_name', t.hostname)
                     ELSE json_object('executor_id', ?3, 'report', ?4) END
         FROM task_instance AS t
         JOIN dag_run AS r ON r.dag_id = t.dag_id AND r.run_id = t.run_id
         JOIN dag AS d ON d.dag_id = t.dag_id WHERE t.id = ?5",
        params![now, state, generation, payload, id],
    )
    .map_err(error)?;
    tx.commit().map_err(error)?;
    Ok(204)
}
