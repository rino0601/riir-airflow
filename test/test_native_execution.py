"""Real SDK supervisor and SQLite tests for native single-slot execution."""

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
from riir_airflow import _lowlevel

from .test_executor_differential import _process, _wait_for_api

pytestmark = pytest.mark.differential

_EXECUTION_API = """\
import sys
from pathlib import Path

import uvicorn
from airflow.api_fastapi.app import create_app

app = create_app(apps="execution")
requests = 0

async def lose_first_state_response(scope, receive, send):
    global requests
    if scope["type"] != "http" or not scope["path"].endswith("/state"):
        return await app(scope, receive, send)
    requests += 1
    Path("state-requests").write_text(str(requests))
    drop = requests == 1

    async def send_response(message):
        if not drop:
            await send(message)
        elif message["type"] == "http.response.start":
            assert message["status"] == 204
            await send({"type": "http.response.start", "status": 503, "headers": []})
        else:
            await send({"type": "http.response.body", "body": b""})

    await app(scope, receive, send_response)

uvicorn.run(lose_first_state_response, fd=int(sys.argv[1]))
"""

_DAG = """\
from pathlib import Path

from airflow.exceptions import AirflowSkipException, TaskDeferred
from airflow.sdk import DAG, Asset, get_current_context, task
from airflow.providers.standard.triggers.temporal import DateTimeTrigger
from datetime import UTC, datetime, timedelta

with DAG("native_execution", schedule=None, max_active_runs=1):
    @task(retries=1, retry_delay=0)
    def work():
        context = get_current_context()
        mode = context["dag_run"].conf["mode"]
        attempt = context["ti"].try_number
        with Path("executions").open("a") as file:
            file.write(f"{mode}:{attempt}\\n")
        if mode == "skip":
            raise AirflowSkipException("intentional skip")
        if mode == "failure" or (mode == "retry" and attempt == 1):
            raise RuntimeError("intentional failure")
        if mode == "defer":
            raise TaskDeferred(
                trigger=DateTimeTrigger(moment=datetime.now(UTC) + timedelta(hours=1)),
                method_name="execute",
            )
        if mode == "restart":
            import time

            while not Path("release").exists():
                time.sleep(0.1)

    work()

with DAG("asset_execution", schedule=None):
    @task(outlets=[Asset("native-execution-test-asset")])
    def produce():
        pass

    produce()
"""

_TRIGGER = """\
import sys
from datetime import timedelta

from airflow.api.common.trigger_dag import trigger_dag
from airflow.sdk import timezone
from airflow.utils.types import DagRunTriggeredByType

mode = sys.argv[1]
trigger_dag("asset_execution" if mode == "asset" else "native_execution",
            run_id=mode, conf={"mode": mode},
            run_after=timezone.utcnow() - timedelta(minutes=1),
            triggered_by=DagRunTriggeredByType.CLI)
"""


def _wait_for(predicate, *logs):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(0.1)
    pytest.fail("\n".join(path.read_text() for path in logs if path.exists()))


def test_manual_dags_use_rust_process_and_outcome_owners(tmp_path):
    dags = tmp_path / "dags"
    dags.mkdir()
    (dags / "native_execution.py").write_text(_DAG)
    database = tmp_path / "airflow.db"
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AIRFLOW")
    }
    env.update(
        AIRFLOW_HOME=str(tmp_path),
        AIRFLOW__CORE__DAGS_FOLDER=str(dags),
        AIRFLOW__CORE__LOAD_EXAMPLES="False",
        AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION="False",
        AIRFLOW__CORE__EXECUTOR="riir_airflow.executors.asgi_executor.AsgiExecutor",
        AIRFLOW__CORE__PARALLELISM="1",
        AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=f"sqlite:///{database}",
        AIRFLOW__API_AUTH__JWT_SECRET="native-execution-test-secret-" * 3,
        AIRFLOW__SCHEDULER__ORPHANED_TASKS_CHECK_INTERVAL="1",
        AIRFLOW__SCHEDULER__SCHEDULER_HEARTBEAT_SEC="1",
        AIRFLOW__SCHEDULER__SCHEDULER_IDLE_SLEEP_TIME="0.1",
    )

    def run(*args):
        result = subprocess.run(
            [sys.executable, *args],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    run("-m", "airflow", "db", "migrate")
    run("-m", "airflow", "dag-processor", "--num-runs", "1")
    api_log = tmp_path / "api.log"
    scheduler_log = tmp_path / "scheduler.log"
    replacement_log = tmp_path / "replacement.log"
    marker = tmp_path / "executions"
    scheduler = ["-m", "riir_airflow.scheduler"]
    with (
        socket.socket() as listener,
        closing(sqlite3.connect(database, isolation_level=None)) as db,
    ):
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        env["AIRFLOW__CORE__EXECUTION_API_SERVER_URL"] = (
            f"http://127.0.0.1:{port}/execution/"
        )

        def complete(mode, task_state, run_state, log):
            _wait_for(
                lambda: db.execute(
                    "SELECT 1 FROM dag_run WHERE run_id = ? AND state = ?",
                    (mode, run_state),
                ).fetchone(),
                log,
                api_log,
            )
            assert db.execute(
                "SELECT state FROM task_instance WHERE run_id = ? AND working_set IS TRUE",
                (mode,),
            ).fetchone() == (task_state,)

        with _process(
            ["-c", _EXECUTION_API, str(listener.fileno())],
            api_log,
            env=env,
            cwd=tmp_path,
            pass_fds=(listener.fileno(),),
        ) as api:
            _wait_for_api(api, port, api_log)
            with _process(scheduler, scheduler_log, env=env, cwd=tmp_path) as first:
                zero_exits = 0
                for mode, state, final in (
                    ("success", "success", "success"),
                    ("skip", "skipped", "success"),
                    ("retry", "success", "success"),
                    ("failure", "failed", "failed"),
                ):
                    run("-c", _TRIGGER, mode)
                    complete(mode, state, final, scheduler_log)
                    if mode in {"success", "skip"}:
                        zero_exits += 1
                        _wait_for(
                            lambda zero_exits=zero_exits: (
                                scheduler_log.read_text().count(
                                    "exited with exit status: 0"
                                )
                                == zero_exits
                            ),
                            scheduler_log,
                        )
                run("-c", _TRIGGER, "restart")
                _wait_for(
                    lambda: marker.exists() and "restart:1" in marker.read_text(),
                    scheduler_log,
                    api_log,
                )
                attempt, owner, group, task_group = db.execute(
                    "SELECT id, job_id, pgid, task_pgid FROM riir_attempt WHERE active = 1"
                ).fetchone()
                first.kill()
                first.wait(timeout=10)
                db.execute("UPDATE job SET state = 'failed' WHERE id = ?", (owner,))

            try:
                with _process(scheduler, replacement_log, env=env, cwd=tmp_path):
                    _wait_for(
                        lambda: db.execute(
                            "SELECT 1 FROM job WHERE id > ? AND job_type = 'SchedulerJob' "
                            "AND (julianday(latest_heartbeat) - julianday(start_date)) * 86400 >= 1",
                            (owner,),
                        ).fetchone(),
                        replacement_log,
                    )
                    os.kill(group, 0)
                    os.killpg(task_group, 0)
                    assert group != task_group
                    assert db.execute(
                        "SELECT queued_by_job_id, state FROM task_instance WHERE id = ?",
                        (attempt,),
                    ).fetchone() == (owner, "running")
                    (tmp_path / "release").touch()
                    complete("restart", "success", "success", replacement_log)
                    run("-c", _TRIGGER, "defer")
                    _wait_for(
                        lambda: (
                            (tmp_path / "state-requests").exists()
                            and (tmp_path / "state-requests").read_text() == "2"
                        ),
                        replacement_log,
                        api_log,
                    )
                    _wait_for(
                        lambda: db.execute(
                            "SELECT 1 FROM riir_attempt AS a JOIN task_instance AS t ON t.id = a.id "
                            "WHERE t.run_id = 'defer' AND a.active = 0"
                        ).fetchone(),
                        replacement_log,
                        api_log,
                    )
                    run("-c", _TRIGGER, "asset")
                    complete("asset", "success", "success", replacement_log)
            finally:
                (tmp_path / "release").touch()
                # A broken implementation must not leave the old task running.
                if db.execute(
                    "SELECT 1 FROM task_instance WHERE id = ? AND state = 'running'",
                    (attempt,),
                ).fetchone():
                    for process_group in (task_group, group):
                        try:
                            os.killpg(process_group, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

        assert marker.read_text().splitlines() == [
            "success:1",
            "skip:1",
            "retry:1",
            "retry:2",
            "failure:1",
            "failure:2",
            "restart:1",
            "defer:1",
        ]
        assert db.execute(
            "SELECT t.run_id, t.try_number, t.state, t.working_set, l.event, a.writer "
            "FROM task_instance t JOIN riir_attempt a ON a.id = t.id "
            "JOIN log l ON l.task_instance_id = t.id AND l.event IN "
            "('up_for_retry', 'success', 'skipped', 'failed') ORDER BY l.id"
        ).fetchall() == [
            ("success", 1, "success", 1, "success", "rust"),
            ("skip", 1, "skipped", 1, "skipped", "rust"),
            ("retry", 1, "failed", None, "up_for_retry", "rust"),
            ("retry", 2, "success", 1, "success", "rust"),
            ("failure", 1, "failed", None, "up_for_retry", "rust"),
            ("failure", 2, "failed", 1, "failed", "rust"),
            ("restart", 1, "success", 1, "success", "rust"),
        ]
        assert db.execute(
            "SELECT t.state, a.writer, a.receipt FROM task_instance AS t "
            "JOIN riir_attempt AS a ON a.id = t.id WHERE t.run_id = 'defer'"
        ).fetchone() == ("deferred", "airflow", None)
        assert db.execute("SELECT count(*) FROM trigger").fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM log WHERE event = 'deferred'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM asset_event WHERE source_dag_id = 'asset_execution'"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM riir_attempt AS a JOIN task_instance AS t ON t.id = a.id "
            "WHERE t.run_id = 'asset'"
        ).fetchone() == (0,)
        _assert_fenced_receipts(db, database)
        atomicity_database = tmp_path / "atomicity.db"
        with closing(sqlite3.connect(atomicity_database, isolation_level=None)) as copy:
            db.backup(copy)
            # This fault probe alternates schema edits between Python SQLite
            # and bundled rusqlite in the same process. Use a rollback journal
            # for the copied database so each sees the other's schema changes.
            assert copy.execute("PRAGMA journal_mode = DELETE").fetchone() == (
                "delete",
            )
            _assert_retry_is_atomic(copy, atomicity_database)


def _assert_fenced_receipts(db, database):
    # Replay the archived retry report under its simulated supervisor identity.
    attempt, generation, receipt = db.execute(
        "SELECT a.id, a.generation, a.receipt FROM riir_attempt AS a "
        "JOIN task_instance AS t ON t.id = a.id WHERE t.run_id = 'retry' AND t.try_number = 1"
    ).fetchone()
    start = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
    db.execute(
        "UPDATE riir_attempt SET pgid = ?, process_start = ? WHERE generation = ?",
        (os.getpid(), start, generation),
    )

    def report(generation, payload):
        return _lowlevel.report_outcome(
            str(database),
            generation,
            attempt,
            payload,
            "64a72def22874711a9ee11aa441121f4",
            None,
            "2026-01-01 00:00:00.000000",
        )

    assert report(generation, receipt) == 204
    assert report(generation, receipt.replace("up_for_retry", "failed")) == 409
    assert report("missing-generation", receipt) == 409
    assert db.execute(
        "SELECT a.successor_id = t.id FROM riir_attempt AS a JOIN task_instance AS t "
        "ON t.run_id = 'retry' AND t.try_number = 2 WHERE a.generation = ?",
        (generation,),
    ).fetchone() == (1,)
    assert db.execute(
        "SELECT count(*) FROM task_instance WHERE run_id = 'retry'"
    ).fetchone() == (2,)
    assert db.execute(
        "SELECT count(*) FROM log WHERE task_instance_id = ? AND event = 'up_for_retry'",
        (attempt,),
    ).fetchone() == (1,)


def _assert_retry_is_atomic(db, database):
    attempt, generation = db.execute(
        "SELECT a.id, a.generation FROM riir_attempt AS a "
        "JOIN task_instance AS t ON t.id = a.id WHERE t.run_id = 'success'"
    ).fetchone()
    start = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
    db.execute(
        "UPDATE riir_attempt SET active = 1, pgid = ?, process_start = ?, "
        "writer = NULL, receipt = NULL WHERE generation = ?",
        (os.getpid(), start, generation),
    )
    db.execute("UPDATE task_instance SET state = 'running' WHERE id = ?", (attempt,))
    end = "2026-01-01 00:00:01.000000"
    retry_at = "2026-01-01 00:00:03.000000"
    successor = "64a72def22874711a9ee11aa441121f4"
    db.execute(
        "INSERT INTO task_instance_note (ti_id, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (attempt, "keep this note", end, end),
    )
    db.execute(
        "CREATE TRIGGER reject_receipt BEFORE UPDATE OF receipt ON riir_attempt "
        "BEGIN SELECT raise(ABORT, 'test rejected receipt'); END"
    )

    def report():
        return _lowlevel.report_outcome(
            str(database),
            generation,
            attempt,
            '{"state":"up_for_retry","end_date":"'
            + end
            + '","retry_delay_seconds":2,"retry_reason":"retry reason"}',
            successor,
            retry_at,
            end,
        )

    with pytest.raises(RuntimeError, match="test rejected receipt"):
        report()
    assert db.execute(
        "SELECT state, working_set FROM task_instance WHERE id = ?", (attempt,)
    ).fetchone() == ("running", 1)
    assert (
        db.execute("SELECT 1 FROM task_instance WHERE id = ?", (successor,)).fetchone()
        is None
    )
    assert (
        db.execute(
            "SELECT 1 FROM log WHERE task_instance_id = ? AND event = 'up_for_retry'",
            (attempt,),
        ).fetchone()
        is None
    )

    db.execute("DROP TRIGGER reject_receipt")
    assert report() == 204
    assert report() == 204
    assert db.execute(
        "SELECT t.state, t.try_number, t.queued_by_job_id, t.pid, t.retry_at, "
        "t.retry_delay_override, t.retry_reason, n.content, n.created_at, n.updated_at "
        "FROM task_instance AS t JOIN task_instance_note AS n ON t.id = n.ti_id WHERE t.id = ?",
        (successor,),
    ).fetchone() == (
        "up_for_retry",
        2,
        None,
        None,
        retry_at,
        2.0,
        "retry reason",
        "keep this note",
        end,
        end,
    )
    assert db.execute(
        "SELECT count(*) FROM log WHERE task_instance_id = ? AND event = 'up_for_retry'",
        (attempt,),
    ).fetchone() == (1,)
