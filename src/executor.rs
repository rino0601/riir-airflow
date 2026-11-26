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

pub(crate) fn initialize(db: &Connection) -> rusqlite::Result<()> {
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS riir_attempt (
            id TEXT NOT NULL REFERENCES task_instance(id) ON DELETE CASCADE,
            generation TEXT PRIMARY KEY,
            job_id INTEGER NOT NULL,
            try_number INTEGER NOT NULL,
            previous_job_id INTEGER,
            pgid INTEGER,
            task_pgid INTEGER,
            boot_id TEXT,
            process_start TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            writer TEXT,
            receipt TEXT,
            successor_id TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS riir_single_slot ON riir_attempt(active)
            WHERE active = 1;",
    )
}

pub(crate) struct Workload {
    pub(crate) id: String,
    pub(crate) generation: String,
    pub(crate) try_number: i64,
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
            "INSERT INTO riir_attempt (id, generation, job_id, try_number, previous_job_id)
            VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                self.id,
                self.generation,
                job_id,
                self.try_number,
                previous_job_id
            ],
        )?;
        Ok(())
    }
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
        // The supervisor inherits this open file description. Closing the parent's
        // copy leaves the lock held even when the scheduler is killed.
        if unsafe { libc::flock(lease.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            let err = io::Error::last_os_error();
            return if err.kind() == io::ErrorKind::WouldBlock {
                Ok(None)
            } else {
                Err(error(err))
            };
        }

        let active = db
            .query_row(
                "SELECT generation, pgid, task_pgid, boot_id FROM riir_attempt WHERE active = 1",
                [],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, Option<i32>>(1)?,
                        row.get::<_, Option<i32>>(2)?,
                        row.get::<_, Option<String>>(3)?,
                    ))
                },
            )
            .optional()
            .map_err(error)?;
        if let Some((generation, pgid, task_pgid, boot)) = active {
            if boot.as_deref() == Some(boot_id()?.as_str()) {
                for group in [pgid, task_pgid].into_iter().flatten() {
                    if group_exists(group)? {
                        return Ok(None);
                    }
                }
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
            finish_lost(&tx, &generation, pgid.is_some()).map_err(error)?;
            tx.commit().map_err(error)?;
        }
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
        // Only the spawned supervisor should inherit the lease.
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

fn own_process_start() -> PyResult<String> {
    // /proc stat field 22 is the start time in ticks since boot. The process
    // name may contain whitespace and parentheses, so skip its final ')'.
    let stat = fs::read_to_string("/proc/self/stat").map_err(error)?;
    stat.rsplit_once(')')
        .and_then(|(_, fields)| fields.split_whitespace().nth(19))
        .map(str::to_owned)
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

fn finish_lost(tx: &Transaction<'_>, generation: &str, started: bool) -> rusqlite::Result<()> {
    // Once a supervisor (and its group) has gone, ordinary Airflow orphan
    // handling may reset a running/restarting attempt. A queued attempt never
    // entered the API and can be put back without incrementing its try number.
    tx.execute(
        "UPDATE task_instance AS t SET
            state = CASE WHEN a.previous_job_id IS NULL THEN 'scheduled' ELSE 'queued' END,
            queued_by_job_id = a.previous_job_id,
            queued_dttm = CASE WHEN a.previous_job_id IS NULL THEN NULL ELSE queued_dttm END
         FROM riir_attempt AS a WHERE a.generation = ?1 AND a.writer IS NULL
            AND t.id = a.id AND t.working_set IS TRUE AND t.try_number = a.try_number
            AND t.queued_by_job_id = a.job_id AND t.state = 'queued'",
        [generation],
    )?;
    if started {
        // Release the dead running attempt to Airflow's ordinary heartbeat
        // handling when the owning job is still current, and orphan recovery
        // otherwise. An outcome already committed remains authoritative.
        tx.execute(
            "UPDATE task_instance AS t SET last_heartbeat_at = '1970-01-01 00:00:00.000000'
             FROM riir_attempt AS a WHERE a.generation = ?1 AND a.writer IS NULL
                AND t.id = a.id AND t.working_set IS TRUE AND t.try_number = a.try_number
                AND t.queued_by_job_id = a.job_id AND t.state IN ('running', 'restarting')",
            [generation],
        )?;
    }
    tx.execute(
        "UPDATE riir_attempt SET active = 0 WHERE generation = ?1",
        [generation],
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

#[pyfunction]
pub(crate) fn register_attempt(path: PathBuf, generation: String) -> PyResult<bool> {
    let pid = std::process::id();
    if unsafe { libc::getpgrp() } != pid as i32 {
        return Ok(false);
    }
    let db = open(&path)?;
    db.execute(
        "UPDATE riir_attempt AS a SET pgid = ?1, boot_id = ?2, process_start = ?3
         WHERE generation = ?4 AND active = 1 AND pgid IS NULL
            AND EXISTS (SELECT 1 FROM task_instance AS t
                WHERE t.id = a.id AND t.working_set IS TRUE AND t.state = 'queued'
                    AND t.try_number = a.try_number AND t.queued_by_job_id = a.job_id)",
        params![pid, boot_id()?, own_process_start()?, generation],
    )
    .map(|count| count == 1)
    .map_err(error)
}

/// The SDK gives the actual task runner a separate process group. Save it
/// before forwarding /run, since the supervisor may die while that group lives.
#[pyfunction]
pub(crate) fn register_task_group(path: PathBuf, generation: String, pid: i32) -> PyResult<bool> {
    if pid <= 1 || unsafe { libc::getpgid(pid) } != pid {
        return Ok(false);
    }
    let db = open(&path)?;
    db.execute(
        "UPDATE riir_attempt AS a SET task_pgid = ?1
         WHERE generation = ?2 AND active = 1 AND pgid = ?3
            AND boot_id = ?4 AND process_start = ?5
            AND (task_pgid IS NULL OR task_pgid = ?1)
            AND EXISTS (SELECT 1 FROM task_instance AS t
                WHERE t.id = a.id AND t.working_set IS TRUE AND t.state IN ('queued', 'running')
                    AND t.try_number = a.try_number AND t.queued_by_job_id = a.job_id)",
        params![
            pid,
            generation,
            std::process::id(),
            boot_id()?,
            own_process_start()?
        ],
    )
    .map(|count| count == 1)
    .map_err(error)
}

/// Return the HTTP status for a native result, or zero for SDK-owned forwarding.
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
            "SELECT a.writer, a.receipt, a.active, a.pgid, t.state, a.boot_id, a.process_start,
          t.working_set IS TRUE AND t.try_number = a.try_number AND t.queued_by_job_id = a.job_id
         FROM riir_attempt AS a JOIN task_instance AS t ON t.id = a.id
         WHERE a.generation = ?1 AND a.id = ?2",
            params![generation, id],
            |row| {
                Ok((
                    row.get::<_, Option<String>>(0)?,
                    row.get::<_, Option<String>>(1)?,
                    row.get::<_, bool>(2)?,
                    row.get::<_, Option<u32>>(3)?,
                    row.get::<_, Option<String>>(4)?,
                    row.get::<_, Option<String>>(5)?,
                    row.get::<_, Option<String>>(6)?,
                    row.get::<_, Option<bool>>(7)?.unwrap_or(false),
                ))
            },
        )
        .optional()
        .map_err(error)?;
    let Some((owner, receipt, active, pid, previous, boot, start, current)) = row else {
        return Ok(409);
    };
    if pid != Some(std::process::id())
        || boot.as_deref() != Some(boot_id()?.as_str())
        || start.as_deref() != Some(own_process_start()?.as_str())
    {
        return Ok(409);
    }
    if let Some(receipt) = receipt {
        return Ok(if receipt == payload { 204 } else { 409 });
    }
    if !active {
        return Ok(409);
    }
    if owner.as_deref() == Some("airflow") {
        // The entire API request, including its own duplicate handling, remains
        // with Airflow after delegation, even if the previous response was lost.
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
            "UPDATE riir_attempt SET writer = 'airflow' WHERE generation = ?1",
            [&generation],
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
                     THEN json_object('host_name', t.hostname) END
         FROM task_instance AS t
         JOIN dag_run AS r ON r.dag_id = t.dag_id AND r.run_id = t.run_id
         JOIN dag AS d ON d.dag_id = t.dag_id WHERE t.id = ?3",
        params![now, state, id],
    )
    .map_err(error)?;
    tx.execute(
        "UPDATE riir_attempt SET writer = 'rust', receipt = ?1, successor_id = ?2 WHERE generation = ?3",
        params![payload, retry.then_some(successor_id), generation],
    )
    .map_err(error)?;
    tx.commit().map_err(error)?;
    Ok(204)
}
